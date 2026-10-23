"""每群二态运行时（等待 / 回复中）测试。"""

import asyncio

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.runtime import (
    REPLYING,
    WAITING,
    GroupReplyGate,
)

GID = "g1"


def test_state_starts_waiting():
    gate = GroupReplyGate(120)
    assert gate.state(GID) == WAITING
    assert gate.is_replying(GID) is False


@pytest.mark.asyncio
async def test_acquire_and_release_switch_state():
    gate = GroupReplyGate(120)
    assert await gate.acquire(GID) is True
    assert gate.state(GID) == REPLYING
    assert gate.replying_groups() == [GID]

    assert await gate.release(GID) is True
    assert gate.state(GID) == WAITING
    assert gate.replying_groups() == []
    # 重复释放是幂等的
    assert await gate.release(GID) is False


@pytest.mark.asyncio
async def test_second_message_waits_until_reply_ends():
    """回复中到达的消息排队，不会与当前轮并发。"""
    gate = GroupReplyGate(120)
    assert await gate.acquire(GID) is True

    order: list[str] = []

    async def queued() -> None:
        assert await gate.acquire(GID) is True
        order.append("queued")

    task = asyncio.create_task(queued())
    await asyncio.sleep(0.05)
    assert order == []
    assert gate.is_replying(GID) is True

    await gate.release(GID)
    await task
    assert order == ["queued"]


@pytest.mark.asyncio
async def test_wait_times_out_when_reply_never_ends():
    gate = GroupReplyGate(0.05)
    assert await gate.acquire(GID) is True
    assert await gate.acquire(GID, timeout=0.05) is False


@pytest.mark.asyncio
async def test_cleanup_forces_stale_replying_group_back_to_waiting():
    gate = GroupReplyGate(10)
    assert (
        await gate.acquire(
            GID,
        )
        is True
    )
    gate._states[GID].since -= 60  # 模拟回复钩子丢失、状态长期不释放

    assert await gate.cleanup() == [GID]
    assert gate.state(GID) == WAITING
    assert await gate.acquire(GID) is True


def test_try_acquire_is_non_blocking():
    gate = GroupReplyGate(120)
    assert gate.try_acquire(GID) is True
    assert gate.try_acquire(GID) is False
    assert gate.is_replying(GID) is True


def test_watermark_marks_consumed_messages_only():
    gate = GroupReplyGate(120)
    gate.update_watermark(GID, 100.0)
    assert gate.covered(GID, 90.0) is True
    assert gate.covered(GID, 100.0) is True
    assert gate.covered(GID, 100.1) is False
    # 水位只增不减，回退写入不会丢失已覆盖区间
    gate.update_watermark(GID, 50.0)
    assert gate.watermark(GID) == 100.0
    # 未知时间戳（0）永远不算已覆盖
    assert gate.covered(GID, 0.0) is False


@pytest.mark.asyncio
async def test_messages_arriving_during_reply_start_next_round_in_order():
    """回复期间到达的多条消息在结束后依次处理，旧消息不重复决策。"""
    gate = GroupReplyGate(120)
    rounds: list[float] = []
    window: list[float] = []

    async def handle(message_at: float) -> None:
        window.append(message_at)
        assert await gate.acquire(GID) is True
        if gate.covered(GID, message_at):
            await gate.release(GID)
            return
        # 决策读取的是整个窗口，水位推进到窗口最新一条
        gate.update_watermark(GID, max(window))
        rounds.append(max(window))
        await asyncio.sleep(0.01)
        await gate.release(GID)

    # 第一轮：消息 1.0 立即开始
    first = asyncio.create_task(handle(1.0))
    await asyncio.sleep(0)
    # 回复期间到达的消息 2.0 / 3.0 排队
    second = asyncio.create_task(handle(2.0))
    third = asyncio.create_task(handle(3.0))
    await asyncio.gather(first, second, third)

    # 第二轮只会为最新消息再回复一次，2.0 被 3.0 那一轮的窗口覆盖
    assert rounds[0] == 1.0
    assert rounds[-1] == 3.0
    assert len(rounds) == 2
    assert gate.is_replying(GID) is False
