"""L1 消息队列与图片队列的 SQLite 持久化。

L1 队列此前只存在于内存，进程重启或插件热重载会丢失尚未总结的
最近对话。本模块按队列键把三段式队列与待解析图片整体落盘，启动时
还原，使 L1 在重启后仍然连续。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .models import ContextMessage, SegmentedMessageQueue

# 段位标识：与 SegmentedMessageQueue 的三段一一对应
SEGMENT_1 = 1
SEGMENT_2 = 2
SEGMENT_3 = 3

_SEGMENT_ORDER = (
    (SEGMENT_3, "segment_3"),
    (SEGMENT_2, "segment_2"),
    (SEGMENT_1, "segment_1"),
)


class L1QueueStore:
    """按队列键持久化 L1 消息队列。

    队列规模由 ``l1_buffer.inject_queue_length`` 限制（默认 50 条），
    因此每次保存采用「整队列重写」：实现简单，且不会留下段位错乱的
    中间态。
    """

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS l1_messages (
                    queue_key TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    segment INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    persona_id TEXT NOT NULL,
                    PRIMARY KEY (queue_key, seq)
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS l1_images (
                    queue_key TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    item_json TEXT NOT NULL,
                    PRIMARY KEY (queue_key, seq)
                )
                """
            )
            self._db.commit()

    def save(self, queue_key: str, queue: SegmentedMessageQueue) -> None:
        """整体重写一个队列的快照。"""
        rows: list[tuple[object, ...]] = []
        seq = 0
        for segment, attr in _SEGMENT_ORDER:
            for msg in getattr(queue, attr):
                rows.append(
                    (
                        queue_key,
                        seq,
                        segment,
                        msg.role,
                        msg.content,
                        msg.timestamp.isoformat(),
                        msg.token_count,
                        msg.source,
                        json.dumps(msg.metadata, ensure_ascii=False, default=str),
                        msg.persona_id,
                    )
                )
                seq += 1

        with self._lock:
            self._db.execute(
                "DELETE FROM l1_messages WHERE queue_key = ?", (queue_key,)
            )
            if rows:
                self._db.executemany(
                    "INSERT INTO l1_messages (queue_key, seq, segment, role, "
                    "content, timestamp, token_count, source, metadata_json, "
                    "persona_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            self._db.commit()

    def load_all(self) -> dict[str, list[tuple[int, ContextMessage]]]:
        """读取全部队列消息，按队列键分组并保持时间顺序。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM l1_messages ORDER BY queue_key, seq"
            ).fetchall()

        snapshot: dict[str, list[tuple[int, ContextMessage]]] = {}
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            message = ContextMessage.from_dict(
                {
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                    "token_count": int(row["token_count"]),
                    "source": row["source"],
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "persona_id": row["persona_id"],
                }
            )
            snapshot.setdefault(row["queue_key"], []).append(
                (int(row["segment"]), message)
            )
        return snapshot

    def save_images(self, queue_key: str, items: list[object]) -> None:
        """整体重写一个会话的图片队列快照。"""
        rows = [
            (
                queue_key,
                seq,
                json.dumps(item.to_dict(), ensure_ascii=False, default=str),
            )
            for seq, item in enumerate(items)
        ]
        with self._lock:
            self._db.execute("DELETE FROM l1_images WHERE queue_key = ?", (queue_key,))
            if rows:
                self._db.executemany(
                    "INSERT INTO l1_images (queue_key, seq, item_json) "
                    "VALUES (?, ?, ?)",
                    rows,
                )
            self._db.commit()

    def load_images(self) -> dict[str, list[dict]]:
        """读取全部图片队列快照（保留原始字典，由调用方还原为对象）。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM l1_images ORDER BY queue_key, seq"
            ).fetchall()

        snapshot: dict[str, list[dict]] = {}
        for row in rows:
            try:
                item = json.loads(row["item_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                snapshot.setdefault(row["queue_key"], []).append(item)
        return snapshot

    def delete(self, queue_key: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM l1_messages WHERE queue_key = ?", (queue_key,)
            )
            self._db.execute("DELETE FROM l1_images WHERE queue_key = ?", (queue_key,))
            self._db.commit()

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM l1_messages")
            self._db.execute("DELETE FROM l1_images")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
