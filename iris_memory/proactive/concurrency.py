"""主动回复 decision 并发编排。

旧的 ``_triggering`` / ``DecisionTicketRegistry`` 都是"每群只允许一个在途
决策"，同一群的新消息必须等上一条决策结束。这里改为按事件发放在途许可，
只限制**全局在途数量**（``reply_max_concurrency``），同群不再互斥。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class DecisionPermit:
    """一次决策事件的在途许可。"""

    permit_id: str
    event_id: str
    group_id: str
    created_at: float


class DecisionPermitLimiter:
    """按事件发放在途许可，限制全局并发数。

    - 同一事件（``event_id``）在 TTL 内只放行一次，避免同一消息被重复决策
    - 并发达到上限时拒绝新许可，由调用方跳过本次候选（下一条消息再来）
    - 持有者必须在决策结束时 ``release``，或由 ``cleanup`` 兜底回收
    """

    def __init__(
        self, max_concurrency: int = 1, timeout_seconds: float = 120.0
    ) -> None:
        self._max = max(1, int(max_concurrency))
        self._timeout = max(1.0, float(timeout_seconds))
        self._active: dict[str, DecisionPermit] = {}
        self._seen_events: dict[str, float] = {}

    @property
    def max_concurrency(self) -> int:
        return self._max

    def set_max_concurrency(self, value: int) -> None:
        """运行期调整并发上限（热更新的配置）。"""
        self._max = max(1, int(value))

    def in_flight(self) -> int:
        return len(self._active)

    def cleanup(self, now: float | None = None) -> list[DecisionPermit]:
        current = time.time() if now is None else float(now)
        expired = [
            permit
            for permit in self._active.values()
            if current - permit.created_at > self._timeout
        ]
        for permit in expired:
            self._active.pop(permit.permit_id, None)
        cutoff = current - self._timeout
        self._seen_events = {
            event_id: seen_at
            for event_id, seen_at in self._seen_events.items()
            if seen_at >= cutoff
        }
        return expired

    def claim(
        self,
        group_id: str,
        event_id: str,
        now: float | None = None,
        *,
        max_concurrency: int | None = None,
    ) -> DecisionPermit | None:
        """申请一次决策许可；同一事件重复申请或并发已满时返回 None。"""
        current = time.time() if now is None else float(now)
        self.cleanup(current)
        if not group_id or not event_id:
            return None
        if event_id in self._seen_events:
            return None

        limit = self._max if max_concurrency is None else max(1, int(max_concurrency))
        if len(self._active) >= limit:
            return None

        permit = DecisionPermit(
            permit_id=uuid.uuid4().hex,
            event_id=event_id,
            group_id=group_id,
            created_at=current,
        )
        self._active[permit.permit_id] = permit
        self._seen_events[event_id] = current
        return permit

    def find(self, permit_id: str) -> DecisionPermit | None:
        return self._active.get(permit_id)

    def owns(self, permit_id: str, group_id: str, event_id: str = "") -> bool:
        permit = self._active.get(permit_id)
        return bool(
            permit
            and permit.group_id == group_id
            and (not event_id or permit.event_id == event_id)
        )

    def group_in_flight(self, group_id: str) -> int:
        """返回某群当前在途的决策数，供状态展示使用。"""
        return sum(1 for p in self._active.values() if p.group_id == group_id)

    def release(self, permit_id: str) -> bool:
        return self._active.pop(permit_id, None) is not None

    def release_group(self, group_id: str) -> int:
        """释放该群最早的一个许可，供缺少 permit_id 的遗留事件兼容释放。"""
        candidates = [
            permit for permit in self._active.values() if permit.group_id == group_id
        ]
        if not candidates:
            return 0
        oldest = min(candidates, key=lambda permit: permit.created_at)
        self._active.pop(oldest.permit_id, None)
        return 1

    def clear(self) -> None:
        self._active.clear()
        self._seen_events.clear()
