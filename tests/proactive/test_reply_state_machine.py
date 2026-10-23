"""插件层每群二态串行化测试：同一群同一时刻只有一轮决策/回复。"""

import asyncio
from unittest.mock import Mock

import pytest

from astrbot_plugin_iris_memory.iris_memory.proactive.decision import DecisionOutcome
from astrbot_plugin_iris_memory.iris_memory.proactive.parser import Decision
from astrbot_plugin_iris_memory.iris_memory.proactive.perception import (
    SlidingWindow,
    WindowMessage,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.runtime import (
    REPLYING,
    GroupReplyGate,
)
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

GID = "g1"


class _FakeCore:
    """记录调用顺序的决策核心；speak=True 时返回发言决策。"""

    def __init__(self, *, speak: bool, hold: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self._speak = speak
        self._hold = hold

    async def decide(self, req, llm_manager, provider_id):
        self.calls.append(req.group_id)
        if self._hold is not None:
            await self._hold.wait()
        action = "speak" if self._speak else "none"
        return DecisionOutcome(
            decision=Decision(action=action),
            system_prompt="sys",
            user_prompt="usr",
            raw_text="{}",
        )

    def clear_rejected_dynamic_context(self, group_id, sources):
        raise AssertionError("unexpected safety cleanup")


def _plugin(config, *, speak: bool, hold: asyncio.Event | None = None):
    from astrbot_plugin_iris_memory.main import IrisMemoryPlugin

    plugin = object.__new__(IrisMemoryPlugin)
    plugin._gate = GroupReplyGate(120)
    plugin._state = StateManager(config)
    plugin._sliding_window = SlidingWindow(config)
    plugin._decision_core = _FakeCore(speak=speak, hold=hold)
    plugin._llm_manager = Mock()
    plugin._stats = Mock()
    plugin._tool_ctx = Mock()
    plugin._reply_config = config
    plugin._kv_save = Mock()
    plugin._state.save_dirty = Mock(return_value=asyncio.sleep(0))
    plugin._sliding_window.append(
        GID,
        WindowMessage(
            sender_id="u1", sender_name="U", content="第一条", timestamp=10.0
        ),
    )
    return plugin


def _event(group_id: str, message_at: float):
    extras = {
        "iris_decision": {
            "motive": "chime_in",
            "provider_id": "p",
            "message_at": message_at,
        },
    }
    event = Mock()
    event.get_group_id.return_value = group_id
    event.get_extra.side_effect = lambda key, default=None: extras.get(key, default)
    event.set_extra.side_effect = lambda key, value=None: extras.__setitem__(key, value)
    return event


@pytest.mark.asyncio
async def test_second_message_waits_for_first_reply(nm_config):
    """回复中到达的消息排队，等本轮结束后才进入决策。"""
    config = nm_config()
    hold = asyncio.Event()
    plugin = _plugin(config, speak=True, hold=hold)

    first = _event(GID, 10.0)
    second = _event(GID, 20.0)

    first_task = asyncio.create_task(plugin._handle_reply_decision(first))
    await asyncio.sleep(0.05)
    assert plugin._gate.state(GID) == REPLYING

    second_task = asyncio.create_task(plugin._handle_reply_decision(second))
    await asyncio.sleep(0.05)
    # 第一轮未结束前，第二条消息不进入决策
    assert plugin._decision_core.calls == [GID]

    hold.set()
    assert await first_task is False  # 第一轮决定发言
    assert plugin._gate.state(GID) == REPLYING  # 仍在第一轮的回复中

    # 回复发送完成 → 群状态回到等待，排队消息开启下一轮
    await plugin._gate.release(GID)
    assert await second_task is False
    assert plugin._decision_core.calls == [GID, GID]
    assert plugin._gate.state(GID) == REPLYING


@pytest.mark.asyncio
async def test_message_already_in_round_window_is_skipped(nm_config):
    """已被本轮决策上下文覆盖的消息不再重复决策。"""
    config = nm_config()
    plugin = _plugin(config, speak=True)
    # 窗口里已经有更新的消息：本轮决策会把它们一起读进上下文
    plugin._sliding_window.append(
        GID,
        WindowMessage(
            sender_id="u1", sender_name="U", content="更新的消息", timestamp=30.0
        ),
    )

    first = _event(GID, 10.0)
    assert await plugin._handle_reply_decision(first) is False
    # 第一轮回复发送完成
    await plugin._gate.release(GID)

    second = _event(GID, 20.0)
    assert await plugin._handle_reply_decision(second) is True
    assert plugin._decision_core.calls == [GID]
    second.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_newer_message_starts_next_round_after_reply(nm_config):
    """回复期间到达的更新消息在回复结束后开启下一轮。"""
    config = nm_config()
    hold = asyncio.Event()
    plugin = _plugin(config, speak=True, hold=hold)

    first = _event(GID, 10.0)
    first_task = asyncio.create_task(plugin._handle_reply_decision(first))
    await asyncio.sleep(0.05)

    # 模拟回复期间窗口新增一条消息
    plugin._sliding_window.append(
        GID,
        WindowMessage(
            sender_id="u1", sender_name="U", content="新消息", timestamp=30.0
        ),
    )
    second = _event(GID, 30.0)
    second_task = asyncio.create_task(plugin._handle_reply_decision(second))
    await asyncio.sleep(0.05)
    assert plugin._decision_core.calls == [GID]

    hold.set()
    assert await first_task is False
    # 第一轮回复发送完成 → 释放群状态
    await plugin._gate.release(GID)

    assert await second_task is False
    assert plugin._decision_core.calls == [GID, GID]
    assert plugin._gate.state(GID) == REPLYING


@pytest.mark.asyncio
async def test_skip_decision_releases_group_immediately(nm_config):
    """LLM 决定不发言时立刻回到等待，不阻塞后续消息。"""
    config = nm_config()
    plugin = _plugin(config, speak=False)

    event = _event(GID, 10.0)
    assert await plugin._handle_reply_decision(event) is True
    assert plugin._gate.state(GID) != REPLYING
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_event_is_decided_once_even_if_hook_repeats(nm_config):
    """同一事件重复进入 on_llm_request（工具循环）时不再重复决策。"""
    config = nm_config()
    plugin = _plugin(config, speak=True)

    event = _event(GID, 10.0)
    assert await plugin._handle_reply_decision(event) is False
    assert await plugin._handle_reply_decision(event) is False
    assert plugin._decision_core.calls == [GID]
