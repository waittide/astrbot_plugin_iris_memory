"""signals.SignalGate 门控矩阵测试

覆盖：停用 / 静音 / 白名单（Gatekeeper 层）/ 锚点用户与关键词命中 /
每条消息都进入决策，以及 evaluate_timer 的 initiate 门控。
"""

import time
from unittest.mock import Mock, patch

from astrbot_plugin_iris_memory.iris_memory.proactive.perception import Gatekeeper
from astrbot_plugin_iris_memory.iris_memory.proactive.signals import SignalGate
from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

GID = "g1"


def _build(nm_config, cfg=None, overrides=None):
    cm = nm_config(cfg=cfg, overrides=overrides)
    st = StateManager(cm)
    return cm, st, SignalGate(cm, st)


def _event(group_id=GID, text="hello", private=False):
    ev = Mock()
    ev.message_str = text
    ev.is_private_chat.return_value = private
    ev.get_group_id.return_value = group_id
    return ev


class TestMessageGateBasic:
    def test_disabled_config_blocks_all(self, nm_config):
        _, st, gate = _build(nm_config, cfg={"proactive": {"enabled": False}})
        st.add_anchor_watch(GID, users=["u1"])
        assert gate.evaluate_message(GID, "u1", "hi") is None

    def test_muted_blocks(self, nm_config):
        _, st, gate = _build(nm_config)
        st.is_muted = lambda: True
        assert gate.evaluate_message(GID, "u1", "hi") is None

    def test_every_message_falls_through_to_chime_in(self, gate):
        # 无锚点命中时不再采样门控，每条消息都进入决策
        assert gate.evaluate_message(GID, "u1", "今天聊点什么") == "chime_in"


class TestMessageGateAnchor:
    def test_anchor_user_hit_returns_follow_up(self, gate, state):
        state.add_anchor_watch(GID, users=["u1"])
        assert gate.evaluate_message(GID, "u1", "我回来了") == "follow_up"

    def test_anchor_keyword_hit_returns_follow_up(self, gate, state):
        state.add_anchor_watch(GID, keywords=["天气"])
        assert gate.evaluate_message(GID, "u9", "今天天气真不错") == "follow_up"

    def test_anchor_miss_still_returns_chime_in(self, gate, state):
        state.add_anchor_watch(GID, users=["u1"], keywords=["天气"])
        assert gate.evaluate_message(GID, "u9", "完全无关的内容") == "chime_in"


class TestTimerGate:
    """主动发起风险率门控。"""

    def _timer_gate(self, nm_config, overrides=None):
        base_overrides = {"proactive_quiet_minutes": 30}
        if overrides:
            base_overrides.update(overrides)
        return _build(
            nm_config,
            cfg={"proactive": {"proactive_enabled": True}},
            overrides=base_overrides,
        )

    def _prime_hazard(self, st, *, tick=900):
        data = st.get_state(GID)
        data.initiate_hazard_at = time.time() - tick

    def test_eligible_hazard_can_fire(self, nm_config, make_msg):
        _, st, gate = self._timer_gate(nm_config)
        old = make_msg(timestamp=time.time() - 1900)
        self._prime_hazard(st)
        with patch(
            "astrbot_plugin_iris_memory.iris_memory.proactive.state.random.random",
            return_value=0.0,
        ):
            assert gate.evaluate_timer(GID, [old]) == "initiate"

    def test_same_eligible_state_can_probabilistically_skip(self, nm_config, make_msg):
        _, st, gate = self._timer_gate(nm_config)
        old = make_msg(timestamp=time.time() - 1900)
        self._prime_hazard(st)
        with patch(
            "astrbot_plugin_iris_memory.iris_memory.proactive.state.random.random",
            return_value=1.0,
        ):
            assert gate.evaluate_timer(GID, [old]) is None

    def test_hard_quiet_floor_blocks(self, nm_config, make_msg):
        _, st, gate = self._timer_gate(nm_config)
        recent = make_msg(timestamp=time.time() - 60)
        self._prime_hazard(st)
        assert gate.evaluate_timer(GID, [recent]) is None
        assert st.get_state(GID).initiate_hazard_probability == 0.0

    def test_proactive_disabled(self, nm_config, make_msg):
        _, _, gate = _build(nm_config)  # proactive_enabled 默认 False
        old = make_msg(timestamp=time.time() - 9999)
        assert gate.evaluate_timer(GID, [old]) is None

    def test_empty_messages(self, nm_config):
        _, _, gate = self._timer_gate(nm_config)
        assert gate.evaluate_timer(GID, []) is None

    def test_pending_blocks(self, nm_config, make_msg):
        _, st, gate = self._timer_gate(nm_config)
        st.record_initiate(GID, bot_message="大家好")
        old = make_msg(timestamp=time.time() - 1900)
        self._prime_hazard(st)
        assert gate.evaluate_timer(GID, [old]) is None

    def test_no_daily_cap_or_streak_limit(self, nm_config, make_msg):
        # 每日次数与无人接话上限已删除：历史计数不再阻断发起
        _, st, gate = self._timer_gate(nm_config)
        st.record_initiate(GID, bot_message="大家好")
        data = st.get_state(GID)
        data.initiate_pending_since = 0.0
        old = make_msg(timestamp=time.time() - 1900)
        self._prime_hazard(st)
        with patch(
            "astrbot_plugin_iris_memory.iris_memory.proactive.state.random.random",
            return_value=0.0,
        ):
            assert gate.evaluate_timer(GID, [old]) == "initiate"

    def test_no_min_interval_limit(self, nm_config, make_msg):
        # 两次发起最小间隔已删除：刚发起过也能再次评估
        _, st, gate = self._timer_gate(nm_config)
        old = make_msg(timestamp=time.time() - 1900)
        st.get_state(GID).last_initiate_time = time.time() - 100
        self._prime_hazard(st)
        with patch(
            "astrbot_plugin_iris_memory.iris_memory.proactive.state.random.random",
            return_value=0.0,
        ):
            assert gate.evaluate_timer(GID, [old]) == "initiate"

    def test_drift_increases_hazard(self, nm_config, make_msg):
        _, st, gate = self._timer_gate(nm_config, {"proactive_drift_delay": 5})
        quiet = make_msg(timestamp=time.time() - 1900)
        self._prime_hazard(st)
        with patch(
            "astrbot_plugin_iris_memory.iris_memory.proactive.state.random.random",
            return_value=1.0,
        ):
            gate.evaluate_timer(GID, [quiet])
        base = st.get_state(GID).initiate_hazard_probability
        st.get_state(GID).last_drift_time = time.time() - 100
        self._prime_hazard(st)
        with patch(
            "astrbot_plugin_iris_memory.iris_memory.proactive.state.random.random",
            return_value=1.0,
        ):
            gate.evaluate_timer(GID, [quiet])
        assert st.get_state(GID).initiate_hazard_probability > base

    def test_muted_freezes_hazard(self, nm_config, make_msg):
        _, st, gate = self._timer_gate(nm_config)
        st.is_muted = lambda: True
        old = make_msg(timestamp=time.time() - 1900)
        self._prime_hazard(st)
        assert gate.evaluate_timer(GID, [old]) is None
        data = st.get_state(GID)
        assert data.initiate_hazard_probability == 0.0
        assert time.time() - data.initiate_hazard_at < 5


class TestGatekeeperWhitelist:
    """白名单门控在 perception.Gatekeeper 层（SignalGate 上游）"""

    def test_whitelist_blocks_unknown_group(self, nm_config):
        cm = nm_config()
        st = StateManager(cm)
        gk = Gatekeeper(cm, st)
        assert gk.should_process(_event()) is False
        st.add_to_whitelist(GID)
        assert gk.should_process(_event()) is True

    def test_empty_command_and_private_blocked(self, nm_config):
        cm = nm_config()
        st = StateManager(cm)
        st.add_to_whitelist(GID)
        gk = Gatekeeper(cm, st)
        assert gk.should_process(_event(text="")) is False
        assert gk.should_process(_event(text="/iris_reply status")) is False
        assert gk.should_process(_event(private=True)) is False

    def test_muted_blocks(self, nm_config):
        cm = nm_config()
        st = StateManager(cm)
        st.add_to_whitelist(GID)
        st.is_muted = lambda: True
        gk = Gatekeeper(cm, st)
        assert gk.should_process(_event()) is False

    def test_state_enum_removed(self):
        # 冷却状态机已删除，StateManager 不再暴露 GroupState
        from astrbot_plugin_iris_memory.iris_memory.proactive import state as state_mod

        assert not hasattr(state_mod, "GroupState")


def test_short_and_symbol_only_messages_enter_decision(gate):
    """质量门控已删除：短消息、纯数字、纯符号都直接进入决策。"""
    for text in ("1", "嗯嗯", "？？？", "😂😂😂", "hello", "123 456"):
        assert gate.evaluate_message(GID, "u1", text) == "chime_in"


def test_gatekeeper_has_no_quality_gate(nm_config):
    """Gatekeeper 不再提供质量打分，消息质量门控整体移除。"""
    cm, st, _ = _build(nm_config)
    gk = Gatekeeper(cm, st)
    assert not hasattr(gk, "quality_score")
    assert not hasattr(cm, "quality_threshold")
