"""
消息钩子处理模块

负责处理用户发送的消息钩子，包括：
- 添加用户消息到 L1 Buffer
- 用户画像更新
"""

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, cast

from ..core import get_logger
from ..core.event_extras import L1_CURRENT_EVENT_RECORD_COUNT

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent
    from ..core.components import ComponentManager
    from ..l1_buffer import L1Buffer

logger = get_logger("message_hook")

_name_cache: OrderedDict = OrderedDict()
_NAME_CACHE_MAX_SIZE = 1000


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
    """添加用户消息到 L1 Buffer（内部函数）"""
    from ..platform import get_adapter

    content = event.message_str
    if not content:
        logger.debug("消息内容为空，跳过添加")
        return

    from ..utils import sanitize_input

    content = sanitize_input(content, source="user_message")

    buffer = component_manager.get_available_component("l1_buffer")
    if not buffer:
        logger.debug("L1 Buffer 组件不可用，跳过消息添加")
        return

    l1_buffer = cast("L1Buffer", buffer)

    adapter = get_adapter(event)
    group_id = adapter.get_group_id(event)
    session_id = adapter.get_session_id(event)
    user_id = adapter.get_user_id(event)
    user_name = adapter.get_user_name(event)
    group_name = adapter.get_group_name(event)

    from ..core.persona import resolve_persona

    persona_id = await resolve_persona(component_manager, event)

    metadata = {}
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
                    logger.debug(
                        f"从平台 API 回填回复信息：message_id={reply_info.message_id}"
                    )

    await l1_buffer.add_message(
        group_id=session_id,
        role="user",
        content=content,
        source=user_id,
        metadata=metadata,
        persona_id=persona_id,
    )
    _mark_current_event_recorded(event)

    logger.debug(f"已添加用户消息到会话 {session_id} 的 L1 Buffer")

    # 展开合并转发消息：拼接为一条消息入队，超长按 token 限制截断
    try:
        forward_messages = await adapter.get_forward_messages(event)
    except Exception as e:
        logger.debug(f"获取合并转发消息失败: {e}")
        forward_messages = []

    if forward_messages:
        from ..config import get_config as _get_cfg
        from ..utils import count_tokens

        max_single_tokens = cast(
            int, _get_cfg().get("l1_max_single_message_tokens", 500)
        )

        reserve_tokens = 30
        budget = max(max_single_tokens - reserve_tokens, 64)

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

        if included_lines:
            header = "【合并转发内容】"
            parts = [header] + included_lines
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
            await l1_buffer.add_message(
                group_id=session_id,
                role="user",
                content=combined_content,
                source=user_id,
                metadata=fwd_metadata,
                persona_id=persona_id,
            )
            _mark_current_event_recorded(event)
            logger.debug(
                f"合并转发消息已入队：会话 {session_id}，"
                f"包含 {included_count}/{len(forward_messages)} 条子消息，"
                f"truncated={truncated}"
            )

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
