"""profile 安全校验：动作限制命中与红旗阻断的本地确定性判定（正本 architecture/02 2.2–2.4）。

S1-05 的输入是三类显式来源：正式档案、拟议补丁后的条件、当次条件。输出是命中的限制与
红旗原因，供调用方阻断处方并提示线下专业评估。六条硬边界：

- **纯规则、不碰 IO**：本模块不读库、不写库、不提交；正式档案读取归
  ``domain.profile.service``。本模块不含记录写入，也不提供任何自动解除红旗的能力。
- **三态读取**：``unknown``（未收集）不得当作「无限制」或「无红旗」；``denied`` 是用户
  明确否认，才可作为「无」参与判定（S1-04 reviewer P2：便捷访问器会把两者折叠，本模块
  一律读 ``Fact`` 三态）。
- **限制判定口径**：动作的模式集合 ∩ 被限制模式集合 ≠ ∅ 即命中（stage1.md §5 S1-03）；
  多归属动作（哑铃上斜卧推＝水平推＋垂直推、自重双杠臂屈伸＝垂直推＋肘伸）按每个归属
  分别命中。限制未命中不等于完整训练安全许可。
- **红旗清单**：6 类已明确红旗任一出现即阻断常规处方并附线下专业评估提示，不输出疾病
  诊断；清单外症状原文只返回未知/需澄清，不判为无红旗或安全放行，也不扩充医学规则
  （2026-09-09 已拍）。分类在**读取时**执行：正式／拟议／当次三来源的身体情况原文
  （``body_conditions``）在每次调用时与清单原词做确定性匹配，存储只保存报告原文
  （2026-09-10 拍板）；只做原词包含匹配，不把前端正则搬进领域层。
- **来源独立**：长期、拟议、当次任一来源出现红旗，另一来源的「无红旗」不能覆盖；限制
  检查通过也不能消除红旗；补丁把红旗改为 denied 同样不能清除正式档案已报告的红旗
  （02 2.3：红旗阻断不因计划修订自动解除）。
- **阻断对象**：处方/指导，不禁止历史读取或表达已发生的训练事实（02 2.3）。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from domain.actions.rules import validate_modes
from domain.actions.schema import Exercise
from domain.profile.rules import (
    apply_patch,
    validate_profile_structure,
    validate_restriction,
    validate_session_conditions,
)
from domain.profile.schema import (
    RED_FLAG_KINDS,
    ActionRestriction,
    Fact,
    FactState,
    Profile,
    ProfilePatch,
    SessionConditions,
)

# 红旗来源：长期档案 / 拟议补丁 / 当次条件（02 2.3、2.4）。
RedFlagSource = Literal["formal_profile", "proposed_patch", "session_conditions"]
RED_FLAG_SOURCES: tuple[RedFlagSource, ...] = (
    "formal_profile",
    "proposed_patch",
    "session_conditions",
)

# 已明确红旗的阻断提示：只提示线下专业评估，不给疾病诊断（02 2.3；S1-05 验收 2）。
RED_FLAG_BLOCK_ADVICE = "存在已明确红旗症状：不生成常规训练处方，建议线下专业评估。"
UNLISTED_RED_FLAG_REASON = "清单外症状原文：只返回未知/需澄清，不判安全"
UNKNOWN_RED_FLAG_REASON = "红旗未收集：不得当作无红旗"
UNKNOWN_RESTRICTION_REASON = "动作限制未收集：不得当作无限制"


@dataclass(frozen=True, slots=True)
class RestrictionHit:
    """一条限制命中一个候选动作。

    ``matched_modes`` 是被限制模式与动作模式集合的交集（具体动作限制恒为空元组），
    用于解释「为什么命中」，也让多归属动作的每个归属可分别核对。
    """

    restriction: ActionRestriction
    exercise_id: str
    standard_name: str
    matched_modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RedFlagFinding:
    """一个来源的一条红旗相关记录：``label`` 为命中的清单原词，清单外时为报告原文。"""

    source: RedFlagSource
    label: str


@dataclass(frozen=True, slots=True)
class RestrictionCheck:
    """动作限制维度结果：``state`` 为正式未收集时恒为 ``unknown``，否则取补丁后条件的三态。

    ``state='unknown'`` 表示正式限制未收集：不得读成「无限制」，命中集为空不代表放行。
    补丁触及限制只改变命中集的计算条件，不把正式未收集升级为「已知无限制」。
    """

    state: FactState
    hits: tuple[RestrictionHit, ...]

    @property
    def is_unknown(self) -> bool:
        return self.state == "unknown"

    @property
    def is_blocked(self) -> bool:
        """是否存在被限制覆盖的候选动作（训练计划不得包含这些动作）。"""
        return bool(self.hits)


@dataclass(frozen=True, slots=True)
class RedFlagCheck:
    """红旗维度结果：已明确红旗、清单外原文、未收集来源三态分离。"""

    confirmed: tuple[RedFlagFinding, ...]
    unlisted: tuple[RedFlagFinding, ...]
    unknown_sources: tuple[RedFlagSource, ...]

    @property
    def is_blocked(self) -> bool:
        """任一来源出现 6 类已明确红旗即阻断常规处方。"""
        return bool(self.confirmed)

    @property
    def needs_clarification(self) -> bool:
        """存在清单外原文或未收集来源：不得据此判为「无红旗」或安全放行。"""
        return bool(self.unlisted or self.unknown_sources)


@dataclass(frozen=True, slots=True)
class SafetyCheckResult:
    """一次本地安全校验结果（不写档案、不增删永久限制、不解除红旗）。

    只暴露「命中限制」与「红旗/未知」两类结论，不提供 ``is_safe`` 之类的完整安全许可
    字段：限制未命中且红旗未收集仍然 ``needs_clarification``。
    """

    restrictions: RestrictionCheck
    red_flags: RedFlagCheck

    @property
    def is_blocked(self) -> bool:
        """命中限制或出现已明确红旗：调用方据此阻断处方/指导。"""
        return self.restrictions.is_blocked or self.red_flags.is_blocked

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification_reasons)

    @property
    def clarification_reasons(self) -> tuple[str, ...]:
        """未收集限制、未收集红旗与清单外症状的「未知/需澄清」原因（可空）。"""
        reasons: list[str] = []
        if self.restrictions.is_unknown:
            reasons.append(UNKNOWN_RESTRICTION_REASON)
        for source in self.red_flags.unknown_sources:
            reasons.append(f"{source}：{UNKNOWN_RED_FLAG_REASON}")
        for finding in self.red_flags.unlisted:
            reasons.append(
                f"{finding.source}：{UNLISTED_RED_FLAG_REASON}（{finding.label}）"
            )
        return tuple(reasons)

    @property
    def advice(self) -> tuple[str, ...]:
        """阻断提示：仅在存在已明确红旗时给出线下专业评估建议。"""
        return (RED_FLAG_BLOCK_ADVICE,) if self.red_flags.is_blocked else ()


def check_action_restrictions(
    restrictions: Iterable[ActionRestriction],
    candidate_actions: Sequence[Exercise],
) -> tuple[RestrictionHit, ...]:
    """返回候选动作中被限制覆盖的命中项（口径：模式集合交集非空即命中）。

    限制与动作模式先经共享词表校验：越界模式是结构错误，不静默当作「不会命中」。
    输出顺序为候选动作顺序、限制顺序，便于调用方稳定比对。
    """
    limited = tuple(restrictions)
    for restriction in limited:
        validate_restriction(restriction)
    for exercise in candidate_actions:
        if not isinstance(exercise, Exercise):
            raise TypeError(
                f"候选动作必须是目录身份 Exercise：{type(exercise).__name__}"
            )
        validate_modes(exercise.modes)
    hits: list[RestrictionHit] = []
    for exercise in candidate_actions:
        for restriction in limited:
            if restriction.scope == "specific_action":
                if restriction.target == exercise.id:
                    hits.append(
                        RestrictionHit(
                            restriction=restriction,
                            exercise_id=exercise.id,
                            standard_name=exercise.standard_name_zh,
                        )
                    )
                continue
            matched = tuple(
                mode for mode in exercise.modes if mode == restriction.target
            )
            if matched:
                hits.append(
                    RestrictionHit(
                        restriction=restriction,
                        exercise_id=exercise.id,
                        standard_name=exercise.standard_name_zh,
                        matched_modes=matched,
                    )
                )
    return tuple(hits)


def assess_red_flags(
    formal: Fact[tuple[str, ...]],
    proposed: Fact[tuple[str, ...]] | None = None,
    session: Fact[tuple[str, ...]] | None = None,
) -> RedFlagCheck:
    """按来源独立评估红旗：任一来源的「无红旗」都不覆盖其他来源的红旗。

    三个入参都是**身体情况报告原文**（``body_conditions``）三态事实：正式档案、拟议补丁、
    当次条件。每次调用都在此做 6 类清单原词匹配（存储层不分类，2026-09-10 拍板）：一条原文
    命中清单原词即计入 ``confirmed``（标签取清单原词），未命中任何原词的原文计入
    ``unlisted``（标签取原文），清单外内容不判安全。

    ``proposed``／``session`` 为 None 表示该来源本次未提供，不计入未收集；``unknown``
    来源记入 ``unknown_sources``（需澄清），``denied`` 来源视为用户明确否认，``known``
    空元组表示已收集且无报告。
    """
    sources: tuple[tuple[RedFlagSource, Fact[tuple[str, ...]] | None], ...] = (
        ("formal_profile", formal),
        ("proposed_patch", proposed),
        ("session_conditions", session),
    )
    confirmed: list[RedFlagFinding] = []
    unlisted: list[RedFlagFinding] = []
    unknown_sources: list[RedFlagSource] = []
    for source, fact in sources:
        if fact is None:
            continue
        if not isinstance(fact, Fact):
            raise TypeError(f"红旗来源 {source} 不是三态事实：{fact!r}")
        if fact.is_unknown:
            unknown_sources.append(source)
            continue
        if fact.is_denied:
            continue
        labels = fact.value
        if not isinstance(labels, tuple):
            raise TypeError(f"红旗来源 {source} 的 known 值需要文本元组：{labels!r}")
        for label in labels:
            if not isinstance(label, str):
                raise TypeError(f"红旗来源 {source} 的报告原文需要文本：{label!r}")
            matched = tuple(kind for kind in RED_FLAG_KINDS if kind in label)
            if matched:
                confirmed.extend(
                    RedFlagFinding(source=source, label=kind) for kind in matched
                )
            else:
                unlisted.append(RedFlagFinding(source=source, label=label))
    return RedFlagCheck(
        confirmed=tuple(confirmed),
        unlisted=tuple(unlisted),
        unknown_sources=tuple(unknown_sources),
    )


def evaluate_safety(
    profile: Profile,
    candidate_actions: Sequence[Exercise],
    *,
    patch: ProfilePatch | None = None,
    session: SessionConditions | None = None,
) -> SafetyCheckResult:
    """对候选动作集合做本地确定性安全校验，输入三类来源、输出命中与阻断原因。

    - ``profile``：正式档案；限制命中按**补丁后**条件计算（01 1.4：不能只按旧条件校验），
      红旗则按正式／拟议／当次三来源的身体情况原文独立评估（分类在读取时执行）。
    - ``patch``：拟议长期补丁（纯内存应用，不改输入对象、不落库）；补丁删除限制后按删除后
      条件判定，生效与版本推进归 Stage 2 确认事务（01 1.4）。
    - ``session``：当次条件；当前只承载当次身体情况，器械条件不构成动作限制（02 2.2、2.4）。

    限制状态：正式未收集（``unknown``）时结果恒为未收集；否则取补丁后条件的三态。补丁触及
    限制只改变命中集，不把「未收集」升级成「已知无限制」，也不当 denied 放行。

    交接注意：``domain.profile.rules.apply_patch`` 在补丁触及限制时把该字段写成 ``known``；
    直接读 ``preview_patch(...).action_restrictions`` 的调用方不得把它当「已知无限制」，
    必须另判正式档案的三态（本函数的 ``state`` 已如此处理）。

    本函数只读：不写档案、不新增/删除永久限制、不解除红旗。
    """
    validate_profile_structure(profile)
    if patch is not None:
        effective = apply_patch(profile, patch)
    else:
        effective = profile
    if session is not None:
        validate_session_conditions(session)
    formal_restriction_fact = profile.action_restrictions
    restriction_fact = effective.action_restrictions
    restrictions: tuple[ActionRestriction, ...] = (
        restriction_fact.value
        if restriction_fact.is_known and restriction_fact.value is not None
        else ()
    )
    hits = check_action_restrictions(restrictions, candidate_actions)
    proposed_body_conditions = (
        None if patch is None else patch.facts.get("body_conditions")
    )
    session_body_conditions = None if session is None else session.body_conditions
    return SafetyCheckResult(
        restrictions=RestrictionCheck(
            state=(
                "unknown"
                if formal_restriction_fact.is_unknown
                else restriction_fact.state
            ),
            hits=hits,
        ),
        red_flags=assess_red_flags(
            profile.body_conditions, proposed_body_conditions, session_body_conditions
        ),
    )
