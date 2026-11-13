"""
Iris Chat Memory - L2 记忆库 SQLite + FTS5 适配器

实现 L2 记忆库的存储和检索功能，支持：
- SQLite 元数据存储（文档、元数据、ID 映射）
- FTS5 全文关键词检索
- 群聊隔离检索
- 人格隔离（独立目录）
- 去重检查
- 超时保护
"""

import asyncio
import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, cast
from pathlib import Path
import uuid

from ..core import Component, get_logger, InitMode
from ..config import get_config
from .models import MemoryEntry, MemorySearchResult

logger = get_logger("l2_memory.adapter")

# 重要度档位阈值：强化跨档时同步 importance_level
IMPORTANCE_LEVEL_HIGH = 0.65
IMPORTANCE_LEVEL_LOW = 0.35


def importance_level_for(value: float) -> str:
    """重要度数值 → 档位文本（与 summarizer.importance_to_float 的三档映射对应）"""
    if value >= IMPORTANCE_LEVEL_HIGH:
        return "high"
    if value <= IMPORTANCE_LEVEL_LOW:
        return "low"
    return "medium"


def _metadata_expired(metadata: Dict[str, Any], now_iso: Optional[str] = None) -> bool:
    """判断元数据中的 TTL 是否已过期（ISO 字符串按字典序比较）"""
    expires_at = metadata.get("expires_at")
    if not expires_at:
        return False
    if now_iso is None:
        now_iso = datetime.now().isoformat()
    return str(expires_at) <= now_iso


class L2MemoryAdapter(Component):
    """L2 记忆库适配器

    使用 SQLite + FTS5 存储和检索记忆。

    Attributes:
        _db: SQLite 连接
        _persist_dir: 数据持久化目录
        _persona_id: 当前人格 ID
    """

    def __init__(self, persona_id: str = "default", context=None):
        super().__init__()
        self._db: Optional[sqlite3.Connection] = None
        self._persist_dir: Optional[Path] = None
        self._persona_id = persona_id
        self._context = context
        self._free_list: List[int] = []
        self._fts_ok = False
        self._dirty = False
        self._pending_writes = 0
        self._checkpointing = False
        self._checkpoint_task: Optional["asyncio.Task"] = None
        self._lock = threading.RLock()
        self._init_mode = InitMode.BACKGROUND

    @property
    def name(self) -> str:
        return "l2_memory"

    # ========================================================================
    # 初始化与关闭
    # ========================================================================

    async def initialize(self) -> None:
        config = get_config()

        if not config.get("l2_memory.enable"):
            logger.info("L2 记忆库未启用，跳过初始化")
            self._is_available = False
            self._init_error = "L2 记忆库未启用"
            return

        try:
            self._persist_dir = config.data_dir / "faiss" / f"memory_{self._persona_id}"
            self._persist_dir.mkdir(parents=True, exist_ok=True)

            await asyncio.to_thread(self._open_storage)
            self._is_available = True

            count = self._count_db()
            logger.info(
                f"L2 记忆库初始化成功，persona: {self._persona_id}，当前条目数: {count}"
            )

        except Exception as e:
            logger.error(f"L2 记忆库初始化失败：{e}", exc_info=True)
            self._is_available = False
            self._init_error = f"L2 记忆库初始化失败：{e}"

    def _open_storage(self) -> None:
        """同步打开 SQLite 存储并确保 FTS5 索引一致。"""
        db_path = self._persist_dir / "metadata.db"
        self._db = self._open_db(db_path)
        self._ensure_fts_consistent()

    def _open_db(self, path: Path) -> sqlite3.Connection:
        """打开 SQLite 数据库并确保表结构"""
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                faiss_idx INTEGER PRIMARY KEY,
                memory_id TEXT UNIQUE NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                group_id TEXT,
                user_id TEXT,
                timestamp TEXT,
                kg_processed INTEGER DEFAULT 0,
                persona_id TEXT NOT NULL DEFAULT 'default'
            )
        """)
        # 向后兼容：旧库无 persona_id 列时补列（默认 'default'）
        cols = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
        if "persona_id" not in cols:
            db.execute(
                "ALTER TABLE memories ADD COLUMN persona_id TEXT NOT NULL DEFAULT 'default'"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_group_id ON memories(group_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_kg_processed ON memories(kg_processed)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_persona_id ON memories(persona_id)"
        )
        self._create_fts_table(db)
        db.commit()
        return db

    def _create_fts_table(self, db: sqlite3.Connection) -> None:
        """创建 FTS5 trigram 虚表；失败时关闭关键词检索路并记录原因"""
        try:
            db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    content='memories',
                    content_rowid='faiss_idx',
                    tokenize='trigram'
                )
                """
            )
            self._fts_ok = True
        except sqlite3.Error as e:
            self._fts_ok = False
            logger.warning(f"FTS5 虚表创建失败（需 SQLite ≥ 3.34），关键词检索路关闭：{e}")

    def _ensure_fts_consistent(self) -> None:
        """启动时重建 FTS 虚表，消除与 memories 的漂移。

        external content FTS 表的行计数是透过内容表计算的，`COUNT(*)` 与
        ``integrity-check`` 都无法可靠检测「漏索引」或「孤儿索引」漂移，
        故唯一确定性做法是启动时从 memories 全量重建。数据量为个人记忆
        量级，trigram 重建开销小，且仅在启动时执行一次。
        """
        if not self._fts_ok or self._db is None:
            return
        with self._lock:
            mem_count = self._db.execute(
                "SELECT COUNT(*) FROM memories"
            ).fetchone()[0]
            if mem_count == 0:
                return
            self._rebuild_fts_unlocked()

    def _rebuild_fts_unlocked(self) -> None:
        """从 memories 表重建 FTS 索引（调用方需持有 _lock）"""
        try:
            self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            self._db.commit()
            logger.info("FTS5 关键词索引重建完成")
        except sqlite3.Error as e:
            self._fts_ok = False
            logger.warning(f"FTS5 重建失败，关键词检索路关闭：{e}")

    def _fts_remove_unlocked(self, faiss_idx: int, content: str) -> None:
        """从 FTS 索引删除指定行（调用方需持有 _lock，content 必须为原索引内容）"""
        if not self._fts_ok:
            return
        try:
            self._db.execute(
                "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', ?, ?)",
                (faiss_idx, content),
            )
        except sqlite3.Error as e:
            logger.warning(f"FTS 删除同步失败（启动时计数校验会自愈）：{e}")

    def _fts_remove_rows_unlocked(self, rows: List[tuple]) -> None:
        """批量删除 FTS 索引项（调用方需持有 _lock）。rows 为 (faiss_idx, content)"""
        for faiss_idx, content in rows:
            self._fts_remove_unlocked(faiss_idx, content)

    def _fts_delete_all_unlocked(self) -> None:
        """清空整个 FTS 索引（调用方需持有 _lock），无需旧内容"""
        if not self._fts_ok:
            return
        try:
            self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")
            self._db.commit()
        except sqlite3.Error as e:
            logger.warning(f"FTS 清空同步失败（启动时计数校验会自愈）：{e}")

    def _fts_sync_for_upsert_unlocked(
        self, faiss_idx: int, memory_id: str, new_content: str
    ) -> None:
        """写入前同步 FTS：先删旧索引项，再插新内容（调用方需持有 _lock）。

        覆盖三种情形：纯新增（无旧行）、同 memory_id 内容更新（同槽位）、
        槽位复用或 memory_id 迁移（按 memory_id 与 faiss_idx 双向查旧行）。
        """
        if not self._fts_ok:
            return
        seen_idx: set[int] = set()
        try:
            old_by_memory = self._db.execute(
                "SELECT faiss_idx, content FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchall()
            for old_idx, old_content in old_by_memory:
                if old_idx not in seen_idx:
                    seen_idx.add(old_idx)
                    self._fts_remove_unlocked(old_idx, old_content)

            old_by_idx = self._db.execute(
                "SELECT content FROM memories WHERE faiss_idx = ?", (faiss_idx,)
            ).fetchone()
            if old_by_idx is not None and faiss_idx not in seen_idx:
                seen_idx.add(faiss_idx)
                self._fts_remove_unlocked(faiss_idx, old_by_idx[0])

            self._db.execute(
                "INSERT INTO memories_fts(rowid, content) VALUES(?, ?)",
                (faiss_idx, new_content),
            )
        except sqlite3.Error as e:
            logger.warning(f"FTS 写入同步失败（启动时计数校验会自愈）：{e}")

    async def shutdown(self) -> None:
        """关闭数据库连接"""
        await asyncio.to_thread(self._shutdown_locked)
        logger.info("L2 记忆库已关闭")

    def _shutdown_locked(self) -> None:
        """同步关闭流程（供线程池执行）。"""
        with self._lock:
            if self._db:
                try:
                    self._db.close()
                except Exception:
                    pass

            self._db = None
            self._reset_state()

    # ========================================================================
    # 记忆存储
    # ========================================================================

    async def add_memory_with_result(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        skip_dedup: bool = False,
        persona_id: str = "default",
    ) -> tuple[Optional[str], bool]:
        """写入记忆并返回 ``(memory_id, created)``。"""
        if not self._is_available:
            logger.warning("L2 记忆库不可用，跳过添加记忆")
            return None, False

        if metadata is None:
            metadata = {}

        if "timestamp" not in metadata:
            metadata["timestamp"] = datetime.now().isoformat()
        if "access_count" not in metadata:
            metadata["access_count"] = 0
        if "confidence" not in metadata:
            metadata["confidence"] = 0.5
        if "last_access_time" not in metadata:
            metadata["last_access_time"] = datetime.now().isoformat()

        try:
            memory_id = f"mem_{uuid.uuid4().hex[:12]}"

            with self._lock:
                if self._db is None:
                    return None, False

                if not skip_dedup:
                    existing_id = self._find_similar_unlocked(content, persona_id)
                    if existing_id:
                        logger.debug(f"发现相同记忆，跳过存储：{content[:50]}...")
                        return existing_id, False

                if self._free_list:
                    faiss_idx = self._free_list.pop(0)
                else:
                    row = self._db.execute(
                        "SELECT MAX(faiss_idx) FROM memories"
                    ).fetchone()
                    max_idx = row[0] if row and row[0] is not None else -1
                    faiss_idx = max_idx + 1

                self._upsert_db_unlocked(
                    faiss_idx, memory_id, content, metadata, persona_id
                )

            logger.debug(f"已添加记忆：{memory_id}")
            return memory_id, True

        except Exception as e:
            logger.error(f"添加记忆失败：{e}", exc_info=True)
            return None, False

    async def add_memory(
        self,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        skip_dedup: bool = False,
        persona_id: str = "default",
    ) -> Optional[str]:
        """兼容入口：写入记忆并仅返回 ID。"""
        memory_id, _created = await self.add_memory_with_result(
            content,
            metadata=metadata,
            skip_dedup=skip_dedup,
            persona_id=persona_id,
        )
        return memory_id

    async def add_memories_bulk(
        self,
        items: List[tuple[str, Dict[str, Any]]],
        *,
        skip_dedup: bool = False,
        persona_id: str = "default",
    ) -> List[Optional[str]]:
        """批量写入记忆。

        返回值与输入顺序一致；去重命中返回既有 ID，单条失败返回 None。
        """

        if not items:
            return []
        if not self._is_available:
            return [None] * len(items)

        prepared: List[tuple[str, Dict[str, Any]]] = []
        now = datetime.now().isoformat()
        for content, metadata in items:
            copied = dict(metadata or {})
            copied.setdefault("timestamp", now)
            copied.setdefault("access_count", 0)
            copied.setdefault("confidence", 0.5)
            copied.setdefault("last_access_time", now)
            prepared.append((content, copied))

        try:
            results: List[Optional[str]] = []
            with self._lock:
                if self._db is None:
                    return [None] * len(items)
                for content, metadata in prepared:
                    if not skip_dedup:
                        existing_id = self._find_similar_unlocked(
                            content, persona_id
                        )
                        if existing_id:
                            results.append(existing_id)
                            continue

                    memory_id = f"mem_{uuid.uuid4().hex[:12]}"
                    if self._free_list:
                        faiss_idx = self._free_list.pop(0)
                    else:
                        row = self._db.execute(
                            "SELECT MAX(faiss_idx) FROM memories"
                        ).fetchone()
                        max_idx = row[0] if row and row[0] is not None else -1
                        faiss_idx = max_idx + 1
                    self._upsert_db_unlocked(
                        faiss_idx,
                        memory_id,
                        content,
                        metadata,
                        persona_id,
                        commit=False,
                    )
                    results.append(memory_id)
                self._db.commit()
            return results
        except Exception as e:
            logger.error(f"批量添加记忆失败：{e}", exc_info=True)
            return [None] * len(items)

    def _find_similar_unlocked(
        self, content: str, persona_id: str = "default"
    ) -> Optional[str]:
        """在已持有 ``_lock`` 的情况下查找相同记忆（供 add_memory 去重）。"""
        if self._db is None:
            return None
        row = self._db.execute(
            "SELECT memory_id FROM memories WHERE content = ? AND persona_id = ? LIMIT 1",
            (content, persona_id),
        ).fetchone()
        return row[0] if row else None

        return None

    # ========================================================================
    # 记忆检索
    # ========================================================================

    async def retrieve(
        self,
        query: str,
        group_id: Optional[str] = None,
        top_k: int = 10,
        persona_id: str = "default",
    ) -> List[MemorySearchResult]:
        if not self._is_available:
            return []

        config = get_config()
        timeout_sec = config.get("l2_timeout_ms") / 1000.0

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._search_with_keyword, query, group_id, top_k, persona_id
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(f"L2 记忆检索超时（{timeout_sec}s），跳过")
            return []
        except Exception as e:
            logger.error(f"L2 记忆检索失败：{e}", exc_info=True)
            return []

    def _build_fts_match_expr(self, query: str) -> Optional[str]:
        """把自然语言查询编译为 FTS5 trigram MATCH 表达式。

        trigram 分词要求检索片段 ≥ 3 字符；按空白切分后每个片段作为短语，
        短语间 OR 连接以扩大召回，bm25 排序自然偏好命中更多片段的文档。
        查询内部的 FTS5 特殊字符（双引号）按短语字面量规则转义。

        Returns:
            MATCH 表达式；无有效片段（全部 < 3 字符）时返回 None
        """
        phrases: List[str] = []
        for seg in query.split():
            seg = seg.strip()
            if len(seg) < 3:
                continue
            phrases.append('"' + seg.replace('"', '""') + '"')
        if not phrases:
            return None
        return " OR ".join(phrases)

    def _search_with_keyword(
        self,
        query: str,
        group_id: Optional[str],
        top_k: int,
        persona_id: str = "default",
    ) -> List[MemorySearchResult]:
        """FTS5 trigram 关键词路检索（BM25 排序，分数取负值转高优）"""
        if not self._fts_ok:
            return []
        match_expr = self._build_fts_match_expr(query)
        if not match_expr:
            return []

        with self._lock:
            if self._db is None:
                return []
            sql = (
                "SELECT m.memory_id, m.content, m.metadata, m.persona_id, "
                "bm25(memories_fts) AS bm25_score "
                "FROM memories_fts "
                "JOIN memories m ON m.faiss_idx = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND m.persona_id = ? "
                "AND (json_extract(m.metadata, '$.expires_at') IS NULL "
                "OR json_extract(m.metadata, '$.expires_at') > ?)"
            )
            params: List[Any] = [match_expr, persona_id, datetime.now().isoformat()]
            if group_id:
                # 全局共享记忆（scope=global）不受群隔离限制
                sql += (
                    " AND (m.group_id = ? "
                    "OR json_extract(m.metadata, '$.scope') = 'global')"
                )
                params.append(group_id)
            sql += " ORDER BY bm25_score ASC LIMIT ?"
            params.append(max(1, top_k))
            try:
                rows = self._db.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"FTS 关键词检索失败，本路跳过：{e}")
                return []

        results: List[MemorySearchResult] = []
        for memory_id, content, metadata_json, row_persona_id, bm25_score in rows:
            entry = MemoryEntry(
                id=memory_id,
                content=content,
                metadata=json.loads(metadata_json),
                persona_id=row_persona_id,
            )
            # bm25() 越小越相关（通常为负）；score = -bm25 转为越高越好
            results.append(
                MemorySearchResult(
                    entry=entry, score=-float(bm25_score), distance=0.0
                )
            )
        return results

    async def retrieve_hybrid(
        self,
        query: str,
        group_id: Optional[str] = None,
        top_k: int = 10,
        persona_id: str = "default",
        relevance_threshold: float = 0.0,
    ) -> List[MemorySearchResult]:
        """关键词检索（原混合检索入口，现统一走 FTS5 BM25）。"""
        if not self._is_available:
            return []

        config = get_config()
        timeout_sec = config.get("l2_timeout_ms") / 1000.0
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(
                    self._search_with_keyword, query, group_id, top_k, persona_id
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning(f"L2 记忆检索超时（{timeout_sec}s），跳过")
            return []
        except Exception as e:
            logger.error(f"L2 记忆检索失败：{e}", exc_info=True)
            return []

        return self._apply_global_demotion(results)

    def _apply_global_demotion(
        self, results: List[MemorySearchResult]
    ) -> List[MemorySearchResult]:
        """对全局共享记忆（scope=global）的融合分乘降权系数并重排。

        系数来自隐藏配置 ``l2_hybrid_global_scope_factor``（默认 0.8），
        1 表示不降权。全局记忆跨群命中，适度降权让群内直接相关的记忆优先。
        """
        if not results:
            return results
        config = get_config()
        try:
            factor = float(config.get("l2_hybrid_global_scope_factor", 0.8))
        except (TypeError, ValueError):
            factor = 0.8
        if factor >= 1.0:
            return results

        demoted: List[MemorySearchResult] = []
        for r in results:
            if r.entry.metadata.get("scope") == "global":
                new_score = r.score * factor
                demoted.append(
                    MemorySearchResult(
                        entry=r.entry, score=new_score, distance=1.0 - new_score
                    )
                )
            else:
                demoted.append(r)
        demoted.sort(key=lambda r: r.score, reverse=True)
        return demoted

    async def set_memory_scope(self, memory_id: str, scope: str) -> bool:
        """设置记忆作用域：``global``（全局共享）或 ``group``。"""
        if not self._is_available or self._db is None or not memory_id:
            return False
        scope = (scope or "").strip().lower()
        if scope not in ("global", "group"):
            return False
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT metadata FROM memories WHERE memory_id = ?", (memory_id,)
                ).fetchone()
                if not row:
                    return False
                metadata = json.loads(row[0])
                if scope == "global":
                    metadata["scope"] = "global"
                else:
                    metadata.pop("scope", None)
                self._db.execute(
                    "UPDATE memories SET metadata = ? WHERE memory_id = ?",
                    (json.dumps(metadata, ensure_ascii=False), memory_id),
                )
                self._db.commit()
            return True
        except Exception as e:
            logger.error(f"设置记忆作用域失败：{e}", exc_info=True)
            return False

    # ========================================================================
    # 访问更新
    # ========================================================================

    async def update_access(self, memory_id: str) -> bool:
        if not self._is_available:
            return False

        config = get_config()
        reinforce = bool(config.get("l2_enable_hit_reinforcement", True))
        step = float(config.get("l2_hit_reinforcement_step", 0.1))

        try:
            # SELECT + UPDATE + COMMIT 下放线程池，避免阻塞事件循环
            updated = await asyncio.to_thread(
                self._update_access_locked, memory_id, reinforce, step
            )
            if updated:
                logger.debug(f"记忆访问更新成功：{memory_id}")
            return updated
        except Exception as e:
            logger.error(f"更新记忆访问失败：{e}", exc_info=True)
            return False

    def _update_access_locked(
        self, memory_id: str, reinforce: bool, step: float
    ) -> bool:
        """持锁更新单条记忆的访问计数（供线程池执行）。"""
        with self._lock:
            if self._db is None:
                return False
            row = self._db.execute(
                "SELECT metadata FROM memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()

            if not row:
                logger.warning(f"记忆不存在：{memory_id}")
                return False

            metadata = json.loads(row[0])
            metadata["access_count"] = metadata.get("access_count", 0) + 1
            metadata["last_access_time"] = datetime.now().isoformat()

            if reinforce:
                old = metadata.get("importance", 0.5)
                try:
                    old = float(old)
                except (ValueError, TypeError):
                    old = 0.5
                new = round(old + step * (1.0 - old), 4)
                metadata["importance"] = new
                metadata["importance_level"] = importance_level_for(new)

            self._db.execute(
                "UPDATE memories SET metadata = ? WHERE memory_id = ?",
                (json.dumps(metadata, ensure_ascii=False), memory_id),
            )
            self._db.commit()
            return True

    async def batch_update_access(self, memory_ids: List[str]) -> int:
        """批量更新记忆访问计数，并按配置做命中强化。

        命中强化（``l2_enable_hit_reinforcement`` 默认开）：对每条命中记忆
        渐近提升其 importance——``I' = I + step·(1-I)``，自限趋近 1 不会失控；
        跨越档位阈值时同步 ``importance_level``。仅在对话注入检索路径调用，
        梦境批量扫描（batch_retrieve）不经过此方法，因此不会被强化。

        Args:
            memory_ids: 需要更新访问的记忆 ID 列表

        Returns:
            成功更新的数量
        """
        if not self._is_available or not memory_ids:
            return 0

        config = get_config()
        reinforce = bool(config.get("l2_enable_hit_reinforcement", True))
        step = float(config.get("l2_hit_reinforcement_step", 0.1))

        try:
            # 批量 SELECT + UPDATE + COMMIT 下放线程池，避免阻塞事件循环
            updated = await asyncio.to_thread(
                self._batch_update_access_locked, list(memory_ids), reinforce, step
            )
            logger.debug(
                f"批量更新记忆访问：{updated}/{len(memory_ids)}"
                f"{'（含命中强化）' if reinforce and updated else ''}"
            )
            return updated
        except Exception as e:
            logger.error(f"批量更新记忆访问失败：{e}", exc_info=True)
            return 0

    def _batch_update_access_locked(
        self, memory_ids: List[str], reinforce: bool, step: float
    ) -> int:
        """持锁批量更新访问计数（供线程池执行）。"""
        now = datetime.now().isoformat()
        updated = 0
        with self._lock:
            if self._db is None:
                return 0
            placeholders = ",".join("?" * len(memory_ids))
            rows = self._db.execute(
                f"SELECT memory_id, metadata FROM memories WHERE memory_id IN ({placeholders})",
                memory_ids,
            ).fetchall()

            for memory_id, metadata_json in rows:
                try:
                    metadata = json.loads(metadata_json)
                except (ValueError, TypeError):
                    continue
                metadata["access_count"] = metadata.get("access_count", 0) + 1
                metadata["last_access_time"] = now

                if reinforce:
                    old = metadata.get("importance", 0.5)
                    try:
                        old = float(old)
                    except (ValueError, TypeError):
                        old = 0.5
                    new = round(old + step * (1.0 - old), 4)
                    metadata["importance"] = new
                    metadata["importance_level"] = importance_level_for(new)

                self._db.execute(
                    "UPDATE memories SET metadata = ? WHERE memory_id = ?",
                    (json.dumps(metadata, ensure_ascii=False), memory_id),
                )
                updated += 1

            self._db.commit()
            return updated

    # ========================================================================
    # 内容与元数据更新
    # ========================================================================

    async def update_metadata(self, memory_id: str, metadata: Dict[str, Any]) -> bool:
        if not self._is_available or not memory_id:
            return False

        try:
            group_id = metadata.get("group_id")
            user_id = metadata.get("user_id")
            timestamp = metadata.get("timestamp")
            kg_processed = 1 if metadata.get("kg_processed") else 0

            self._db_write(
                "UPDATE memories SET metadata = ?, group_id = ?, user_id = ?, timestamp = ?, kg_processed = ? WHERE memory_id = ?",
                (
                    json.dumps(metadata, ensure_ascii=False),
                    group_id,
                    user_id,
                    timestamp,
                    kg_processed,
                    memory_id,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"更新元数据失败：{e}", exc_info=True)
            return False

    async def update_content(self, memory_id: str, new_content: str) -> bool:
        return (await self.batch_update_contents([(memory_id, new_content)])) == 1

    async def batch_update_contents(self, updates: List[tuple[str, str]]) -> int:
        """批量更新内容，仅发起一次 Embedding 请求。

        ``timestamp`` 表示原记忆/事件基准时间，不因文本规范化而覆盖；更新时写入
        ``updated_at``，并把 ``kg_processed`` 重置为 False，使修改后的内容能够
        重新同步到 L3。
        """
        if not self._is_available or self._db is None or not updates:
            return 0

        # 保留最后一次同 ID 更新并过滤空 ID，避免同一槽位在一批内重复替换。
        update_map = {memory_id: content for memory_id, content in updates if memory_id}
        if not update_map:
            return 0

        try:
            with self._lock:
                placeholders = ",".join("?" for _ in update_map)
                rows = self._db.execute(
                    f"SELECT memory_id, faiss_idx, metadata, persona_id "
                    f"FROM memories WHERE memory_id IN ({placeholders})",
                    tuple(update_map),
                ).fetchall()

                if not rows:
                    return 0

                now = datetime.now().isoformat()
                updated = 0
                for memory_id, faiss_idx, metadata_json, persona_id in rows:
                    metadata = json.loads(metadata_json)
                    metadata["updated_at"] = now
                    metadata["kg_processed"] = False
                    self._upsert_db_unlocked(
                        faiss_idx,
                        memory_id,
                        update_map[memory_id],
                        metadata,
                        persona_id,
                    )
                    updated += 1

            if updated:
                logger.info(f"已批量更新记忆内容：{updated}/{len(update_map)}")
            return updated
        except Exception as e:
            logger.error(f"批量更新记忆内容失败：{e}", exc_info=True)
            return 0

    # ========================================================================
    # 删除操作
    # ========================================================================

    async def delete_entries(self, memory_ids: List[str]) -> bool:
        if not self._is_available or self._db is None or not memory_ids:
            return False

        try:
            with self._lock:
                placeholders = ",".join("?" for _ in memory_ids)
                rows = self._db.execute(
                    f"SELECT faiss_idx, content FROM memories WHERE memory_id IN ({placeholders})",
                    memory_ids,
                ).fetchall()

                if not rows:
                    return False

                faiss_indices = [row[0] for row in rows]
                self._free_list.extend(faiss_indices)
                self._free_list.sort()

                # 同步 FTS 索引删除
                self._fts_remove_rows_unlocked(rows)

                # 从 SQLite 删除
                self._db.execute(
                    f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                    memory_ids,
                )
                self._db.commit()

            logger.info(f"已删除 {len(faiss_indices)} 条记忆")
            return True
        except Exception as e:
            logger.error(f"删除记忆失败：{e}", exc_info=True)
            return False

    async def delete_collection(self) -> bool:
        if self._persist_dir is None:
            return False

        try:
            # 取锁后再关闭/清理，避免与并发的检索/写入交错导致 SQLite 错误
            with self._lock:
                # 关闭数据库
                if self._db:
                    self._db.close()
                    self._db = None

                # 删除所有文件
                import shutil

                if self._persist_dir.exists():
                    shutil.rmtree(self._persist_dir)

                self._free_list = []
                self._dirty = False
            logger.info(f"已删除 collection: {self._persona_id}")
            return True
        except Exception as e:
            logger.error(f"删除 collection 失败：{e}", exc_info=True)
            return False

    # ========================================================================
    # 容量管理
    # ========================================================================

    async def get_entry_by_id(
        self,
        memory_id: str,
        persona_id: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        """按 memory_id 精确查询单条记忆。

        供 correct_memory 等需要按 ID 定位（而非语义检索）的场景使用，
        避免用 memory_id 作为语义查询导致误命中不相关记忆。若指定
        persona_id 则同时限定命名空间。
        """
        if not self._is_available or not self._db:
            return None

        try:
            with self._lock:
                if persona_id is not None:
                    row = self._db.execute(
                        "SELECT memory_id, content, metadata, persona_id FROM memories WHERE memory_id = ? AND persona_id = ?",
                        (memory_id, persona_id),
                    ).fetchone()
                else:
                    row = self._db.execute(
                        "SELECT memory_id, content, metadata, persona_id FROM memories WHERE memory_id = ?",
                        (memory_id,),
                    ).fetchone()

            if not row:
                return None

            return MemoryEntry(
                id=row[0],
                content=row[1],
                metadata=json.loads(row[2]),
                persona_id=row[3],
            )
        except Exception as e:
            logger.error(f"按 ID 查询记忆失败：{e}")
            return None

    async def get_entry_count(self) -> int:
        if not self._is_available or not self._db:
            return 0
        try:
            return self._count_db()
        except Exception as e:
            logger.error(f"获取条目数失败：{e}")
            return 0

    async def get_all_entries(
        self, persona_id: Optional[str] = None
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            if persona_id is not None:
                rows = self._db_fetchall(
                    "SELECT memory_id, content, metadata, persona_id FROM memories WHERE persona_id = ?",
                    (persona_id,),
                )
            else:
                rows = self._db_fetchall(
                    "SELECT memory_id, content, metadata, persona_id FROM memories"
                )

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取所有条目失败：{e}")
            return []

    async def get_all_persona_ids(self) -> List[str]:
        """获取库中所有出现过的 persona_id（供 dream 按人格遍历）"""
        if not self._is_available or not self._db:
            return []
        try:
            rows = self._db_fetchall(
                "SELECT DISTINCT persona_id FROM memories"
            )
            return [row[0] for row in rows if row[0]]
        except Exception as e:
            logger.error(f"获取 persona 列表失败：{e}")
            return []

    async def get_entries_by_group(
        self, group_id: str, persona_id: str = "default"
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            rows = self._db_fetchall(
                "SELECT memory_id, content, metadata, persona_id FROM memories WHERE group_id = ? AND persona_id = ?",
                (group_id, persona_id),
            )

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取群聊条目失败：{e}")
            return []

    async def get_entries_by_user(
        self, user_id: str, persona_id: str = "default"
    ) -> List[MemoryEntry]:
        if not self._is_available or not self._db:
            return []

        try:
            rows = self._db_fetchall(
                "SELECT memory_id, content, metadata, persona_id FROM memories WHERE user_id = ? AND persona_id = ?",
                (user_id, persona_id),
            )

            return [
                MemoryEntry(
                    id=row[0],
                    content=row[1],
                    metadata=json.loads(row[2]),
                    persona_id=row[3],
                )
                for row in rows
            ]
        except Exception as e:
            logger.error(f"获取用户条目失败：{e}")
            return []

    async def get_stats(self) -> Dict[str, Any]:
        if not self._is_available or not self._db:
            return {"total_count": 0, "group_count": 0}

        try:
            # 走线程池，避免在事件循环线程持同步锁、与 executor 中的长 FAISS
            # 检索争用而阻塞整个插件
            row = await asyncio.to_thread(
                self._db_fetchone,
                "SELECT COUNT(*), COUNT(DISTINCT group_id) FROM memories",
            )
            return {"total_count": row[0], "group_count": row[1]}
        except Exception as e:
            logger.error(f"获取L2统计失败：{e}", exc_info=True)
            return {"total_count": 0, "group_count": 0}

    async def delete_by_group(self, group_id: str, persona_id: str = "default") -> int:
        if not self._is_available or self._db is None:
            return 0

        try:
            with self._lock:
                # 全局共享记忆（scope=global）属于知识库而非群资产，群级清空跳过
                scope_guard = (
                    " AND (json_extract(metadata, '$.scope') IS NULL "
                    "OR json_extract(metadata, '$.scope') != 'global')"
                )
                rows = self._db.execute(
                    "SELECT faiss_idx, content FROM memories "
                    "WHERE group_id = ? AND persona_id = ?" + scope_guard,
                    (group_id, persona_id),
                ).fetchall()

                if not rows:
                    logger.debug(f"群聊 {group_id} (persona {persona_id}) 没有记忆记录")
                    return 0

                faiss_indices = [row[0] for row in rows]

                self._free_list.extend(faiss_indices)
                self._free_list.sort()

                self._fts_remove_rows_unlocked(rows)

                self._db.execute(
                    "DELETE FROM memories WHERE group_id = ? AND persona_id = ?"
                    + scope_guard,
                    (group_id, persona_id),
                )
                self._db.commit()

            logger.info(
                f"已删除群聊 {group_id} (persona {persona_id}) 的 {len(faiss_indices)} 条记忆"
            )
            return len(faiss_indices)
        except Exception as e:
            logger.error(f"删除群聊记忆失败: {e}", exc_info=True)
            return 0

    async def delete_by_user(
        self, user_id: str, group_id: Optional[str] = None, persona_id: str = "default"
    ) -> int:
        if not self._is_available or self._db is None:
            return 0

        try:
            with self._lock:
                if group_id:
                    rows = self._db.execute(
                        "SELECT faiss_idx, memory_id, metadata, content FROM memories WHERE group_id = ? AND persona_id = ?",
                        (group_id, persona_id),
                    ).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT faiss_idx, memory_id, metadata, content FROM memories WHERE persona_id = ?",
                        (persona_id,),
                    ).fetchall()

                if not rows:
                    return 0

                ids_to_delete = []
                faiss_indices_to_delete = []
                fts_rows_to_delete = []
                for faiss_idx, memory_id, metadata_json, content in rows:
                    metadata = json.loads(metadata_json)
                    # 全局共享记忆属于知识库资产，用户级清理跳过
                    if metadata.get("scope") == "global":
                        continue
                    # 命中条件二选一：记忆主体即该用户（metadata.user_id，
                    # 如 save_memory 工具写入），或该用户参与了场景记忆
                    # （active_users CSV，如 L1 总结写入）。仅匹配任一条件，
                    # 避免只依赖 active_users 时漏删无该字段的工具记忆。
                    owner_hit = metadata.get("user_id") == user_id
                    participant_hit = False
                    active_users = metadata.get("active_users", "")
                    if active_users:
                        participants = [
                            u.strip() for u in active_users.split(",") if u.strip()
                        ]
                        participant_hit = user_id in participants
                    if owner_hit or participant_hit:
                        ids_to_delete.append(memory_id)
                        faiss_indices_to_delete.append(faiss_idx)
                        fts_rows_to_delete.append((faiss_idx, content))

                if not ids_to_delete:
                    logger.debug(f"用户 {user_id} 没有记忆记录")
                    return 0

                self._free_list.extend(faiss_indices_to_delete)
                self._free_list.sort()

                self._fts_remove_rows_unlocked(fts_rows_to_delete)

                placeholders = ",".join("?" for _ in ids_to_delete)
                self._db.execute(
                    f"DELETE FROM memories WHERE memory_id IN ({placeholders})",
                    ids_to_delete,
                )
                self._db.commit()

            logger.info(f"已删除用户 {user_id} 的 {len(ids_to_delete)} 条记忆")
            return len(ids_to_delete)
        except Exception as e:
            logger.error(f"删除用户记忆失败: {e}", exc_info=True)
            return 0

    async def delete_all(self, persona_id: Optional[str] = None) -> int:
        if not self._is_available:
            return 0

        try:
            with self._lock:
                if persona_id is not None:
                    rows = self._db.execute(
                        "SELECT faiss_idx, content FROM memories WHERE persona_id = ?",
                        (persona_id,),
                    ).fetchall()
                    count = len(rows)
                    if count == 0:
                        return 0
                    faiss_indices = [row[0] for row in rows]
                    self._free_list.extend(faiss_indices)
                    self._free_list.sort()
                    self._fts_remove_rows_unlocked(rows)
                    self._db.execute(
                        "DELETE FROM memories WHERE persona_id = ?", (persona_id,)
                    )
                    self._db.commit()
                    logger.info(f"已删除 persona {persona_id} 的 {count} 条记忆")
                    return count

                count = self._count_db()
                if count == 0:
                    return 0

                self._db_write("DELETE FROM memories")
                self._fts_delete_all_unlocked()

                self._free_list = []
                logger.info(f"已删除所有记忆，共 {count} 条")
                return count
        except Exception as e:
            logger.error(f"删除所有记忆失败: {e}", exc_info=True)
            return 0

    # ========================================================================
    # 内部辅助
    # ========================================================================

    def _db_fetchone(self, sql: str, params=()) -> Optional[tuple]:
        """线程安全的 DB 查询：持锁内完成 execute + fetchone，返回数据行。

        数据在锁内取出，避免 cursor 在锁外 fetch 时
        受并发写影响；适合配合 asyncio.to_thread 把读路径移出事件循环。
        """
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    def _db_fetchall(self, sql: str, params=()) -> List[tuple]:
        """线程安全的 DB 查询：持锁内完成 execute + fetchall，返回数据行列表。"""
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _db_write(self, sql: str, params=()):
        """线程安全的 DB 写入（INSERT/UPDATE/DELETE + COMMIT）"""
        with self._lock:
            self._db.execute(sql, params)
            self._db.commit()

    def _count_db(self) -> int:
        if not self._db:
            return 0
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM memories").fetchone()
            return row[0]

    def _upsert_db(
        self,
        faiss_idx: int,
        memory_id: str,
        content: str,
        metadata: Dict[str, Any],
        persona_id: str = "default",
    ) -> None:
        """插入或更新 SQLite 记录"""
        group_id = metadata.get("group_id")
        user_id = metadata.get("user_id")
        timestamp = metadata.get("timestamp")
        kg_processed = 1 if metadata.get("kg_processed") else 0
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        with self._lock:
            self._fts_sync_for_upsert_unlocked(faiss_idx, memory_id, content)
            self._db.execute(
                """INSERT OR REPLACE INTO memories
                   (faiss_idx, memory_id, content, metadata, group_id, user_id, timestamp, kg_processed, persona_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    faiss_idx,
                    memory_id,
                    content,
                    metadata_json,
                    group_id,
                    user_id,
                    timestamp,
                    kg_processed,
                    persona_id,
                ),
            )
            self._db.commit()

    def _upsert_db_unlocked(
        self,
        faiss_idx: int,
        memory_id: str,
        content: str,
        metadata: Dict[str, Any],
        persona_id: str = "default",
        *,
        commit: bool = True,
    ) -> None:
        """插入或更新 SQLite 记录（调用方需持有 _lock）"""
        group_id = metadata.get("group_id")
        user_id = metadata.get("user_id")
        timestamp = metadata.get("timestamp")
        kg_processed = 1 if metadata.get("kg_processed") else 0
        metadata_json = json.dumps(metadata, ensure_ascii=False)

        self._fts_sync_for_upsert_unlocked(faiss_idx, memory_id, content)
        self._db.execute(
            """INSERT OR REPLACE INTO memories
               (faiss_idx, memory_id, content, metadata, group_id, user_id, timestamp, kg_processed, persona_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                faiss_idx,
                memory_id,
                content,
                metadata_json,
                group_id,
                user_id,
                timestamp,
                kg_processed,
                persona_id,
            ),
        )
        if commit:
            self._db.commit()
