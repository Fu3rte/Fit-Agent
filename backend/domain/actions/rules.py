"""actions 确定性规则：13 项动作模式词表与写入记录前的口径校验（Stage 1 子任务 02 §4）。

纯规则：不碰 IO、不读数据库、不依赖 Agent 框架（``domain/__init__`` 约束）。三条口径：

- ``MODE_VOCABULARY``：13 项动作模式词表原词，是 ``exercises.modes_json`` 的唯一取值域
  （库内 CHECK 只保证 ``json_valid``，元素级校验只能在这里）。
- ``RECORD_TYPES``／``LOAD_CONVENTIONS``：三类记录口径与五种负重口径，与库内 CHECK 同集合；
  记录侧（``domain.records``）复用本词表，不另造第二套。
- ``validate_record_against_exercise``：写入训练记录前的确定性校验（03 records 复用）——
  动作存在（在 service 层查库）之外，记录口径必须与目录一致；外加负重型必须给出与目录
  相同的负重口径，自重／计时型必须无口径（不虚构 0kg）。
"""

from collections.abc import Sequence

from domain.actions.schema import Exercise, LoadConvention, RecordType

#: 13 项动作模式词表（pre-prj/stage/stage1.md §5 S1-03「已确认的动作模式映射」）。
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

#: 三类记录口径（不新增辅助负重型、不建第四类）。
RECORD_TYPES: tuple[RecordType, ...] = ("reps_weight", "reps_bodyweight", "time")

#: 五种负重口径（仅外加负重类型需要）。
LOAD_CONVENTIONS: tuple[LoadConvention, ...] = (
    "barbell_includes_bar_total",
    "dumbbell_per_hand",
    "machine_pin_displayed_value",
    "plate_loaded_total_excluding_empty",
    "unilateral_setting_per_side",
)


class InvalidMode(ValueError):
    """模式集合为空或含有 13 项词表外的取值：目录数据损坏或调用方越界。"""


class UnknownExercise(ValueError):
    """动作稳定身份不在目录内。"""


class RecordLoadMismatch(ValueError):
    """记录口径或负重口径与目录动作不一致。"""


def validate_modes(modes: Sequence[str]) -> None:
    """校验模式集合：至少一项且全部落在 13 项词表内。"""
    if not modes:
        raise InvalidMode("动作模式集合不能为空")
    unknown = [mode for mode in modes if mode not in MODE_VOCABULARY]
    if unknown:
        raise InvalidMode(f"模式不在已拍 13 项词表内：{unknown}")


def validate_record_against_exercise(
    exercise: Exercise,
    *,
    record_type: RecordType,
    load_convention: LoadConvention | None,
) -> None:
    """写入训练记录前的口径校验：记录口径必须与目录一致，负重口径按记录口径对齐。

    - ``reps_weight``（外加负重）：记录必须给出负重口径，且与目录取值相同。
    - ``reps_bodyweight``／``time``（自重／计时）：记录不得携带任何负重口径。
    口径不符即拒绝，不静默改写成目录口径（不猜、不补默认值）。
    """
    if record_type not in RECORD_TYPES:
        raise RecordLoadMismatch(f"记录口径不在目录三类内：{record_type!r}")
    if record_type != exercise.record_type:
        raise RecordLoadMismatch(
            f"记录口径 {record_type!r} 与目录动作 {exercise.id!r} 的"
            f" {exercise.record_type!r} 不一致"
        )
    if exercise.record_type == "reps_weight":
        if load_convention != exercise.load_convention:
            raise RecordLoadMismatch(
                f"负重口径 {load_convention!r} 与目录动作 {exercise.id!r} 的"
                f" {exercise.load_convention!r} 不一致"
            )
        return
    if load_convention is not None:
        raise RecordLoadMismatch(
            f"{exercise.record_type!r} 型动作 {exercise.id!r} 不得携带负重口径"
        )
