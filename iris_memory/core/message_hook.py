"""
消息钩子处理模块

负责处理用户发送的消息钩子，包括：
- 添加用户消息到 L1 Buffer
- 嵌套合并转发提取入队
- 可选图片简述（仅当显式选择支持视觉的模型时生效）
- 用户画像更新
"""

from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..core import get_logger
from ..core.event_extras import L1_CURRENT_EVENT_RECORD_COUNT
from ..llm_modules import IMAGE_CAPTION

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from ..core.components import ComponentManager
    from ..l1_buffer import L1Buffer
    from ..llm import LLMManager

logger = get_logger("message_hook")

_name_cache: OrderedDict = OrderedDict()
_NAME_CACHE_MAX_SIZE = 1000
_RICH_MEDIA_EXTRA = "_iris_rich_media_cache"

_IMAGE_CAPTION_PROMPT = (
    "请用一句简短中文客观描述图片的主要内容（不超过60字，不要换行或添加多余前后缀）。"
)


def _mark_current_event_recorded(event: "AstrMessageEvent") -> None:
    """Record how many trailing L1 entries were produced by this event."""
    try:
        current = event.get_extra(L1_CURRENT_EVENT_RECORD_COUNT)
        count = current if type(current) is int and current > 0 else 0
        event.set_extra(L1_CURRENT_EVENT_RECORD_COUNT, count + 1)
    except Exception:
        pass


def _get_cached_name(key: str) -> str | None:
    if key in _name_cache:
        _name_cache.move_to_end(key)
        return _name_cache[key]
    return None


def _set_cached_name(key: str, name: str) -> None:
    if key in _name_cache:
        _name_cache.move_to_end(key)
        _name_cache[key] = name
    else:
        if len(_name_cache) >= _NAME_CACHE_MAX_SIZE:
            _name_cache.popitem(last=False)
        _name_cache[key] = name


def _extract_image_urls(event: "AstrMessageEvent", adapter: Any) -> list[str]:
    """从消息事件中提取最多 3 个可用图片 URL 或本地路径。"""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(candidate: Any) -> None:
        if not candidate or len(urls) >= 3:
            return
        val = str(candidate).strip()
        if not val or val in seen:
            return
        if val.startswith(("http://", "https://", "data:image/", "file://")):
            seen.add(val)
            urls.append(val)
        elif Path(val).is_file():
            seen.add(val)
            urls.append(val)

    try:
        messages = (
            event.get_messages()
            if hasattr(event, "get_messages")
            else getattr(getattr(event, "message_obj", None), "message", None) or []
        )
        for comp in messages:
            if type(comp).__name__ == "Image":
                _add(getattr(comp, "url", None) or getattr(comp, "file", None))
    except Exception:
        pass

    if not urls:
        try:
            raw_msg = adapter.get_raw_message(event)
            segments = raw_msg.get("message", []) if isinstance(raw_msg, dict) else []
            if isinstance(segments, list):
                for seg in segments:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        data = seg.get("data", {}) or {}
                        _add(data.get("url") or data.get("file"))
        except Exception:
            pass

    return urls


async def _caption_images_if_configured(
    event: "AstrMessageEvent",
    adapter: Any,
    component_manager: "ComponentManager",
) -> str:
    """若显式配置了支持图片的图片转述模型，则生成简短中文描述；否则静默跳过。"""
    from ..config import get_config

    image_provider = str(get_config().get("l1_buffer.image_provider", "") or "").strip()
    if not image_provider:
        return ""

    llm_raw = component_manager.get_available_component("llm_manager")
    if not llm_raw:
        return ""
    llm_manager = cast("LLMManager", llm_raw)

    if not llm_manager.supports_image(image_provider):
        logger.debug(f"所选图片模型 {image_provider} 不支持图片模态，跳过图片转述")
        return ""

    image_urls = _extract_image_urls(event, adapter)
    if not image_urls:
        return ""

    try:
        raw_desc = await llm_manager.generate_direct(
            prompt=_IMAGE_CAPTION_PROMPT,
            provider_id=image_provider,
            image_urls=image_urls,
            module=IMAGE_CAPTION,
            timeout=25.0,
        )
    except Exception as exc:
        err_lower = str(exc).lower()
        if any(
            kw in err_lower
            for kw in (
                "image",
                "vision",
                "multimodal",
                "modality",
                "unsupported",
                "not support",
                "invalid_argument",
                "400",
            )
        ):
            llm_manager.mark_image_unsupported(image_provider)
            logger.info(
                f"模型 {image_provider} 不支持图片输入，后续自动跳过图片转述：{exc}"
            )
        else:
            logger.debug(f"图片转述失败，已跳过：{exc}")
        return ""

    desc = " ".join((raw_desc or "").strip().splitlines()).strip()
    if not desc:
        return ""
    if len(desc) > 120:
        desc = desc[:120] + "…"
    return f"[图片: {desc}]"


async def _build_forward_block(
    event: "AstrMessageEvent", adapter: Any
) -> tuple[str, dict[str, Any] | None]:
    """提取并格式化合并转发消息（含多层嵌套）。"""
    try:
        forward_messages = await adapter.get_forward_messages(event)
    except Exception as e:
        logger.debug(f"获取合并转发消息失败: {e}")
        forward_messages = []

    if not forward_messages:
        return "", None

    from ..config import get_config
    from ..utils import count_tokens, sanitize_input

    max_single_tokens = cast(
        int, get_config().get("l1_max_single_message_tokens", 500)
    )
    budget = max(max_single_tokens - 30, 64)

    included_lines: list[str] = []
    included_count = 0
    used_tokens = 0
    truncated = False

    for fwd in forward_messages:
        if not fwd.content:
            continue
        fwd_content = sanitize_input(fwd.content, source="user_message")
        if not fwd_content:
            continue
        user_label = fwd.user_name or "用户"
        line = f"[{user_label}]: {fwd_content}"
        line_tokens = count_tokens(line)

        if used_tokens + line_tokens > budget and included_lines:
            truncated = True
            break
        included_lines.append(line)
        included_count += 1
        used_tokens += line_tokens

    if not included_lines:
        return "", None

    parts = ["【合并转发内容】"] + included_lines
    if truncated:
        total = sum(1 for f in forward_messages if f.content)
        parts.append(f"（已截断，共 {total} 条，已展示 {included_count} 条）")
    combined_content = "\n".join(parts)

    fwd_metadata: dict[str, Any] = {
        "forward": True,
        "forward_total": len(forward_messages),
        "forward_included": included_count,
        "forward_truncated": truncated,
    }
    return combined_content, fwd_metadata


async def extract_event_rich_media(
    event: "AstrMessageEvent", component_manager: "ComponentManager"
) -> tuple[str, str, dict[str, Any] | None]:
    """幂等提取单次消息中的（图片简述, 合并转发块, 转发元数据），缓存在 event extra 中。"""
    try:
        cached = event.get_extra(_RICH_MEDIA_EXTRA)
        if isinstance(cached, tuple) and len(cached) == 3:
            return cached
    except Exception:
        pass

    from ..platform import get_adapter

    adapter = get_adapter(event)
    image_caption = await _caption_images_if_configured(
        event, adapter, component_manager
    )
    forward_text, forward_meta = await _build_forward_block(event, adapter)
    result = (image_caption, forward_text, forward_meta)

    try:
        event.set_extra(_RICH_MEDIA_EXTRA, result)
    except Exception:
        pass
    return result


async def handle_user_message(
    event: "AstrMessageEvent", component_manager: "ComponentManager"
) -> None:
    """处理用户消息钩子：添加用户消息到 L1 Buffer 并更新画像名称。"""
    await _add_to_l1_buffer(event, component_manager)


def _backfill_reply_from_buffer(
    l1_buffer: "L1Buffer",
    session_id: str,
    reply_message_id: str,
    metadata: dict,
) -> None:
    """从 L1 Buffer 中回填被回复消息的内容"""
    try:
        messages = l1_buffer.get_context(session_id)
        for msg in reversed(messages):
            msg_mid = msg.metadata.get("message_id") if msg.metadata else None
            if msg_mid and str(msg_mid) == str(reply_message_id):
                if msg.content and "reply_content" not in metadata:
                    metadata["reply_content"] = msg.content
                if "reply_user_name" not in metadata and msg.metadata:
                    msg_name = msg.metadata.get("user_name")
                    if msg_name:
                        metadata["reply_user_name"] = msg_name
                logger.debug(
                    f"从 L1 Buffer 回填回复信息：message_id={reply_message_id}"
                )
                return
    except Exception as e:
        logger.debug(f"回填回复信息失败: {e}")


async def _update_profile_names(
    component_manager: "ComponentManager",
    group_id: str,
    group_name: str,
    user_id: str,
    user_name: str,
    persona_id: str = "default",
) -> None:
    """更新用户昵称和群聊名称（内部函数）"""
    from ..config import get_config

    config = get_config()
    if not config.get("profile.enable"):
        return

    profile_storage = component_manager.get_available_component("profile")
    if not profile_storage:
        return

    group_key = f"group:{persona_id}:{group_id}"
    effective_group_id = (
        group_id if config.get("isolation_config.enable_group_isolation") else "default"
    )
    user_key = f"user:{persona_id}:{effective_group_id}:{user_id}"

    group_name_changed = group_name and _get_cached_name(group_key) != group_name
    user_name_changed = user_name and _get_cached_name(user_key) != user_name

    if not group_name_changed and not user_name_changed:
        return

    try:
        from ..profile import GroupProfileManager, UserProfileManager

        group_manager = GroupProfileManager(profile_storage)
        user_manager = UserProfileManager(profile_storage)

        if group_name_changed:
            await group_manager.update_group_name(group_id, group_name, persona_id)
            _set_cached_name(group_key, group_name)

        if user_name_changed:
            await user_manager.update_user_name(
                user_id, effective_group_id, user_name, persona_id
            )
            _set_cached_name(user_key, user_name)

    except Exception as e:
        logger.error(f"更新画像名称失败: {e}", exc_info=True)


async def _add_to_l1_buffer(
    event: "AstrMessageEvent", component_manager: "ComponentManager"
) -> None:
    """添加用户消息（含可选图片简述与嵌套合并转发）到 L1 Buffer"""
    from ..platform import get_adapter
    from ..utils import sanitize_input

    buffer = component_manager.get_available_component("l1_buffer")
    if not buffer:
        logger.debug("L1 Buffer 组件不可用，跳过消息添加")
        return

    l1_buffer = cast("L1Buffer", buffer)

    raw_text = sanitize_input(event.message_str or "", source="user_message")
    image_caption, forward_text, forward_meta = await extract_event_rich_media(
        event, component_manager
    )
    main_content = " ".join(p for p in (raw_text, image_caption) if p).strip()

    if not main_content and not forward_text:
        logger.debug("消息无文本、图片简述或合并转发内容，跳过添加")
        return

    adapter = get_adapter(event)
    group_id = adapter.get_group_id(event)
    session_id = adapter.get_session_id(event)
    user_id = adapter.get_user_id(event)
    user_name = adapter.get_user_name(event)
    group_name = adapter.get_group_name(event)

    from ..core.persona import resolve_persona

    persona_id = await resolve_persona(component_manager, event)

    metadata: dict[str, Any] = {}
    if user_name:
        metadata["user_name"] = user_name

    raw_msg = adapter.get_raw_message(event)
    message_id = str(raw_msg.get("message_id", "")) if raw_msg else ""
    if message_id:
        metadata["message_id"] = message_id

    reply_info = adapter.get_reply_info(event)
    if reply_info.has_reply:
        metadata["reply_message_id"] = reply_info.message_id
        if reply_info.user_id:
            metadata["reply_user_id"] = reply_info.user_id
        if reply_info.user_name:
            metadata["reply_user_name"] = reply_info.user_name
        if reply_info.content:
            metadata["reply_content"] = reply_info.content
        elif reply_info.message_id:
            _backfill_reply_from_buffer(
                l1_buffer, session_id, reply_info.message_id, metadata
            )
            if "reply_content" not in metadata:
                api_reply = await adapter.get_msg_by_id(event, reply_info.message_id)
                if api_reply.has_reply:
                    if api_reply.content:
                        metadata["reply_content"] = api_reply.content
                    if not metadata.get("reply_user_name") and api_reply.user_name:
                        metadata["reply_user_name"] = api_reply.user_name
                    if not metadata.get("reply_user_id") and api_reply.user_id:
                        metadata["reply_user_id"] = api_reply.user_id

    if main_content:
        await l1_buffer.add_message(
            group_id=session_id,
            role="user",
            content=main_content,
            source=user_id,
            metadata=metadata,
            persona_id=persona_id,
        )
        _mark_current_event_recorded(event)

    if forward_text:
        fwd_meta = dict(forward_meta or {})
        if user_name:
            fwd_meta["user_name"] = user_name
        await l1_buffer.add_message(
            group_id=session_id,
            role="user",
            content=forward_text,
            source=user_id,
            metadata=fwd_meta,
            persona_id=persona_id,
        )
        _mark_current_event_recorded(event)

    await _update_profile_names(
        component_manager, group_id, group_name, user_id, user_name, persona_id
    )


async def update_l1_buffer(
    event: "AstrMessageEvent",
    component_manager: "ComponentManager",
    role: str,
    content: str,
) -> None:
    """更新 L1 Buffer（添加用户消息或助手响应）"""
    from ..platform import get_adapter

    buffer = component_manager.get_available_component("l1_buffer")
    if not buffer:
        logger.debug("L1 Buffer 组件不可用，跳过消息更新")
        return

    l1_buffer = cast("L1Buffer", buffer)

    adapter = get_adapter(event)
    session_id = adapter.get_session_id(event)
    user_id = adapter.get_user_id(event)

    from ..core.persona import resolve_persona

    persona_id = await resolve_persona(component_manager, event)

    await l1_buffer.add_message(
        group_id=session_id,
        role=role,
        content=content,
        source=user_id if role == "user" else "assistant",
        persona_id=persona_id,
    )

    logger.debug(f"已添加 {role} 消息到会话 {session_id} 的 L1 Buffer")
