"""主动回复并发许可测试。"""

import asyncio

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.concurrency import (
    DecisionPermitLimiter,
)

GID = "g1"


def test_same_group_allows_concurrent_permits():
    limiter = DecisionPermitLimiter(max_concurrency=3, timeout_seconds=120)
    permits = [limiter.claim(GID, f"event-{i}") for i in range(3)]
    assert all(p is not None for p in permits)
    assert limiter.in_flight() == 3
    assert limiter.group_in_flight(GID) == 3
    # 超过上限拒绝
    assert limiter.claim(GID, "event-overflow") is None


def test_limit_can_be_updated_at_runtime():
    limiter = DecisionPermitLimiter(max_concurrency=1, timeout_seconds=120)
    assert limiter.claim(GID, "event-1") is not None
    assert limiter.claim("g2", "event-2") is None
    limiter.set_max_concurrency(2)
    assert limiter.claim("g2", "event-2") is not None
    assert limiter.max_concurrency == 2


def test_duplicate_event_rejected_until_ttl():
    limiter = DecisionPermitLimiter(max_concurrency=4, timeout_seconds=10)
    assert limiter.claim(GID, "event-1", now=100.0) is not None
    assert limiter.claim(GID, "event-1", now=105.0) is None
    limiter.cleanup(now=111.0)
    assert limiter.claim(GID, "event-1", now=112.0) is not None


def test_release_frees_capacity():
    limiter = DecisionPermitLimiter(max_concurrency=1, timeout_seconds=120)
    permit = limiter.claim(GID, "event-1")
    assert permit is not None
    assert limiter.claim(GID, "event-2") is None
    assert limiter.release(permit.permit_id) is True
    assert limiter.claim(GID, "event-2") is not None


def test_release_group_clears_oldest_only():
    limiter = DecisionPermitLimiter(max_concurrency=4, timeout_seconds=120)
    first = limiter.claim(GID, "event-1", now=100.0)
    second = limiter.claim(GID, "event-2", now=101.0)
    assert first is not None and second is not None
    assert limiter.release_group(GID) == 1
    assert limiter.owns(second.permit_id, GID, "event-2")
    assert not limiter.owns(first.permit_id, GID, "event-1")


@pytest.mark.asyncio
async def test_concurrent_decisions_do_not_serialize():
    """同群两条消息可同时在途，不再被上一个决策阻塞。"""

    limiter = DecisionPermitLimiter(max_concurrency=2, timeout_seconds=120)
    started = asyncio.Event()
    release = asyncio.Event()
    finished = 0

    async def decide(event_id: str) -> None:
        nonlocal finished
        permit = limiter.claim(GID, event_id)
        assert permit is not None
        started.set()
        await release.wait()
        limiter.release(permit.permit_id)
        finished += 1

    tasks = [asyncio.create_task(decide(f"event-{i}")) for i in range(2)]
    await started.wait()
    await asyncio.sleep(0)
    assert limiter.in_flight() == 2
    release.set()
    await asyncio.gather(*tasks)
    assert finished == 2
    assert limiter.in_flight() == 0


def test_expired_permits_are_reclaimable():
    limiter = DecisionPermitLimiter(max_concurrency=1, timeout_seconds=5)
    permit = limiter.claim(GID, "event-1", now=100.0)
    assert permit is not None
    expired = limiter.cleanup(now=106.0)
    assert [p.permit_id for p in expired] == [permit.permit_id]
    assert limiter.claim(GID, "event-2", now=107.0) is not None


def test_default_limit_is_serial(make_config):
    """默认并发为 1：不显式配置时同群仍保持串行。"""
    assert make_config().reply_max_concurrency == 1

    limiter = DecisionPermitLimiter()
    assert limiter.max_concurrency == 1
    assert limiter.claim(GID, "event-1") is not None
    assert limiter.claim("g2", "event-2") is None
