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
    image_provider: str = ""


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
    token_budget_max_tokens: int = field(
        default=5000,
        metadata={"description": "Token 预算上限", "group": "Token 预算"},
    )

    # L1 缓冲内部参数
    l1_segment_1_length: int = field(
        default=10,
        metadata={"description": "L1-1 最新段消息数（始终注入上下文）", "group": "L1 缓冲"},
    )
    l1_segment_3_length: int = field(
        default=10,
        metadata={"description": "L1-3 缓冲段消息数（辅助总结理解）", "group": "L1 缓冲"},
    )
    l1_max_queue_tokens: int = field(
        default=4000,
        metadata={"description": "队列最大 Token 数，超限触发总结", "group": "L1 缓冲"},
    )
    l1_max_single_message_tokens: int = field(
        default=500,
        metadata={"description": "单条消息最大 Token 数，超限丢弃或截断", "group": "L1 缓冲"},
    )
    l1_inject_max_content_chars: int = field(
        default=300,
        metadata={"description": "注入时单条消息最大字符数，0 不截断", "group": "L1 缓冲"},
    )
    l1_max_memories_per_summary: int = field(
        default=10,
        metadata={"description": "每次总结写入 L2 的最大记忆条数", "group": "L1 缓冲"},
    )
    l1_summary_queue_limit: int = field(
        default=500,
        metadata={"description": "L1 总结待处理会话上限", "group": "L1 缓冲"},
    )
    l1_summary_worker_count: int = field(
        default=2,
        metadata={"description": "L1 总结固定 Worker 数", "group": "L1 缓冲"},
    )
    l1_outbox_worker_count: int = field(
        default=2,
        metadata={"description": "L1 Outbox 后台写入 Worker 数", "group": "L1 缓冲"},
    )
    l1_outbox_poll_seconds: int = field(
        default=60,
        metadata={"description": "L1 Outbox 崩溃恢复扫描间隔(秒)", "group": "L1 缓冲"},
    )
    l1_persist_enable: bool = field(
        default=True,
        metadata={"description": "持久化 L1 消息队列，重启后恢复对话", "group": "L1 缓冲"},
    )
    l1_persist_interval_seconds: float = field(
        default=2.0,
        metadata={"description": "L1 队列落盘节流间隔(秒)", "group": "L1 缓冲"},
    )

    # L2 记忆参数
    l2_timeout_ms: int = field(
        default=4000,
        metadata={"description": "L2 检索超时(ms)", "group": "L2 记忆"},
    )
    l2_hybrid_global_scope_factor: float = field(
        default=0.8,
        metadata={"description": "全局共享记忆（scope=global）分数权重系数", "group": "L2 记忆"},
    )
    l2_enable_hit_reinforcement: bool = field(
        default=True,
        metadata={"description": "对话检索命中时渐近提升记忆重要度", "group": "L2 记忆"},
    )
    l2_hit_reinforcement_step: float = field(
        default=0.1,
        metadata={"description": "命中强化步长", "group": "L2 记忆"},
    )

    # L2 查询改写参数
    l2_query_rewrite_enable: bool = field(
        default=False,
        metadata={"description": "启用 L2 检索查询改写", "group": "L2 查询改写"},
    )
    l2_query_rewrite_provider: str = field(
        default="",
        metadata={"description": "查询改写使用的 Provider(空则使用默认)", "group": "L2 查询改写"},
    )
    l2_query_rewrite_timeout_ms: int = field(
        default=3000,
        metadata={"description": "查询改写超时(ms)", "group": "L2 查询改写"},
    )
    l2_query_rewrite_queue_timeout_ms: int = field(
        default=800,
        metadata={"description": "查询改写排队超时(ms)", "group": "L2 查询改写"},
    )
    l2_query_rewrite_cache_size: int = field(
        default=256,
        metadata={"description": "查询改写 LRU 缓存条数", "group": "L2 查询改写"},
    )
    l2_query_rewrite_inflight_limit: int = field(
        default=64,
        metadata={"description": "查询改写最大在途 singleflight 数", "group": "L2 查询改写"},
    )

    # L3 知识图谱参数
    l3_timeout_ms: int = field(
        default=1500,
        metadata={"description": "L3 检索超时(ms)", "group": "L3 知识图谱"},
    )
    l3_expansion_depth: int = field(
        default=2,
        metadata={"description": "图谱检索路径扩展深度", "group": "L3 知识图谱"},
    )
    l3_enable_type_whitelist: bool = field(
        default=True,
        metadata={"description": "启用 LLM 实体类型白名单约束", "group": "L3 知识图谱"},
    )
    l3_max_inject_tokens: int = field(
        default=600,
        metadata={"description": "知识图谱注入上下文最大 token 数", "group": "L3 知识图谱"},
    )
    node_confidence_threshold: float = field(
        default=0.3,
        metadata={"description": "节点最低置信度", "group": "L3 知识图谱"},
    )

    # LLM 调用管理参数
    enable_context_logging: bool = field(
        default=False,
        metadata={"description": "启用 LLM 上下文日志输出", "group": "LLM 调用管理"},
    )
    llm_global_concurrency: int = field(
        default=4,
        metadata={"description": "全插件 LLM 最大并发", "group": "LLM 调用管理"},
    )
    llm_provider_concurrency: int = field(
        default=2,
        metadata={"description": "单 Provider 最大并发", "group": "LLM 调用管理"},
    )
    llm_provider_background_concurrency: int = field(
        default=1,
        metadata={"description": "单 Provider 后台最大并发", "group": "LLM 调用管理"},
    )
    llm_provider_rpm: int = field(
        default=30,
        metadata={"description": "单 Provider 每分钟调用上限，0 关闭", "group": "LLM 调用管理"},
    )
    llm_provider_min_interval_ms: int = field(
        default=0,
        metadata={"description": "单 Provider 两次调用的最小间隔(ms)，0 关闭", "group": "LLM 调用管理"},
    )
    llm_queue_limit: int = field(
        default=500,
        metadata={"description": "LLM 等待队列上限", "group": "LLM 调用管理"},
    )
    llm_interactive_reserved_slots: int = field(
        default=1,
        metadata={"description": "为交互请求保留的并发槽位", "group": "LLM 调用管理"},
    )
    llm_circuit_failure_threshold: int = field(
        default=5,
        metadata={"description": "Provider 连续失败熔断阈值，0 关闭", "group": "LLM 调用管理"},
    )
    llm_circuit_open_seconds: int = field(
        default=60,
        metadata={"description": "Provider 熔断持续时间(秒)", "group": "LLM 调用管理"},
    )
    llm_priority_aging_seconds: int = field(
        default=30,
        metadata={"description": "低优先级等待多久提升一级(秒)", "group": "LLM 调用管理"},
    )
    llm_queue_timeout_ms: int = field(
        default=300000,
        metadata={"description": "等待 LLM 调用资格的超时(ms)，0 不限制", "group": "LLM 调用管理"},
    )
    llm_provider_timeout_ms: int = field(
        default=60000,
        metadata={"description": "Provider 实际调用超时(ms)，0 不限制", "group": "LLM 调用管理"},
    )
    llm_total_timeout_ms: int = field(
        default=360000,
        metadata={"description": "排队加 Provider 调用的整体超时(ms)，0 不限制", "group": "LLM 调用管理"},
    )
    llm_call_timeout_ms: int = field(
        default=60000,
        metadata={"description": "旧版 Provider 超时兼容项", "group": "LLM 调用管理"},
    )

    # 画像系统参数
    profile_first_mid_min_summaries: int = field(
        default=3,
        metadata={"description": "新画像首次中期分析所需总结次数", "group": "画像系统"},
    )
    profile_first_long_min_summaries: int = field(
        default=10,
        metadata={"description": "新画像首次长期分析所需总结次数", "group": "画像系统"},
    )
    profile_long_min_new_summaries: int = field(
        default=3,
        metadata={"description": "后续长期分析所需最小新增总结证据", "group": "画像系统"},
    )
    profile_batch_size: int = field(
        default=8,
        metadata={"description": "单次画像分析最多包含的用户数", "group": "画像系统"},
    )
    profile_batch_user_messages: int = field(
        default=12,
        metadata={"description": "批量画像中每个用户最多保留的近期消息数", "group": "画像系统"},
    )
    profile_max_messages_for_analysis: int = field(
        default=50,
        metadata={"description": "群聊画像分析时最大消息数", "group": "画像系统"},
    )
    profile_max_messages_for_user_analysis: int = field(
        default=30,
        metadata={"description": "用户画像分析时最大消息数", "group": "画像系统"},
    )
    profile_mid_update_interval_summaries: int = field(
        default=5,
        metadata={"description": "中期更新: 每隔N次总结触发", "group": "画像系统"},
    )
    profile_mid_update_interval_hours: float = field(
        default=24.0,
        metadata={"description": "中期更新: 最短间隔(小时)", "group": "画像系统"},
    )
    profile_long_update_interval_hours: float = field(
        default=168.0,
        metadata={"description": "长期更新: 最短间隔(小时)", "group": "画像系统"},
    )
    profile_favorability_max_delta: float = field(
        default=20.0,
        metadata={"description": "好感度单次更新最大变化量", "group": "画像系统"},
    )
    profile_message_max_chars: int = field(
        default=150,
        metadata={"description": "画像分析时单条消息最大字符数（0 不截断）", "group": "画像系统"},
    )

    # 输入清理与上下文控制参数
    input_sanitizer_enable: bool = field(
        default=True,
        metadata={"description": "启用 Prompt 注入过滤", "group": "输入清理"},
    )
    input_sanitizer_max_length: int = field(
        default=10000,
        metadata={"description": "输入最大长度", "group": "输入清理"},
    )
    enable_legacy_cleanup: bool = field(
        default=False,
        metadata={"description": "启用旧版对话清理模式(完成后删除整个对话)", "group": "上下文控制"},
    )

    # 主动回复内部参数
    reply_mute_start_hour: int = field(
        default=1,
        metadata={"description": "静音时段开始小时(0-23)", "group": "主动回复·触发门控"},
    )
    reply_mute_start_minute: int = field(
        default=0,
        metadata={"description": "静音时段开始分钟(0-59)", "group": "主动回复·触发门控"},
    )
    reply_mute_end_hour: int = field(
        default=7,
        metadata={"description": "静音时段结束小时(0-23)", "group": "主动回复·触发门控"},
    )
    reply_mute_end_minute: int = field(
        default=0,
        metadata={"description": "静音时段结束分钟(0-59)", "group": "主动回复·触发门控"},
    )
    reply_passive_watch_min_interval: int = field(
        default=600,
        metadata={"description": "同群两次 passive watch 评估最小间隔(秒)", "group": "主动回复·触发门控"},
    )
    reply_passive_watch_enabled: bool = field(
        default=False,
        metadata={"description": "启用被动回复后的 LLM 跟进评估", "group": "主动回复·触发门控"},
    )
    reply_window_size: int = field(
        default=15,
        metadata={"description": "滑动窗口大小(条)", "group": "主动回复·上下文"},
    )
    reply_max_token: int = field(
        default=3000,
        metadata={"description": "上下文 Token 上限", "group": "主动回复·上下文"},
    )
    reply_follow_up_ttl: int = field(
        default=10,
        metadata={"description": "跟进锚点 TTL(分钟)", "group": "主动回复·上下文"},
    )
    reply_follow_up_aggregate_window: int = field(
        default=6,
        metadata={"description": "跟进聚合窗口(秒)", "group": "主动回复·上下文"},
    )
    proactive_check_interval: int = field(
        default=5,
        metadata={"description": "候选检查基准(分钟)", "group": "主动回复·主动发起"},
    )
    proactive_quiet_minutes: int = field(
        default=120,
        metadata={"description": "冷场节奏基准(分钟)", "group": "主动回复·主动发起"},
    )
    proactive_drift_delay: int = field(
        default=15,
        metadata={"description": "话题结束发起延迟(分钟)", "group": "主动回复·主动发起"},
    )
    proactive_pending_timeout: int = field(
        default=30,
        metadata={"description": "发起接话等待(分钟)", "group": "主动回复·主动发起"},
    )
    proactive_instruction: str = field(
        default="",
        metadata={"description": "发起话题倾向自定义说明", "group": "主动回复·主动发起"},
    )
    proactive_max_message_len: int = field(
        default=300,
        metadata={"description": "发起消息最大长度(字符)", "group": "主动回复·主动发起"},
    )


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
