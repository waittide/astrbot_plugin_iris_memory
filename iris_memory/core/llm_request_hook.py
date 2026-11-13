"""
LLM 请求钩子处理模块

负责处理 LLM 请求前的钩子逻辑，包括：
- L1 上下文注入
- 用户画像注入
- 图片解析（related 模式）
- L2 记忆注入
- L3 知识图谱注入
- learning 学习上下文注入（表达风格/暗语/对话样例）

注入策略：
所有动态内容（L1/画像/L2/L3/learning）统一通过 req.extra_user_content_parts 注入，
作为额外的用户消息内容块放在本轮用户输入之后。不修改 req.system_prompt、
req.contexts 和 req.prompt。

这样做的理由：
- L1/L2/L3/画像/learning 均为每轮变化的动态上下文，不属于稳定角色设定
- 注入 system_prompt 会使系统提示词每轮变化，破坏模型服务端的提示词缓存，
  显著增加请求成本和首 token 延迟
- extra_user_content_parts 适合承载"本轮相关记忆片段"等动态上下文
- 不修改 req.contexts 和 req.prompt，避免与 AstrBot 对话管理冲突

所有注入的 TextPart 均调用 mark_as_temp() 标记为临时内容（需 AstrBot >= 4.24.0），
使其不持久化到会话历史中。若当前 AstrBot 版本不支持 mark_as_temp()，则自动跳过。
"""

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, List, Optional, cast

from ..core import get_logger
from ..core.event_extras import L1_CURRENT_EVENT_RECORD_COUNT
from ..llm_modules import L2_QUERY_REWRITE

_KG_STOPWORDS = frozenset(
    {
        "什么",
        "怎么",
        "如何",
        "为什么",
        "这个",
        "那个",
        "今天",
        "昨天",
        "明天",
        "喜欢",
        "觉得",
        "想要",
        "可以",
        "知道",
        "一下",
        "一些",
        "告诉",
        "请问",
        "还是",
        "的话",
        "不是",
        "没有",
        "现在",
        "已经",
        "应该",
        "可能",
        "因为",
        "所以",
        "但是",
    }
)

_QUOTED_PATTERN = re.compile(r'[""「」『』]([^""「」『』]+)[""「」『』]')
_CHINESE_WORD_PATTERN = re.compile(r"[一-龥]{2,6}")

# 查询改写本身需要额外调用一次 LLM。仅在用户明确引用过去的信息、记忆、
# 偏好或历史对话时才值得支付这段延迟；普通闲聊仍会使用原始消息做 L2 检索。
_MEMORY_INTENT_PATTERNS = (
    re.compile(
        r"(?:还|是否)?记得|记不记得|回忆|忘了|记忆|"
        r"以前|之前|曾经|上次|过去|历史|"
        r"我(?:之前|以前|上次)?(?:说过|提过|告诉过)|"
        r"你知道我|关于我|我的(?:偏好|喜好|习惯|经历)"
    ),
    re.compile(
        r"\b(?:remember|recall|memory|memories|previously|before|last\s+time|"
        r"told\s+you|mentioned|my\s+(?:preference|preferences|history))\b",
        re.IGNORECASE,
    ),
)

# 查询改写是交互热路径：成功结果做小型 LRU，同一个 persona 的同一标准化
# 查询只允许一个在途请求，其余调用加入 singleflight。
_QUERY_REWRITE_CACHE: "OrderedDict[tuple[str, str], str]" = OrderedDict()
_QUERY_REWRITE_INFLIGHT: dict[tuple[str, str], asyncio.Task[Optional[str]]] = {}


def _normalize_rewrite_query(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _local_memory_query_rewrite(text: str) -> Optional[str]:
    """Handle stable high-frequency forms without spending an LLM call."""

    compact = re.sub(r"[\s，。！？、,.!?]", "", text or "")
    if re.search(r"(?:还)?记得我(?:喜欢|爱好|偏好|喜好)(?:什么|啥)", compact):
        return "用户 偏好 喜好"
    if re.search(r"(?:还)?记得我(?:不喜欢|讨厌)(?:什么|啥)", compact):
        return "用户 不喜欢 讨厌 偏好"
    if re.search(r"(?:还)?记得我(?:以前|之前)?说过什么", compact):
        return "用户 过去 提及 事件"
    if re.search(r"(?:remember|recall)\s+my\s+preferences?", text, re.I):
        return "user preferences likes"
    return None


def _skip_llm_query_rewrite(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < 4:
        return True
    if stripped.startswith((">", "「", "『", '"')) and stripped.endswith(
        ("」", "』", '"')
    ):
        return True
    # 已是短关键词列表时，原文就是更好的检索 query。
    if (
        " " in stripped
        and len(stripped.split()) <= 8
        and not re.search(r"[？?！!。.]", stripped)
        and not _has_memory_retrieval_intent(stripped)
    ):
        return True
    return False

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from astrbot.api.provider import ProviderRequest
    from ..core.components import ComponentManager
    from ..l1_buffer import L1Buffer
    from ..l1_buffer.models import ContextMessage
    from ..l2_memory.models import MemorySearchResult
    from ..profile.models import GroupProfile, UserProfile

logger = get_logger("llm_request_hook")


async def preprocess_llm_request(
    event: "AstrMessageEvent",
    req: "ProviderRequest",
    component_manager: "ComponentManager",
) -> None:
    """LLM 请求钩子处理

    执行所有 LLM 对话前的预处理逻辑。

    注入策略：
    - req.extra_user_content_parts: L1/画像/L2/L3/learning（全部作为动态上下文内容块）
    - req.system_prompt: 不修改
    - req.contexts: 不修改
    - req.prompt: 不修改

    Args:
        event: AstrBot 消息事件对象
        req: LLM 提供者请求对象
        component_manager: 组件管理器实例
    """
    # 图片只调度后台解析，不再占用 on_llm_request 的会话锁。
    # L1 / 画像 / L2 / learning 互不依赖，并发收集；L3 仅等待 L2
    # 的结果，以便使用关联节点做图扩展。每个任务内部故障隔离。
    preprocess_started = time.perf_counter()
    inject_meta: dict = {
        "l1": {},
        "profile": {},
        "l2": {},
        "l3": {},
    }

    user_message = ""
    if hasattr(event, "message_str") and event.message_str:
        user_message = event.message_str
    elif hasattr(event, "get_message_str"):
        user_message = event.get_message_str()

    l1_task = asyncio.create_task(
        _run_stage(
            "l1",
            inject_meta["l1"],
            _collect_l1_context(event, req, component_manager, meta=inject_meta["l1"]),
            "",
        )
    )
    profile_task = asyncio.create_task(
        _run_stage(
            "profile",
            inject_meta["profile"],
            _collect_user_profile(event, component_manager, meta=inject_meta["profile"]),
            "",
        )
    )
    l2_task = asyncio.create_task(
        _run_stage(
            "l2",
            inject_meta["l2"],
            _collect_l2_memory(event, component_manager, meta=inject_meta["l2"]),
            ("", []),
        )
    )

    async def collect_l3_after_l2() -> str:
        _l2_text, l2_results = await l2_task
        return await _run_stage(
            "l3",
            inject_meta["l3"],
            _collect_l3_knowledge_graph(
                event,
                component_manager,
                l2_results,
                user_message,
                meta=inject_meta["l3"],
            ),
            "",
        )

    l3_task = asyncio.create_task(collect_l3_after_l2())
    l1_text, profile_text, l2_pair, l3_text = await asyncio.gather(
        l1_task, profile_task, l2_task, l3_task
    )
    l2_text, _l2_results = l2_pair
    total_duration_ms = _elapsed_ms(preprocess_started)

    combined = _inject_to_extra_user_content_parts(
        req, l1_text, profile_text, l2_text, l3_text
    )

    _record_injection_log(
        event,
        req,
        l1_text=l1_text,
        profile_text=profile_text,
        l2_text=l2_text,
        l3_text=l3_text,
        meta=inject_meta,
        combined=combined,
        user_message=user_message,
        total_duration_ms=total_duration_ms,
    )

    _log_final_context(req)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


async def _run_stage(
    stage: str,
    meta: dict,
    awaitable: Awaitable[Any],
    fallback: Any,
) -> Any:
    """运行单个上下文收集阶段，统一记录耗时并隔离异常。"""
    started = time.perf_counter()
    try:
        return await awaitable
    except Exception as exc:
        meta["error"] = str(exc)
        logger.error(f"{stage} 上下文收集失败，已隔离：{exc}", exc_info=True)
        return fallback
    finally:
        meta["duration_ms"] = _elapsed_ms(started)


def _record_injection_log(
    event: "AstrMessageEvent",
    req: "ProviderRequest",
    *,
    l1_text: str,
    profile_text: str,
    l2_text: str,
    l3_text: str,
    meta: dict,
    combined: str,
    user_message: str,
    total_duration_ms: float,
) -> None:
    """写入统一运行日志（injection 类型），失败不影响主流程"""
    try:
        from ..core.run_log import get_run_log_manager

        group_id = ""
        session_id = ""
        try:
            from ..platform import get_adapter

            adapter = get_adapter(event)
            group_id = adapter.get_group_id(event) or ""
            session_id = adapter.get_session_id(event) or ""
        except Exception:
            pass

        sections = {
            "l1_context": {"chars": len(l1_text), "injected": bool(l1_text), **meta.get("l1", {})},
            "profile": {"chars": len(profile_text), "injected": bool(profile_text), **meta.get("profile", {})},
            "l2_memory": {"chars": len(l2_text), "injected": bool(l2_text), **meta.get("l2", {})},
            "l3_kg": {"chars": len(l3_text), "injected": bool(l3_text), **meta.get("l3", {})},
        }
        injected_count = sum(1 for s in sections.values() if s["injected"])

        if injected_count:
            title = f"注入 {injected_count} 个 section（共 {len(combined)} 字符）"
        else:
            title = "所有 section 均为空，未注入"

        get_run_log_manager().record(
            "injection",
            title,
            success=injected_count > 0,
            group_id=group_id,
            session_id=session_id,
            user_message=user_message,
            injected_sections=injected_count,
            total_chars=len(combined),
            sections=sections,
            stage_timings_ms={
                stage: round(float(meta.get(stage, {}).get("duration_ms", 0.0)), 2)
                for stage in ("l1", "profile", "l2", "l3")
            },
            total_duration_ms=round(total_duration_ms, 2),
            content=combined,
        )
    except Exception as e:
        logger.debug(f"注入运行日志记录失败（已忽略）：{e}")


def _inject_to_extra_user_content_parts(
    req: "ProviderRequest",
    l1_text: str,
    profile_text: str,
    l2_text: str,
    l3_text: str,
) -> str:
    """将所有动态内容注入到 req.extra_user_content_parts"""
    sections = [
        ("l1_context", l1_text),
        ("profile", profile_text),
        ("l2_memory", l2_text),
        ("l3_kg", l3_text),
    ]

    parts = []
    inject_summary = []
    for section_name, content in sections:
        if content:
            parts.append(f"<iris:{section_name}>\n{content}\n</iris:{section_name}>")
            inject_summary.append(f"{section_name}({len(content)}字)")
        else:
            inject_summary.append(f"{section_name}(空)")

    if not parts:
        logger.debug("注入摘要：所有 section 均为空，跳过注入")
        return ""

    logger.debug(f"注入摘要：{', '.join(inject_summary)}")

    combined = "\n\n".join(parts)

    from astrbot.core.agent.message import TextPart as _TextPart

    text_part = _TextPart(text=combined)

    if hasattr(text_part, "mark_as_temp"):
        text_part.mark_as_temp()

    req.extra_user_content_parts.append(text_part)

    return combined


async def _collect_l1_context(
    event: "AstrMessageEvent",
    req: "ProviderRequest",
    component_manager: "ComponentManager",
    meta: Optional[dict] = None,
) -> str:
    """收集 L1 上下文文本

    将 L1 上下文格式化为纯文本返回，作为动态上下文注入到 extra_user_content_parts。
    不模拟为 user/assistant 对话格式，因为 L1 是辅助上下文而非真实对话历史。

    Args:
        event: AstrBot 消息事件对象
        req: LLM 提供者请求对象
        component_manager: 组件管理器实例
        meta: 可选的运行日志元信息字典，函数会填充消息数与截断统计

    Returns:
        格式化的 L1 上下文文本，不可用时返回空字符串
    """
    from ..platform import get_adapter

    buffer = component_manager.get_available_component("l1_buffer")
    if not buffer:
        logger.debug("L1 Buffer 组件不可用，跳过上下文注入")
        if meta is not None:
            meta["skipped"] = "component_unavailable"
        return ""

    from ..config import get_config

    config = get_config()

    l1_buffer = cast("L1Buffer", buffer)

    adapter = get_adapter(event)
    group_id = adapter.get_group_id(event)
    # L1 队列键使用会话 ID（私聊为 private:{user_id}），
    # 与写入侧 message_hook 保持一致，避免不同私聊用户共享空字符串队列
    session_id = adapter.get_session_id(event)

    # schema hint 承诺「最少 20 条」，此处夹紧保证行为与提示一致
    max_length = max(20, cast(int, config.get("l1_buffer.inject_queue_length", 50)))

    messages = l1_buffer.get_context(session_id, max_length)
    if not messages:
        logger.debug(f"群聊 {group_id} 的 L1 上下文为空，跳过注入")
        if meta is not None:
            meta["skipped"] = "empty"
        return ""

    current_user_id = adapter.get_user_id(event)
    excluded_current = False
    try:
        recorded_count = event.get_extra(L1_CURRENT_EVENT_RECORD_COUNT)
    except Exception:
        recorded_count = 0
    if type(recorded_count) is not int or recorded_count <= 0:
        recorded_count = 0

    # 只排除已确认由当前事件写入的尾部记录。纯 @ 不会写入 L1，不能仅凭
    # “最后一条来自同一用户”就删除，否则会误删用户紧邻 @ 之前的发言。
    removed = 0
    while (
        removed < recorded_count
        and messages
        and messages[-1].role == "user"
        and messages[-1].source == current_user_id
    ):
        messages = messages[:-1]
        removed += 1
    excluded_current = removed > 0

    if not messages:
        logger.debug(f"群聊 {group_id} 排除当前消息后 L1 上下文为空，跳过注入")
        if meta is not None:
            meta["skipped"] = "empty_after_excluding_current"
        return ""

    max_content_chars = cast(int, config.get("l1_inject_max_content_chars"))

    lines = []
    if group_id:
        lines.append("【近期群聊记录】")
        lines.append(
            "以下是群里最近的对话，帮助你了解当前话题。"
            "消息按从旧到新排列，越靠下越接近当前对话："
        )
    else:
        lines.append("【近期对话记录】")
        lines.append(
            "以下是你们最近的对话。"
            "消息按从旧到新排列，越靠下越接近当前对话："
        )

    lines.append("← 较早")

    msg_id_map: dict[str, tuple[str, str, str]] = {}
    for msg in messages:
        if msg.metadata:
            mid = msg.metadata.get("message_id")
            if mid:
                uname = (
                    msg.metadata.get("user_name", "") if msg.role == "user" else "Bot"
                )
                uid = msg.source if msg.role == "user" and msg.source else ""
                msg_id_map[str(mid)] = (msg.content, uname, uid)

    truncated_messages = 0
    truncated_replies = 0
    for idx, msg in enumerate(messages):
        content = msg.content
        role = msg.role

        if max_content_chars > 0 and len(content) > max_content_chars:
            truncated_messages += 1
            logger.debug(
                f"L1 上下文消息内容截断：群聊 {group_id}，"
                f"消息 #{idx} 原始 {len(msg.content)} 字符 → {max_content_chars} 字符"
            )
            content = content[:max_content_chars] + "..."

        if role == "user":
            user_name = msg.metadata.get("user_name") if msg.metadata else None
            user_id = msg.source if msg.source else None
            reply_content = msg.metadata.get("reply_content") if msg.metadata else None
            reply_user_name = (
                msg.metadata.get("reply_user_name") if msg.metadata else None
            )
            reply_user_id = msg.metadata.get("reply_user_id") if msg.metadata else None

            if not reply_content and msg.metadata:
                reply_mid = msg.metadata.get("reply_message_id")
                if reply_mid and str(reply_mid) in msg_id_map:
                    ref_content, ref_name, ref_uid = msg_id_map[str(reply_mid)]
                    reply_content = ref_content
                    if not reply_user_name and ref_name:
                        reply_user_name = ref_name
                    if not reply_user_id and ref_uid:
                        reply_user_id = ref_uid

            reply_tag = ""
            if reply_content:
                ref_sender = reply_user_name or "某人"
                if reply_user_id and ref_sender != reply_user_id:
                    ref_sender = f"{ref_sender}({reply_user_id})"
                if len(reply_content) > 80:
                    truncated_replies += 1
                    logger.debug(
                        f"L1 回复内容截断：群聊 {group_id}，"
                        f"消息 #{idx} 回复原始 {len(reply_content)} 字符 → 80 字符"
                    )
                    reply_content = reply_content[:80] + "..."
                reply_tag = f" ↩️回复{ref_sender}「{reply_content}」"
            elif msg.metadata and msg.metadata.get("reply_message_id"):
                reply_tag = " ↩️回复了某条消息"

            sender = user_name or "对方"
            if user_id and user_id != sender:
                sender = f"{sender}({user_id})"

            lines.append(f"{sender}:{reply_tag} {content}")
        elif role == "assistant":
            lines.append(f"Bot: {content}")

    lines.append("→ 较近（紧邻当前消息）")

    try:
        req._l1_context_count = len(messages)
    except AttributeError:
        pass

    if meta is not None:
        meta["message_count"] = len(messages)
        meta["excluded_current_message"] = excluded_current
        meta["truncated_messages"] = truncated_messages
        meta["truncated_replies"] = truncated_replies
        meta["max_content_chars"] = max_content_chars

    logger.debug(f"已收集 {len(messages)} 条 L1 上下文消息到群聊 {group_id}")

    return "\n".join(lines)


async def _collect_user_profile(
    event: "AstrMessageEvent",
    component_manager: "ComponentManager",
    meta: Optional[dict] = None,
) -> str:
    """收集用户画像文本（不直接修改 req）

    Args:
        event: AstrBot 消息事件对象
        component_manager: 组件管理器实例
        meta: 可选的运行日志元信息字典

    Returns:
        格式化的画像文本，不可用时返回空字符串
    """
    from ..config import get_config
    from ..platform import get_adapter

    config = get_config()
    if not config.get("profile.enable"):
        if meta is not None:
            meta["skipped"] = "disabled"
        return ""

    enable_auto_injection = config.get("profile.enable_auto_injection")
    if enable_auto_injection is not None and not enable_auto_injection:
        if meta is not None:
            meta["skipped"] = "auto_injection_disabled"
        return ""

    profile_storage = component_manager.get_available_component("profile")
    if not profile_storage:
        logger.debug("画像系统组件不可用，跳过画像注入")
        if meta is not None:
            meta["skipped"] = "component_unavailable"
        return ""

    adapter = get_adapter(event)
    group_id = adapter.get_group_id(event)
    user_id = adapter.get_user_id(event)

    if not user_id:
        logger.debug("无法获取用户ID，跳过画像注入")
        if meta is not None:
            meta["skipped"] = "no_user_id"
        return ""

    effective_group_id = (
        group_id if config.get("isolation_config.enable_group_isolation") else "default"
    )

    from ..core.persona import resolve_persona
    from ..profile import GroupProfileManager, UserProfileManager

    persona_id = await resolve_persona(component_manager, event)

    group_manager = GroupProfileManager(profile_storage)
    user_manager = UserProfileManager(profile_storage)

    group_profile = await group_manager.get_or_create(group_id, persona_id)
    user_profile = await user_manager.get_or_create(
        user_id, effective_group_id, persona_id
    )

    profile_text = _format_profiles_for_injection(group_profile, user_profile)

    if profile_text:
        logger.debug(f"已收集画像信息：群聊 {group_id} 用户 {user_id}")

    if meta is not None:
        meta["user_id"] = user_id
        meta["group_id"] = group_id or ""
        meta["persona_id"] = persona_id or ""

    return profile_text


async def _rewrite_query_for_retrieval(
    user_message: str,
    component_manager: "ComponentManager",
    persona_id: str = "default",
) -> Optional[str]:
    """查询改写：从用户消息中提取检索意图

    使用 LLM 将用户原始消息改写为更适合向量检索的查询文本。
    例如："你还记得我喜欢什么吗？" → "用户偏好 喜好"

    Args:
        user_message: 用户原始消息
        component_manager: 组件管理器实例

    Returns:
        改写后的查询文本，失败时返回 None（使用原始消息）
    """
    from ..config import get_config
    import asyncio

    config = get_config()

    if not config.get("l2_query_rewrite_enable", True):
        return None

    if not _has_memory_retrieval_intent(user_message):
        logger.debug("用户消息无明确记忆检索意图，跳过 L2 查询改写")
        return None

    local_rewrite = _local_memory_query_rewrite(user_message)
    if local_rewrite:
        return local_rewrite
    if _skip_llm_query_rewrite(user_message):
        return None

    cache_size = max(1, int(config.get("l2_query_rewrite_cache_size", 256)))
    inflight_limit = max(
        1, int(config.get("l2_query_rewrite_inflight_limit", 64))
    )
    key = (persona_id or "default", _normalize_rewrite_query(user_message))
    cached = _QUERY_REWRITE_CACHE.get(key)
    if cached is not None:
        _QUERY_REWRITE_CACHE.move_to_end(key)
        return cached

    llm_manager = component_manager.get_component("llm_manager")
    if not llm_manager or not llm_manager.is_available:
        return None

    timeout_ms = config.get("l2_query_rewrite_timeout_ms", 3000)
    queue_timeout_ms = config.get("l2_query_rewrite_queue_timeout_ms", 800)

    async def do_rewrite() -> Optional[str]:
        prompt = (
            "从用户消息中提取用于记忆检索的关键词，空格分隔，不要解释。\n"
            "提取核心实体、事件和偏好，去除口语语气词。\n"
            f"用户消息：{user_message}\n\n搜索关键词："
        )
        try:
            rewritten = await asyncio.wait_for(
                llm_manager.generate_direct(
                    prompt=prompt,
                    module=L2_QUERY_REWRITE,
                    queue_timeout=max(0.001, float(queue_timeout_ms) / 1000.0),
                    total_timeout=max(0.001, float(timeout_ms) / 1000.0),
                ),
                timeout=max(0.001, float(timeout_ms) / 1000.0),
            )
            rewritten = rewritten.strip()
            if not rewritten or rewritten == "无":
                return None
            _QUERY_REWRITE_CACHE[key] = rewritten
            _QUERY_REWRITE_CACHE.move_to_end(key)
            while len(_QUERY_REWRITE_CACHE) > cache_size:
                _QUERY_REWRITE_CACHE.popitem(last=False)
            logger.debug(f"查询改写：'{user_message[:30]}...' -> '{rewritten}'")
            return rewritten
        except asyncio.TimeoutError:
            logger.debug(f"查询改写超时（{timeout_ms}ms），使用原始消息")
            return None
        except Exception as e:
            logger.debug(f"查询改写失败：{e}，使用原始消息")
            return None

    task = _QUERY_REWRITE_INFLIGHT.get(key)
    if task is None:
        if len(_QUERY_REWRITE_INFLIGHT) >= inflight_limit:
            logger.info("查询改写在途任务已满，回退原始查询")
            return None
        task = asyncio.create_task(do_rewrite(), name="iris-l2-query-rewrite")
        _QUERY_REWRITE_INFLIGHT[key] = task

        def _discard_finished(done: asyncio.Task, *, entry_key: str = key) -> None:
            # 等待者被取消时其 finally 判断 task 未 done 而不入表项弹出，
            # 由本回调在任务真正结束后清理；否则僵尸条目会一直占用
            # inflight_limit 配额，直到积累满后永久拒绝新的改写。
            if _QUERY_REWRITE_INFLIGHT.get(entry_key) is done:
                _QUERY_REWRITE_INFLIGHT.pop(entry_key, None)

        task.add_done_callback(_discard_finished)
    else:
        record_join = getattr(llm_manager, "record_singleflight_join", None)
        if callable(record_join):
            record_join()
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _QUERY_REWRITE_INFLIGHT.get(key) is task:
            _QUERY_REWRITE_INFLIGHT.pop(key, None)


def _has_memory_retrieval_intent(text: str) -> bool:
    """判断消息是否明确要求回忆过往信息。

    这里只控制「是否额外调用 LLM 改写查询」，不控制 L2 检索本身；
    未命中时仍会用原始消息检索，因此宁可保守跳过改写。
    """
    if not text or not text.strip():
        return False
    return any(pattern.search(text) for pattern in _MEMORY_INTENT_PATTERNS)


async def _collect_l2_memory(
    event: "AstrMessageEvent",
    component_manager: "ComponentManager",
    meta: Optional[dict] = None,
) -> tuple[str, List["MemorySearchResult"]]:
    """收集 L2 记忆文本（不直接修改 req）

    执行 L2 向量检索并返回格式化文本和检索结果。

    Args:
        event: AstrBot 消息事件对象
        component_manager: 组件管理器实例
        meta: 可选的运行日志元信息字典，函数会填充检索与预算裁剪统计

    Returns:
        (格式化的记忆文本, L2 检索结果列表)
    """
    from ..config import get_config
    from ..platform import get_adapter

    config = get_config()

    if not config.get("l2_memory.enable"):
        logger.debug("L2 记忆库未启用，跳过记忆注入")
        if meta is not None:
            meta["skipped"] = "disabled"
        return "", []

    l2_status = component_manager.check_component("l2_memory")
    if l2_status == "disabled":
        logger.debug("L2 记忆库未启用，跳过记忆注入")
        if meta is not None:
            meta["skipped"] = "disabled"
        return "", []
    if l2_status == "initializing":
        logger.debug("L2 记忆库正在初始化中，跳过记忆注入")
        if meta is not None:
            meta["skipped"] = "initializing"
        return "", []
    if l2_status != "available":
        logger.debug("L2 记忆库组件不可用，跳过记忆注入")
        if meta is not None:
            meta["skipped"] = "component_unavailable"
        return "", []

    adapter = get_adapter(event)
    group_id = adapter.get_group_id(event)

    from ..core.persona import resolve_persona

    persona_id = await resolve_persona(component_manager, event)

    query_text = ""
    if hasattr(event, "message_str") and event.message_str:
        query_text = event.message_str
    elif hasattr(event, "get_message_str"):
        query_text = event.get_message_str()

    if not query_text:
        logger.debug("无法获取用户消息，跳过记忆检索")
        if meta is not None:
            meta["skipped"] = "no_query"
        return "", []

    try:
        rewritten_query = await _rewrite_query_for_retrieval(
            query_text, component_manager, persona_id
        )
        search_query = rewritten_query if rewritten_query else query_text

        from ..l2_memory import MemoryRetriever

        llm_manager = component_manager.get_component("llm_manager")
        retriever = MemoryRetriever(component_manager, llm_manager)

        l2_results = await retriever.retrieve(
            query=search_query,
            group_id=group_id,
            persona_id=persona_id,
        )

        context_text = ""
        injected_count = 0
        budget_tokens = 0
        if l2_results:
            max_tokens = config.get("token_budget_max_tokens", 5000)
            budget_tokens = max_tokens
            trimmed = MemoryRetriever.trim_by_token_budget(l2_results, max_tokens)
            injected_count = len(trimmed)
            context_lines = ["## 相关记忆"]
            for i, result in enumerate(trimmed, 1):
                context_lines.append(f"{i}. {result.entry.content}")
            context_text = "\n".join(context_lines)

        if context_text:
            logger.debug(f"已收集检索记忆到群聊 {group_id}")

        if meta is not None:
            meta["query"] = query_text
            meta["rewritten_query"] = rewritten_query or ""
            meta["result_count"] = len(l2_results)
            meta["injected_count"] = injected_count
            meta["dropped_by_budget"] = len(l2_results) - injected_count
            meta["budget_tokens"] = budget_tokens

        return context_text, l2_results

    except Exception as e:
        logger.error(f"L2 记忆注入失败: {e}", exc_info=True)
        if meta is not None:
            meta["error"] = str(e)
        return "", []


async def _collect_l3_knowledge_graph(
    event: "AstrMessageEvent",
    component_manager: "ComponentManager",
    l2_results: List["MemorySearchResult"],
    user_message: str = "",
    meta: Optional[dict] = None,
) -> str:
    """收集 L3 知识图谱文本（不直接修改 req）

    检索策略：
    1. 优先基于 L2 记忆关联的节点 ID 进行路径扩展
    2. 若 L2 结果无节点 ID，则基于用户消息关键词搜索图谱
    3. 两种策略的结果合并去重

    Args:
        event: AstrBot 消息事件对象
        component_manager: 组件管理器实例
        l2_results: L2 检索结果
        user_message: 用户当前消息文本
        meta: 可选的运行日志元信息字典，函数会填充检索策略与裁剪统计

    Returns:
        格式化的图谱文本，不可用时返回空字符串
    """
    from ..config import get_config
    from ..platform import get_adapter

    config = get_config()

    if not config.get("l3_kg.enable"):
        logger.debug("L3 知识图谱未启用，跳过图谱注入")
        if meta is not None:
            meta["skipped"] = "disabled"
        return ""

    l3_status = component_manager.check_component("l3_kg")
    if l3_status == "disabled":
        logger.debug("L3 知识图谱未启用，跳过图谱注入")
        if meta is not None:
            meta["skipped"] = "disabled"
        return ""
    if l3_status == "initializing":
        logger.debug("L3 知识图谱正在初始化中，跳过图谱注入")
        if meta is not None:
            meta["skipped"] = "initializing"
        return ""
    if l3_status != "available":
        logger.debug("L3 知识图谱组件不可用，跳过图谱注入")
        if meta is not None:
            meta["skipped"] = "component_unavailable"
        return ""

    kg_adapter = component_manager.get_available_component("l3_kg")

    adapter = get_adapter(event)
    group_id = adapter.get_group_id(event)
    from ..core.persona import resolve_persona

    persona_id = await resolve_persona(component_manager, event)

    # 与 L2 retriever 对齐：群记忆隔离关闭时不按 group_id 过滤，跨群共享图谱
    if not config.get("isolation_config.enable_group_memory_isolation"):
        group_id = None

    try:
        from ..l3_kg import GraphRetriever

        retriever = GraphRetriever(kg_adapter)

        all_nodes: dict[str, dict] = {}
        all_edges: dict[str, dict] = {}
        strategy = "none"
        keywords_used: List[str] = []

        memory_node_ids: List[str] = []
        if l2_results:
            for result in l2_results:
                metadata = result.entry.metadata
                node_id = (
                    metadata.get("memory_node_id")
                    or metadata.get("kg_node_id")
                    or metadata.get("node_id")
                    or metadata.get("entity_id")
                )
                if node_id:
                    memory_node_ids.append(node_id)

            if not memory_node_ids:
                l2_memory_ids = [r.entry.id for r in l2_results]
                if l2_memory_ids:
                    try:
                        memory_node_ids = (
                            await kg_adapter.get_node_ids_by_source_memory_ids(
                                l2_memory_ids, persona_id=persona_id
                            )
                        )
                        if memory_node_ids:
                            logger.debug(
                                f"通过来源记忆反向查找找到 {len(memory_node_ids)} 个图谱节点"
                            )
                    except Exception as e:
                        logger.debug(f"来源记忆反向查找失败: {e}")

        if memory_node_ids:
            nodes, edges = await retriever.retrieve_with_expansion(
                memory_node_ids=memory_node_ids,
                group_id=group_id,
                persona_id=persona_id,
            )
            for n in nodes:
                nid = n.get("id")
                if nid:
                    all_nodes[nid] = n
            for e in edges:
                eid = f"{e.get('source', '')}-{e.get('relation_type', '')}-{e.get('target', '')}"
                all_edges[eid] = e
            strategy = "l2_expansion"
            logger.debug(f"基于 L2 节点扩展：{len(nodes)} 节点，{len(edges)} 边")

        if not memory_node_ids and user_message:
            keywords = _extract_kg_keywords(user_message)
            if keywords:
                keywords_used = keywords
                nodes, edges = await retriever.retrieve_by_keywords(
                    keywords=keywords,
                    group_id=group_id,
                    persona_id=persona_id,
                )
                for n in nodes:
                    nid = n.get("id")
                    if nid:
                        all_nodes[nid] = n
                for e in edges:
                    eid = f"{e.get('source', '')}-{e.get('relation_type', '')}-{e.get('target', '')}"
                    all_edges[eid] = e
                strategy = "keywords"
                logger.debug(f"基于关键词检索：{len(nodes)} 节点，{len(edges)} 边")

        if not all_nodes:
            if meta is not None:
                meta["strategy"] = strategy
                meta["keywords"] = keywords_used
                meta["skipped"] = "no_nodes"
            return ""

        l3_max_tokens = cast(int, config.get("l3_max_inject_tokens", 600))

        graph_text, included_node_ids = retriever.format_for_context(
            list(all_nodes.values()),
            list(all_edges.values()),
            max_tokens=l3_max_tokens,
        )

        if graph_text:
            # 仅对实际注入文本的节点更新访问计数，避免被 token 预算
            # 裁剪掉的节点 access_count 被错误抬升，污染淘汰评分
            await retriever.update_access_count(list(included_node_ids))

            logger.debug(f"图谱检索完成（{len(all_nodes)} 节点，{len(all_edges)} 边）")

        if meta is not None:
            meta["strategy"] = strategy
            meta["keywords"] = keywords_used
            meta["node_count"] = len(all_nodes)
            meta["edge_count"] = len(all_edges)
            meta["included_nodes"] = len(included_node_ids)
            meta["budget_tokens"] = l3_max_tokens

        return graph_text

    except Exception as e:
        logger.error(f"L3 知识图谱注入失败: {e}", exc_info=True)
        if meta is not None:
            meta["error"] = str(e)
        return ""


def _extract_kg_keywords(text: str) -> List[str]:
    """从用户消息中提取知识图谱检索关键词

    Args:
        text: 用户消息文本

    Returns:
        关键词列表
    """
    if not text:
        return []

    keywords: List[str] = []

    quoted = _QUOTED_PATTERN.findall(text)
    keywords.extend(quoted)

    chinese_words = _CHINESE_WORD_PATTERN.findall(text)
    filtered = [w for w in chinese_words if w not in _KG_STOPWORDS]
    keywords.extend(filtered)

    seen = set()
    unique: List[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            unique.append(k)

    return unique[:8]


def _format_profiles_for_injection(
    group_profile: "GroupProfile", user_profile: "UserProfile"
) -> str:
    """格式化画像为注入文本

    将用户画像和群聊画像分开显示，节约 token。

    Args:
        group_profile: 群聊画像对象
        user_profile: 用户画像对象

    Returns:
        格式化的画像文本，任一部分为空则不注入该部分
    """
    from ..config import get_config
    from ..profile.models import favorability_level

    config = get_config()
    favorability_enabled = config.get("profile.favorability_enable", True)

    sections = []

    user_parts = [f"【发送者】ID: {user_profile.user_id}"]
    if user_profile.user_name:
        user_parts.append(f"昵称: {user_profile.user_name}")
    if user_profile.historical_names:
        user_parts.append(f"曾用昵称: {', '.join(user_profile.historical_names)}")
    if user_profile.personality_tags:
        user_parts.append(f"性格: {', '.join(user_profile.personality_tags)}")
    if user_profile.interests:
        user_parts.append(f"兴趣: {', '.join(user_profile.interests)}")
    if user_profile.occupation:
        user_parts.append(f"职业: {user_profile.occupation}")
    if user_profile.language_style:
        user_parts.append(f"语言风格: {user_profile.language_style}")
    if user_profile.communication_style:
        user_parts.append(f"沟通偏好: {user_profile.communication_style}")
    if user_profile.emotional_baseline:
        user_parts.append(f"情感: {user_profile.emotional_baseline}")
    if favorability_enabled and user_profile.favorability > 0:
        level = favorability_level(user_profile.favorability)
        user_parts.append(f"好感度: {int(user_profile.favorability)}({level})")
    if user_profile.bot_relationship:
        user_parts.append(f"称呼: {user_profile.bot_relationship}")
    if user_profile.important_dates:
        dates_str = ", ".join(
            f"{d['date']}({d['description']})" for d in user_profile.important_dates
        )
        user_parts.append(f"重要日期: {dates_str}")
    if user_profile.important_events:
        user_parts.append(f"重要事件: {', '.join(user_profile.important_events)}")
    if user_profile.taboo_topics:
        user_parts.append(f"禁忌: {', '.join(user_profile.taboo_topics)}")
    if user_profile.custom_fields:
        custom_str = ", ".join(
            f"{k}: {v}" for k, v in user_profile.custom_fields.items()
        )
        user_parts.append(custom_str)

    if len(user_parts) > 1:
        sections.append("\n".join(user_parts))

    group_parts = ["【群聊】"]
    if group_profile.interests:
        group_parts.append(f"兴趣: {', '.join(group_profile.interests)}")
    if group_profile.atmosphere_tags:
        group_parts.append(f"氛围: {', '.join(group_profile.atmosphere_tags)}")
    if group_profile.long_term_tags:
        group_parts.append(f"核心特征: {', '.join(group_profile.long_term_tags)}")
    if group_profile.blacklist_topics:
        group_parts.append(f"禁忌: {', '.join(group_profile.blacklist_topics)}")
    if group_profile.custom_fields:
        custom_str = ", ".join(
            f"{k}: {v}" for k, v in group_profile.custom_fields.items()
        )
        group_parts.append(custom_str)

    if len(group_parts) > 1:
        sections.append("\n".join(group_parts))

    return "\n\n".join(sections) if sections else ""


def _log_final_context(req: "ProviderRequest") -> None:
    """输出最终上下文内容的 debug 日志

    在所有注入完成后，输出完整的上下文信息用于问题排查。

    Args:
        req: LLM 提供者请求对象
    """
    from ..config import get_config

    config = get_config()
    if not config.get("enable_context_logging", False):
        return

    log_parts = ["\n" + "=" * 60 + "\n[LLM 请求上下文详情]\n" + "=" * 60]

    if req.system_prompt:
        log_parts.append(
            f"\n[System Prompt]\n{'-' * 40}\n{req.system_prompt}\n{'-' * 40}"
        )
    else:
        log_parts.append("\n[System Prompt]\n(无)")

    if req.extra_user_content_parts:
        log_parts.append(
            f"\n[Extra User Content Parts] (共 {len(req.extra_user_content_parts)} 个)"
        )
        import re as _re

        _iris_tag_pattern = _re.compile(r"<iris:(\w+)>\n(.*?)\n</iris:\1>", _re.DOTALL)
        _section_truncation = 300

        for i, part in enumerate(req.extra_user_content_parts, 1):
            text = getattr(part, "text", None) or str(part)
            tag_sections = _iris_tag_pattern.findall(text)
            if tag_sections:
                log_parts.append(f"  [{i}] ({len(text)} 字符)")
                for sec_name, sec_content in tag_sections:
                    display = sec_content
                    if len(display) > _section_truncation:
                        display = (
                            display[: _section_truncation // 2]
                            + f"\n  ... 省略 {len(display) - _section_truncation} 字 ...\n"
                            + display[-_section_truncation // 2 :]
                        )
                    log_parts.append(f"    <iris:{sec_name}> ({len(sec_content)} 字)")
                    for line in display.split("\n"):
                        log_parts.append(f"      {line}")
            else:
                if len(text) > 500:
                    text = text[:500] + "..."
                log_parts.append(f"  [{i}] {text}")
    else:
        log_parts.append("\n[Extra User Content Parts]\n(无)")

    if req.contexts:
        log_parts.append(f"\n[Contexts] (共 {len(req.contexts)} 条)")
        for i, ctx in enumerate(req.contexts, 1):
            role = ctx.get("role", "unknown")
            content = ctx.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            log_parts.append(f"  [{i}] {role}: {content}")
    else:
        log_parts.append("\n[Contexts]\n(无)")

    if hasattr(req, "functions") and req.functions:
        log_parts.append(f"\n[Functions] (共 {len(req.functions)} 个)")
        for i, func in enumerate(req.functions, 1):
            name = (
                func.get("name", "unknown")
                if isinstance(func, dict)
                else getattr(func, "name", "unknown")
            )
            log_parts.append(f"  [{i}] {name}")

    log_parts.append("\n" + "=" * 60)

    logger.debug("\n".join(log_parts))
