"""
Iris Chat Memory - 默认配置定义

使用 dataclass 提供类型安全的配置定义与扁平化键名访问。
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict


@dataclass
class L1BufferConfig:
    """L1 消息上下文缓冲配置（用户可见选项）"""

    enable: bool = True
    summary_provider: str = ""
    inject_queue_length: int = 50


@dataclass
class L2MemoryConfig:
    """L2 记忆库配置"""

    enable: bool = True
    top_k: int = 10
    relevance_threshold: float = 0.3


@dataclass
class L3KGConfig:
    """L3 知识图谱配置"""

    enable: bool = True
    extraction_provider: str = ""


@dataclass
class ProfileConfig:
    """画像系统配置"""

    enable: bool = True
    analysis_provider: str = ""
    enable_auto_injection: bool = True
    favorability_enable: bool = True


@dataclass
class IsolationConfig:
    """隔离配置"""

    enable_group_memory_isolation: bool = False
    enable_group_isolation: bool = False
    enable_persona_isolation: bool = False


@dataclass
class ContextControlConfig:
    """上下文控制配置"""

    enable_conversation_cleanup: bool = True


@dataclass
class HiddenConfig:
    """隐藏配置(内部参数)，支持持久化到 data/iris_memory/hidden_config.json"""

    # Token 预算控制
    token_budget_max_tokens: int = field(default=5000)

    # L1 缓冲内部参数
    l1_segment_1_length: int = field(default=10)
    l1_segment_3_length: int = field(default=10)
    l1_max_queue_tokens: int = field(default=4000)
    l1_max_single_message_tokens: int = field(default=500)
    l1_inject_max_content_chars: int = field(default=300)
    l1_max_memories_per_summary: int = field(default=10)
    l1_summary_queue_limit: int = field(default=500)
    l1_summary_worker_count: int = field(default=2)
    l1_outbox_worker_count: int = field(default=2)
    l1_outbox_poll_seconds: int = field(default=60)
    l1_persist_enable: bool = field(default=True)
    l1_persist_interval_seconds: float = field(default=2.0)

    # L2 记忆参数
    l2_timeout_ms: int = field(default=4000)
    l2_hybrid_global_scope_factor: float = field(default=0.8)
    l2_enable_hit_reinforcement: bool = field(default=True)
    l2_hit_reinforcement_step: float = field(default=0.1)

    # L2 查询改写参数
    l2_query_rewrite_enable: bool = field(default=False)
    l2_query_rewrite_provider: str = field(default="")
    l2_query_rewrite_timeout_ms: int = field(default=3000)
    l2_query_rewrite_queue_timeout_ms: int = field(default=800)
    l2_query_rewrite_cache_size: int = field(default=256)
    l2_query_rewrite_inflight_limit: int = field(default=64)

    # L3 知识图谱参数
    l3_timeout_ms: int = field(default=1500)
    l3_expansion_depth: int = field(default=2)
    l3_enable_type_whitelist: bool = field(default=True)
    l3_max_inject_tokens: int = field(default=600)
    node_confidence_threshold: float = field(default=0.3)

    # LLM 调用管理参数
    enable_context_logging: bool = field(default=False)
    llm_global_concurrency: int = field(default=4)
    llm_provider_concurrency: int = field(default=2)
    llm_provider_background_concurrency: int = field(default=1)
    llm_provider_rpm: int = field(default=30)
    llm_provider_min_interval_ms: int = field(default=0)
    llm_queue_limit: int = field(default=500)
    llm_interactive_reserved_slots: int = field(default=1)
    llm_circuit_failure_threshold: int = field(default=5)
    llm_circuit_open_seconds: int = field(default=60)
    llm_priority_aging_seconds: int = field(default=30)
    llm_queue_timeout_ms: int = field(default=300000)
    llm_provider_timeout_ms: int = field(default=60000)
    llm_total_timeout_ms: int = field(default=360000)
    llm_call_timeout_ms: int = field(default=60000)

    # 画像系统参数
    profile_first_mid_min_summaries: int = field(default=3)
    profile_first_long_min_summaries: int = field(default=10)
    profile_long_min_new_summaries: int = field(default=3)
    profile_batch_size: int = field(default=8)
    profile_batch_user_messages: int = field(default=12)
    profile_max_messages_for_analysis: int = field(default=50)
    profile_max_messages_for_user_analysis: int = field(default=30)
    profile_mid_update_interval_summaries: int = field(default=5)
    profile_mid_update_interval_hours: float = field(default=24.0)
    profile_long_update_interval_hours: float = field(default=168.0)
    profile_favorability_max_delta: float = field(default=20.0)
    profile_message_max_chars: int = field(default=150)

    # 输入清理与上下文控制参数
    input_sanitizer_enable: bool = field(default=True)
    input_sanitizer_max_length: int = field(default=10000)
    enable_legacy_cleanup: bool = field(default=False)

    # 主动回复内部参数
    reply_mute_start_hour: int = field(default=1)
    reply_mute_start_minute: int = field(default=0)
    reply_mute_end_hour: int = field(default=7)
    reply_mute_end_minute: int = field(default=0)
    reply_passive_watch_min_interval: int = field(default=600)
    reply_passive_watch_enabled: bool = field(default=False)
    reply_window_size: int = field(default=15)
    reply_max_token: int = field(default=3000)
    reply_follow_up_ttl: int = field(default=10)
    reply_follow_up_aggregate_window: int = field(default=6)
    proactive_check_interval: int = field(default=5)
    proactive_quiet_minutes: int = field(default=120)
    proactive_drift_delay: int = field(default=15)
    proactive_pending_timeout: int = field(default=30)
    proactive_instruction: str = field(default="")
    proactive_max_message_len: int = field(default=300)


@dataclass
class Defaults:
    """所有默认配置的统一入口"""

    l1_buffer: L1BufferConfig = field(default_factory=L1BufferConfig)
    l2_memory: L2MemoryConfig = field(default_factory=L2MemoryConfig)
    l3_kg: L3KGConfig = field(default_factory=L3KGConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    isolation_config: IsolationConfig = field(default_factory=IsolationConfig)
    context_control: ContextControlConfig = field(default_factory=ContextControlConfig)
    hidden: HiddenConfig = field(default_factory=HiddenConfig)

    def get_by_flat_key(self, flat_key: str) -> Optional[object]:
        parts = flat_key.split(".")

        if len(parts) == 1:
            return getattr(self.hidden, parts[0], None)
        elif len(parts) == 2:
            section, key = parts
            section_config = getattr(self, section, None)
            if section_config is not None:
                return getattr(section_config, key, None)
        elif len(parts) == 3:
            section, subsection, key = parts
            section_config = getattr(self, section, None)
            if section_config is not None:
                subsection_config = getattr(section_config, subsection, None)
                if subsection_config is not None:
                    return getattr(subsection_config, key, None)

        return None

    def get_section_defaults(self, section: str) -> Dict[str, object]:
        section_config = getattr(self, section, None)
        if section_config is None:
            return {}
        return asdict(section_config)
