"""Stage 4 子任务 04／05：Planner/Evaluator 节点、一次修订图与 checkpoint 等待边界（固定替身，不调真实模型）。

依据：``refactor-log/stage4.md`` §2.2／§3.2–§3.7／§3.9／§3.10／§5.3／§6 Subtask 04／§6 Subtask 05／
§7／§8.2／§8.5–§8.7／§9.1–§9.3；``LANGGRAPH_REFACTOR_PLAN.md`` §5.6／:149；
``Fit-Agent-LangGraph-重构讨论总结.md`` §4.2／§5.1／§8／§9／§10。
覆盖矩阵见 ``refactor-log/stage4.md`` §8.2／§8.5／§8.6 与 §8.7。

只驱动生成计划子图（``graph/workflow.py::build_generate_plan_graph``）：模型是可脚本化的固定替身
（按系统提示词区分 Planner／Evaluator），Skill 加载器／MemoryAssembler／持久化服务是计数替身，
Checkpointer 用 ``tmp_path`` 下的独立 SQLite 存档。每次 invocation 都经
``graph/workflow.py::invoke_generate_plan``（唯一入口，Run 时限包住完整一次 Graph 调用）。整份文件
不调真实模型、不需要任何 ``MODEL_*`` 环境变量；确认／拒绝／激活事务与「checkpoint 缺失时业务 draft
兜底」留 Stage 5，本文件末尾的 §8.7 一节测 checkpoint 等待边界、重启恢复与 thread 隔离。

计划事实按既有测试口径用直接 SQL 预置 active 计划（正式创建／激活入口留 Stage 5）；``_harness``
退出时自动调用 ``_Harness.assert_active_unchanged``，对每个 Graph 行为测试断言原 active
（id／状态／版本／内容／确认时间）逐字段不变且行数至多 1，并覆盖 invocation 抛错与没有 active 行
的测试（stage4.md §9.2）；纯拓扑测试不发生操作，无需该断言。
"""

import ast
import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from api.app import create_app
from config import (
    GRAPH_RUN_TIMEOUT_SECONDS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MODEL_API_KEY_ENV,
    MODEL_BASE_URL_ENV,
    MODEL_MODEL_ENV,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)
from domain.actions.service import ActionCatalogService
from domain.plans.schema import (
    EvaluationResult,
    PlanDraft,
    evaluation_result_from_json,
    plan_draft_from_json,
)
from domain.plans.service import (
    PlanActivationService,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.safety import MESSAGE_RED_FLAG_TERMS, message_red_flag_hits
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from domain.stats.repo import StatsRepo
from graph.checkpointer import open_checkpointer, thread_config
from graph.context import MemoryAssembler
from graph.model import (
    InvalidModelResponse,
    ModelCall,
    ModelConfigurationError,
    build_chat_model,
    openai_compatible_model_call,
)
from graph.nodes import (
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    PLANNING_SKILL_NAME,
    GeneratePlanDeps,
    GeneratePlanRun,
    ModelRequestBudget,
    ModelRequestBudgetExceeded,
    RequiredProfileMissing,
)
from graph.skills import LoadedSkill, SkillLoader
from graph.state import WorkflowState
from graph.workflow import NODE_NAMES, build_generate_plan_graph, invoke_generate_plan
from storage.db import Database

#: 固定 conversation id：它同时就是 Checkpointer 的 thread id（Stage 3 契约）。
CONVERSATION_ID = "stage4-workflow-conversation"
#: 第二个会话 id：只用于「不同 thread 不混用」的隔离断言（单 draft 约束见 §3.8／``003`` 索引）。
OTHER_CONVERSATION_ID = "stage4-workflow-other-conversation"
#: 从未使用过的 thread：没有任何存档时快照为空且没有下一步。
ABSENT_CONVERSATION_ID = "stage4-workflow-absent-conversation"
#: 后端根目录：源码级断言（不新增 Agent 路由）按真实文件读取。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
#: 注入的业务日期与固定时钟：节点不读系统时钟。
BUSINESS_DAY = date(2026, 6, 1)
FIXED_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
CREATED_AT = FIXED_NOW.isoformat()
#: 计划窗口内的一个训练日。
SCHEDULED_ON = "2026-06-02"
PULL_UP = "pull-up"  # 纯自重引体：处方不携带负荷
WEIGHTED = "barbell-back-squat"  # 外加负重动作：可用于伪造禁用动作输出
#: 固定替身在模型调用期间读取业务库的等待上限（持锁即超时暴露）。
DB_LOCK_PROBE_TIMEOUT = 1.0

BODYWEIGHT_PRESCRIPTION: dict[str, Any] = {
    "type": "bodyweight_reps",
    "reps_min": 8,
    "reps_max": 12,
    "progression_note": None,
}
NEEDS_CALIBRATION_PRESCRIPTION: dict[str, Any] = {
    "type": "weighted_reps",
    "reps_min": 5,
    "reps_max": 8,
    "progression_note": None,
    "load": {"status": "needs_calibration"},
}


# ---------- 固定事实构造（纯领域对象，不读库、不调模型） ----------


def _profile(
    *,
    weekly_frequency: Fact[int] | None = None,
    forbidden: Sequence[str] = (),
    injuries: Sequence[str] = (),
) -> Profile:
    """固定画像：``weekly_frequency`` 默认明确值 1；空列表用 ``denied`` 表达明确为空。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=(
            Fact.known(1) if weekly_frequency is None else weekly_frequency
        ),
        available_equipment=Fact.known(("barbell", "bodyweight")),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.known(tuple(injuries)) if injuries else Fact.denied(),
        forbidden_exercise_ids=(
            Fact.known(tuple(forbidden)) if forbidden else Fact.denied()
        ),
    )


DEFAULT_PROFILE = _profile()


def _plan_payload(
    *,
    exercise_id: str = PULL_UP,
    prescription: dict[str, Any] | None = None,
    goal: str = "增肌",
    explanation: str = "每周一练，覆盖既定目标动作",
) -> dict[str, Any]:
    """一个结构合法的单训练日计划（训练日数量恰等于每周训练次数 1）。"""
    return {
        "goal": goal,
        "starts_on": BUSINESS_DAY.isoformat(),
        "explanation": explanation,
        "weekly_frequency": 1,
        "training_days": [
            {
                "scheduled_on": SCHEDULED_ON,
                "exercises": [
                    {
                        "exercise_id": exercise_id,
                        "sets": 3,
                        "prescription": BODYWEIGHT_PRESCRIPTION
                        if prescription is None
                        else prescription,
                    }
                ],
            }
        ],
    }


def _plan_text(**kwargs: Any) -> str:
    """固定替身 Planner 的响应文本。"""
    return json.dumps(_plan_payload(**kwargs), ensure_ascii=False)


def _rubric_text(
    *, goal_alignment: bool = True, schedule_reasonableness: bool = True,
    explanation_quality: bool = True,
) -> str:
    """固定替身 Evaluator 的响应文本：三个布尔判定与理由。"""

    def verdict(passed: bool) -> dict[str, Any]:
        return {"passed": passed, "reason": "固定替身判定"}

    return json.dumps(
        {
            "goal_alignment": verdict(goal_alignment),
            "schedule_reasonableness": verdict(schedule_reasonableness),
            "explanation_quality": verdict(explanation_quality),
        },
        ensure_ascii=False,
    )


# ---------- 固定替身与计数替身 ----------


class ModelUnavailable(RuntimeError):
    """固定替身模拟的上游模型调用失败：运行基础设施错误，不消耗修订次数。"""


class ScriptedModel:
    """固定替身模型：按系统提示词区分 Planner／Evaluator 调用，按脚本顺序返回固定文本。

    脚本项是文本即返回该文本，是异常实例即抛出该异常（模拟调用失败）。每次调用都读一次业务库：
    若模型调用发生在数据库事务内（持业务库单连接锁），该读取会被阻塞到超时，从而在上层失败暴露
    ``stage4.md §3.7「模型调用不在数据库事务中发生」``的回归。
    """

    def __init__(
        self,
        *,
        db: Database,
        planner: Sequence[str | Exception] = (),
        evaluator: Sequence[str | Exception] = (),
    ) -> None:
        self._db = db
        self._scripts: dict[str, list[str | Exception]] = {
            PLANNER_SYSTEM_PROMPT: list(planner),
            EVALUATOR_SYSTEM_PROMPT: list(evaluator),
        }
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system_prompt: str, user_payload: str) -> str:
        self.calls.append((system_prompt, user_payload))
        async with asyncio.timeout(DB_LOCK_PROBE_TIMEOUT):
            await PlanReadService(self._db).list_versions()
        script = self._scripts.get(system_prompt)
        if script is None:
            raise AssertionError(f"固定替身模型收到未知系统提示词：{system_prompt[:40]!r}")
        if not script:
            raise AssertionError("固定替身模型没有更多脚本响应")
        response = script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def planner_calls(self) -> list[tuple[str, str]]:
        """Planner 调用（系统提示词为 :data:`PLANNER_SYSTEM_PROMPT`）的原始记录。"""
        return [call for call in self.calls if call[0] == PLANNER_SYSTEM_PROMPT]

    @property
    def evaluator_calls(self) -> list[tuple[str, str]]:
        """Evaluator（模型 Rubric）调用记录。"""
        return [call for call in self.calls if call[0] == EVALUATOR_SYSTEM_PROMPT]

    def planner_payload(self, index: int = 0) -> dict[str, Any]:
        """第 ``index`` 次 Planner 调用的用户载荷（JSON 文本解码）。"""
        return json.loads(self.planner_calls[index][1])

    def evaluator_payload(self, index: int = 0) -> dict[str, Any]:
        """第 ``index`` 次 Evaluator 调用的用户载荷。"""
        return json.loads(self.evaluator_calls[index][1])


class CountingSkillLoader(SkillLoader):
    """Skill 加载计数替身：命中后才加载正文，安全命中时不得被调用。"""

    def __init__(self) -> None:
        super().__init__()
        self.loaded: list[str] = []

    def load(self, name: str) -> LoadedSkill:
        self.loaded.append(name)
        return super().load(name)


class CountingAssembler(MemoryAssembler):
    """MemoryAssembler 计数替身：安全命中与画像缺失时都不得装配。

    ``assemble_delay_seconds`` 模拟非模型节点超出 Run 时限（完整一次 invocation 的时限回归）。
    """

    def __init__(self, db: Database, *, assemble_delay_seconds: float = 0.0) -> None:
        super().__init__(db)
        self.assemblies = 0
        self.assemble_delay_seconds = assemble_delay_seconds

    async def assemble(
        self,
        request: str,
        *,
        business_day: date,
        exercise_ids: Sequence[str] | None = None,
    ) -> Any:
        self.assemblies += 1
        if self.assemble_delay_seconds:
            await asyncio.sleep(self.assemble_delay_seconds)
        return await super().assemble(
            request, business_day=business_day, exercise_ids=exercise_ids
        )


class CountingPersistence(PlanPersistenceService):
    """持久化计数替身：安全命中与运行错误路径都不得发生业务写入。"""

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


# ---------- 测试库与子图装配 ----------


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _insert_plan(db: Database, *, version: int, status: str) -> int:
    """直接 SQL 写入一个计划版本行（正式计划写入口留 Stage 5），返回计划身份。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at,"
            " confirmed_at) VALUES (?, ?, '{}', ?, ?)",
            (version, status, CREATED_AT, CREATED_AT if status == "active" else None),
        )
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await cursor.close()


async def _active_rows(db: Database) -> list[dict[str, Any]]:
    async def op(conn: Any) -> list[dict[str, Any]]:
        async with conn.execute(
            "SELECT id, status, version, structured_content, confirmed_at FROM plans"
            " WHERE status = 'active'"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _count(db: Database, status: str) -> int:
    async def op(conn: Any) -> int:
        async with conn.execute(
            "SELECT COUNT(*) FROM plans WHERE status = ?", (status,)
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0])

    return await db.under_lock(op)


async def _row_counts(db: Database) -> tuple[int, int]:
    """业务行数快照（计划版本总数、计划日程总数）：恢复读取不得产生任何业务提交。"""

    async def op(conn: Any) -> tuple[int, int]:
        async with conn.execute("SELECT COUNT(*) FROM plans") as cursor:
            plans = int((await cursor.fetchone())[0])
        async with conn.execute("SELECT COUNT(*) FROM plan_sessions") as cursor:
            sessions = int((await cursor.fetchone())[0])
        return plans, sessions

    return await db.under_lock(op)


@dataclass
class _Doubles:
    """一次测试的固定替身与计数替身：``_harness`` 与重启恢复测试用同一套构造，避免两处装配漂移。"""

    model: Any
    skills: CountingSkillLoader
    assembler: CountingAssembler
    persistence: CountingPersistence

    def deps(self, db: Database) -> GeneratePlanDeps:
        """子图依赖：领域服务绑定给定业务库，其余注入项即本套替身（模型不入库、不读环境变量）。"""
        return GeneratePlanDeps(
            profiles=ProfileService(db),
            catalog=ActionCatalogService(db),
            stats=StatsRepo(db),
            assembler=self.assembler,
            skills=self.skills,
            persistence=self.persistence,
            plans=PlanReadService(db),
            activation=PlanActivationService(db),
            model=self.model,
            now=lambda: FIXED_NOW,
        )


def _doubles(
    db: Database,
    *,
    planner: Sequence[str | Exception] = (),
    evaluator: Sequence[str | Exception] = (),
    model: ModelCall | None = None,
    assemble_delay_seconds: float = 0.0,
) -> _Doubles:
    """构造一次测试的替身集合：给出 ``model`` 就用它，否则用可脚本化的 :class:`ScriptedModel`。"""
    return _Doubles(
        model=(
            ScriptedModel(db=db, planner=planner, evaluator=evaluator)
            if model is None
            else model
        ),
        skills=CountingSkillLoader(),
        assembler=CountingAssembler(db, assemble_delay_seconds=assemble_delay_seconds),
        persistence=CountingPersistence(db),
    )


# ---------- 等待边界的存档观察替身（§3.10 顺序与 §9.1 checkpoint 写入失败） ----------


class CheckpointWriteFailure(RuntimeError):
    """测试注入的 checkpoint 写入失败：等待边界的存档没落盘（stage4.md §9.1）。"""


def _observing_boundary_saver(
    observations: list[dict[str, Any]],
) -> Callable[[Any, Database], Any]:
    """只观察、不替身：包一层真实 ``AsyncSqliteSaver.aput``，非等待边界的写入原样委托。

    ``confirmation == "pending"`` 的每次写入都在存档提交点读一次业务库：用来取证 §3.10 的
    「业务事务先提交 draft，再写等待位置」顺序（读取发生在 ``aput`` 内部，不是事后回查）。
    """

    def wrap(saver: Any, db: Database) -> Any:
        stored_aput = saver.aput

        async def aput(config: Any, checkpoint: Any, metadata: Any, versions: Any) -> Any:
            channel_values = checkpoint.get("channel_values", {})
            if channel_values.get("confirmation") == "pending":
                plan_id = channel_values.get("draft_plan_id")
                plan = (
                    None
                    if plan_id is None
                    else await PlanReadService(db).get_by_id(plan_id)
                )
                observations.append(
                    {
                        "draft_plan_id": plan_id,
                        "status": None if plan is None else plan.status,
                        "content": None if plan is None else plan.structured_content,
                        "evaluator_result": (
                            None if plan is None else plan.evaluator_result
                        ),
                    }
                )
            return await stored_aput(config, checkpoint, metadata, versions)

        saver.aput = aput
        return saver

    return wrap


def _failing_boundary_writes(
    attempted: list[Any],
) -> Callable[[Any, Database], Any]:
    """等待边界的存档写入抛错，其余写入仍委托真实 saver：模拟 §9.1「Checkpoint 写入失败」。"""

    def wrap(saver: Any, db: Database) -> Any:
        stored_aput = saver.aput

        async def aput(config: Any, checkpoint: Any, metadata: Any, versions: Any) -> Any:
            channel_values = checkpoint.get("channel_values", {})
            if channel_values.get("confirmation") == "pending":
                attempted.append(channel_values.get("draft_plan_id"))
                raise CheckpointWriteFailure("存档写入失败（测试注入，stage4.md §9.1）")
            return await stored_aput(config, checkpoint, metadata, versions)

        saver.aput = aput
        return saver

    return wrap


def _initial_state(
    request: str, conversation_id: str = CONVERSATION_ID
) -> WorkflowState:
    """一次新用户请求携带的事实（身份、请求文本、intent）：不预置本 Run 的路由与修订预算。"""
    return {"conversation_id": conversation_id, "request": request, "intent": "generate_plan"}

@dataclass
class _Harness:
    """一次测试的子图、固定替身与测试开始前的 active 快照。"""

    graph: Any
    db: Database
    model: Any
    skills: CountingSkillLoader
    assembler: CountingAssembler
    persistence: CountingPersistence
    active_before: list[dict[str, Any]] = field(default_factory=list)

    async def invoke(
        self,
        request: str,
        *,
        budget: ModelRequestBudget | None = None,
        conversation_id: str = CONVERSATION_ID,
    ) -> dict[str, Any]:
        """以 ``intent='generate_plan'`` 驱动一次 Run（经 §3.7 的唯一 invocation 入口）。

        输入就是一个新用户请求携带的事实（身份、请求文本、intent）：本次 Run 的 ``revision_count``
        与 ``termination_reason`` 由图中入口节点与首个候选写出，不由调用方预置（stage4.md §3.6）。
        整次调用走 ``graph.workflow.invoke_generate_plan``：Run 时限包住完整一次 Graph 调用。
        """
        return await invoke_generate_plan(
            self.graph,
            _initial_state(request, conversation_id),
            thread_config(conversation_id),
            GeneratePlanRun(
                business_day=BUSINESS_DAY,
                budget=ModelRequestBudget() if budget is None else budget,
            ),
        )

    async def state(self, conversation_id: str = CONVERSATION_ID) -> Any:
        """给定 thread 的 checkpoint 状态（interrupt 落盘后的等待位置）。"""
        return await self.graph.aget_state(thread_config(conversation_id))

    async def assert_active_unchanged(self) -> None:
        """§9.2：原 active 的 id／状态／版本／内容／确认时间与行数逐字段不变。

        由 ``_harness`` 在每个测试退出时自动调用（含 invocation 抛错的测试与没有 active 行的测试），
        单个测试不必逐条手写。
        """
        after = await _active_rows(self.db)
        assert after == self.active_before
        assert len(after) <= 1


@asynccontextmanager
async def _harness(
    tmp_path: Path,
    *,
    planner: Sequence[str | Exception] = (),
    evaluator: Sequence[str | Exception] = (),
    model: ModelCall | None = None,
    profile: Profile | None = DEFAULT_PROFILE,
    active_plan: bool = True,
    assemble_delay_seconds: float = 0.0,
    saver_wrapper: Callable[[Any, Database], Any] | None = None,
) -> AsyncIterator[_Harness]:
    """装配一次测试：临时业务库 ＋ 临时 checkpoint 存档 ＋ 固定替身与计数替身。

    退出时自动断言原 active 逐字段不变（stage4.md §9.2）：无论测试正常结束还是 invocation 抛错，
    也无论测试开始时有没有 active 行。

    ``saver_wrapper`` 只包一层真实 ``AsyncSqliteSaver``（观察 ``aput`` 或让它抛错），存档仍是真实
    SQLite 实现，持久化不被替身化。
    """
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        if profile is not None:
            await ProfileService(db).update(profile)
        if active_plan:
            await _insert_plan(db, version=1, status="active")
        active_before = await _active_rows(db)
        doubles = _doubles(
            db,
            planner=planner,
            evaluator=evaluator,
            model=model,
            assemble_delay_seconds=assemble_delay_seconds,
        )
        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            harness = _Harness(
                graph=build_generate_plan_graph(
                    doubles.deps(db),
                    checkpointer=saver if saver_wrapper is None else saver_wrapper(saver, db),
                ),
                db=db,
                model=doubles.model,
                skills=doubles.skills,
                assembler=doubles.assembler,
                persistence=doubles.persistence,
                active_before=active_before,
            )
            try:
                yield harness
            finally:
                await harness.assert_active_unchanged()
    finally:
        await db.close()


# ---------- §5.1 安全分流：10 项词与禁用动作 ----------

#: 逐项命中用例：10 项封闭词表 ＋ 保守误拦的否定表达。
SAFETY_REQUESTS: tuple[str, ...] = (
    *MESSAGE_RED_FLAG_TERMS,
    "最近训练后没有麻木，只是想换个计划",
)


async def test_graph_is_the_direct_generate_plan_subgraph_without_a_router(
    tmp_path: Path,
) -> None:
    """拓扑就是 §6 Subtask 04 任务 2 的节点集合：没有 Router、没有第三个 Agent。"""
    async with _harness(tmp_path) as h:
        assert set(h.graph.get_graph().nodes) == set(NODE_NAMES) | {
            "__start__",
            "__end__",
        }
        assert NODE_NAMES[0] == "safety_check"


@pytest.mark.parametrize("request_text", SAFETY_REQUESTS)
# 说明：本仓库用 conftest 给所有 async 测试自动打 anyio 标记；该组合下参数化 async 测试必须显式
# 声明 ``anyio_backend``（后端仍是 conftest 固定的 asyncio），否则 pytest 无法解析参数。
async def test_red_flag_hits_stop_before_skill_memory_planner_or_persistence(
    tmp_path: Path, anyio_backend: str, request_text: str
) -> None:
    """10 项词逐项与「没有麻木」都在 Planner 前安全停止：无模型、无 Skill、无装配、无写入、无计划行。"""
    async with _harness(
        tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]
    ) as h:
        result = await h.invoke(request_text)

        assert result["termination_reason"] == "safety_stop"
        assert "draft_plan_id" not in result
        assert "evaluation" not in result
        assert h.model.calls == []
        assert h.skills.loaded == []
        assert h.assembler.assemblies == 0
        assert h.persistence.writes == []
        assert await _count(h.db, "draft") == 0
        assert await _count(h.db, "rejected") == 0


async def test_multiple_hits_stop_and_keep_the_closed_vocabulary_order(
    tmp_path: Path,
) -> None:
    """一个请求命中多词时按封闭词表顺序返回命中项，并同样直接安全停止。"""
    request_text = "最近晕厥并且麻木，想重新排一份计划"
    assert message_red_flag_hits(request_text) == ("晕厥", "麻木")

    async with _harness(tmp_path) as h:
        result = await h.invoke(request_text)
        assert result["termination_reason"] == "safety_stop"
        assert h.model.calls == []


async def test_non_vocabulary_soreness_does_not_stop_the_plan_path(
    tmp_path: Path,
) -> None:
    """普通肌肉酸痛与关节异响不在封闭词表内：不误命中，继续走到 Planner 与 Evaluator。"""
    async with _harness(tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]) as h:
        result = await h.invoke("最近肌肉酸痛、关节有点响，帮我重新排一份计划")

        assert len(h.model.planner_calls) == 1
        assert len(h.model.evaluator_calls) == 1
        assert result["confirmation"] == "pending"
        assert result["termination_reason"] is None


async def test_planner_payload_carries_six_context_categories_and_filtered_candidates(
    tmp_path: Path,
) -> None:
    """Planner 只接收六类上下文与确定性过滤后的候选动作；Skill 只加载命中的那一个。"""
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text()],
        profile=_profile(forbidden=(WEIGHTED,)),
    ) as h:
        await h.invoke("生成一份增肌计划")

        payload = h.model.planner_payload()
        assert set(payload) == {
            "profile",
            "active_plan",
            "recent_sessions",
            "personal_bests",
            "trend_summary",
            "request",
            "skill",
            "candidate_actions",
        }
        assert payload["request"] == "生成一份增肌计划"
        assert payload["profile"]["training_goal"] == {"state": "known", "value": "增肌"}
        assert payload["skill"]["metadata"]["name"] == PLANNING_SKILL_NAME
        assert h.skills.loaded == [PLANNING_SKILL_NAME]

        candidate_ids = [
            action["exercise_id"] for action in payload["candidate_actions"]
        ]
        catalog = await ActionCatalogService(h.db).list_all()
        assert candidate_ids == [
            exercise.id
            for exercise in catalog
            if exercise.recommendable and exercise.id != WEIGHTED
        ]
        assert WEIGHTED not in candidate_ids
        assert PULL_UP in candidate_ids
        # 没有有效工作组历史：候选动作的起始负荷只能是待校准。
        assert all(
            action["starting_load"] == {"status": "needs_calibration"}
            for action in payload["candidate_actions"]
        )


async def test_known_injuries_never_derive_new_forbidden_ids(tmp_path: Path) -> None:
    """已知伤病文本不推导新禁用动作：禁用 ID 只取画像 ``known`` 值（讨论总结 §8.3）。"""
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text()],
        profile=_profile(injuries=("腰椎间盘突出",)),
    ) as h:
        await h.invoke("生成计划")

        candidate_ids = [
            action["exercise_id"]
            for action in h.model.planner_payload()["candidate_actions"]
        ]
        assert WEIGHTED in candidate_ids


async def test_forged_forbidden_exercise_is_blocked_by_the_deterministic_layer(
    tmp_path: Path,
) -> None:
    """伪造输出里的禁用动作被 Evaluator 确定性层阻断：不调 Rubric，二次阻断失败写 rejected。

    确定性失败时模型 Rubric 没有运行：阻断项与 warning 只能来自真实确定性失败，不得把
    :func:`graph.nodes._rubric_not_run` 的三个「未运行」判定伪造成阻断失败或解释质量 warning。
    """
    forged = _plan_text(
        exercise_id=WEIGHTED, prescription=NEEDS_CALIBRATION_PRESCRIPTION
    )
    async with _harness(
        tmp_path,
        planner=[forged, forged],
        evaluator=[_rubric_text()],
        profile=_profile(forbidden=(WEIGHTED,)),
    ) as h:
        result = await h.invoke("生成计划")
        evaluation = result["evaluation"]

        assert result["termination_reason"] == "reject_draft"
        assert len(h.model.planner_calls) == 2
        assert h.model.evaluator_calls == []
        assert evaluation.deterministic.passed is False
        # 阻断项恰为真实确定性失败；未运行的 Rubric 不贡献 goal_alignment／schedule_reasonableness
        assert evaluation.blocking_failures == tuple(
            f"{failure.code}: {failure.message}"
            for failure in evaluation.deterministic.failures
        )
        assert any(
            failure.startswith("forbidden_exercise:")
            for failure in evaluation.blocking_failures
        )
        assert not any(
            failure.startswith(("goal_alignment:", "schedule_reasonableness:"))
            for failure in evaluation.blocking_failures
        )
        # 未运行的 explanation_quality 也不得伪造成 warning
        assert evaluation.warnings == ()
        not_run_verdicts = (
            evaluation.rubric.goal_alignment,
            evaluation.rubric.schedule_reasonableness,
            evaluation.rubric.explanation_quality,
        )
        assert all(verdict.passed is False for verdict in not_run_verdicts)
        assert all(
            "未调用模型 Rubric" in verdict.reason for verdict in not_run_verdicts
        )
        assert await _count(h.db, "rejected") == 1
        assert await _count(h.db, "draft") == 0
        assert "__interrupt__" not in result

        rejected = [
            plan
            for plan in await PlanReadService(h.db).list_versions()
            if plan.status == "rejected"
        ]
        assert len(rejected) == 1
        persisted = rejected[0].evaluator_result
        assert persisted["blocking_failures"] == list(evaluation.blocking_failures)
        assert persisted["warnings"] == []
        assert persisted["rubric"]["explanation_quality"]["passed"] is False
        assert (
            "未调用模型 Rubric" in persisted["rubric"]["explanation_quality"]["reason"]
        )


# ---------- §5.2 Evaluator 分层 ----------


async def test_blocking_deterministic_failure_revises_once_then_persists_draft(
    tmp_path: Path,
) -> None:
    """确定性失败（动作不在目录）阻断 → 结构化理由交回 Planner 修订一次 → 通过则写 draft 并等待。"""
    async with _harness(
        tmp_path,
        planner=[_plan_text(exercise_id="not-in-catalog"), _plan_text()],
        evaluator=[_rubric_text()],
    ) as h:
        result = await h.invoke("生成计划")

        assert len(h.model.planner_calls) == 2  # 首次 ＋ 唯一一次修订
        assert len(h.model.evaluator_calls) == 1  # 首轮确定性失败没有调用 Rubric
        revision = h.model.planner_payload(1)["revision"]
        assert revision["previous_plan"]["goal"] == "增肌"
        assert revision["evaluation"]["deterministic"]["passed"] is False
        # 交回 Planner 的失败理由也只能是真实确定性失败（未运行的 Rubric 不冒充阻断项／warning）
        assert revision["evaluation"]["blocking_failures"] == [
            f"{failure['code']}: {failure['message']}"
            for failure in revision["evaluation"]["deterministic"]["failures"]
        ]
        assert revision["evaluation"]["warnings"] == []
        assert result["revision_count"] == 1
        assert result["evaluation"].passed is True
        assert result["evaluation"].revision_count == 1
        assert result["confirmation"] == "pending"
        assert result["__interrupt__"]
        drafts = await PlanReadService(h.db).list_drafts()
        assert len(drafts) == 1
        assert plan_draft_from_json(json.dumps(drafts[0].structured_content)) == (
            plan_draft_from_json(_plan_text())
        )


@pytest.mark.parametrize(
    "failed_dimension", ["goal_alignment", "schedule_reasonableness"]
)
async def test_second_blocking_failure_rejects_without_waiting(
    tmp_path: Path, anyio_backend: str, failed_dimension: str
) -> None:
    """两个硬门槛失败都是阻断项：二次仍阻断即 rejected，不再回 Planner、不进入等待确认。"""
    failing = _rubric_text(**{failed_dimension: False})
    async with _harness(
        tmp_path,
        planner=[_plan_text(), _plan_text()],
        evaluator=[failing, failing],
    ) as h:
        result = await h.invoke("生成计划")

        assert result["termination_reason"] == "reject_draft"
        assert len(h.model.planner_calls) == 2
        assert len(h.model.evaluator_calls) == 2
        assert result["revision_count"] == 1
        assert result["evaluation"].passed is False
        assert result["evaluation"].deterministic.passed is True
        assert any(
            failure.startswith(f"{failed_dimension}:")
            for failure in result["evaluation"].blocking_failures
        )
        assert await _count(h.db, "rejected") == 1
        assert await _count(h.db, "draft") == 0
        assert "__interrupt__" not in result
        snapshot = await h.state()
        assert snapshot.next == ()


async def test_explanation_warning_only_does_not_revise_and_still_waits(
    tmp_path: Path,
) -> None:
    """解释质量失败只进 warnings：不触发修订、不阻断，仍然写 draft 并等待确认。"""
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text(explanation_quality=False)],
    ) as h:
        result = await h.invoke("生成计划")
        evaluation = result["evaluation"]

        assert evaluation.passed is True
        assert evaluation.blocking_failures == ()
        assert evaluation.warnings == ("explanation_quality: 固定替身判定",)
        assert len(h.model.planner_calls) == 1
        assert len(h.model.evaluator_calls) == 1
        assert result["revision_count"] == 0
        assert result["confirmation"] == "pending"
        assert await _count(h.db, "draft") == 1


async def test_planner_and_evaluator_use_distinct_system_prompts(
    tmp_path: Path,
) -> None:
    """两个节点使用不同系统提示词；Rubric 输入只有请求、画像事实与候选计划。"""
    async with _harness(tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]) as h:
        result = await h.invoke("生成计划")

        assert PLANNER_SYSTEM_PROMPT != EVALUATOR_SYSTEM_PROMPT
        assert {call[0] for call in h.model.calls} == {
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
        }
        # 数据形状在提示词里给出：Skill 只声明「交由 Stage 4 统一 Schema 校验」。
        assert "exercise_id" in PLANNER_SYSTEM_PROMPT
        assert "goal_alignment" in EVALUATOR_SYSTEM_PROMPT

        payload = h.model.evaluator_payload()
        assert set(payload) == {"request", "profile", "plan"}
        assert payload["plan"] == json.loads(_plan_text())
        assert result["draft_plan"].goal == "增肌"


# ---------- §5.3 一次修订闭环与等待 ----------


async def test_first_pass_persists_draft_then_waits_and_models_run_outside_transactions(
    tmp_path: Path,
) -> None:
    """通过路径：先提交业务 draft，再 interrupt 等待确认；模型调用期间不持有业务库锁。"""
    async with _harness(tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]) as h:
        result = await h.invoke("生成计划")

        assert result["revision_count"] == 0
        assert len(h.model.planner_calls) == 1
        assert len(h.model.evaluator_calls) == 1
        assert len(h.persistence.writes) == 1
        assert result["confirmation"] == "pending"

        drafts = await PlanReadService(h.db).list_drafts()
        assert len(drafts) == 1
        assert drafts[0].status == "draft"
        assert drafts[0].confirmed_at is None and drafts[0].archived_at is None
        assert result["draft_plan_id"] == drafts[0].id
        assert drafts[0].structured_content == json.loads(_plan_text())

        interrupts = result["__interrupt__"]
        assert len(interrupts) == 1
        assert interrupts[0].value == {"draft_plan_id": drafts[0].id}

        snapshot = await h.state()
        assert snapshot.next == ("wait_for_confirmation",)
        assert set(snapshot.values) <= set(WorkflowState.__annotations__)


async def test_invalid_rubric_response_is_a_run_error_without_revision_or_rejected(
    tmp_path: Path,
) -> None:
    """Rubric 非法结构是运行错误：不消耗修订次数、不创建 draft／rejected（stage4.md §3.6）。"""
    async with _harness(tmp_path, planner=[_plan_text()], evaluator=["这不是 JSON"]) as h:
        with pytest.raises(InvalidModelResponse):
            await h.invoke("生成计划")

        assert len(h.model.planner_calls) == 1
        assert len(h.model.evaluator_calls) == 1
        assert h.persistence.writes == []
        assert await _count(h.db, "draft") == 0
        assert await _count(h.db, "rejected") == 0
        snapshot = await h.state()
        assert snapshot.values["revision_count"] == 0
        assert "draft_plan_id" not in snapshot.values


async def test_model_call_failure_is_a_run_error_without_records(tmp_path: Path) -> None:
    """模型调用失败是运行基础设施错误：终止 Run，不消耗修订次数、不创建 rejected。"""
    async with _harness(tmp_path, planner=[ModelUnavailable("上游模型不可用")]) as h:
        with pytest.raises(ModelUnavailable):
            await h.invoke("生成计划")

        assert len(h.model.planner_calls) == 1
        assert h.persistence.writes == []
        assert await _count(h.db, "draft") == 0
        assert await _count(h.db, "rejected") == 0


async def test_model_request_timeout_is_a_run_error_without_records(
    tmp_path: Path,
) -> None:
    """单次请求超时（受 Run 剩余时限约束）是运行错误：不写 draft／rejected。"""

    async def slow_model(system_prompt: str, user_payload: str) -> str:
        await asyncio.sleep(1.0)
        return _plan_text()

    async with _harness(tmp_path, model=slow_model) as h:
        budget = ModelRequestBudget(run_timeout_seconds=0.05)
        with pytest.raises(TimeoutError):
            await h.invoke("生成计划", budget=budget)

        assert budget.used == 1  # 预算来自本次 invocation 的运行上下文
        assert await _count(h.db, "draft") == 0
        assert await _count(h.db, "rejected") == 0


@pytest.mark.parametrize("active_plan", [True, False])
async def test_whole_run_deadline_bounds_non_model_nodes_and_writes_nothing(
    tmp_path: Path, anyio_backend: str, active_plan: bool
) -> None:
    """180 秒 Run 时限包住完整一次 invocation：非模型节点超出时限即以 TimeoutError 终止本次 Run。

    修复前该时限只在模型调用资格处检查（``ModelRequestBudget.begin_request``）：``load_context``
    这类非模型节点跑多久都不受限，会一直走到 Planner 才以 ``ModelRequestBudgetExceeded`` 结束。
    现在整次调用由 ``graph.workflow.invoke_generate_plan`` 的 ``asyncio.timeout`` 兜住：
    ``budget.used == 0`` 证明模型链路根本没有开始，也不写 draft／rejected；有无原 active 行两种
    情况的原 active 不变断言由 ``_harness`` 退出时统一给出（stage4.md §9.2）。
    """
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text()],
        active_plan=active_plan,
        assemble_delay_seconds=0.5,
    ) as h:
        budget = ModelRequestBudget(run_timeout_seconds=0.05)
        with pytest.raises(TimeoutError):
            await h.invoke("生成计划", budget=budget)

        assert budget.used == 0  # 非模型节点被 Run 时限截断，未消耗任何模型请求
        assert h.model.calls == []
        assert (
            h.assembler.assemblies == 1
        )  # 超时发生在非模型节点 load_context（MemoryAssembler）
        assert h.persistence.writes == []
        assert await _count(h.db, "draft") == 0
        assert await _count(h.db, "rejected") == 0


# ---------- §5.3 同一 thread 的多次 Run：路由与修订预算按 Run 决定（stage4.md §3.6） ----------


async def test_second_run_on_the_same_thread_is_not_terminated_by_the_previous_run(
    tmp_path: Path,
) -> None:
    """同 thread 的第二次 Run 照常走计划链路：不被上一次 Run 留在存档里的 ``safety_stop`` 带着终止。"""
    async with _harness(tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]) as h:
        first = await h.invoke("最近麻木，想换计划")
        assert first["termination_reason"] == "safety_stop"
        assert h.model.calls == []

        second = await h.invoke("生成一份增肌计划")

        assert second["termination_reason"] is None
        assert len(h.model.planner_calls) == 1
        assert len(h.model.evaluator_calls) == 1
        assert second["confirmation"] == "pending"
        assert await _count(h.db, "draft") == 1


async def test_revision_budget_is_per_run_on_the_same_thread(tmp_path: Path) -> None:
    """上一次 Run 用掉的那一次修订不占本次 Run 的额度：同 thread 的第二次 Run 仍有一次修订。"""
    blocking = _plan_text(exercise_id="not-in-catalog")
    async with _harness(
        tmp_path,
        planner=[blocking, blocking, blocking, _plan_text()],
        evaluator=[_rubric_text()],
    ) as h:
        first = await h.invoke("生成计划")
        assert first["termination_reason"] == "reject_draft"
        assert first["revision_count"] == 1
        assert len(h.model.planner_calls) == 2  # 首次候选 ＋ 该 Run 唯一一次修订
        assert h.model.evaluator_calls == []

        second = await h.invoke("生成计划")

        assert second["termination_reason"] is None
        assert second["revision_count"] == 1
        assert len(h.model.planner_calls) == 4  # 第二次 Run 也拿到自己的首次候选与修订
        assert len(h.model.evaluator_calls) == 1
        assert second["confirmation"] == "pending"
        assert await _count(h.db, "rejected") == 1
        assert await _count(h.db, "draft") == 1


# ---------- §5.4 画像缺失与无 API Key 运行 ----------


@pytest.mark.parametrize("weekly", [Fact.unknown(), Fact.denied()])
async def test_missing_weekly_frequency_fails_before_the_planner(
    tmp_path: Path, anyio_backend: str, weekly: Fact[int]
) -> None:
    """``weekly_frequency`` 为 unknown／denied：Planner 前明确失败，不用默认频率或模型猜测。"""
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text()],
        profile=_profile(weekly_frequency=weekly),
    ) as h:
        with pytest.raises(RequiredProfileMissing) as excinfo:
            await h.invoke("生成计划")

        assert "每周训练次数" in str(excinfo.value)
        assert h.model.calls == []
        assert h.skills.loaded == []
        assert h.assembler.assemblies == 0
        assert h.persistence.writes == []
        assert await _count(h.db, "draft") == 0
        assert await _count(h.db, "rejected") == 0


async def test_unregistered_profile_fails_before_the_planner(tmp_path: Path) -> None:
    """未建档画像即缺失必需事实：同样在 Planner 前明确失败，不伪造空画像。"""
    async with _harness(tmp_path, profile=None) as h:
        with pytest.raises(RequiredProfileMissing):
            await h.invoke("生成计划")

        assert h.model.calls == []
        assert await _count(h.db, "draft") == 0


async def test_fixed_double_workflow_needs_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整条固定替身链路在没有任何 ``MODEL_*`` 环境变量时照常运行，State 只有冻结字段。"""
    for name in (MODEL_API_KEY_ENV, MODEL_BASE_URL_ENV, MODEL_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)

    async with _harness(tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]) as h:
        result = await h.invoke("生成计划")

        assert result["confirmation"] == "pending"
        assert await _count(h.db, "draft") == 1
        snapshot = await h.state()
        assert set(snapshot.values) <= set(WorkflowState.__annotations__)


# ---------- §5.4 固定 Run 限制与生产模型入口 ----------


def test_run_limits_are_fixed_and_the_request_budget_is_per_invocation() -> None:
    """固定上限：单次请求 60 秒、单 Run 180 秒、最多 5 次模型请求（决策 8B）。"""
    assert MODEL_REQUEST_TIMEOUT_SECONDS == 60
    assert GRAPH_RUN_TIMEOUT_SECONDS == 180
    assert MAX_MODEL_REQUESTS_PER_RUN == 5

    budget = ModelRequestBudget()
    assert budget.request_timeout_seconds == 60
    assert budget.run_timeout_seconds == 180
    assert budget.max_requests == 5

    for _ in range(MAX_MODEL_REQUESTS_PER_RUN):
        assert budget.begin_request() == 60
    assert budget.used == MAX_MODEL_REQUESTS_PER_RUN
    with pytest.raises(ModelRequestBudgetExceeded):
        budget.begin_request()

    assert ModelRequestBudget().used == 0  # 每次 invocation 一份，不跨 Run 累计


def test_request_timeout_is_capped_by_the_remaining_run_time() -> None:
    """单次请求超时不超过 Run 剩余时限；时限用尽即运行错误。"""
    budget = ModelRequestBudget(run_timeout_seconds=0.02)
    assert 0 < budget.begin_request() <= 0.02

    expired = ModelRequestBudget(run_timeout_seconds=-1.0)
    with pytest.raises(ModelRequestBudgetExceeded):
        expired.begin_request()


def test_production_model_entry_validates_environment_and_fixes_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三个环境变量缺一即配置错误；齐全时创建的具体模型对象固定 60 秒单次请求超时。"""
    for name in (MODEL_API_KEY_ENV, MODEL_BASE_URL_ENV, MODEL_MODEL_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ModelConfigurationError):
        build_chat_model()

    secret = "stage4-secret-key-must-not-appear"
    monkeypatch.setenv(MODEL_API_KEY_ENV, secret)
    monkeypatch.setenv(MODEL_BASE_URL_ENV, "https://stage4-provider.invalid/v1")
    with pytest.raises(ModelConfigurationError) as excinfo:
        openai_compatible_model_call()
    assert MODEL_MODEL_ENV in str(excinfo.value)
    assert secret not in str(excinfo.value)

    monkeypatch.setenv(MODEL_MODEL_ENV, "stage4-model")
    chat = build_chat_model()
    assert chat.request_timeout == MODEL_REQUEST_TIMEOUT_SECONDS
    assert chat.model_name == "stage4-model"
    assert callable(openai_compatible_model_call())


# ---------- §8.7 Checkpoint 等待边界、重启恢复与 thread 隔离 ----------


async def test_business_draft_is_committed_before_the_checkpoint_records_the_waiting_boundary(
    tmp_path: Path,
) -> None:
    """§3.10 固定顺序 ＋ §8.7「draft 持久化后才发生 interrupt」。

    存档是真实 ``AsyncSqliteSaver``（``graph.checkpointer.open_checkpointer``），只在 ``aput`` 外包一层
    观察：等待边界（``channel_values['confirmation'] == 'pending'``）的每次写入都在存档提交点读业务库，
    必须已能按 ``channel_values['draft_plan_id']`` 读到同一 draft 与同一 Evaluator 结果。
    """
    observations: list[dict[str, Any]] = []
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text()],
        saver_wrapper=_observing_boundary_saver(observations),
    ) as h:
        result = await h.invoke("生成计划")

        assert observations, "没有观察到等待边界的存档写入，测试无效"
        for observation in observations:
            assert observation["draft_plan_id"] == result["draft_plan_id"]
            assert observation["status"] == "draft"
            assert observation["content"] is not None
            assert observation["evaluator_result"] is not None
            assert plan_draft_from_json(json.dumps(observation["content"])) == (
                result["draft_plan"]
            )
            assert evaluation_result_from_json(
                json.dumps(observation["evaluator_result"])
            ) == result["evaluation"]


async def test_waiting_boundary_survives_checkpointer_restart_on_the_same_thread(
    tmp_path: Path,
) -> None:
    """§8.7「重启恢复到 wait_for_confirmation」＋ §6 Subtask 05 任务 3／4。

    关闭真实存档连接后重开同一 SQLite 文件：同一 thread 仍定位到等待位置，interrupt 载荷恰为
    ``draft_plan_id``；按 ID 从业务库重读得到统一 ``PlanDraft`` 与 ``EvaluationResult``；按 ``None``
    恢复只是重入 interrupt，不产生任何业务提交（计划／日程行数、draft 行与原 active 逐字段不变）。
    """
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(DEFAULT_PROFILE)
        await _insert_plan(db, version=1, status="active")
        active_before = await _active_rows(db)
        doubles = _doubles(db, planner=[_plan_text()], evaluator=[_rubric_text()])
        checkpoint_path = tmp_path / "checkpoints.db"

        async with open_checkpointer(checkpoint_path) as saver:
            first = await invoke_generate_plan(
                build_generate_plan_graph(doubles.deps(db), checkpointer=saver),
                _initial_state("生成计划"),
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(business_day=BUSINESS_DAY),
            )

        assert first["confirmation"] == "pending"
        draft_plan_id = first["draft_plan_id"]
        assert len(doubles.persistence.writes) == 1
        counts_before = await _row_counts(db)
        draft_before = await PlanReadService(db).get_by_id(draft_plan_id)

        # 重开同一存档文件：内存里的图与 State 都不参与恢复，只能重读 checkpoint 与业务库。
        async with open_checkpointer(checkpoint_path) as saver:
            recovered = build_generate_plan_graph(doubles.deps(db), checkpointer=saver)
            snapshot = await recovered.aget_state(thread_config(CONVERSATION_ID))

            assert snapshot.next == ("wait_for_confirmation",)
            assert len(snapshot.interrupts) == 1
            assert snapshot.interrupts[0].value == {"draft_plan_id": draft_plan_id}
            assert snapshot.values["draft_plan_id"] == draft_plan_id
            assert snapshot.values["confirmation"] == "pending"

            draft = await PlanReadService(db).get_by_id(draft_plan_id)
            assert draft is not None and draft.status == "draft"
            assert plan_draft_from_json(json.dumps(draft.structured_content)) == (
                plan_draft_from_json(_plan_text())
            )
            assert evaluation_result_from_json(
                json.dumps(draft.evaluator_result)
            ) == first["evaluation"]

            resumed = await invoke_generate_plan(
                recovered,
                None,
                thread_config(CONVERSATION_ID),
                GeneratePlanRun(business_day=BUSINESS_DAY),
            )

        assert resumed["confirmation"] == "pending"
        assert resumed["__interrupt__"][0].value == {"draft_plan_id": draft_plan_id}
        assert len(doubles.persistence.writes) == 1  # 恢复读取没有产生第二次业务提交
        assert await _row_counts(db) == counts_before
        assert await _count(db, "draft") == 1
        assert await _count(db, "rejected") == 0
        assert await PlanReadService(db).get_by_id(draft_plan_id) == draft_before
        assert await _active_rows(db) == active_before
        assert len(await _active_rows(db)) <= 1
    finally:
        await db.close()


async def test_checkpointed_threads_do_not_mix(tmp_path: Path) -> None:
    """§8.7「不同 thread 不混用」＋ §3.8 单 draft 约束。

    单用户同时最多一个可确认 draft（``003_rejected_plan_status.sql:54`` 的 ``idx_plans_single_draft``），
    因此第二个会话用二次阻断失败走到 ``reject_draft`` 终态，而不是第二条 draft 行（与 Stage 3
    ``test_stage3_checkpoint.py::test_checkpointed_threads_do_not_share_state`` 的 draft ＋ rejected 口径
    一致）：每个会话的 State、interrupt 载荷与业务行只属于该 thread，从未使用过的 thread 没有下一步。
    """
    blocking = _plan_text(exercise_id="not-in-catalog")
    async with _harness(
        tmp_path,
        planner=[_plan_text(), blocking, blocking],
        evaluator=[_rubric_text()],
    ) as h:
        first = await h.invoke("生成计划")
        draft_plan_id = first["draft_plan_id"]
        snapshot_before = await h.state()
        draft_before = await PlanReadService(h.db).get_by_id(draft_plan_id)

        second = await h.invoke("生成计划", conversation_id=OTHER_CONVERSATION_ID)

        # 第二个会话跑过后，第一个会话的存档与业务 draft 逐字段不变（不串线、不被覆盖）。
        assert second["termination_reason"] == "reject_draft"
        assert "__interrupt__" not in second
        # 第二个会话确实走完了自己的候选与一次修订：不是提前失败造成的「不混用」假象。
        assert len(h.model.planner_calls) == 3
        snapshot_a = await h.state()
        assert snapshot_a == snapshot_before
        assert await PlanReadService(h.db).get_by_id(draft_plan_id) == draft_before

        assert snapshot_a.values["conversation_id"] == CONVERSATION_ID
        assert snapshot_a.values["draft_plan_id"] == draft_plan_id
        assert snapshot_a.values["confirmation"] == "pending"
        assert snapshot_a.next == ("wait_for_confirmation",)
        assert [item.value for item in snapshot_a.interrupts] == [
            {"draft_plan_id": draft_plan_id}
        ]

        other = await h.state(OTHER_CONVERSATION_ID)
        assert other.values["conversation_id"] == OTHER_CONVERSATION_ID
        assert other.values.get("draft_plan_id") is None
        assert other.values["termination_reason"] == "reject_draft"
        assert other.next == ()
        assert other.interrupts == ()

        absent = await h.state(ABSENT_CONVERSATION_ID)
        assert absent.next == ()
        assert absent.values == {}
        assert absent.interrupts == ()

        assert await _count(h.db, "draft") == 1
        assert await _count(h.db, "rejected") == 1


async def test_checkpoint_write_failure_keeps_the_committed_business_draft(
    tmp_path: Path,
) -> None:
    """§3.10／§9.1／§9.3：等待边界的存档写入失败必须上报，但不伪装成跨库原子事务。

    存档替身在 ``confirmation == "pending"`` 时从真实 ``aput`` 抛错：已提交的业务 draft 保留（供
    Stage 5 的数据库 fallback 用），不写 rejected、不动原 active（``_harness`` 退出时仍断言），也没有
    任何 interrupt 载荷落盘。
    """
    attempted: list[Any] = []
    async with _harness(
        tmp_path,
        planner=[_plan_text()],
        evaluator=[_rubric_text()],
        saver_wrapper=_failing_boundary_writes(attempted),
    ) as h:
        with pytest.raises(CheckpointWriteFailure):
            await h.invoke("生成计划")

        assert len(attempted) == 1
        draft = await PlanReadService(h.db).get_by_id(attempted[0])
        assert draft is not None and draft.status == "draft"
        assert plan_draft_from_json(json.dumps(draft.structured_content)) == (
            plan_draft_from_json(_plan_text())
        )
        persisted = evaluation_result_from_json(json.dumps(draft.evaluator_result))
        assert persisted.passed is True
        assert len(h.persistence.writes) == 1
        assert await _count(h.db, "draft") == 1
        assert await _count(h.db, "rejected") == 0
        assert (await h.state()).interrupts == ()


async def test_checkpoint_file_carries_no_provider_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``LANGGRAPH_REFACTOR_PLAN.md:149`` ＋ §3.7／§8.7：序列化存档不含三个环境变量的名称与取值。"""
    api_key = "stage4-checkpoint-api-key-must-not-persist"
    base_url = "https://stage4-checkpoint-provider.invalid/v1"
    model_name = "stage4-checkpoint-model-must-not-persist"
    monkeypatch.setenv(MODEL_API_KEY_ENV, api_key)
    monkeypatch.setenv(MODEL_BASE_URL_ENV, base_url)
    monkeypatch.setenv(MODEL_MODEL_ENV, model_name)

    async with _harness(tmp_path, planner=[_plan_text()], evaluator=[_rubric_text()]) as h:
        result = await h.invoke("生成计划")
        assert result["confirmation"] == "pending"

    serialized = (tmp_path / "checkpoints.db").read_bytes()
    # 非空断言：确认读到的是真实落盘的存档，而不是空文件。
    assert CONVERSATION_ID.encode() in serialized
    for forbidden in (
        MODEL_API_KEY_ENV,
        MODEL_BASE_URL_ENV,
        MODEL_MODEL_ENV,
        api_key,
        base_url,
        model_name,
    ):
        assert forbidden.encode() not in serialized


# ---------- §2.2：Stage 4 子图没有 Agent 路由／SSE；Subtask 05 的新增入口在此正向取证 ----------

#: SSE 入口形态只允许出现在 ``api/routes_agent.py``：别的 api 模块出现这些词即说明 SSE 入口外泄。
FORBIDDEN_SSE_TOKENS: tuple[str, ...] = (
    "StreamingResponse",
    "EventSource",
    "text/event-stream",
)
#: 路由注册以外还能直接新增入口的 FastAPI 方法。
DIRECT_ROUTE_REGISTRATIONS: tuple[str, ...] = ("add_route", "add_api_route")
#: §3.7 的三个 Agent 端点：Stage 5 的唯一新增路由。
AGENT_ROUTES: tuple[str, ...] = (
    "/api/agent/run",
    "/api/agent/confirm",
    "/api/agent/reject",
)


def _registered_paths(routes: Sequence[Any]) -> set[str]:
    """已注册路径（含 ``include_router`` 的子树）：不同 FastAPI 版本把子树包在 ``routes`` 或原始 router 里。"""
    paths: set[str] = set()
    for route in routes:
        nested = getattr(route, "routes", None)
        if not nested:
            original = getattr(route, "original_router", None)
            nested = getattr(original, "routes", None) if original is not None else None
        if nested:
            paths |= _registered_paths(nested)
        elif path := getattr(route, "path", None):
            paths.add(path)
    return paths


def test_stage5_registers_the_agent_endpoints_and_keeps_sse_in_routes_agent(
    tmp_path: Path,
) -> None:
    """§4.5 定点更新：Stage 4 的「无 routes_agent／SSE」否定断言改为 Stage 5 正向断言。

    ``create_app`` 注册四个既有路由 ＋ ``routes_agent``（三个 Agent 端点，§3.7）；SSE 入口形态（
    ``StreamingResponse``／``text/event-stream``）只允许出现在 ``api/routes_agent.py``，其它 api 模块
    仍不得新增 SSE 入口，也不得新增 checkpoint 查询类路径（``stream``／``sse``）。
    """
    tree = ast.parse((BACKEND_ROOT / "api" / "app.py").read_text(encoding="utf-8"))
    create_app_node = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    registered = [
        node.args[0].value.id
        for node in ast.walk(create_app_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
        and isinstance(node.args[0], ast.Attribute)
        and isinstance(node.args[0].value, ast.Name)
    ]
    assert registered == [
        "routes_profile",
        "routes_records",
        "routes_plans",
        "routes_stats",
        "routes_agent",
    ]
    assert not [
        node
        for node in ast.walk(create_app_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in DIRECT_ROUTE_REGISTRATIONS
    ]

    for module in sorted((BACKEND_ROOT / "api").glob("*.py")):
        source = module.read_text(encoding="utf-8")
        if module.name == "routes_agent.py":
            assert "StreamingResponse" in source
            assert "text/event-stream" in source
            continue
        for token in FORBIDDEN_SSE_TOKENS:
            assert token not in source, f"{module.name} 出现了 SSE 入口：{token}"

    app = create_app(tmp_path)
    api_paths = {
        path for path in _registered_paths(app.routes) if path.startswith("/api")
    }
    assert api_paths, "没有读到已注册的表单 API 路由，测试无效"
    assert set(AGENT_ROUTES) <= api_paths
    assert not [path for path in api_paths if "stream" in path or "sse" in path]
