"""LLM 调用优先级与默认策略。

所有插件内部调用都由模块名映射到统一优先级。调用方仍可显式覆盖，
但不应在业务模块中自行实现另一套并发或限流规则。
"""

from __future__ import annotations

from enum import IntEnum

from ..llm_modules import (
    L1_SUMMARIZER,
    L2_QUERY_REWRITE,
    PROFILE_ANALYSIS,
    PROACTIVE_DECISION_CHIME_IN,
    PROACTIVE_DECISION_FOLLOW_UP,
    PROACTIVE_DECISION_INITIATE,
    PROACTIVE_DECISION_WATCH,
    PROACTIVE_REPLY_CHIME_IN,
    PROACTIVE_REPLY_FOLLOW_UP,
    PROACTIVE_REPLY_INITIATE,
    PROACTIVE_REPLY_PASSIVE,
)


class CallPriority(IntEnum):
    """数值越小优先级越高；Governor 会对等待任务进行 aging。"""

    INTERACTIVE = 0
    NEARLINE = 1
    BACKGROUND = 2
    MAINTENANCE = 3


_INTERACTIVE_MODULES = frozenset(
    {
        L2_QUERY_REWRITE,
        PROACTIVE_DECISION_CHIME_IN,
        PROACTIVE_DECISION_FOLLOW_UP,
        PROACTIVE_DECISION_INITIATE,
        PROACTIVE_REPLY_CHIME_IN,
        PROACTIVE_REPLY_FOLLOW_UP,
        PROACTIVE_REPLY_INITIATE,
        PROACTIVE_REPLY_PASSIVE,
        "framework_reply",
    }
)

_NEARLINE_MODULES = frozenset({L1_SUMMARIZER})

_BACKGROUND_MODULES = frozenset(
    {
        PROFILE_ANALYSIS,
        PROACTIVE_DECISION_WATCH,
    }
)

_MAINTENANCE_MODULES = frozenset(
    {
        "scheduled_tasks",
    }
)


def priority_for_module(module: str) -> CallPriority:
    """返回模块的默认优先级；未知内部任务按 BACKGROUND 处理。"""

    if module in _INTERACTIVE_MODULES:
        return CallPriority.INTERACTIVE
    if module in _NEARLINE_MODULES:
        return CallPriority.NEARLINE
    if module in _MAINTENANCE_MODULES:
        return CallPriority.MAINTENANCE
    if module in _BACKGROUND_MODULES:
        return CallPriority.BACKGROUND
    return CallPriority.BACKGROUND


def coerce_priority(value: CallPriority | int | str | None, module: str) -> CallPriority:
    """将配置/调用方传值收窄为 ``CallPriority``。"""

    if value is None:
        return priority_for_module(module)
    if isinstance(value, CallPriority):
        return value
    if isinstance(value, str):
        try:
            return CallPriority[value.strip().upper()]
        except KeyError:
            return priority_for_module(module)
    try:
        return CallPriority(int(value))
    except (TypeError, ValueError):
        return priority_for_module(module)
