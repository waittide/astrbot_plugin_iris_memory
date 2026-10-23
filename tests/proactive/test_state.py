"""proactive.state.StateManager 状态机测试

覆盖：锚点 TTL 过期、willingness、initiate pending 记账与风险率。
"""

import time

from astrbot_plugin_iris_memory.iris_memory.proactive.state import StateManager

GID = "g1"


class TestAnchor:
    def test_write_and_match(self, state):
        state.write_anchor(
            GID,
            kind="chime_in",
            topic="周末计划",
            bot_message="去哪玩",
            users=["u1"],
            keywords=["天气"],
            reason="感兴趣",
        )
        assert state.match_anchor_user(GID, "u1") is True
        assert state.match_anchor_user(GID, "u9") is False
        assert state.match_anchor_keyword(GID, "今天天气不错") == ["天气"]
        assert state.match_anchor_keyword(GID, "无关内容") == []
        anchor = state.get_anchor(GID)
        assert anchor.active is True
        assert anchor.kind == "chime_in"
        assert anchor.topic == "周末计划"

    def test_user_ttl_expiry(self, state):
        state.add_anchor_watch(GID, users=["u1"], ttl_minutes=-1)
        assert state.match_anchor_user(GID, "u1") is False
        assert state.get_anchor(GID).active is False

    def test_keyword_ttl_expiry(self, state):
        state.add_anchor_watch(GID, keywords=["天气"], ttl_minutes=-1)
        assert state.match_anchor_keyword(GID, "天气") == []
        assert state.get_anchor(GID).active is False

    def test_write_replaces_old_anchor(self, state):
        state.write_anchor(GID, kind="chime_in", users=["u1"])
        state.write_anchor(GID, kind="follow_up", users=["u2"])
        assert state.match_anchor_user(GID, "u1") is False
        assert state.match_anchor_user(GID, "u2") is True
        assert state.get_anchor(GID).kind == "follow_up"

    def test_add_watch_merges_without_replace(self, state):
        state.write_anchor(GID, kind="chime_in", bot_message="hi", users=["u1"])
        state.add_anchor_watch(GID, users=["u2"], keywords=["k1"], reason="r")
        anchor = state.get_anchor(GID)
        assert anchor.participants == {"u1", "u2"}
        assert anchor.keywords == {"k1"}
        assert anchor.bot_message == "hi"
        assert anchor.reason == "r"

    def test_anchor_user_cap(self, state):
        state.add_anchor_watch(GID, users=[f"u{i}" for i in range(25)])
        assert len(state.get_anchor(GID).participants) == 20

    def test_anchor_keyword_cap(self, state):
        state.add_anchor_watch(GID, keywords=[f"k{i}" for i in range(15)])
        assert len(state.get_anchor(GID).keywords) == 10

    def test_remove_anchor_watch(self, state):
        state.add_anchor_watch(GID, users=["u1", "u2"], keywords=["k1"])
        state.remove_anchor_watch(GID, user_ids=["u1"])
        anchor = state.get_anchor(GID)
        assert anchor.participants == {"u2"}
        # 不带参数清空整个锚点
        state.remove_anchor_watch(GID)
        assert state.get_anchor(GID).active is False

    def test_close_anchor(self, state):
        state.write_anchor(GID, kind="chime_in", bot_message="hi", users=["u1"])
        state.close_anchor(GID)
        anchor = state.get_anchor(GID)
        assert anchor.active is False
        assert anchor.has_context is False

    def test_clear_decision_context_preserves_unrelated_state(self, state):
        state.set_willingness(GID, "high")
        state.set_observation(GID, "旧观察")
        state.write_anchor(
            GID,
            kind="follow_up",
            topic="旧话题",
            bot_message="旧回复",
            users=["u1"],
            keywords=["旧关键词"],
            reason="旧原因",
        )

        cleared = state.clear_decision_context(GID)

        assert cleared == {"observation": True, "anchor": True}
        assert state.get_observation(GID) == ""
        assert state.get_anchor(GID).has_context is False
        assert state.get_anchor(GID).topic == ""
        assert state.get_willingness(GID) == "high"


class TestInitiateAccounting:
    def test_record_initiate(self, state):
        state.record_initiate(GID, topic="新话题", bot_message="大家好", users=["u1"])
        data = state.get_state(GID)
        assert data.initiate_daily_count == 1
        assert len(data.initiate_timestamps) == 1
        assert data.last_initiate_time > 0
        assert data.initiate_pending_since > 0
        assert state.is_initiate_pending(GID) is True
        anchor = state.get_anchor(GID)
        assert anchor.kind == "initiate"
        assert anchor.bot_message == "大家好"

    def test_record_initiate_resets_drive(self, state):
        data = state.get_state(GID)
        data.initiate_hazard_probability = 0.9
        state.record_initiate(GID)
        assert data.initiate_hazard_probability == 0.0
        assert data.initiate_hazard_at > 0

    def test_consume_pending_resets_streak(self, state):
        state.record_initiate(GID)
        data = state.get_state(GID)
        data.initiate_no_reply_streak = 2
        assert state.consume_initiate_pending(GID) is True
        assert data.initiate_pending_since == 0.0
        assert data.initiate_no_reply_streak == 0
        assert state.consume_initiate_pending(GID) is False

    def test_unanswered_increments_streak(self, state):
        state.record_initiate(GID)
        state.record_initiate_unanswered(GID)
        data = state.get_state(GID)
        assert data.initiate_pending_since == 0.0
        assert data.initiate_no_reply_streak == 1
        # 无 pending 时重复调用不累计
        state.record_initiate_unanswered(GID)
        assert data.initiate_no_reply_streak == 1

    def test_rolling_window_prunes_without_midnight_reset(self, state):
        data = state.get_state(GID)
        now = time.time()
        data.initiate_timestamps = [now - 25 * 3600, now - 60]
        data.last_initiate_time = now - 60
        data.initiate_no_reply_streak = 3
        refreshed = state.get_state(GID)
        assert refreshed.initiate_daily_count == 1
        assert refreshed.initiate_timestamps == [data.initiate_timestamps[0]]
        assert refreshed.initiate_no_reply_streak == 3


class TestInitiateHazard:
    """主动发起风险率：硬静默底线、概率、因子、冻结和持久化。"""

    def _evaluate(self, state, *, quiet=7200, draw=1.0, elapsed=900):
        now = time.time()
        state.get_state(GID).initiate_hazard_at = now - elapsed
        fired = state.evaluate_initiate_hazard(
            GID, now=now, quiet_seconds=quiet, random_value=draw
        )
        return fired, state.get_state(GID).initiate_hazard_probability

    def test_hard_quiet_floor_blocks(self, state):
        fired, probability = self._evaluate(state, quiet=60, draw=0.0)
        assert fired is False
        assert probability == 0.0

    def test_probability_trial_can_fire_or_skip(self, state):
        fired, probability = self._evaluate(state, draw=0.0)
        assert probability > 0
        assert fired is True
        fired, _ = self._evaluate(state, draw=1.0)
        assert fired is False

    def test_willingness_changes_probability(self, state):
        _, medium = self._evaluate(state)
        state.set_willingness(GID, "high")
        _, high = self._evaluate(state)
        state.set_willingness(GID, "low")
        _, low = self._evaluate(state)
        assert high > medium > low

    def test_context_and_drift_raise_probability(self, state):
        _, base = self._evaluate(state)
        state.write_anchor(GID, kind="passive", bot_message="旧话题")
        _, anchored = self._evaluate(state)
        state.get_state(GID).last_drift_time = time.time() - 60
        _, drifted = self._evaluate(state)
        assert anchored > base
        assert drifted > anchored

    def test_freeze_advances_clock_without_probability(self, state):
        now = time.time()
        data = state.get_state(GID)
        data.initiate_hazard_at = now - 900
        assert (
            state.evaluate_initiate_hazard(
                GID, now=now, quiet_seconds=9999, freeze=True, random_value=0.0
            )
            is False
        )
        assert data.initiate_hazard_probability == 0.0
        assert data.initiate_hazard_at == now

    def test_elapsed_is_capped_after_downtime(self, state):
        _, long_gap = self._evaluate(state, elapsed=86400)
        _, capped_gap = self._evaluate(
            state, elapsed=StateManager.INITIATE_HAZARD_ELAPSED_MIN
        )
        assert long_gap == capped_gap

    def test_human_message_invalidates_schedule(self, state):
        state.set_next_initiate_check(GID, time.time() + 1000)
        now = time.time()
        state.record_human_message(GID, now)
        data = state.get_state(GID)
        assert data.last_human_message_at == now
        assert data.initiate_next_check_at == 0.0
        assert data.initiate_hazard_at == now

    def test_hazard_fields_serialize_round_trip(self, state):
        data = state.get_state(GID)
        data.last_human_message_at = 12000.0
        data.initiate_next_check_at = 13000.0
        data.initiate_hazard_at = 12345.0
        data.initiate_hazard_probability = 0.23
        data.initiate_timestamps = [11000.0]
        snapshot = state._serialize_group(data)
        restored = state._deserialize_group(snapshot)
        assert restored.last_human_message_at == 12000.0
        assert restored.initiate_next_check_at == 13000.0
        assert restored.initiate_hazard_at == 12345.0
        assert restored.initiate_hazard_probability == 0.23
