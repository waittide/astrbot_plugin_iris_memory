from __future__ import annotations

import time
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import ConfigManager
from .parser import Decision, parse_decision
from .perception import ContextPackager, SlidingWindow
from .prompts import MOTIVE_INSTRUCTIONS, VALID_MOTIVES, WILLINGNESS_PROMPTS
from .state import StateManager, ThreadAnchor
from .time_hint import wrap_system_reminder
from ..llm_modules import proactive_decision_module

# group_id -> 当前时间提示（<system_reminder> 块），无则返回空串
TimeHintGet = Callable[[str], str]

_INPUT_SENSITIVE_1026 = re.compile(
    r"(?:input\s+new_sensitive\s*\(\s*1026\s*\)|"
    r"['\"]?message['\"]?\s*:\s*['\"]input\s+new_sensitive|"
    r"['\"]?code['\"]?\s*:\s*1026)",
    re.IGNORECASE,
)


@dataclass
class DecisionRequest:
    """一次统一决策请求。wake 为唤醒源，motive 为候选动机（LLM 可否决）。"""

    group_id: str
    wake: str  # "message" | "timer"
    motive: str  # "chime_in" | "follow_up" | "initiate" | "watch"
    quiet_minutes: int = 0  # 仅 initiate 使用


@dataclass
class DecisionOutcome:
    """决策调用结果，附带 prompt 与原始响应（供统计与日志）。"""

    decision: Decision | None
    system_prompt: str
    user_prompt: str
    raw_text: str = ""
    error: str = ""
    duration_ms: float = 0.0
    error_kind: str = ""
    retryable: bool = True
    dynamic_context_sources: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class SafetyCleanupResult:
    """一次 Provider 输入安全拒绝后的主动回复上下文清理结果。"""

    window_removed: int
    observation_cleared: bool
    anchor_cleared: bool
    dynamic_source_count: int


def classify_decision_error(exc: BaseException | str) -> tuple[str, bool]:
    """将已知 Provider 错误分类为（类型，是否可原样重试）。"""
    message = str(exc)
    if _INPUT_SENSITIVE_1026.search(message):
        return "input_content_safety_1026", False
    return "provider_error", True


def build_anchor_block(anchor: ThreadAnchor, motive: str) -> str:
    """构建 <thread> 锚点块，无锚点信息时返回空字符串。"""
    if not anchor.has_context:
        return ""
    parts = []
    if anchor.bot_message:
        parts.append(f'你之前在群里说："{anchor.bot_message}"')
    if anchor.participants:
        parts.append(f"你关注这些用户：{', '.join(sorted(anchor.participants))}")
    if anchor.keywords:
        parts.append(f"你关注这些关键词：{', '.join(sorted(anchor.keywords))}")
    if anchor.reason:
        parts.append(f"原因：{anchor.reason}")
    text = "\n\n<thread>" + "；".join(parts)
    if motive == "follow_up":
        text += "。现在相关对话有了新进展，请综合评估所有新消息后决定是否回应。"
    text += "</thread>"
    return text


class DecisionCore:
    """统一决策核心：三种发言动机 + 跟进评估共用同一 prompt 骨架与同一次 LLM 调用。"""

    def __init__(
        self,
        config: ConfigManager,
        state: StateManager,
        window: SlidingWindow,
        packager: ContextPackager,
        time_hint_get: TimeHintGet | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._window = window
        self._packager = packager
        self._time_hint_get = time_hint_get

    def build_prompt(self, req: DecisionRequest) -> tuple[str, str]:
        """组装 (user_prompt, system_prompt)。"""
        willingness = self._state.get_willingness(req.group_id)
        prompts = WILLINGNESS_PROMPTS[willingness]

        user_prompt = prompts["persona"]

        observation = self._state.get_observation(req.group_id)
        if observation:
            user_prompt += f"\n\n<recent_observation>之前的观察：{observation}</recent_observation>"

        user_prompt += build_anchor_block(
            self._state.get_anchor(req.group_id), req.motive
        )

        instruction = MOTIVE_INSTRUCTIONS[req.motive]
        if req.motive == "initiate":
            instruction = instruction.format(quiet_minutes=max(0, req.quiet_minutes))
            instruction += "\n" + prompts["initiate_style"]
            custom = self._config.proactive_instruction
            if custom:
                instruction += f"\n话题倾向：{custom}"
        user_prompt += f"\n\n<instruction>{instruction}</instruction>"

        messages = self._window.get_messages(req.group_id)
        user_prompt += "\n\n" + self._packager.package(
            req.group_id, messages, req.motive
        )

        system_prompt = prompts["decision_system"]
        time_hint = wrap_system_reminder(
            self._time_hint_get(req.group_id) if self._time_hint_get else ""
        )
        if time_hint:
            system_prompt = f"{time_hint}\n\n{system_prompt}"
        return user_prompt, system_prompt

    def collect_dynamic_context_sources(
        self, req: DecisionRequest
    ) -> list[dict[str, Any]]:
        """返回决策 prompt 中所有动态内容的来源，供安全拦截定位。"""
        sources: list[dict[str, Any]] = []

        observation = self._state.get_observation(req.group_id)
        if observation:
            sources.append({"source": "recent_observation", "content": observation})

        anchor = self._state.get_anchor(req.group_id)
        if anchor.bot_message:
            sources.append(
                {"source": "thread.bot_message", "content": anchor.bot_message}
            )
        if anchor.participants:
            sources.append(
                {
                    "source": "thread.participants",
                    "values": sorted(anchor.participants),
                }
            )
        if anchor.keywords:
            sources.append(
                {"source": "thread.keywords", "values": sorted(anchor.keywords)}
            )
        if anchor.reason:
            sources.append({"source": "thread.reason", "content": anchor.reason})

        if req.motive == "initiate" and self._config.proactive_instruction:
            sources.append(
                {
                    "source": "proactive_instruction",
                    "content": self._config.proactive_instruction,
                }
            )

        messages = self._window.get_messages(req.group_id)
        total = len(messages)
        for index, msg in enumerate(messages):
            sources.append(
                {
                    "source": "window_message",
                    "window_index": index,
                    "from_latest": total - index - 1,
                    "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "timestamp": msg.timestamp,
                    "chars": len(msg.content),
                    "content": msg.content,
                }
            )
        return sources

    def clear_rejected_dynamic_context(
        self,
        group_id: str,
        dynamic_context_sources: list[dict[str, Any]] | None,
    ) -> SafetyCleanupResult:
        """清除被 Provider 拒绝的动态上下文，并保留请求期间的新消息。

        调用方应持有该群的 StateManager 锁，并在返回后持久化 dirty state。
        """
        sources = dynamic_context_sources or []
        timestamps = [
            item.get("timestamp")
            for item in sources
            if item.get("source") == "window_message"
            and isinstance(item.get("timestamp"), (int, float))
            and not isinstance(item.get("timestamp"), bool)
        ]
        window_removed = 0
        if timestamps:
            window_removed = self._window.discard_through(
                group_id,
                max(timestamps),
            )

        cleared = self._state.clear_decision_context(group_id)
        return SafetyCleanupResult(
            window_removed=window_removed,
            observation_cleared=cleared["observation"],
            anchor_cleared=cleared["anchor"],
            dynamic_source_count=len(sources),
        )

    async def decide(
        self,
        req: DecisionRequest,
        llm_manager: Any,
        provider_id: str,
    ) -> DecisionOutcome:
        """执行一次决策调用。LLM 异常不抛出，以 error 字段返回。"""
        assert req.motive in VALID_MOTIVES, f"unknown motive: {req.motive}"
        user_prompt, system_prompt = self.build_prompt(req)
        dynamic_context_sources = self.collect_dynamic_context_sources(req)
        start = time.time()
        try:
            raw = await llm_manager.generate_direct(
                prompt=user_prompt,
                system_prompt=system_prompt,
                provider_id=provider_id,
                module=proactive_decision_module(req.motive),
            )
        except Exception as e:
            error_kind, retryable = classify_decision_error(e)
            return DecisionOutcome(
                decision=None,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                error=str(e),
                duration_ms=(time.time() - start) * 1000,
                error_kind=error_kind,
                retryable=retryable,
                dynamic_context_sources=dynamic_context_sources,
            )
        return DecisionOutcome(
            decision=parse_decision(raw, mode=req.motive),
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            raw_text=raw,
            duration_ms=(time.time() - start) * 1000,
            dynamic_context_sources=dynamic_context_sources,
        )
