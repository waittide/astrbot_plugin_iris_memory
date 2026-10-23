from __future__ import annotations

import time

from astrbot.api import logger

from .config import ConfigManager
from .perception import WindowMessage
from .state import StateManager


class SignalGate:
    """本地信号门控：零 LLM 成本地评估两种唤醒源是否值得进入决策层。

    - 消息唤醒：每条群消息都进入决策层，锚点命中时标为 follow_up，否则 chime_in
    - 定时器唤醒：冷场静默 / 话题刚结束 → initiate
    """

    def __init__(self, config: ConfigManager, state: StateManager) -> None:
        self._config = config
        self._state = state

    def evaluate_message(self, group_id: str, sender_id: str, text: str) -> str | None:
        """消息唤醒门控，返回候选动机 "follow_up" | "chime_in" | None。"""
        if not self._config.enabled:
            return None
        if self._state.is_muted():
            return None

        if sender_id and self._state.match_anchor_user(group_id, sender_id):
            logger.debug("Iris Reply: anchor user trigger for group %s", group_id)
            return "follow_up"

        matched = self._state.match_anchor_keyword(group_id, text)
        if matched:
            logger.debug(
                "Iris Reply: anchor keyword trigger for group %s, keywords=%s",
                group_id,
                matched,
            )
            return "follow_up"

        return "chime_in"

    def evaluate_timer(
        self, group_id: str, messages: list[WindowMessage]
    ) -> str | None:
        """定时器唤醒门控，返回 "initiate" | None。

        达到最小静默底线后按风险率进行一次独立概率试验；风险率综合静默
        时长、回复意愿和话题状态。不设每日次数、发起间隔或无人接话上限。
        """
        if not self._config.enabled or not self._config.proactive_enabled:
            return None
        data = self._state.get_state(group_id)
        if not messages:
            return None
        # 等待群友接话期间不重复发起（互斥）
        if data.initiate_pending_since > 0:
            return None

        now = time.time()
        last_activity = max(messages[-1].timestamp, data.last_human_message_at)
        quiet = max(0.0, now - last_activity)
        fired = self._state.evaluate_initiate_hazard(
            group_id,
            now=now,
            quiet_seconds=quiet,
            freeze=self._state.is_muted(),
        )
        return "initiate" if fired else None
