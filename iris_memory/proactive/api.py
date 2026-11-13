from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .perception import SlidingWindow
from .prompts import resolve_level
from .state import StateManager

if TYPE_CHECKING:
    from astrbot.api.star import Context

KvSaveFn = Callable[[str, Any], Awaitable[None]]


def group_state_summary(state: StateManager, group_id: str) -> dict:
    """构建群状态摘要字典。"""
    data = state.get_state(group_id)
    return {
        "state": "idle",
        "willingness": data.willingness,
        "initiate_daily_count": data.initiate_daily_count,
        "initiate_rolling_24h_count": data.initiate_daily_count,
        "initiate_hazard_probability": data.initiate_hazard_probability,
        "initiate_next_check_at": data.initiate_next_check_at,
    }


def register_web_apis(
    *,
    context: Context,
    plugin_name: str,
    state: StateManager,
    window: SlidingWindow,
    kv_save: KvSaveFn,
) -> None:
    """注册主动回复白名单与群管理 Web API。"""
    from quart import jsonify
    from quart import request as qrequest

    async def _json_body() -> dict:
        return await qrequest.get_json(force=True, silent=True) or {}

    def _body_group_id(body: dict) -> str:
        return str(body.get("group_id") or "")

    async def _whitelist_list():
        groups = []
        for gid in state.get_whitelist():
            data = state.get_state(gid)
            anchor = data.anchor
            entry = {"group_id": gid, **group_state_summary(state, gid)}
            entry["anchor_kind"] = anchor.kind
            entry["anchor_users"] = sorted(anchor.participants)
            entry["anchor_keywords"] = sorted(anchor.keywords)
            entry["anchor_reason"] = anchor.reason
            entry["anchor_bot_message"] = anchor.bot_message
            entry["initiate_pending"] = data.initiate_pending_since > 0
            groups.append(entry)
        return jsonify(groups)

    async def _whitelist_enable():
        body = await _json_body()
        group_id = _body_group_id(body)
        if not group_id:
            return jsonify({"error": "group_id required"}), 400
        state.add_to_whitelist(group_id)
        await state.save_dirty(kv_save)
        return jsonify({"ok": True, "group_id": group_id})

    async def _whitelist_disable():
        body = await _json_body()
        group_id = _body_group_id(body)
        if not group_id:
            return jsonify({"error": "group_id required"}), 400
        state.remove_from_whitelist(group_id)
        window.remove_group(group_id)
        state.remove_group_lock(group_id)
        await state.save_dirty(kv_save)
        return jsonify({"ok": True, "group_id": group_id})

    async def _group_set_willingness():
        body = await _json_body()
        group_id = _body_group_id(body)
        level = str(body.get("willingness") or "")
        if not group_id or not level:
            return jsonify({"error": "group_id and willingness required"}), 400
        resolved = resolve_level(level)
        if not resolved:
            return jsonify({"error": "invalid willingness level"}), 400
        state.set_willingness(group_id, resolved)
        await state.save_dirty(kv_save)
        return jsonify({"ok": True, "group_id": group_id, "willingness": resolved})

    async def _group_reset():
        body = await _json_body()
        group_id = _body_group_id(body)
        if not group_id:
            return jsonify({"error": "group_id required"}), 400
        state.reset_group(group_id)
        window.remove_group(group_id)
        await state.save_dirty(kv_save)
        return jsonify({"ok": True, "group_id": group_id})

    wl_prefix = f"/{plugin_name}/reply/whitelist"
    context.register_web_api(f"{wl_prefix}/list", _whitelist_list, ["GET"], "Whitelist list")
    context.register_web_api(f"{wl_prefix}/enable", _whitelist_enable, ["POST"], "Whitelist enable")
    context.register_web_api(f"{wl_prefix}/disable", _whitelist_disable, ["POST"], "Whitelist disable")

    grp_prefix = f"/{plugin_name}/reply/group"
    context.register_web_api(f"{grp_prefix}/set_willingness", _group_set_willingness, ["POST"], "Group set willingness")
    context.register_web_api(f"{grp_prefix}/reset", _group_reset, ["POST"], "Group reset")
