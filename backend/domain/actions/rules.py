"""actions 确定性规则：动作模式词表、记录口径与负重口径校验（正本 architecture/03）。

共享契约（S1-04 限制匹配、S1-05 模式限制判定复用本模块，不各自发明词表）：

- ``MODE_VOCABULARY``：13 项动作模式词表，逐字取自 stage1.md §5 S1-03
  「已确认的动作模式映射」。
- ``CATALOG_STANDARD_NAMES``：已拍首批 24 项中文标准名原词（目录清单，不是训练
  计划，也不代表已通过可推荐检查；2026-09-09 拍定移除「哑铃分腿蹲」）。
- ``EXERCISE_MODES``：标准名 → 动作模式集合；单一主模式归属，仅跨界高复合型
  动作多归属（哑铃上斜卧推＝水平推＋垂直推；自重双杠臂屈伸＝垂直推＋肘伸）。
- ``RECORD_TYPES`` / ``LOAD_CONVENTIONS``：三类记录口径与五种负重口径。

本模块是纯规则：不碰 IO、不读数据库、不依赖 Agent 框架（domain/__init__ 约束）。
限制判定口径为「动作的模式集合 ∩ 被限制模式集合 ≠ ∅ 即阻断」，判定函数归 S1-05。
"""

# 13 项模式词表（stage1.md §5 S1-03「已确认的动作模式映射」）。
MODE_VOCABULARY: tuple[str, ...] = (
    "深蹲",
    "髋铰链",
    "水平推",
    "垂直推",
    "水平拉",
    "垂直拉",
    "肩孤立",
    "膝屈",
    "膝伸",
    "小腿（踝跖屈）",
    "肘屈",
    "肘伸",
    "核心",
)

# 已拍首批清单（2026-09-09 追加「保加利亚分腿蹲」、同日拍定移除「哑铃分腿蹲」，共 24 项）。
CATALOG_STANDARD_NAMES: tuple[str, ...] = (
    "杠铃背蹲",
    "杠铃传统硬拉",
    "杠铃罗马尼亚硬拉",
    "45°腿举",
    "保加利亚分腿蹲",
    "杠铃平板卧推",
    "哑铃平板卧推",
    "哑铃上斜卧推",
    "坐姿哑铃肩推",
    "哑铃侧平举",
    "哑铃反向飞鸟",
    "杠铃俯身划船",
    "坐姿绳索划船",
    "单臂哑铃划船",
    "高位下拉",
    "自重引体向上",
    "坐姿腿弯举",
    "腿屈伸",
    "器械站姿提踵",
    "哑铃弯举",
    "绳索下压",
    "绳索过顶臂屈伸",
    "自重双杠臂屈伸",
    "悬垂举腿",
)

# 标准名 → 模式集合；仅两处多归属（见模块 docstring）。
EXERCISE_MODES: dict[str, tuple[str, ...]] = {
    "杠铃背蹲": ("深蹲",),
    "杠铃传统硬拉": ("髋铰链",),
    "杠铃罗马尼亚硬拉": ("髋铰链",),
    "45°腿举": ("深蹲",),
    "保加利亚分腿蹲": ("深蹲",),
    "杠铃平板卧推": ("水平推",),
    "哑铃平板卧推": ("水平推",),
    "哑铃上斜卧推": ("水平推", "垂直推"),
    "坐姿哑铃肩推": ("垂直推",),
    "哑铃侧平举": ("肩孤立",),
    "哑铃反向飞鸟": ("肩孤立",),
    "杠铃俯身划船": ("水平拉",),
    "坐姿绳索划船": ("水平拉",),
    "单臂哑铃划船": ("水平拉",),
    "高位下拉": ("垂直拉",),
    "自重引体向上": ("垂直拉",),
    "坐姿腿弯举": ("膝屈",),
    "腿屈伸": ("膝伸",),
    "器械站姿提踵": ("小腿（踝跖屈）",),
    "哑铃弯举": ("肘屈",),
    "绳索下压": ("肘伸",),
    "绳索过顶臂屈伸": ("肘伸",),
    "自重双杠臂屈伸": ("垂直推", "肘伸"),
    "悬垂举腿": ("核心",),
}

# 三类记录口径（03「本章已拍结论」）：不新增辅助负重型、不建第四类。
RECORD_TYPES: tuple[str, ...] = ("reps_weight", "reps_bodyweight", "time")

# 五种负重口径（stage1.md §5 S1-03「已确认的负重口径」）；仅负重次数型需要。
LOAD_CONVENTIONS: tuple[str, ...] = (
    "barbell_includes_bar_total",
    "dumbbell_per_hand",
    "machine_pin_displayed_value",
    "plate_loaded_total_excluding_empty",
    "unilateral_setting_per_side",
)


class UnknownCatalogExercise(ValueError):
    """标准名不在已拍 24 项清单内：拒绝凭相似名推断模式归属。"""


class InvalidMode(ValueError):
    """模式不在 13 项已拍词表内。"""


def modes_for(standard_name: str) -> tuple[str, ...]:
    """返回标准名的模式集合；不在清单内抛 UnknownCatalogExercise（不猜、不替换）。"""
    try:
        return EXERCISE_MODES[standard_name]
    except KeyError as exc:
        raise UnknownCatalogExercise(
            f"标准名不在已拍首批清单内：{standard_name}"
        ) from exc


def validate_modes(modes: tuple[str, ...] | list[str]) -> None:
    """校验模式集合：至少一项且全部落在 13 项词表内。"""
    if not modes:
        raise InvalidMode("动作模式集合不能为空")
    unknown = [mode for mode in modes if mode not in MODE_VOCABULARY]
    if unknown:
        raise InvalidMode(f"模式不在已拍 13 项词表内：{unknown}")


# 按定义单侧（单腿／单臂）的清单动作：口径要求区分左右、次数按每侧记录。
UNILATERAL_STANDARD_NAMES: tuple[str, ...] = (
    "保加利亚分腿蹲",
    "单臂哑铃划船",
)


def is_unilateral(standard_name: str) -> bool:
    """单侧动作（单腿／单臂类）：口径要求区分左右、次数按每侧记录。

    仅覆盖已拍清单内按定义单侧的动作；「单侧设定值」负载口径适用于单侧器械
    动作，本函数不推断某台器械是否为单侧配重。
    """
    if standard_name not in EXERCISE_MODES:
        raise UnknownCatalogExercise(f"标准名不在已拍首批清单内：{standard_name}")
    return standard_name in UNILATERAL_STANDARD_NAMES
