"""每个群聊的回复状态机：等待 / 回复中。

同一个群聊里 bot 只有两个状态：

- ``WAITING``：空闲。新消息到达后可以直接进入决策并回复。
- ``REPLYING``：正在决策或回复。期间到达的消息只进入滑动窗口，
  不触发任何其它动作。

一轮回复结束时状态回到 ``WAITING``，排队等待的消息重新检查窗口水位：
已被上一轮决策上下文覆盖的消息直接放弃，更新的消息开启下一轮，
因此一次窗口更新可能触发连续多条回复。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from astrbot.api import logger

WAITING = "waiting"
REPLYING = "replying"


@dataclass
class GroupReplyState:
    """单群运行时状态。"""

    state: str = WAITING
    since: float = 0.0
    watermark: float = 0.0


class GroupReplyGate:
    """按群串行化回复，同一群同一时刻最多只有一轮回复。

    - ``acquire``：等待态立即进入回复中；回复中则排队，直到上一轮
      ``release`` 或超时（超时返回 False，调用方把本次消息只当上下文）
    - ``try_acquire``：非阻塞版本，供主动发起等无法排队的路径使用
    - ``watermark``：上一轮决策已经读取到的窗口消息时间戳上界；时间戳
      不超过它的消息不需要再开一轮
    """

    def __init__(self, timeout_seconds: float = 120.0) -> None:
        self._timeout = max(1.0, float(timeout_seconds))
        self._states: dict[str, GroupReplyState] = {}
        self._cond = asyncio.Condition()

    @property
    def timeout(self) -> float:
        return self._timeout

    def _ensure(self, group_id: str) -> GroupReplyState:
        state = self._states.get(group_id)
        if state is None:
            state = GroupReplyState()
            self._states[group_id] = state
        return state

    def state(self, group_id: str) -> str:
        state = self._states.get(group_id)
        return state.state if state else WAITING

    def is_replying(self, group_id: str) -> bool:
        return self.state(group_id) == REPLYING

    def replying_groups(self) -> list[str]:
        return [
            group_id
            for group_id, state in self._states.items()
            if state.state == REPLYING
        ]

    def watermark(self, group_id: str) -> float:
        state = self._states.get(group_id)
        return state.watermark if state else 0.0

    def update_watermark(self, group_id: str, timestamp: float) -> None:
        """记录本轮决策已经读取到的窗口消息时间戳上界。"""
        state = self._ensure(group_id)
        if timestamp > state.watermark:
            state.watermark = timestamp

    def covered(self, group_id: str, timestamp: float) -> bool:
        """该消息是否已被上一轮回复的决策上下文覆盖。"""
        return timestamp > 0 and timestamp <= self.watermark(group_id)

    def try_acquire(self, group_id: str, now: float | None = None) -> bool:
        """非阻塞进入回复中；群正在回复时返回 False。"""
        state = self._ensure(group_id)
        if state.state == REPLYING:
            return False
        state.state = REPLYING
        state.since = time.time() if now is None else float(now)
        return True

    async def acquire(self, group_id: str, timeout: float | None = None) -> bool:
        """排队进入回复中；超时未能取得轮次时返回 False。"""
        limit = self._timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + limit
        async with self._cond:
            state = self._ensure(group_id)
            while state.state == REPLYING:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except TimeoutError:
                    return False
                state = self._ensure(group_id)
            state.state = REPLYING
            state.since = time.time()
            return True

    async def release(self, group_id: str) -> bool:
        """回复结束：回到等待态，并唤醒排队中的消息。"""
        async with self._cond:
            state = self._states.get(group_id)
            if state is None or state.state != REPLYING:
                return False
            state.state = WAITING
            state.since = 0.0
            self._cond.notify_all()
            return True

    async def cleanup(self, now: float | None = None) -> list[str]:
        """回收卡在回复中的群（回复钩子丢失时的兜底）。"""
        current = time.time() if now is None else float(now)
        stale: list[str] = []
        async with self._cond:
            for group_id, state in self._states.items():
                if state.state != REPLYING or not state.since:
                    continue
                if current - state.since <= self._timeout:
                    continue
                state.state = WAITING
                state.since = 0.0
                stale.append(group_id)
            if stale:
                self._cond.notify_all()
        for group_id in stale:
            logger.info(
                "Iris Reply: group %s stuck in replying for %.0fs, forced back to waiting",
                group_id,
                self._timeout,
            )
        return stale

    async def clear(self) -> None:
        async with self._cond:
            self._states.clear()
            self._cond.notify_all()
