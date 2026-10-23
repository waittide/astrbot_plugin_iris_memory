from unittest.mock import AsyncMock, Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.concurrency import (
    DecisionPermitLimiter,
)


def test_permit_limiter_allows_same_group_concurrency():
    limiter = DecisionPermitLimiter(max_concurrency=2, timeout_seconds=120)
    first = limiter.claim("g1", "event-1")
    second = limiter.claim("g1", "event-2")
    assert first is not None and second is not None
    # 同一事件不重复发放
    assert limiter.claim("g1", "event-1") is None
    # 并发上限生效
    assert limiter.claim("g1", "event-3") is None
    assert limiter.in_flight() == 2
    assert limiter.group_in_flight("g1") == 2
    assert limiter.release(first.permit_id)
    assert limiter.claim("g1", "event-3") is not None


def test_permit_limiter_checks_owner_and_reclaims_stale():
    limiter = DecisionPermitLimiter(max_concurrency=1, timeout_seconds=10)
    permit = limiter.claim("g1", "event-1", now=100.0)
    assert permit is not None
    assert limiter.owns(permit.permit_id, "g1", "event-1")
    assert not limiter.owns(permit.permit_id, "g2", "event-1")
    expired = limiter.cleanup(now=111.0)
    assert [p.event_id for p in expired] == ["event-1"]
    assert limiter.in_flight() == 0
    # 超时后事件 ID 的 TTL 一过即可重新领取
    assert limiter.claim("g1", "event-1", now=122.0) is not None


def test_permit_limiter_releases_group_residue():
    limiter = DecisionPermitLimiter(max_concurrency=2, timeout_seconds=120)
    assert limiter.claim("g1", "event-1") is not None
    assert limiter.release_group("g1") == 1
    assert limiter.in_flight() == 0
    assert limiter.release_group("g1") == 0


@pytest.mark.asyncio
async def test_stale_event_cannot_consume_active_permit():
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    plugin = object.__new__(IrisMemoryPlugin)
    plugin._decision_permits = DecisionPermitLimiter(4, 120)
    current = plugin._decision_permits.claim("g1", "event-new")
    assert current is not None
    plugin._decision_core = Mock()
    plugin._decision_core.decide = AsyncMock()

    event = Mock()
    event.get_group_id.return_value = "g1"
    event.get_extra.return_value = {
        "motive": "chime_in",
        "provider_id": "provider",
        "event_id": "event-old",
        "ticket_id": "stale-ticket",
    }

    assert await plugin._handle_reply_decision(event) is True
    event.stop_event.assert_called_once()
    plugin._decision_core.decide.assert_not_awaited()
    assert plugin._decision_permits.owns(current.permit_id, "g1", current.event_id)
