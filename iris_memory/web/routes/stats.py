"""
统计相关 API 路由

提供记忆统计、知识图谱统计、组件状态与并发治理快照。
"""

from typing import Dict, Any
from quart import jsonify
from ...core import get_component_manager, get_logger, get_uptime
from ...llm.manager import LLMManager
from ...l1_buffer.buffer import L1Buffer
from ...l2_memory.adapter import L2MemoryAdapter
from ...l3_kg.adapter import L3KGAdapter

logger = get_logger("web.stats")

PLUGIN_NAME = "astrbot_plugin_iris_memory"

_OPTIONAL_COMPONENT_CONFIG = {
    "l1_buffer": "l1_buffer.enable",
    "l2_memory": "l2_memory.enable",
    "l3_kg": "l3_kg.enable",
    "profile": "profile.enable",
}


def _get_uptime() -> int:
    try:
        return get_uptime()
    except Exception as e:
        logger.warning(f"获取运行时间失败：{e}")
        return 0


def _augment_disabled_components(component_states: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from ...config import get_config

        config = get_config()
    except Exception:
        return component_states
    for name, key in _OPTIONAL_COMPONENT_CONFIG.items():
        if name not in component_states and not config.get(key):
            component_states[name] = {
                "status": "unavailable",
                "error": "组件未启用",
                "error_type": "disabled",
            }
    return component_states


def _governor_alerts(metrics: Dict[str, Any]) -> list[dict]:
    from ...config import get_config

    alerts: list[dict] = []
    rpm_limit = int(get_config().get("llm_provider_rpm", 30) or 0)
    if rpm_limit > 0:
        for provider_id, modules in metrics.get("calls_per_minute", {}).items():
            total = sum(int(value) for value in modules.values())
            if total >= rpm_limit * 0.8:
                alerts.append(
                    {
                        "type": "provider_rpm_high",
                        "provider_id": provider_id,
                        "value": total,
                        "threshold": rpm_limit * 0.8,
                    }
                )

    queue_limit = max(1, int(metrics.get("queue_limit", 500)))
    if int(metrics.get("queue_depth", 0)) >= queue_limit * 0.7:
        alerts.append(
            {
                "type": "queue_depth_high",
                "value": int(metrics.get("queue_depth", 0)),
                "threshold": queue_limit * 0.7,
            }
        )
    if int(metrics.get("queue_wait_p95_ms", 0)) > 1000:
        alerts.append(
            {
                "type": "queue_wait_high",
                "value": int(metrics.get("queue_wait_p95_ms", 0)),
                "threshold": 1000,
            }
        )
    return alerts


async def get_memory_stats():
    try:
        manager = get_component_manager()
        stats: Dict[str, Any] = {"l1": {}, "l2": {}, "l3": {}}

        l1_buffer = manager.get_component("l1_buffer", L1Buffer)
        if l1_buffer and l1_buffer.is_available:
            try:
                stats["l1"] = l1_buffer.get_stats()
            except Exception as e:
                logger.warning(f"获取L1统计失败：{e}")

        l2_memory = manager.get_component("l2_memory", L2MemoryAdapter)
        if l2_memory and l2_memory.is_available:
            try:
                stats["l2"] = await l2_memory.get_stats()
            except Exception as e:
                logger.warning(f"获取L2统计失败：{e}")

        l3_kg = manager.get_component("l3_kg", L3KGAdapter)
        if l3_kg and l3_kg.is_available:
            try:
                stats["l3"] = await l3_kg.get_stats()
            except Exception as e:
                logger.warning(f"获取L3统计失败：{e}")

        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.error(f"获取记忆统计失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def get_kg_stats():
    try:
        manager = get_component_manager()
        l3_adapter = manager.get_component("l3_kg", L3KGAdapter)
        if not l3_adapter or not l3_adapter.is_available:
            return jsonify({"success": False, "error": "L3 知识图谱不可用"}), 503
        stats = await l3_adapter.get_stats()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.error(f"获取图谱统计失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def get_system_stats():
    try:
        manager = get_component_manager()
        component_states = _augment_disabled_components(manager.get_all_states())
        stats = {
            "components": component_states,
            "global_status": manager.status.global_status.value,
            "uptime": _get_uptime(),
        }
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.error(f"获取系统统计失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def get_llm_governance_stats():
    try:
        manager = get_component_manager()
        llm_manager = manager.get_component("llm_manager", LLMManager)
        if not llm_manager or not llm_manager.is_available:
            return jsonify({"success": False, "error": "LLM 管理器不可用"}), 503
        metrics = llm_manager.get_governor_metrics()
        return jsonify(
            {
                "success": True,
                "metrics": metrics,
                "alerts": _governor_alerts(metrics),
            }
        )
    except Exception as e:
        logger.error(f"获取 LLM 治理统计失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def get_isolation_status():
    try:
        from ...config import get_config

        config = get_config()
        status = {
            "enable_group_memory_isolation": bool(
                config.get("isolation_config.enable_group_memory_isolation")
            ),
            "enable_group_isolation": bool(
                config.get("isolation_config.enable_group_isolation")
            ),
            "enable_persona_isolation": bool(
                config.get("isolation_config.enable_persona_isolation")
            ),
        }
        return jsonify({"success": True, "status": status})
    except Exception as e:
        logger.error(f"获取隔离状态失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


async def get_all_stats():
    try:
        manager = get_component_manager()
        memory_stats: Dict[str, Any] = {"l1": {}, "l2": {}, "l3": {}}

        l1_buffer = manager.get_component("l1_buffer", L1Buffer)
        if l1_buffer and l1_buffer.is_available:
            try:
                memory_stats["l1"] = l1_buffer.get_stats()
            except Exception as e:
                logger.warning(f"获取L1统计失败：{e}")

        l2_memory = manager.get_component("l2_memory", L2MemoryAdapter)
        if l2_memory and l2_memory.is_available:
            try:
                memory_stats["l2"] = await l2_memory.get_stats()
            except Exception as e:
                logger.warning(f"获取L2统计失败：{e}")

        l3_kg = manager.get_component("l3_kg", L3KGAdapter)
        kg_stats: Dict[str, Any] = {
            "node_count": 0,
            "edge_count": 0,
            "node_types": {},
            "relation_types": {},
        }
        if l3_kg and l3_kg.is_available:
            try:
                kg_stats = await l3_kg.get_stats()
                memory_stats["l3"] = kg_stats
            except Exception as e:
                logger.warning(f"获取L3统计失败：{e}")

        component_states = _augment_disabled_components(manager.get_all_states())
        system_stats = {
            "components": component_states,
            "global_status": manager.status.global_status.value,
            "uptime": _get_uptime(),
        }

        llm_governance: Dict[str, Any] = {"metrics": {}, "alerts": []}
        llm_manager = manager.get_component("llm_manager", LLMManager)
        if llm_manager and llm_manager.is_available:
            try:
                governance_metrics = llm_manager.get_governor_metrics()
                llm_governance = {
                    "metrics": governance_metrics,
                    "alerts": _governor_alerts(governance_metrics),
                }
            except Exception as e:
                logger.warning(f"获取 LLM 治理统计失败：{e}")

        return jsonify(
            {
                "success": True,
                "memory": memory_stats,
                "kg": kg_stats,
                "system": system_stats,
                "llm_governance": llm_governance,
            }
        )
    except Exception as e:
        logger.error(f"获取所有统计失败：{e}", exc_info=True)
        return jsonify({"success": False, "error": "内部错误，详见服务日志"}), 500


def register_stats_routes(context) -> None:
    prefix = f"/{PLUGIN_NAME}/stats"
    routes = [
        (f"{prefix}/memory", get_memory_stats, ["GET"], "获取记忆统计"),
        (f"{prefix}/kg", get_kg_stats, ["GET"], "获取图谱统计"),
        (f"{prefix}/system", get_system_stats, ["GET"], "获取系统统计"),
        (f"{prefix}/llm-governance", get_llm_governance_stats, ["GET"], "获取 LLM 治理统计"),
        (f"{prefix}/isolation", get_isolation_status, ["GET"], "获取隔离状态"),
        (f"{prefix}/all", get_all_stats, ["GET"], "获取所有统计"),
    ]
    for route, handler, methods, desc in routes:
        context.register_web_api(route, handler, methods, desc)
