"""插件 LLM 调用模块标识（位于顶层以避免 core/llm 循环导入）。"""

L1_SUMMARIZER = "l1_summarizer"
L2_QUERY_REWRITE = "l2_query_rewrite"
L3_KG_EXTRACTION = "l3_kg_extraction"
IMAGE_CAPTION = "image_caption"
PROFILE_ANALYSIS = "profile_analysis"
PROACTIVE_DECISION_CHIME_IN = "proactive_decision_chime_in"
PROACTIVE_DECISION_FOLLOW_UP = "proactive_decision_follow_up"
PROACTIVE_DECISION_INITIATE = "proactive_decision_initiate"
PROACTIVE_DECISION_WATCH = "proactive_decision_watch"
PROACTIVE_REPLY_CHIME_IN = "proactive_reply_chime_in"
PROACTIVE_REPLY_FOLLOW_UP = "proactive_reply_follow_up"
PROACTIVE_REPLY_INITIATE = "proactive_reply_initiate"
PROACTIVE_REPLY_PASSIVE = "proactive_reply_passive"
FRAMEWORK_REPLY = "framework_reply"

LEGACY_MODULES = frozenset({"default", "scheduled_tasks"})
ALL_LLM_MODULES = frozenset(
    value
    for name, value in globals().copy().items()
    if name.isupper() and isinstance(value, str)
) | LEGACY_MODULES


def proactive_decision_module(motive: str) -> str:
    return {
        "chime_in": PROACTIVE_DECISION_CHIME_IN,
        "follow_up": PROACTIVE_DECISION_FOLLOW_UP,
        "initiate": PROACTIVE_DECISION_INITIATE,
        "watch": PROACTIVE_DECISION_WATCH,
    }.get(motive, f"proactive_decision_{motive or 'unknown'}")


def proactive_reply_module(mode: str) -> str:
    return {
        "chime_in": PROACTIVE_REPLY_CHIME_IN,
        "follow_up": PROACTIVE_REPLY_FOLLOW_UP,
        "passive": PROACTIVE_REPLY_PASSIVE,
        "initiate": PROACTIVE_REPLY_INITIATE,
    }.get(mode, f"proactive_reply_{mode or 'unknown'}")
