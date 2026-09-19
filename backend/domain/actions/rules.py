"""actions 确定性规则：13 项动作模式词表与写入记录前的口径校验。"""

from collections.abc import Sequence

from domain.actions.schema import Exercise, LoadConvention, RecordType

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

RECORD_TYPES: tuple[RecordType, ...] = ("reps_weight", "reps_bodyweight", "time")

LOAD_CONVENTIONS: tuple[LoadConvention, ...] = (
    "barbell_includes_bar_total",
    "dumbbell_per_hand",
    "machine_pin_displayed_value",
    "plate_loaded_total_excluding_empty",
    "unilateral_setting_per_side",
    "external_added_weight",
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
    """写入训练记录前的口径校验：记录口径必须与目录一致，负重口径按记录口径对齐。"""
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
