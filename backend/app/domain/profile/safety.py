"""profile 安全机制最小适配：当前请求的 10 项急性关键词精确子串兜底。"""

MESSAGE_RED_FLAG_TERMS: tuple[str, ...] = (
    "胸部异常不适",
    "晕厥",
    "异常气短",
    "锐痛",
    "麻木",
    "放射痛",
    "疼痛持续加重",
    "明显肿胀",
    "卡锁",
    "关节失稳",
)


def message_red_flag_hits(text: str) -> tuple[str, ...]:
    """当前请求命中的急性关键词（按词表顺序、精确子串、独立命中）。"""
    return tuple(term for term in MESSAGE_RED_FLAG_TERMS if term in text)
