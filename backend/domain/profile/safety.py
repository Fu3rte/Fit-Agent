"""profile 安全机制最小适配：当前请求的 10 项急性关键词精确子串兜底（讨论总结 §8、REFACTOR_PLAN §6.6）。

Stage 1 只保留这一条语义：对**当前请求**做封闭词表的精确子串扫描，任一命中即应进入
``safety_stop``（不生成计划、提示咨询专业人员）。三条边界：

- 只做精确子串匹配：不做词形还原、同义词扩展、否定语义分析或医学诊断，因此「没有麻木」
  会被保守拦截（讨论总结 §8 已明确接受）。
- 纯函数：不碰 IO、不读库、不依赖 Agent 框架。
- 旧实现的档案限制命中、多层红旗状态与基于三态事实的判定链随 ``ActionRestriction``／
  ``ProfilePatch``／``SessionConditions``／``Fact`` 一并删除；本阶段不新增任何安全语义
  （禁用动作的确定性排除归计划链路的候选过滤，不在本模块）。

词表原词逐字取自 ``MESSAGE_RED_FLAG_TERMS``（10 项，不扩充、不缩写）。
"""

#: 当前请求命中的急性关键词词表（10 项封闭清单，原词不改写）。
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
    """当前请求命中的急性关键词（按词表顺序、精确子串、独立命中）。

    普通肌肉酸痛与无痛且无功能异常的关节异响不得命中：词表不含对应词形。
    """
    return tuple(term for term in MESSAGE_RED_FLAG_TERMS if term in text)
