"""Stage 5：§3.3／§3.4 的代码前断言（子任务 01）、调整计划的图行为（子任务 03）与确认 resume／
唯一 draft 兜底（子任务 04）。

依据：``refactor-log/stage5.md`` §3.1–§3.4／§3.9／§6 Subtask 01／§6 Subtask 03／§6 Subtask 04／
§7.2–§7.4；
``LANGGRAPH_REFACTOR_PLAN.md`` §5.6／§7.2／§9.2；``Fit-Agent-LangGraph-重构讨论总结.md`` §3.3／§3.4。

上半部分（子任务 01）只冻结契约：确认 resume／唯一 draft 兜底的两条路径、请求 ``plan_id`` 与
interrupt／draft 身份必须相等、调整计划的 active 前置、``source_plan_id`` 来源、同类 regenerate 规则、
progression-aware 校验由 Subtask 02–04 落地。已合入源码侧的交叉断言只读 Stage 5 不得改动的既有事实：
``resolve_progression`` 的输入面与四类决策、``PlanDraft``／``Plan`` 的既有字段、
``backend/skills/plan-adjustment`` 已存在、``WorkflowState`` 仍是 11 字段。

中间部分（子任务 03）用固定替身驱动 ``graph/workflow.py::invoke_agent_run`` 的调整分支：无 active 在
Planner 前失败、预读并解析 active、PB 只装配 active 涉及动作、加载 ``plan-adjustment``、注入确定性
渐进决策、``source_plan_id`` 等于当前 active、未受证据影响的训练日／动作／处方原样保留、一次修订上限
与安全优先仍然成立。

末尾部分（子任务 04）用同一套替身驱动 ``graph/workflow.py::invoke_confirmation``：图停在确认 interrupt
且 interrupt ID 等于请求 ``plan_id`` 时按 ``Command(resume=...)`` 进入确认分支，重启（重开同一存档文件）
后仍能恢复；没有 checkpoint／无法恢复／已完成时按业务库唯一 draft 兜底；两条路径都经同一个
``PlanActivationService``（计数替身取证），重复请求到达领域幂等矩阵；身份不一致明确冲突且不写任何行。

计划事实（active 计划行、日程、关联训练）用 ``tmp_path`` 下真实迁移库直接 SQL 预置；图行为经唯一运行
入口与唯一确认入口，模型是可脚本化的固定替身。全部行为不调真实模型、不需要任何 ``MODEL_*`` 环境变量；
Router 的确定性命中与严格枚举行为在 ``tests/test_stage5_router.py``。
"""

import inspect
import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from langgraph.types import Command

from domain.actions.service import ActionCatalogService
from domain.plans.rules import ProgressionDecision, resolve_progression
from domain.plans.schema import (
    EvaluationResult,
    Plan,
    PlanDraft,
    plan_draft_from_json,
)
from domain.plans.service import (
    PlanActivationConflict,
    PlanActivationService,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from domain.stats.service import StatsService
from graph.checkpointer import open_checkpointer, thread_config
from graph.context import MemoryAssembler
from graph.nodes import (
    ADJUSTMENT_PLANNER_SYSTEM_PROMPT,
    EVALUATOR_SYSTEM_PROMPT,
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanRun,
    ModelRequestBudget,
    RequiredActivePlanMissing,
)
from graph.router import ROUTER_SYSTEM_PROMPT
from graph.skills import LoadedSkill, SkillLoader
from graph.state import WorkflowState
from graph.workflow import (
    AgentRunDeps,
    AgentRunResult,
    ConfirmationAction,
    _route_after_confirmation,
    build_generate_plan_graph,
    invoke_agent_run,
    invoke_confirmation,
)
from storage.db import Database
from tests.conftest import Stage5Plan

BACKEND_ROOT = Path(__file__).resolve().parents[1]

#: §3.3 确认恢复协议四步：checkpoint 优先、唯一 draft 兜底、同一服务、服务内幂等（总计划 §5.6）。
CONFIRMATION_STEPS = (
    "1. **优先**用 `conversation_id` 作 `thread_id` 读取 checkpoint；仅当图确实停在确认 interrupt，且 interrupt 中的 `draft_plan_id` 与请求 `plan_id` 相等时，才用 `Command(resume=...)` 进入确认分支；不相等则明确冲突；",
    "2. checkpoint 不存在、无法恢复或已无等待任务时，读取业务库唯一 `plans.status='draft'`；它也必须与请求 `plan_id` 相等，否则明确冲突；",
    "3. 两条路径最终调用**同一个** `PlanActivationService` 的 confirm/reject 方法；重复请求不能只返回旧 checkpoint State 而绕过领域幂等服务；",
    "4. 服务内按 §3.2 幂等，禁止双路径同时提交。",
)

#: §3.3 收尾：resume 载荷只有动作与身份，不复制计划／评估／业务事实。
CONFIRMATION_RESUME_PAYLOAD_RULE = (
    "resume 载荷只携带动作与 `plan_id`，不复制完整计划、评估或业务事实。请求 `plan_id` 是唯一操作目标。"
)

#: §3.4 调整计划六条：active 前置、上下文与 Skill、progression-aware 校验、source 与不变量、
#: 已有 draft 的普通请求规则、同类 regenerate 规则。
ADJUST_BULLETS = (
    "- 无 active：`require_active_plan` 在 Planner 前明确失败；不调 Planner、不写 draft/rejected。",
    "- 有 active：先按统一 `PlanDraft` 解析其 `structured_content`，提取去重且顺序稳定的 `exercise_id`，再调用 `load_context(..., exercise_ids=...)`；`load_skill` 加载 `plan-adjustment`。其余 safety/planner/evaluator/一次修订/wait 与生成共用，不建第二套 Planner Agent。",
    "- 调整候选必须以 active 的目标组数、次数区间、目标负荷和关联日程训练为输入调用 `resolve_progression`；确定性校验按该决策验证加重、保持、回退或待校准，不能再用“必须等于最近工作组重量”的生成规则拒绝合法调整。",
    "- 新调整 draft 的 `source_plan_id` 必须等于本次读取的 active 计划 id；未被调整证据推翻的训练日、动作和处方保持不变，并由固定案例行为测试验证。",
    "- 已有 draft 且 `regenerate` 缺省/false：generate 仅在该 draft 的 `source_plan_id IS NULL` 时返回既有 draft；adjust 一律明确失败；跨类型请求明确冲突。以上路径均不调模型、不写第二条 draft。",
    "- `regenerate=true` 只允许同类替换：generate 只能替换 `source_plan_id IS NULL` 的 generate draft；adjust 只能在 draft 的 `source_plan_id == 当前 active.id` 时替换同一 id/version，并保持该 `source_plan_id`。跨类型或来源 active 已变化时明确冲突；没有 draft 时按本次 intent 正常生成。",
)

#: §3.9：调整／regenerate 信号不进冻结 State，而进 ``GeneratePlanRun`` 运行上下文。
STATE_BOUNDARY_RULE = "保持 `WorkflowState` 11 字段；adjust/regenerate 信号和预读 active 身份放 `GeneratePlanRun` 运行上下文，不复制业务事实到 State。"

#: ``resolve_progression`` 的输入面：目标组数、次数区间、目标负荷与关联日程训练（§3.4 第 3 条）。
_PROGRESSION_INPUTS = (
    "target_sets",
    "reps_min",
    "reps_max",
    "target_load_kg",
    "linked_workout_session_ids",
)

#: 调整计划的四类确定性决策（加重／保持／回退／待校准）。
_PROGRESSION_DECISIONS = {"increase", "keep", "regress", "needs_calibration"}

#: 确认协议与调整计划的编号／条目行（整段相等断言用）。
_NUMBERED_STEP = re.compile(r"\d+\. ")


def test_confirmation_protocol_steps_are_frozen_and_ordered(stage5_plan: Stage5Plan) -> None:
    """§3.3 四步顺序冻结：checkpoint 优先、唯一 draft 兜底、同一服务、禁止双路径同时提交。"""
    section = stage5_plan.section("3.3 确认恢复协议")

    steps = tuple(line for line in section.splitlines() if _NUMBERED_STEP.match(line))
    assert steps == CONFIRMATION_STEPS


def test_confirmation_requires_request_plan_id_to_equal_the_interrupt_or_draft_id(
    stage5_plan: Stage5Plan,
) -> None:
    """§3.3：请求 ``plan_id`` 必须等于 interrupt 的 ``draft_plan_id``，或兜底读到的唯一 draft id。"""
    section = stage5_plan.section("3.3 确认恢复协议")

    assert CONFIRMATION_STEPS[0] in section
    assert CONFIRMATION_STEPS[1] in section
    assert "请求 `plan_id` 是唯一操作目标" in section
    # ``conversation_id`` 就是 Checkpointer 的 ``thread_id``（Stage 3 冻结，Stage 5 不另建映射）。
    assert thread_config("stage5-confirmation") == {
        "configurable": {"thread_id": "stage5-confirmation"}
    }


def test_confirmation_paths_share_one_activation_service_and_never_bypass_idempotency(
    stage5_plan: Stage5Plan,
) -> None:
    """§3.3：两条读取路径调用同一个 ``PlanActivationService``，重复请求不得绕过领域幂等服务。"""
    section = stage5_plan.section("3.3 确认恢复协议")

    assert CONFIRMATION_STEPS[2] in section
    assert CONFIRMATION_STEPS[3] in section
    assert CONFIRMATION_RESUME_PAYLOAD_RULE in section


def test_adjust_plan_bullets_are_frozen_and_counted(stage5_plan: Stage5Plan) -> None:
    """§3.4 六条整段相等：无 active 前置、active 解析与上下文过滤、progression 校验、来源与不变量、两条 draft 规则。"""
    section = stage5_plan.section("3.4 调整计划")

    bullets = tuple(line for line in section.splitlines() if line.startswith("- "))
    assert bullets == ADJUST_BULLETS


def test_adjust_requires_an_active_plan_before_the_planner(stage5_plan: Stage5Plan) -> None:
    """§3.4：无 active 时在 Planner 前明确失败，不调 Planner、不写 draft／rejected。"""
    assert ADJUST_BULLETS[0] in stage5_plan.section("3.4 调整计划")


def test_adjust_reuses_the_active_plan_and_the_plan_adjustment_skill(stage5_plan: Stage5Plan) -> None:
    """§3.4：先解析 active 的 ``PlanDraft`` 取稳定 ``exercise_id`` 过滤上下文，Skill 加载 ``plan-adjustment``。"""
    assert ADJUST_BULLETS[1] in stage5_plan.section("3.4 调整计划")
    assert (BACKEND_ROOT / "skills" / "plan-adjustment" / "SKILL.md").is_file()


def test_adjust_validation_is_progression_aware(stage5_plan: Stage5Plan) -> None:
    """§3.4：以 active 的目标组数／次数区间／目标负荷与关联日程训练调用 ``resolve_progression``，
    确定性校验按加重／保持／回退／待校准四类决策判断，不再用生成计划的最近工作组规则拒绝合法调整。"""
    assert ADJUST_BULLETS[2] in stage5_plan.section("3.4 调整计划")
    assert set(_PROGRESSION_INPUTS) <= set(inspect.signature(resolve_progression).parameters)
    assert set(get_args(ProgressionDecision.__annotations__["action"])) == _PROGRESSION_DECISIONS


def test_adjust_draft_points_at_the_active_plan_and_keeps_untouched_content(
    stage5_plan: Stage5Plan,
) -> None:
    """§3.4：新调整 draft 的 ``source_plan_id`` 等于本次读取的 active id，未受证据影响的训练日／
    动作／处方保持不变。"""
    assert ADJUST_BULLETS[3] in stage5_plan.section("3.4 调整计划")
    assert "source_plan_id" in {field.name for field in fields(Plan)}


def test_existing_draft_and_regenerate_rules_are_same_kind_only(stage5_plan: Stage5Plan) -> None:
    """§3.4：已有 draft 时普通请求不调模型、不写第二条 draft；``regenerate=true`` 只允许同类替换。"""
    section = stage5_plan.section("3.4 调整计划")

    assert ADJUST_BULLETS[4] in section
    assert ADJUST_BULLETS[5] in section


def test_adjust_and_regenerate_signals_stay_out_of_the_frozen_state(stage5_plan: Stage5Plan) -> None:
    """§3.9：``WorkflowState`` 仍是 11 字段；adjust／regenerate 信号进运行上下文，不进 State。"""
    assert STATE_BOUNDARY_RULE in stage5_plan.section("3.9 模型与 State 边界")
    assert len(WorkflowState.__annotations__) == 11


# ---------- §3.4 调整分支的图行为（Subtask 03） ----------

BUSINESS_DAY = date(2026, 6, 1)
FIXED_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
CREATED_AT = FIXED_NOW.isoformat()
CONVERSATION_ID = "stage5-adjust-conversation"
SQUAT = "barbell-back-squat"  # 外加负重动作：目录加重单位 2.5kg
PULL_UP = "pull-up"  # 纯自重动作：处方不携带负荷
BENCH_PRESS = "barbell-bench-press"  # 不属于 active 计划：PB 过滤的对照
TARGET_LOAD_KG = 50.0
INCREASE_LOAD_KG = 52.5  # 目标负荷 ＋ 一次目录加重单位
TARGET_SETS = 3
REPS_MIN = 8
REPS_MAX = 12
WEEKLY_FREQUENCY = 2
ACTIVE_STARTS_ON = date(2026, 5, 1)
ACTIVE_DAYS = (date(2026, 5, 2), date(2026, 5, 5))
EXPLANATION = "固定案例：按当前 active 与关联训练调整"
ADJUST_REQUEST = "调整计划"
#: 确认场景的训练日窗口：``starts_on`` 不早于业务日，否则激活会被 ``PlanDraftStale`` 拒绝（§3.1）。
CONFIRM_STARTS_ON = date(2026, 6, 2)
CONFIRM_DAYS = (date(2026, 6, 2), date(2026, 6, 5))
#: 激活／归档写入的时间戳：节点与确认入口都取注入时钟 ``FIXED_NOW``（节点不读系统时钟）。
CONFIRMED_AT = FIXED_NOW.isoformat()


def _squat(load_kg: float) -> dict[str, Any]:
    """active／候选里的 squat：外加负重 3 组 8–12 次，负荷来源写固定的组身份（只比重量）。"""
    return {
        "exercise_id": SQUAT,
        "sets": TARGET_SETS,
        "prescription": {
            "type": "weighted_reps",
            "reps_min": REPS_MIN,
            "reps_max": REPS_MAX,
            "progression_note": None,
            "load": {
                "status": "known",
                "weight_kg": load_kg,
                "source_workout_session_id": 1,
                "source_set_no": 1,
            },
        },
    }


def _pull_up() -> dict[str, Any]:
    """active／候选里的 pull-up：纯自重处方不携带负荷，未受调整证据影响时必须原样沿用。"""
    return {
        "exercise_id": PULL_UP,
        "sets": TARGET_SETS,
        "prescription": {
            "type": "bodyweight_reps",
            "reps_min": REPS_MIN,
            "reps_max": REPS_MAX,
            "progression_note": None,
        },
    }


def _content(
    load_kg: float,
    *,
    starts_on: date = ACTIVE_STARTS_ON,
    days: tuple[date, date] = ACTIVE_DAYS,
) -> dict[str, Any]:
    """一份结构合法的统一计划内容：训练日数量恰等于每周训练次数。

    默认日期就是预置 active 的日期（均为业务日之前的历史）；激活用例必须传 ``starts_on`` 不早于业务日
    的窗口，否则 draft 会被 §3.1 的日期新鲜度拒绝。
    """
    return {
        "goal": "增肌",
        "starts_on": starts_on.isoformat(),
        "explanation": EXPLANATION,
        "weekly_frequency": WEEKLY_FREQUENCY,
        "training_days": [
            {
                "scheduled_on": days[0].isoformat(),
                "exercises": [_squat(load_kg)],
            },
            {
                "scheduled_on": days[1].isoformat(),
                "exercises": [_pull_up()],
            },
        ],
    }


def _plan_text(content: Mapping[str, Any]) -> str:
    """固定替身 Planner 的响应文本。"""
    return json.dumps(content, ensure_ascii=False)


def _intent_text(intent: str) -> str:
    """固定替身 Router 的分类响应（严格形状）。"""
    return json.dumps({"intent": intent}, ensure_ascii=False)


def _rubric_text(*, goal_alignment: bool = True) -> str:
    """固定替身 Evaluator 的三个布尔判定。"""

    def verdict(passed: bool) -> dict[str, Any]:
        return {"passed": passed, "reason": "固定替身判定"}

    return json.dumps(
        {
            "goal_alignment": verdict(goal_alignment),
            "schedule_reasonableness": verdict(True),
            "explanation_quality": verdict(True),
        },
        ensure_ascii=False,
    )


def _increase_decision() -> dict[str, Any]:
    """两次关联训练都在目标负荷上做满次数上限时的渐进决策：按目录加重单位递增一次。"""
    return {
        "exercise_id": SQUAT,
        "sets": TARGET_SETS,
        "reps_min": REPS_MIN,
        "reps_max": REPS_MAX,
        "target_load_kg": TARGET_LOAD_KG,
        "decision": {"action": "increase", "load_kg": INCREASE_LOAD_KG},
    }


class ScriptedModel:
    """按系统提示词分派固定响应的模型替身：没有脚本的提示词被调用即失败。"""

    def __init__(self, scripts: Mapping[str, Sequence[str]] | None = None) -> None:
        self._scripts = {
            prompt: list(items) for prompt, items in (scripts or {}).items()
        }
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system_prompt: str, user_payload: str) -> str:
        self.calls.append((system_prompt, user_payload))
        script = self._scripts.get(system_prompt)
        if script is None:
            raise AssertionError(
                f"固定替身模型收到未脚本化的系统提示词：{system_prompt[:40]!r}"
            )
        if not script:
            raise AssertionError("固定替身模型没有更多脚本响应")
        return script.pop(0)

    def calls_for(self, system_prompt: str) -> list[tuple[str, str]]:
        """某一类调用的原始记录（Router／调整 Planner／Evaluator 各一套）。"""
        return [call for call in self.calls if call[0] == system_prompt]

    def payload(self, system_prompt: str, index: int = 0) -> dict[str, Any]:
        """第 ``index`` 次该类调用的用户载荷（JSON 文本解码）。"""
        return json.loads(self.calls_for(system_prompt)[index][1])


class CountingSkillLoader(SkillLoader):
    """Skill 加载计数替身：加载正文的名称顺序即证据。"""

    def __init__(self) -> None:
        super().__init__()
        self.loaded: list[str] = []

    def load(self, name: str) -> LoadedSkill:
        self.loaded.append(name)
        return super().load(name)


class CountingAssembler(MemoryAssembler):
    """MemoryAssembler 计数替身：同时记录每次装配的 ``exercise_ids``（PB 过滤口径的证据）。"""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.assemblies = 0
        self.exercise_ids: list[Sequence[str] | None] = []

    async def assemble(
        self,
        request: str,
        *,
        business_day: date,
        exercise_ids: Sequence[str] | None = None,
    ) -> Any:
        self.assemblies += 1
        self.exercise_ids.append(exercise_ids)
        return await super().assemble(
            request, business_day=business_day, exercise_ids=exercise_ids
        )


class CountingPersistence(PlanPersistenceService):
    """持久化计数替身：失败路径（无 active、安全终止）不得有任何业务写入。"""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.writes: list[EvaluationResult] = []

    async def persist_plan_result(
        self,
        draft: PlanDraft,
        evaluation: EvaluationResult,
        *,
        existing_draft_id: int | None,
        created_at: str,
        source_plan_id: int | None = None,
    ) -> Any:
        self.writes.append(evaluation)
        return await super().persist_plan_result(
            draft,
            evaluation,
            existing_draft_id=existing_draft_id,
            created_at=created_at,
            source_plan_id=source_plan_id,
        )


class CountingActivation(PlanActivationService):
    """激活服务计数替身：确认／拒绝的两条读取路径必须都经过同一个领域服务（§3.3 第 3 条）。"""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.calls: list[tuple[str, int]] = []

    async def activate(
        self,
        plan_id: int,
        *,
        business_day: date,
        confirmed_at: str,
        archived_at: str,
    ) -> Plan:
        self.calls.append(("activate", plan_id))
        return await super().activate(
            plan_id,
            business_day=business_day,
            confirmed_at=confirmed_at,
            archived_at=archived_at,
        )

    async def reject(self, plan_id: int, *, archived_at: str) -> Plan:
        self.calls.append(("reject", plan_id))
        return await super().reject(plan_id, archived_at=archived_at)


def _profile() -> Profile:
    """固定画像：每周训练次数与 active 计划一致（Planner 前前提）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(WEEKLY_FREQUENCY),
        available_equipment=Fact.known(("barbell", "bodyweight")),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.denied(),
    )


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(
    db: Database,
    *,
    version: int,
    status: str,
    content: Mapping[str, Any],
    confirmed_at: str | None = None,
    source_plan_id: int | None = None,
) -> int:
    """直接 SQL 预置一个计划版本行（正式写入入口是被测服务），返回计划身份。

    ``source_plan_id`` 非空即一条调整 draft：激活再校验按该 active 的渐进决策判定负荷（§3.1）。
    """
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                version,
                status,
                source_plan_id,
                json.dumps(content, ensure_ascii=False),
                CREATED_AT,
                confirmed_at,
            ),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_session(db: Database, *, plan_id: int, scheduled_on: date) -> int:
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plan_sessions (plan_id, scheduled_on, cancelled_at)"
            " VALUES (?, ?, NULL)",
            (plan_id, scheduled_on.isoformat()),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _insert_squat_training(
    db: Database,
    *,
    performed_on: date,
    plan_session_id: int | None,
    weight_kg: float,
    reps: int,
) -> None:
    """预置一次 squat 训练（3 个有效工作组）：``plan_session_id`` 非空即「关联日程训练」。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, ?)",
            (performed_on.isoformat(), plan_session_id),
        )
        session_id = int(cursor.lastrowid or 0)
        await cursor.close()
        for set_no in (1, 2, 3):
            await conn.execute(
                "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no,"
                " set_type, load_convention, weight_kg, reps)"
                " VALUES (?, ?, ?, 'work', 'barbell_includes_bar_total', ?, ?)",
                (session_id, SQUAT, set_no, weight_kg, reps),
            )


async def _insert_extra_training(
    db: Database, *, performed_on: date, exercise_id: str, weight_kg: float, reps: int
) -> None:
    """预置一次额外训练（``plan_session_id`` 为 NULL）：渐进历史不计入，PB 也不该进装配范围。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, NULL)",
            (performed_on.isoformat(),),
        )
        session_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await conn.execute(
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps) VALUES (?, ?, 1, 'work',"
            " 'barbell_includes_bar_total', ?, ?)",
            (session_id, exercise_id, weight_kg, reps),
        )


async def _active_rows(db: Database) -> list[dict[str, Any]]:
    """active plan 行的全列快照：调整 Run 期间原 active 必须逐字段不变（且至多 1 行）。"""

    async def op(conn: Any) -> list[dict[str, Any]]:
        async with conn.execute(
            "SELECT id, version, status, source_plan_id, structured_content,"
            " evaluator_result, created_at, confirmed_at, archived_at"
            " FROM plans WHERE status = 'active'"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _plan_status_counts(db: Database) -> dict[str, int]:
    async def op(conn: Any) -> dict[str, int]:
        async with conn.execute(
            "SELECT status, COUNT(*) FROM plans GROUP BY status"
        ) as cursor:
            return {str(row[0]): int(row[1]) for row in await cursor.fetchall()}

    return await db.under_lock(op)


class _Assembly:
    """一次测试的替身、子图依赖与编译入口（与存档连接无关）。

    重启用例需要在同一业务库上按**新**存档连接重新编译一份图：内存里的图与 State 都不参与恢复，
    只能重读 checkpoint 与业务库（stage5.md §6 Subtask 04「含重启」）。
    """

    def __init__(
        self,
        *,
        model: ScriptedModel,
        skills: CountingSkillLoader,
        assembler: CountingAssembler,
        persistence: CountingPersistence,
        activation: CountingActivation,
        deps: GeneratePlanDeps,
    ) -> None:
        self.model = model
        self.skills = skills
        self.assembler = assembler
        self.persistence = persistence
        self.activation = activation
        self.deps = deps

    def build(self, checkpointer: Any) -> Any:
        """按本套依赖编译计划子图（存档连接由调用方给出）。"""
        return build_generate_plan_graph(self.deps, checkpointer=checkpointer)


def _assembly(
    db: Database, *, scripts: Mapping[str, Sequence[str]] | None = None
) -> _Assembly:
    """构造一次测试的替身与子图依赖：服务全部绑定给定业务库，模型不入库、不读环境变量。"""
    model = ScriptedModel(scripts)
    skills = CountingSkillLoader()
    assembler = CountingAssembler(db)
    persistence = CountingPersistence(db)
    activation = CountingActivation(db)
    return _Assembly(
        model=model,
        skills=skills,
        assembler=assembler,
        persistence=persistence,
        activation=activation,
        deps=GeneratePlanDeps(
            profiles=ProfileService(db),
            catalog=ActionCatalogService(db),
            stats=StatsRepo(db),
            assembler=assembler,
            skills=skills,
            persistence=persistence,
            plans=PlanReadService(db),
            activation=activation,
            model=model,
            now=lambda: FIXED_NOW,
        ),
    )


class _Harness:
    """一次测试的图、业务库与固定替身：调整分支经唯一运行入口驱动，确认／拒绝经唯一确认入口。"""

    def __init__(
        self,
        *,
        graph: Any,
        db: Database,
        model: ScriptedModel,
        skills: CountingSkillLoader,
        assembler: CountingAssembler,
        persistence: CountingPersistence,
        activation: CountingActivation,
        deps: GeneratePlanDeps,
        active_before: list[dict[str, Any]],
    ) -> None:
        self.graph = graph
        self.db = db
        self.model = model
        self.skills = skills
        self.assembler = assembler
        self.persistence = persistence
        self.activation = activation
        self.deps = deps
        self.active_before = active_before

    def run(self, *, budget: ModelRequestBudget | None = None) -> GeneratePlanRun:
        """本次 invocation 的运行上下文（业务日期 ＋ 模型请求预算），确认入口与运行入口共用。"""
        return GeneratePlanRun(
            business_day=BUSINESS_DAY,
            budget=ModelRequestBudget() if budget is None else budget,
        )

    async def invoke(
        self, request: str, *, budget: ModelRequestBudget | None = None
    ) -> AgentRunResult:
        """经 ``invoke_agent_run`` 驱动一次请求（Router 与计划链路共享同一份 Run 预算）。"""
        return await invoke_agent_run(
            self.graph,
            {"conversation_id": CONVERSATION_ID, "request": request},
            thread_config(CONVERSATION_ID),
            self.run(budget=budget),
            AgentRunDeps(
                model=self.model,
                stats=StatsService(self.db),
                plans=self.deps.plans,
                persistence=self.deps.persistence,
            ),
        )

    async def confirm(
        self, plan_id: int, *, action: ConfirmationAction = "confirm"
    ) -> Plan:
        """经 ``invoke_confirmation`` 驱动一次确认／拒绝（checkpoint 优先 ＋ 唯一 draft 兜底）。"""
        return await invoke_confirmation(
            self.graph,
            conversation_id=CONVERSATION_ID,
            plan_id=plan_id,
            action=action,
            run=self.run(),
            deps=self.deps,
        )

    async def assert_active_unchanged(self) -> None:
        """§8：调整 Run 在用户确认前不得改动原 active：快照逐字段相等且 active 行数至多 1。"""
        after = await _active_rows(self.db)
        assert after == self.active_before
        assert len(after) <= 1


@asynccontextmanager
async def _harness(
    tmp_path: Path,
    *,
    active: bool = True,
    link_trainings: int = 0,
    scripts: Mapping[str, Sequence[str]] | None = None,
    active_guard: bool = True,
) -> AsyncIterator[_Harness]:
    """装配一次测试：临时业务库 ＋ 临时 checkpoint 存档 ＋ 固定替身。

    ``active`` 为真时预置一个 active 计划（两个训练日）与其日程；``link_trainings`` 指定前几个训练日
    各预置一次「关联日程」的 squat 训练（达到次数上限即触发加重决策）。``active_guard`` 为假时不在
    测试退出时断言原 active 不变：用户确认激活会合法地换掉原 active，那些用例自己写前后快照断言。
    """
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(_profile())
        if active:
            plan_id = await _insert_plan(
                db,
                version=1,
                status="active",
                content=_content(TARGET_LOAD_KG),
                confirmed_at=CREATED_AT,
            )
            for index, day in enumerate(ACTIVE_DAYS):
                session_id = await _insert_session(db, plan_id=plan_id, scheduled_on=day)
                if index < link_trainings:
                    await _insert_squat_training(
                        db,
                        performed_on=day,
                        plan_session_id=session_id,
                        weight_kg=TARGET_LOAD_KG,
                        reps=REPS_MAX,
                    )
        active_before = await _active_rows(db)
        assembly = _assembly(db, scripts=scripts)
        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            harness = _Harness(
                graph=assembly.build(saver),
                db=db,
                model=assembly.model,
                skills=assembly.skills,
                assembler=assembly.assembler,
                persistence=assembly.persistence,
                activation=assembly.activation,
                deps=assembly.deps,
                active_before=active_before,
            )
            try:
                yield harness
            finally:
                if active_guard:
                    await harness.assert_active_unchanged()
    finally:
        await db.close()


async def test_adjust_without_an_active_plan_fails_before_the_planner(
    tmp_path: Path,
) -> None:
    """§3.4：没有 active 计划时在 Planner 前明确失败：0 模型调用、0 装配、0 draft／rejected。"""
    async with _harness(tmp_path, active=False) as h:
        with pytest.raises(RequiredActivePlanMissing):
            await h.invoke(ADJUST_REQUEST)

        assert h.model.calls == []
        assert h.skills.loaded == []
        assert h.assembler.assemblies == 0
        assert h.persistence.writes == []
        assert await _plan_status_counts(h.db) == {}


async def test_adjust_preloads_the_active_plan_and_filters_personal_bests(
    tmp_path: Path,
) -> None:
    """§3.4：预读并解析当前 active；PB 只装配 active 涉及动作；只加载 ``plan-adjustment``；
    新 draft 的 ``source_plan_id`` 等于该 active。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [_plan_text(_content(TARGET_LOAD_KG))],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    async with _harness(tmp_path, scripts=scripts) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None
        # 一次不属于 active 动作的训练 ＋ 一次 active 动作的额外训练：PB 过滤只看动作身份。
        await _insert_extra_training(
            h.db, performed_on=date(2026, 5, 30), exercise_id=BENCH_PRESS,
            weight_kg=60.0, reps=5,
        )
        await _insert_extra_training(
            h.db, performed_on=date(2026, 5, 29), exercise_id=SQUAT,
            weight_kg=45.0, reps=5,
        )

        result = await h.invoke(ADJUST_REQUEST)

        assert result.intent == "adjust_plan"
        assert result.draft_plan_id is not None
        assert h.skills.loaded == ["plan-adjustment"]
        assert h.assembler.exercise_ids == [(SQUAT, PULL_UP)]
        payload = h.model.payload(ADJUSTMENT_PLANNER_SYSTEM_PROMPT)
        assert payload["skill"]["metadata"]["name"] == "plan-adjustment"
        assert payload["active_plan_draft"] == _content(TARGET_LOAD_KG)
        assert [best["exercise_id"] for best in payload["personal_bests"]] == [SQUAT]
        assert payload["progression_decisions"] == [
            {
                "exercise_id": SQUAT,
                "sets": TARGET_SETS,
                "reps_min": REPS_MIN,
                "reps_max": REPS_MAX,
                "target_load_kg": TARGET_LOAD_KG,
                "decision": {"action": "keep", "load_kg": TARGET_LOAD_KG},
            }
        ]

        written = await PlanReadService(h.db).get_by_id(result.draft_plan_id)
        assert written is not None
        assert written.status == "draft"
        assert written.source_plan_id == active.id
        assert written.version == 2


async def test_adjust_records_the_source_plan_id_and_preserves_untouched_content(
    tmp_path: Path,
) -> None:
    """§3.4：新 draft 的 ``source_plan_id`` 等于本次 active；未被调整证据推翻的训练日、动作与处方
    原样保留（只有证据支持的 squat 负荷变化）。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [_plan_text(_content(INCREASE_LOAD_KG))],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    async with _harness(
        tmp_path, link_trainings=len(ACTIVE_DAYS), scripts=scripts
    ) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None

        result = await h.invoke(ADJUST_REQUEST)

        assert result.termination_reason is None
        assert result.draft_plan_id is not None
        written = await PlanReadService(h.db).get_by_id(result.draft_plan_id)
        assert written is not None
        assert written.source_plan_id == active.id
        draft = plan_draft_from_json(json.dumps(written.structured_content))
        active_draft = PlanDraft.model_validate(active.structured_content)

        # 未受证据影响的部分逐项不变：pull-up 整个训练日、squat 的训练日与组次区间、目标与频率。
        assert draft.training_days[1] == active_draft.training_days[1]
        assert draft.training_days[0].scheduled_on == active_draft.training_days[0].scheduled_on
        assert draft.training_days[0].exercises[0].sets == TARGET_SETS
        assert draft.training_days[0].exercises[0].prescription.reps_min == REPS_MIN
        assert draft.training_days[0].exercises[0].prescription.reps_max == REPS_MAX
        assert draft.weekly_frequency == active_draft.weekly_frequency
        assert draft.goal == active_draft.goal
        assert draft.explanation == active_draft.explanation
        # 唯一变化就是渐进决策给出的负荷。
        assert draft.training_days[0].exercises[0].prescription.load.weight_kg == INCREASE_LOAD_KG
        assert (
            active_draft.training_days[0].exercises[0].prescription.load.weight_kg
            == TARGET_LOAD_KG
        )


async def test_adjust_validation_follows_the_progression_decision(tmp_path: Path) -> None:
    """§3.4：调整候选负荷必须等于渐进决策——沿用旧负荷的候选被确定性层阻断并修订一次。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [
            _plan_text(_content(TARGET_LOAD_KG)),
            _plan_text(_content(INCREASE_LOAD_KG)),
        ],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    async with _harness(
        tmp_path, link_trainings=len(ACTIVE_DAYS), scripts=scripts
    ) as h:
        result = await h.invoke(ADJUST_REQUEST)

        # 首次候选 ＋ 唯一一次修订：都是调整模式的 Planner，用同一个系统提示词。
        assert len(h.model.calls_for(ADJUSTMENT_PLANNER_SYSTEM_PROMPT)) == 2
        assert len(h.model.calls_for(EVALUATOR_SYSTEM_PROMPT)) == 1
        assert h.model.payload(ADJUSTMENT_PLANNER_SYSTEM_PROMPT, 0)[
            "progression_decisions"
        ] == [_increase_decision()]
        revision = h.model.payload(ADJUSTMENT_PLANNER_SYSTEM_PROMPT, 1)["revision"]
        failures = revision["evaluation"]["deterministic"]["failures"]
        assert [failure["code"] for failure in failures] == ["load_source_mismatch"]
        assert "渐进决策" in revision["evaluation"]["blocking_failures"][0]

        assert result.termination_reason is None
        written = await PlanReadService(h.db).get_by_id(result.draft_plan_id)
        assert written is not None
        draft = plan_draft_from_json(json.dumps(written.structured_content))
        assert draft.training_days[0].exercises[0].prescription.load.weight_kg == (
            INCREASE_LOAD_KG
        )


async def test_adjust_second_blocking_failure_rejects_within_one_revision(
    tmp_path: Path,
) -> None:
    """§3.4＋一次修订上限：阻断失败只允许修订一次，二次仍失败即 ``rejected``，不产生可激活 draft。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [
            _plan_text(_content(TARGET_LOAD_KG)),
            _plan_text(_content(TARGET_LOAD_KG)),
        ],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    async with _harness(
        tmp_path, link_trainings=len(ACTIVE_DAYS), scripts=scripts
    ) as h:
        result = await h.invoke(ADJUST_REQUEST)

        assert result.termination_reason == "reject_draft"
        assert result.draft_plan_id is None
        assert len(h.model.calls_for(ADJUSTMENT_PLANNER_SYSTEM_PROMPT)) == 2
        assert h.model.calls_for(EVALUATOR_SYSTEM_PROMPT) == []
        assert await _plan_status_counts(h.db) == {"active": 1, "rejected": 1}


async def test_zero_hit_request_routes_into_the_adjust_branch_on_the_shared_budget(
    tmp_path: Path,
) -> None:
    """§3.6／§3.9：零命中请求经一次模型分类进入 adjust 分支，分类与计划链路共享同一份 Run 预算。"""
    budget = ModelRequestBudget()
    scripts = {
        ROUTER_SYSTEM_PROMPT: [_intent_text("adjust_plan")],
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [_plan_text(_content(TARGET_LOAD_KG))],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }
    async with _harness(tmp_path, scripts=scripts) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None

        result = await h.invoke("把现在的安排弄轻一点", budget=budget)

        assert [prompt for prompt, _ in h.model.calls] == [
            ROUTER_SYSTEM_PROMPT,
            ADJUSTMENT_PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
        ]
        assert budget.used == 3
        assert h.model.calls[0][1] == "把现在的安排弄轻一点"
        written = await PlanReadService(h.db).get_by_id(result.draft_plan_id)
        assert written is not None
        assert written.source_plan_id == active.id


async def test_adjust_red_flag_request_stops_before_any_model_call_or_write(
    tmp_path: Path,
) -> None:
    """§3.5／A1：安全优先不变——急性关键词在 Router 之前就终止，不调分类或计划模型、不写任何行。

    ``done.intent`` 为 ``None``：本次 Run 没有 Router 结论（修复前是确定性命中的 ``adjust_plan``）。
    """
    async with _harness(tmp_path, link_trainings=len(ACTIVE_DAYS)) as h:
        result = await h.invoke("最近麻木，调整计划")

        assert result.intent is None
        assert result.termination_reason == "safety_stop"
        assert result.draft_plan_id is None
        assert h.model.calls == []
        assert h.assembler.assemblies == 0
        assert h.skills.loaded == []
        assert h.persistence.writes == []
        assert await _plan_status_counts(h.db) == {"active": 1}


# ---------- §3.1／§3.2／§3.3 确认 resume 与唯一 draft 兜底（Subtask 04） ----------


def _confirmation_scripts(
    load_kg: float = TARGET_LOAD_KG,
) -> dict[str, Sequence[str]]:
    """调整分支的固定替身脚本：产出一份 ``starts_on`` 不早于业务日的调整 draft（可激活）。"""
    return {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [
            _plan_text(_content(load_kg, starts_on=CONFIRM_STARTS_ON, days=CONFIRM_DAYS))
        ],
        EVALUATOR_SYSTEM_PROMPT: [_rubric_text()],
    }


async def _draft_via_graph(h: _Harness) -> int:
    """经唯一运行入口把图跑到确认等待，返回 draft 身份（与 interrupt 载荷里的 id 同一个）。"""
    result = await h.invoke(ADJUST_REQUEST)
    assert result.termination_reason is None
    assert result.draft_plan_id is not None
    snapshot = await h.graph.aget_state(thread_config(CONVERSATION_ID))
    assert snapshot.next == ("wait_for_confirmation",)
    assert snapshot.interrupts[0].value == {"draft_plan_id": result.draft_plan_id}
    return result.draft_plan_id


async def _plans_by_id(db: Database) -> dict[int, Plan]:
    """全部计划版本行（按 id）：确认／拒绝前后的逐字段比对用（走只读入口，不写 SQL）。"""
    return {plan.id: plan for plan in await PlanReadService(db).list_versions()}


async def test_confirmation_resumes_the_waiting_checkpoint_and_activates_the_draft(
    tmp_path: Path,
) -> None:
    """§3.3 第 1 条 ＋ §3.1：interrupt 的 ``draft_plan_id`` 等于请求 ``plan_id`` 时按 ``Command(resume)``
    进入确认分支；新计划激活、原 active 归档、旧未到期日程取消、历史日程保留、新计划逐训练日建日程。"""
    async with _harness(
        tmp_path, scripts=_confirmation_scripts(), active_guard=False
    ) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None
        # 原 active 再加一条尚未到期（>= 业务日）的日程：激活只取消它，历史日程保留（§3.1 第 5 步）。
        upcoming_session = await _insert_session(
            h.db, plan_id=active.id, scheduled_on=CONFIRM_DAYS[0]
        )
        plans_before = await _plans_by_id(h.db)
        sessions_before = await PlanReadService(h.db).list_sessions(active.id)

        draft_id = await _draft_via_graph(h)
        assert h.activation.calls == []

        activated = await h.confirm(draft_id)

        assert activated.id == draft_id
        assert activated.status == "active"
        assert activated.confirmed_at == CONFIRMED_AT
        assert activated.source_plan_id == active.id
        # 确认路径只提交一次，且经的是同一个激活服务。
        assert h.activation.calls == [("activate", draft_id)]
        plans_after = await _plans_by_id(h.db)
        assert set(plans_after) == set(plans_before) | {draft_id}
        # 原 active 只有 status／archived_at 变化；任意时刻至多一条 active。
        assert replace(
            plans_before[active.id], status="archived", archived_at=CONFIRMED_AT
        ) == plans_after[active.id]
        assert [plan.id for plan in plans_after.values() if plan.status == "active"] == [
            draft_id
        ]
        # 日程：历史行原样保留、未到期行写入同一业务瞬间的取消时间、新计划逐训练日各一条。
        expected_sessions = [
            replace(session, cancelled_at=CONFIRMED_AT)
            if session.id == upcoming_session
            else session
            for session in sessions_before
        ]
        assert list(await PlanReadService(h.db).list_sessions(active.id)) == (
            expected_sessions
        )
        assert [
            (session.scheduled_on, session.cancelled_at)
            for session in await PlanReadService(h.db).list_sessions(draft_id)
        ] == [(day, None) for day in CONFIRM_DAYS]
        # checkpoint 证据：图确实走完了确认分支，终态不再等待。
        snapshot = await h.graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.next == ()
        assert snapshot.values["confirmation"] == "confirmed"
        assert snapshot.values["draft_plan_id"] == draft_id


async def test_confirmation_with_a_mismatched_plan_id_conflicts_without_writing(
    tmp_path: Path,
) -> None:
    """§3.3 第 1 条：请求 ``plan_id`` 与 interrupt 的 ``draft_plan_id`` 不相等即明确冲突：不写任何行，
    图仍停在确认 interrupt（等待位置与载荷都不变）。"""
    async with _harness(tmp_path, scripts=_confirmation_scripts()) as h:
        draft_id = await _draft_via_graph(h)
        plans_before = await _plans_by_id(h.db)

        with pytest.raises(ConfirmationConflict):
            await h.confirm(draft_id + 1)

        assert h.activation.calls == []
        assert await _plans_by_id(h.db) == plans_before
        snapshot = await h.graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.next == ("wait_for_confirmation",)
        assert snapshot.interrupts[0].value == {"draft_plan_id": draft_id}


#: §3.3 resume 载荷的三种非法形状：身份不等、额外字段、非 ``confirm``／``reject`` 动作。
MALFORMED_RESUME_CASES: tuple[str, ...] = (
    "plan-id-mismatch",
    "extra-field",
    "unknown-action",
)


def test_confirmation_edge_never_defaults_to_confirm() -> None:
    """§3.3：确认边只认 ``wait_for_confirmation`` 写下的两个动作；其它取值明确冲突，不默认放行激活。"""
    assert _route_after_confirmation({"confirmation": "confirmed"}) == "confirm"
    assert _route_after_confirmation({"confirmation": "rejected"}) == "reject"
    for unexpected in ("pending", None):
        with pytest.raises(ConfirmationConflict):
            _route_after_confirmation({"confirmation": unexpected})


@pytest.mark.parametrize("case", MALFORMED_RESUME_CASES)
# 说明：本仓库用 conftest 给所有 async 测试自动打 anyio 标记；该组合下参数化 async 测试必须显式
# 声明 ``anyio_backend``（后端仍是 conftest 固定的 asyncio），否则 pytest 无法解析参数。
async def test_resume_payload_outside_action_and_plan_id_is_a_conflict(
    tmp_path: Path, case: str, anyio_backend: str
) -> None:
    """§3.3：resume 载荷只允许动作与 ``plan_id``，且身份必须等于 interrupt 的 ``draft_plan_id``；
    不合法的载荷明确冲突，且根本走不到确认节点（领域服务零调用）。"""
    async with _harness(tmp_path, scripts=_confirmation_scripts()) as h:
        draft_id = await _draft_via_graph(h)
        payloads: dict[str, dict[str, Any]] = {
            "plan-id-mismatch": {"action": "confirm", "plan_id": draft_id + 1},
            "extra-field": {"action": "confirm", "plan_id": draft_id, "draft_plan": {}},
            "unknown-action": {"action": "approve", "plan_id": draft_id},
        }

        with pytest.raises(ConfirmationConflict):
            await h.graph.ainvoke(
                Command(resume=payloads[case]),
                thread_config(CONVERSATION_ID),
                context=h.run(),
            )

        assert h.activation.calls == []


async def test_rejection_resumes_the_waiting_checkpoint_and_archives_the_draft(
    tmp_path: Path,
) -> None:
    """§3.2／§3.3：resume 动作为拒绝时进入 ``archive_draft``：draft 归档、原 active 逐字段不变、
    全程不出现 ``rejected``；checkpoint 完成后重复拒绝仍调用领域服务并幂等返回同一归档行。"""
    async with _harness(tmp_path, scripts=_confirmation_scripts()) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None
        plans_before = await _plans_by_id(h.db)
        draft_id = await _draft_via_graph(h)

        archived = await h.confirm(draft_id, action="reject")

        assert archived.status == "archived"
        assert archived.archived_at == CONFIRMED_AT
        assert h.activation.calls == [("reject", draft_id)]
        plans_after = await _plans_by_id(h.db)
        assert set(plans_after) == set(plans_before) | {draft_id}
        assert plans_after[active.id] == plans_before[active.id]
        assert "rejected" not in {plan.status for plan in plans_after.values()}
        snapshot = await h.graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.next == ()
        assert snapshot.values["confirmation"] == "rejected"
        assert snapshot.values["termination_reason"] == "archive_draft"

        # checkpoint 已完成（没有等待任务）：重复拒绝仍经领域服务，幂等返回同一归档行。
        assert await h.confirm(draft_id, action="reject") == archived
        assert h.activation.calls == [("reject", draft_id), ("reject", draft_id)]
        assert await _plans_by_id(h.db) == plans_after


async def test_confirmation_without_a_checkpoint_falls_back_to_the_unique_draft(
    tmp_path: Path,
) -> None:
    """§3.3 第 2 条：该 thread 没有 checkpoint（没有等待任务）时读业务库唯一 draft，ID 相等则按同一个
    领域服务提交；兜底路径完全不碰 checkpoint。"""
    async with _harness(tmp_path, active_guard=False) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None
        plans_before = await _plans_by_id(h.db)
        draft_id = await _insert_plan(
            h.db,
            version=2,
            status="draft",
            content=_content(
                TARGET_LOAD_KG, starts_on=CONFIRM_STARTS_ON, days=CONFIRM_DAYS
            ),
            source_plan_id=active.id,
        )
        snapshot = await h.graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.values == {} and snapshot.next == ()

        activated = await h.confirm(draft_id)

        assert activated.status == "active"
        assert activated.confirmed_at == CONFIRMED_AT
        assert h.activation.calls == [("activate", draft_id)]
        assert replace(
            plans_before[active.id], status="archived", archived_at=CONFIRMED_AT
        ) == (await _plans_by_id(h.db))[active.id]
        assert [
            (session.scheduled_on, session.cancelled_at)
            for session in await PlanReadService(h.db).list_sessions(draft_id)
        ] == [(day, None) for day in CONFIRM_DAYS]
        after = await h.graph.aget_state(thread_config(CONVERSATION_ID))
        assert after.values == {} and after.next == ()


async def test_confirmation_fallback_conflicts_when_the_unique_draft_is_a_different_plan(
    tmp_path: Path,
) -> None:
    """§3.3 第 2 条：兜底读到的唯一 draft 与请求 ``plan_id`` 不相等即明确冲突，不写任何行。"""
    async with _harness(tmp_path) as h:
        active = await PlanReadService(h.db).get_active()
        assert active is not None
        draft_id = await _insert_plan(
            h.db,
            version=2,
            status="draft",
            content=_content(
                TARGET_LOAD_KG, starts_on=CONFIRM_STARTS_ON, days=CONFIRM_DAYS
            ),
            source_plan_id=active.id,
        )
        plans_before = await _plans_by_id(h.db)

        with pytest.raises(ConfirmationConflict):
            await h.confirm(draft_id + 1)

        assert h.activation.calls == []
        assert await _plans_by_id(h.db) == plans_before


async def test_a_completed_checkpoint_still_reaches_domain_idempotency(
    tmp_path: Path,
) -> None:
    """§3.3 第 2–3 条 ＋ §3.2：确认完成后已无等待任务，重复 confirm 仍调用领域服务并按幂等矩阵返回
    既有 active 行（不重复建日程、不产生第二条 active）。"""
    async with _harness(
        tmp_path, scripts=_confirmation_scripts(), active_guard=False
    ) as h:
        draft_id = await _draft_via_graph(h)
        first = await h.confirm(draft_id)
        sessions_after_first = await PlanReadService(h.db).list_sessions(draft_id)
        snapshot = await h.graph.aget_state(thread_config(CONVERSATION_ID))
        assert snapshot.next == () and snapshot.interrupts == ()
        assert await PlanReadService(h.db).list_drafts() == ()

        again = await h.confirm(draft_id)

        assert again == first
        assert h.activation.calls == [("activate", draft_id), ("activate", draft_id)]
        assert await PlanReadService(h.db).list_sessions(draft_id) == (
            sessions_after_first
        )
        assert [
            plan.id for plan in (await _plans_by_id(h.db)).values() if plan.status == "active"
        ] == [draft_id]


async def test_confirmation_resumes_from_a_reopened_checkpoint_after_a_restart(
    tmp_path: Path,
) -> None:
    """§3.3／§7.4「含重启」：关掉存档连接后重开同一 SQLite 文件（内存里的图与 State 都不参与恢复），
    同一 ``conversation_id`` 仍按等待中的 interrupt 恢复并把 draft 激活。"""
    db = await _migrated(tmp_path / "fit_agent.db")
    checkpoint_path = tmp_path / "checkpoints.db"
    try:
        await ProfileService(db).update(_profile())
        await _insert_plan(
            db,
            version=1,
            status="active",
            content=_content(TARGET_LOAD_KG),
            confirmed_at=CREATED_AT,
        )
        first_process = _assembly(db, scripts=_confirmation_scripts())
        async with open_checkpointer(checkpoint_path) as saver:
            result = await invoke_agent_run(
                first_process.build(saver),
                {"conversation_id": CONVERSATION_ID, "request": ADJUST_REQUEST},
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(business_day=BUSINESS_DAY),
                AgentRunDeps(
                    model=first_process.model,
                    stats=StatsService(db),
                    plans=first_process.deps.plans,
                    persistence=first_process.deps.persistence,
                ),
            )
        draft_id = result.draft_plan_id
        assert draft_id is not None
        assert first_process.activation.calls == []

        second_process = _assembly(db)
        async with open_checkpointer(checkpoint_path) as saver:
            graph = second_process.build(saver)
            plan = await invoke_confirmation(
                graph,
                conversation_id=CONVERSATION_ID,
                plan_id=draft_id,
                action="confirm",
                run=GeneratePlanRun(business_day=BUSINESS_DAY),
                deps=second_process.deps,
            )
            snapshot = await graph.aget_state(thread_config(CONVERSATION_ID))

        assert plan.id == draft_id
        assert plan.status == "active"
        assert plan.confirmed_at == CONFIRMED_AT
        assert second_process.activation.calls == [("activate", draft_id)]
        # 重开后的存档里也留下了确认动作与终态：恢复走的是确认分支，不是绕过图的兜底直调。
        assert snapshot.values["confirmation"] == "confirmed"
        assert snapshot.next == ()
        assert [
            found.id for found in (await _plans_by_id(db)).values() if found.status == "active"
        ] == [draft_id]
    finally:
        await db.close()


async def test_confirming_a_rejected_plan_conflicts_and_never_activates(
    tmp_path: Path,
) -> None:
    """§3.2／§3.3：二次阻断失败的 ``rejected`` 是终态且没有 draft 可激活：confirm 明确冲突、无写入，
    且该请求仍到达领域服务（不是只回旧 checkpoint State）。"""
    scripts = {
        ADJUSTMENT_PLANNER_SYSTEM_PROMPT: [
            _plan_text(
                _content(TARGET_LOAD_KG, starts_on=CONFIRM_STARTS_ON, days=CONFIRM_DAYS)
            ),
            _plan_text(
                _content(TARGET_LOAD_KG, starts_on=CONFIRM_STARTS_ON, days=CONFIRM_DAYS)
            ),
        ],
    }
    async with _harness(
        tmp_path, link_trainings=len(ACTIVE_DAYS), scripts=scripts
    ) as h:
        result = await h.invoke(ADJUST_REQUEST)
        assert result.termination_reason == "reject_draft"
        assert result.draft_plan_id is None
        rejected = [
            plan
            for plan in (await _plans_by_id(h.db)).values()
            if plan.status == "rejected"
        ]
        assert len(rejected) == 1
        plans_before = await _plans_by_id(h.db)
        assert await PlanReadService(h.db).list_drafts() == ()

        with pytest.raises(PlanActivationConflict):
            await h.confirm(rejected[0].id)

        assert h.activation.calls == [("activate", rejected[0].id)]
        assert await _plans_by_id(h.db) == plans_before
