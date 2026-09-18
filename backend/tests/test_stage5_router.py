"""Stage 5：§2.6 Router 契约的代码前断言与 Router／非计划分支的行为。

依据：``refactor-log/stage5.md`` §2.6／§2.9／§4.1／§5.1；
``Fit-Agent-LangGraph-重构讨论总结.md`` §4.1／§4.3；``LANGGRAPH_REFACTOR_PLAN.md`` §9.1。

上半部分只冻结契约：

1. ``refactor-log/stage5.md`` §2.6 的表格整行冻结：五类 intent 的封闭词表、零／多命中回退一次模型分类、
   严格枚举输出、五类行为表；加词、删词、加行或改写即失败。
2. 已合入源码中 Stage 5 不得改动的事实：``graph/state.py::INTENTS`` 是同一份五类词表（讨论总结 §4.1
   路由表、REFACTOR_PLAN §9.1），``config.py`` 的 60／180／5 上限不变（本文件 §2.9）。

下半部分用固定替身驱动 ``graph/router.py::classify_intent`` 与
``graph/workflow.py::invoke_agent_run``：封闭短语单命中不调分类模型、零／多命中恰调一次、严格枚举外
或额外字段是运行错误、Router 与计划链路共享每 Run 5 次预算，以及三类非计划 intent 的只读行为
（form 只引导表单 API、view 的数值逐项等于 ``StatsService`` 输出、NL 已按 Stage 6 进入结构化提取
但仍不在确认前写库，都不写库）。
计划事实用 ``tmp_path`` 下的真实迁移库；计划链路只在本文件里跑最坏路径（不需要 active 计划行）。

整份文件不调真实模型、不需要任何 ``MODEL_*`` 环境变量；调整计划的图行为在
``tests/test_stage5_adjust_and_confirm_graph.py``。
"""

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from config import (
    GRAPH_RUN_TIMEOUT_SECONDS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)
from domain.actions.service import ActionCatalogService
from domain.plans.service import (
    PlanActivationService,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.schema import Fact, Profile
from domain.profile.service import ProfileService
from domain.records.service import WorkoutRecordsService
from domain.stats.repo import StatsRepo
from domain.stats.service import StatsService
from graph.checkpointer import open_checkpointer, thread_config
from graph.context import MemoryAssembler
from graph.model import InvalidModelResponse
from graph.nodes import (
    EVALUATOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    GeneratePlanDeps,
    GeneratePlanRun,
    ModelRequestBudget,
    ModelRequestBudgetExceeded,
)
from graph.router import (
    ADJUST_PLAN_PHRASES,
    FORM_RECORD_PHRASES,
    GENERATE_PLAN_PHRASES,
    NATURAL_LANGUAGE_RECORD_FACT_MARKERS,
    NATURAL_LANGUAGE_RECORD_VERBS,
    ROUTER_SYSTEM_PROMPT,
    VIEW_PROGRESS_PHRASES,
    classify_intent,
    deterministic_intents,
)
from graph.skills import LoadedSkill, SkillLoader
from graph.state import INTENTS
from graph.workflow import (
    FORM_RECORD_GUIDE,
    NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
    NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
    VIEW_PROGRESS_SYSTEM_PROMPT,
    AgentRunDeps,
    AgentRunResult,
    build_generate_plan_graph,
    invoke_agent_run,
)
from storage.db import Database
from tests.conftest import Stage5Plan

#: §2.6 前言：Router 是函数/节点，不是第三个 Agent。
ROUTER_PREAMBLE = "Router 为函数/节点，不是第三 Agent。请求先 trim；英文匹配忽略大小写。"

#: §2.6 封闭词表整表（首列 intent，第二列确定性命中条件）；行序即路由表顺序，条件即封闭短语集合。
ROUTER_PHRASE_TABLE = (
    ("`form_record`", "`打开打卡表单`／`使用表单记录`／`表单打卡`"),
    (
        "`natural_language_record`",
        "记录动词（`记录`／`打卡`／`练了`／`完成了`） + 事实标记（`kg`／`公斤`／`次`／`组`／`秒`／`今天`／`昨天`）",
    ),
    (
        "`view_progress`",
        "`查看进步`／`训练进展`／`最近表现`／`个人最佳`／`PB`／`趋势`／`看板`",
    ),
    ("`generate_plan`", "`生成计划`／`制定计划`／`新训练计划`／`做个训练计划`"),
    ("`adjust_plan`", "`调整计划`／`修改计划`／`改计划`／`调整训练安排`"),
)

#: §2.6 五类 intent 行为整表：两类计划进子图，form／view／NL 都不写库。
ROUTER_BEHAVIOR_TABLE = (
    ("`generate_plan`", "进入计划子图"),
    ("`adjust_plan`", "进入 adjustment 分支"),
    ("`form_record`", "只引导使用既有表单 API"),
    ("`view_progress`", "查询 `StatsService`，模型只解释结果"),
    ("`natural_language_record`", "明确 Stage 6 未实现"),
)

#: §2.6 兜底与严格输出：零命中或多 intent 命中才调一次模型分类；只接受五类枚举，不猜默认值。
ROUTER_FALLBACK_AND_STRICT_ENUM = (
    "单一命中直接返回。零命中或多 intent 命中时只调用一次模型分类，严格输出：",
    "非法枚举、额外字段、非对象或非法 JSON 均为 Run error。Router 与计划链路共享同一 "
    "`ModelRequestBudget`；限制仍为 **60s / 180s / 5 次模型请求**。",
)


def test_router_vocabulary_is_the_frozen_five_intents(stage5_plan: Stage5Plan) -> None:
    """§2.6：Router 只判五类 intent，与 ``graph/state.py::INTENTS`` 是同一份词表，不是第三个 Agent。"""
    section = stage5_plan.section("2.6 Router")

    assert INTENTS == tuple(intent.strip("`") for intent, _ in ROUTER_PHRASE_TABLE)
    assert ROUTER_PREAMBLE in section


def test_router_closed_phrase_table_is_frozen(stage5_plan: Stage5Plan) -> None:
    """§2.6 命中条件是封闭词表：整表相等，增删词或改写条件即失败。"""
    assert stage5_plan.table("2.6 Router", "确定性命中条件") == ROUTER_PHRASE_TABLE


def test_router_stage5_behavior_table_is_frozen(stage5_plan: Stage5Plan) -> None:
    """§2.6 五类行为整表相等：form 只引导表单、view 只读统计、NL 记 Stage 6 未实现、两类计划进子图。"""
    assert stage5_plan.table("2.6 Router", "行为") == ROUTER_BEHAVIOR_TABLE


def test_router_zero_or_multiple_hits_call_the_model_once_with_a_strict_enum(
    stage5_plan: Stage5Plan,
) -> None:
    """§2.6：只在命中为空或多 intent 命中时分类一次；输出只能是五类枚举，额外字段或非法值即 Run error。"""
    section = stage5_plan.section("2.6 Router")

    assert [line for line in ROUTER_FALLBACK_AND_STRICT_ENUM if line not in section] == []


def test_router_and_plan_chain_reuse_the_frozen_run_limits() -> None:
    """§2.6／§2.9：分类与计划链路共享每 Run 5 次预算，60 秒／180 秒上限不变。"""
    assert MAX_MODEL_REQUESTS_PER_RUN == 5
    assert MODEL_REQUEST_TIMEOUT_SECONDS == 60
    assert GRAPH_RUN_TIMEOUT_SECONDS == 180


# ---------- §2.6 Router 行为：封闭命中、一次兜底与严格枚举 ----------

BUSINESS_DAY = date(2026, 6, 1)
FIXED_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
CONVERSATION_ID = "stage5-router-conversation"
SCHEDULED_ON = "2026-06-02"
PULL_UP = "pull-up"  # 纯自重引体：处方不携带负荷
BENCH_PRESS = "barbell-bench-press"  # 外加负重动作：用来造一条非 active 计划动作的 PB
BENCH_CONVENTION = "barbell_includes_bar_total"
PROFILE_WEEKLY_FREQUENCY = 1

#: 五类 intent 的封闭短语单命中用例：每条都必须 0 次分类模型调用（§2.6 确定性命中）。
SINGLE_HIT_REQUESTS: tuple[tuple[str, str], ...] = (
    ("打开打卡表单", "form_record"),
    ("使用表单记录一下", "form_record"),
    ("表单打卡", "form_record"),
    ("记录今天深蹲3组5次", "natural_language_record"),
    ("打卡：今天练了5kg深蹲", "natural_language_record"),
    ("完成了昨天那练", "natural_language_record"),
    ("查看进步", "view_progress"),
    ("训练进展", "view_progress"),
    ("最近表现", "view_progress"),
    ("个人最佳", "view_progress"),
    ("看下PB", "view_progress"),
    ("看趋势", "view_progress"),
    ("打开看板", "view_progress"),
    ("生成计划", "generate_plan"),
    ("制定计划", "generate_plan"),
    ("新训练计划", "generate_plan"),
    ("做个训练计划", "generate_plan"),
    ("调整计划", "adjust_plan"),
    ("修改计划", "adjust_plan"),
    ("改计划", "adjust_plan"),
    ("调整训练安排", "adjust_plan"),
)

#: 零命中用例：非词表文本，以及只满足记录动词或只满足事实标记的单条件文本。
ZERO_HIT_REQUESTS: tuple[str, ...] = (
    "帮我看看现在能不能加练",
    "记录一下",  # 只有记录动词
    "今天好累",  # 只有事实标记
    "练了",  # 只有记录动词
)

#: 多 intent 命中用例（按 ``graph.state.INTENTS`` 顺序返回全部命中项）。
MULTIPLE_HIT_REQUESTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("表单打卡记录今天3组深蹲", ("form_record", "natural_language_record")),
    ("生成计划还是调整计划", ("generate_plan", "adjust_plan")),
)

#: 严格枚举之外的模型输出：非法枚举（含大小写不同）、额外字段、非 JSON、非对象。
INVALID_ROUTER_RESPONSES: tuple[str, ...] = (
    '{"intent": "unknown"}',
    '{"intent": "Generate_Plan"}',
    '{"intent": "generate_plan", "confidence": 0.9}',
    '{"intent": null}',
    "不是 JSON",
    '["generate_plan"]',
)

#: 表格单元格里的行内代码（``§2.6`` 的短语与枚举都写在反引号里）。
_BACKTICKED = re.compile(r"`([^`]+)`")


class RecordingModel:
    """按系统提示词分派固定响应的模型替身；没有脚本就被调用即失败（确定性命中必须 0 次调用）。"""

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
        """某一类调用的原始记录（Router／Planner／Evaluator／统计解释各一套）。"""
        return [call for call in self.calls if call[0] == system_prompt]

    def payload(self, system_prompt: str, index: int = 0) -> dict[str, Any]:
        """第 ``index`` 次该类调用的用户载荷（JSON 文本解码）。"""
        return json.loads(self.calls_for(system_prompt)[index][1])


class CountingSkillLoader(SkillLoader):
    """Skill 加载计数替身：装载正文的名称顺序即证据。"""

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


def _profile() -> Profile:
    """固定画像：每周训练次数是明确值（生成计划的 Planner 前前提）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        weekly_frequency=Fact.known(PROFILE_WEEKLY_FREQUENCY),
        available_equipment=Fact.known(("barbell", "bodyweight")),
        explicit_preferences=Fact.denied(),
        current_level=Fact.known("中级"),
        known_injuries=Fact.denied(),
        forbidden_exercise_ids=Fact.denied(),
    )


def _intent_text(intent: str) -> str:
    """固定替身 Router 的分类响应：严格形状 ``{"intent": <五类之一>}``。"""
    return json.dumps({"intent": intent}, ensure_ascii=False)


def _plan_text() -> str:
    """固定替身 Planner 的响应文本：一个结构合法的单训练日计划（每周 1 练）。"""
    return json.dumps(
        {
            "goal": "增肌",
            "starts_on": BUSINESS_DAY.isoformat(),
            "explanation": "每周一练，覆盖既定目标动作",
            "weekly_frequency": PROFILE_WEEKLY_FREQUENCY,
            "training_days": [
                {
                    "scheduled_on": SCHEDULED_ON,
                    "exercises": [
                        {
                            "exercise_id": PULL_UP,
                            "sets": 3,
                            "prescription": {
                                "type": "bodyweight_reps",
                                "reps_min": 8,
                                "reps_max": 12,
                                "progression_note": None,
                            },
                        }
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )


def _rubric_text(*, goal_alignment: bool = True) -> str:
    """固定替身 Evaluator 的三个布尔判定；``goal_alignment=False`` 是硬门槛阻断。"""

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


async def _migrated(path: Path) -> Database:
    db = Database(path)
    await db.open()
    await db.migrate()
    return db


async def _row_counts(db: Database) -> dict[str, int]:
    """非计划分支的只读断言用业务行数快照（任何一张表变化即失败）。"""
    tables = (
        "plans",
        "plan_sessions",
        "workout_sessions",
        "workout_sets",
        "body_metrics",
    )

    async def op(conn: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in tables:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                counts[table] = int((await cursor.fetchone())[0])
        return counts

    return await db.under_lock(op)


async def _insert_weighted_work_set(
    db: Database, *, performed_on: date, weight_kg: float, reps: int
) -> None:
    """预置一次外加负重训练（额外训练：``plan_session_id`` 为 NULL）：给 view 造确定性 PB 与趋势事实。"""
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO workout_sessions (performed_on, plan_session_id) VALUES (?, NULL)",
            (performed_on.isoformat(),),
        )
        session_id = int(cursor.lastrowid or 0)
        await cursor.close()
        await conn.execute(
            "INSERT INTO workout_sets (workout_session_id, exercise_id, set_no, set_type,"
            " load_convention, weight_kg, reps) VALUES (?, ?, 1, 'work', ?, ?, ?)",
            (session_id, BENCH_PRESS, BENCH_CONVENTION, weight_kg, reps),
        )


async def _insert_body_metric(db: Database, *, measured_on: date, weight_kg: float) -> None:
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO body_metrics (measured_on, weight_kg, body_fat_pct) VALUES (?, ?, NULL)",
            (measured_on.isoformat(), weight_kg),
        )


@dataclass
class _Harness:
    """一次测试的图、业务库与固定替身：计划链路与非计划分支共用一个运行入口。"""

    graph: Any
    db: Database
    model: RecordingModel
    skills: CountingSkillLoader
    assembler: CountingAssembler

    async def invoke(
        self, request: str, *, budget: ModelRequestBudget | None = None
    ) -> AgentRunResult:
        """经唯一运行入口驱动一次请求（Router 也在里面，与计划链路共享同一份 Run 预算）。

        ``plans``／``persistence`` 供运行入口判断已有 draft 是复用还是同类替换（§2.4「已有 draft 时」）。
        """
        run = GeneratePlanRun(
            business_day=BUSINESS_DAY,
            budget=ModelRequestBudget() if budget is None else budget,
        )
        return await invoke_agent_run(
            self.graph,
            {"conversation_id": CONVERSATION_ID, "request": request},
            thread_config(CONVERSATION_ID),
            run,
            AgentRunDeps(
                model=self.model,
                stats=StatsService(self.db),
                plans=PlanReadService(self.db),
                persistence=PlanPersistenceService(self.db),
                catalog=ActionCatalogService(self.db),
                records=WorkoutRecordsService(self.db),
            ),
        )


@asynccontextmanager
async def _harness(
    tmp_path: Path, *, scripts: Mapping[str, Sequence[str]] | None = None
) -> AsyncIterator[_Harness]:
    """装配一次测试：临时业务库 ＋ 临时 checkpoint 存档 ＋ 固定替身（模型不入库、不读环境变量）。"""
    db = await _migrated(tmp_path / "fit_agent.db")
    try:
        await ProfileService(db).update(_profile())
        model = RecordingModel(scripts)
        skills = CountingSkillLoader()
        assembler = CountingAssembler(db)
        deps = GeneratePlanDeps(
            profiles=ProfileService(db),
            catalog=ActionCatalogService(db),
            stats=StatsRepo(db),
            assembler=assembler,
            skills=skills,
            persistence=PlanPersistenceService(db),
            plans=PlanReadService(db),
            activation=PlanActivationService(db),
            model=model,
            now=lambda: FIXED_NOW,
        )
        async with open_checkpointer(tmp_path / "checkpoints.db") as saver:
            yield _Harness(
                graph=build_generate_plan_graph(deps, checkpointer=saver),
                db=db,
                model=model,
                skills=skills,
                assembler=assembler,
            )
    finally:
        await db.close()


def test_router_phrase_constants_are_the_frozen_document_table() -> None:
    """实现侧封闭词表与 §2.6 契约表逐项相等：文档与源码任一漂移（增删、改写）都会失败。"""
    documented = {
        intent.strip("`"): _BACKTICKED.findall(cell)
        for intent, cell in ROUTER_PHRASE_TABLE
    }
    implemented = {
        "form_record": FORM_RECORD_PHRASES,
        "natural_language_record": NATURAL_LANGUAGE_RECORD_VERBS
        + NATURAL_LANGUAGE_RECORD_FACT_MARKERS,
        "view_progress": VIEW_PROGRESS_PHRASES,
        "generate_plan": GENERATE_PLAN_PHRASES,
        "adjust_plan": ADJUST_PLAN_PHRASES,
    }

    assert {intent: list(phrases) for intent, phrases in implemented.items()} == (
        documented
    )


def test_router_prompt_gives_only_the_request_and_the_five_intent_definitions() -> None:
    """§2.6：模型只接收当前请求与五类定义，并被要求严格输出五类枚举中的一个。"""
    assert '{"intent": "<五类之一>"}' in ROUTER_SYSTEM_PROMPT
    for intent in INTENTS:
        assert intent in ROUTER_SYSTEM_PROMPT


@pytest.mark.parametrize("request_text, expected", SINGLE_HIT_REQUESTS)
# 说明：本仓库用 conftest 给所有 async 测试自动打 anyio 标记；该组合下参数化 async 测试必须显式
# 声明 ``anyio_backend``（后端仍是 conftest 固定的 asyncio），否则 pytest 无法解析参数。
async def test_single_closed_phrase_hit_returns_the_intent_without_a_model_call(
    request_text: str, expected: str, anyio_backend: str
) -> None:
    """§2.6 确定性命中：五类各条封闭短语单命中即直接返回该 intent，分类模型 0 次调用。"""
    model = RecordingModel()
    budget = ModelRequestBudget()

    assert deterministic_intents(request_text) == (expected,)
    assert (
        await classify_intent(request_text, model=model, budget=budget) == expected
    )
    assert model.calls == []
    assert budget.used == 0


@pytest.mark.parametrize("request_text", ZERO_HIT_REQUESTS)
def test_non_vocabulary_and_single_condition_requests_hit_nothing(
    request_text: str,
) -> None:
    """§2.6：非词表文本零命中；``natural_language_record`` 必须同时有记录动词与事实标记。"""
    assert deterministic_intents(request_text) == ()


@pytest.mark.parametrize("request_text, expected", MULTIPLE_HIT_REQUESTS)
def test_multiple_intent_hits_return_every_hit_in_vocabulary_order(
    request_text: str, expected: tuple[str, ...]
) -> None:
    """§2.6：多 intent 命中要原样报出全部命中项（顺序即词表顺序），由兜底分类决定最终 intent。"""
    assert deterministic_intents(request_text) == expected


@pytest.mark.parametrize(
    "request_text, classified",
    [("帮我弄一份练腿的安排", "generate_plan"), ("表单打卡记录今天3组深蹲", "form_record")],
)
async def test_zero_or_multiple_hits_call_the_model_once_with_the_trimmed_request(
    request_text: str, classified: str, anyio_backend: str
) -> None:
    """§2.6：零命中或多命中时恰调一次模型分类，载荷只有 trim 后的当前请求，模型定义在系统提示词里。"""
    model = RecordingModel({ROUTER_SYSTEM_PROMPT: [_intent_text(classified)]})
    budget = ModelRequestBudget()

    assert len(deterministic_intents(request_text)) != 1
    intent = await classify_intent(f"  {request_text}  ", model=model, budget=budget)

    assert intent == classified
    assert len(model.calls) == 1
    assert model.calls[0][1] == request_text
    assert budget.used == 1


@pytest.mark.parametrize("response", INVALID_ROUTER_RESPONSES)
async def test_router_output_outside_the_strict_enum_is_a_run_error(
    response: str, anyio_backend: str
) -> None:
    """§2.6：非法枚举、额外字段、非 JSON、非对象都是 Run error，不退化成默认 intent。"""
    model = RecordingModel({ROUTER_SYSTEM_PROMPT: [response]})
    budget = ModelRequestBudget()

    with pytest.raises(InvalidModelResponse):
        await classify_intent("帮我弄一份练腿的安排", model=model, budget=budget)
    assert budget.used == 1


async def test_worst_case_run_shares_one_budget_across_router_and_plan_chain(
    tmp_path: Path,
) -> None:
    """§2.6／§2.9：分类 1 次 ＋ 计划链路 4 次（Planner、Rubric、修订、修订后的 Rubric）恰为 5 次。

    走最坏路径需要一次兜底分类：请求零命中封闭词表，Router 调一次模型判成 ``generate_plan``；
    随后首轮 Rubric 失败触发修订，修订后再评一次才通过。整次 Run 用同一份 ``ModelRequestBudget``，
    第 6 次请求会被拒绝（每 Run 上限 5 次）。
    """
    budget = ModelRequestBudget()
    scripts = {
        ROUTER_SYSTEM_PROMPT: [_intent_text("generate_plan")],
        PLANNER_SYSTEM_PROMPT: [_plan_text(), _plan_text()],
        EVALUATOR_SYSTEM_PROMPT: [
            _rubric_text(goal_alignment=False),
            _rubric_text(),
        ],
    }
    async with _harness(tmp_path, scripts=scripts) as h:
        result = await h.invoke("帮我弄一份练腿的安排", budget=budget)

        assert [prompt for prompt, _ in h.model.calls] == [
            ROUTER_SYSTEM_PROMPT,
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
            PLANNER_SYSTEM_PROMPT,
            EVALUATOR_SYSTEM_PROMPT,
        ]
        assert budget.used == MAX_MODEL_REQUESTS_PER_RUN
        assert result.intent == "generate_plan"
        assert result.draft_plan_id is not None
        assert result.termination_reason is None
        # 生成计划写出的 draft 没有来源计划（``source_plan_id`` 只在调整分支写，§2.4）。
        generated = await PlanReadService(h.db).get_by_id(result.draft_plan_id)
        assert generated is not None and generated.source_plan_id is None
        assert h.model.calls[0][1] == "帮我弄一份练腿的安排"

    with pytest.raises(ModelRequestBudgetExceeded):
        budget.begin_request()


async def test_form_record_only_guides_to_the_existing_form_api(tmp_path: Path) -> None:
    """§2.6 行为表：``form_record`` 不写库，只给出去表单 API 的引导，不进入计划子图。"""
    async with _harness(tmp_path) as h:
        before = await _row_counts(h.db)
        result = await h.invoke("打开打卡表单")

        assert result.intent == "form_record"
        assert result.messages == (FORM_RECORD_GUIDE,)
        assert result.termination_reason is None
        assert result.draft_plan_id is None
        assert h.model.calls == []  # 确定性命中：分类与计划链路都没有模型调用
        assert h.assembler.assemblies == 0
        assert h.skills.loaded == []
        assert await _row_counts(h.db) == before


async def test_view_progress_explains_statservice_values_without_recomputing(
    tmp_path: Path,
) -> None:
    """§2.7／§2.6：``view_progress`` 只读既有 ``StatsService``，模型只解释，不写库、不重算数值。"""
    async with _harness(
        tmp_path, scripts={VIEW_PROGRESS_SYSTEM_PROMPT: ["最近一次卧推 60kg×5，趋势数据不足。"]}
    ) as h:
        await _insert_weighted_work_set(
            h.db, performed_on=date(2026, 5, 30), weight_kg=60.0, reps=5
        )
        await _insert_body_metric(h.db, measured_on=date(2026, 5, 20), weight_kg=79.0)
        await _insert_body_metric(h.db, measured_on=date(2026, 5, 30), weight_kg=80.0)
        stats = StatsService(h.db)
        bests = await stats.list_personal_bests()
        trend = await stats.trend_summary(BUSINESS_DAY)
        before = await _row_counts(h.db)

        result = await h.invoke("查看进步")

        assert result.intent == "view_progress"
        assert result.messages == ("最近一次卧推 60kg×5，趋势数据不足。",)
        assert result.draft_plan_id is None
        assert len(h.model.calls) == 1  # 确定性命中：只有一次统计解释调用
        payload = h.model.payload(VIEW_PROGRESS_SYSTEM_PROMPT)
        assert set(payload) == {"request", "personal_bests", "trend_summary"}
        assert payload["request"] == "查看进步"
        # 数值逐项等于既有 StatsService 的现算结果（模型不参与计算）。
        assert payload["personal_bests"] == json.loads(
            json.dumps([asdict(best) for best in bests], default=str)
        )
        assert payload["trend_summary"] == json.loads(
            json.dumps(asdict(trend), default=str)
        )
        assert payload["personal_bests"][0]["exercise_id"] == BENCH_PRESS
        assert payload["personal_bests"][0]["value"] == 60.0
        assert payload["trend_summary"]["weight_change"] == {
            "status": "ok",
            "current": 80.0,
            "current_on": "2026-05-30",
            "previous": 79.0,
            "previous_on": "2026-05-20",
            "change": 1.0,
        }
        assert payload["trend_summary"]["days_since_last_workout"]["days"] == 2
        assert h.assembler.assemblies == 0
        assert h.skills.loaded == []
        assert await _row_counts(h.db) == before


async def test_natural_language_record_extracts_without_writing(tmp_path: Path) -> None:
    """Stage 6 取代 Stage 5 的「未实现」声明：``natural_language_record`` 进入结构化提取，

    但仍不在确认前写业务库（本用例只断言行为边界与行数不变；SSE 事件顺序与 waiting 载荷的正本
    覆盖在 ``tests/test_stage6_natural_language.py``）。
    """
    scripts = {
        NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT: [
            json.dumps(
                {
                    "performed_on": BUSINESS_DAY.isoformat(),
                    "sets": [
                        {
                            "exercise_id": PULL_UP,
                            "set_no": 1,
                            "set_type": "work",
                            "reps": 8,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ],
        NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: ["固定替身摘要"],
    }
    async with _harness(tmp_path, scripts=scripts) as h:
        before = await _row_counts(h.db)
        result = await h.invoke("记录今天做8个引体")

        assert result.intent == "natural_language_record"
        assert result.messages == ("固定替身摘要",)
        assert result.draft_plan_id is None
        assert h.assembler.assemblies == 0
        assert await _row_counts(h.db) == before
