# 首批只读工具的行为边界：四个工具只经既有 Repo／Service 读事实，输出是标准 JSON 文本，
# 未声明参数被 args_schema 拒绝，业务表行数在调用前后完全一致，且不触达模型与预算。
# 事实用 tmp_path 下的真实迁移库与真实 Repo／Service；模型入口只用不可调用的替身。

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.registry import (
    PLANNING_TOOLS,
    PROGRESS_TOOLS,
    SCHEDULE_TOOLS,
)
from app.application.agent.harness.snapshot import PLAN_REQUIRED_FACTS
from app.application.agent.harness.tools import read_user_profile
from app.application.agent.harness.tools.common import TrainingHarnessContext
from app.application.agent.harness.tools.exercise_dataset.store import (
    InMemoryCanonicalExerciseDataset,
)
from app.application.ports import ModelGateway
from app.bootstrap import SqliteHealthProbe, build_repositories, build_services
from app.domain.plans.schema import PlanSession
from app.domain.profile.schema import Fact, Profile
from app.domain.records.schema import WorkoutSetInput
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.actions_repository import ExerciseRepo
from app.infrastructure.database.repositories.plans_repository import PlanRepo

BUSINESS_DAY = date(2026, 6, 1)
CREATED_AT = "2026-06-01T08:00:00+00:00"
CANCELLED_AT = "2026-06-02T08:00:00+00:00"
EXTRA_ARGUMENT = "business_day"

BARBELL_BACK_SQUAT = "barbell-back-squat"
BARBELL_BENCH_PRESS = "barbell-bench-press"
PULL_UP = "pull-up"
PLANK = "plank"

#: 只读断言口径：库内七张业务表任何一张变化即失败。
BUSINESS_TABLES = (
    "exercises",
    "athlete_profile",
    "body_metrics",
    "plans",
    "plan_sessions",
    "workout_sessions",
    "workout_sets",
)


class _UnusedBudget:
    """替身预算：只读工具不经 policy wrapper，被调用即失败。"""

    def begin_request(self) -> float:
        raise AssertionError("只读工具不发起模型请求")

    def take_tool_call(self) -> None:
        raise AssertionError("只读工具不经 policy wrapper，不扣工具预算")


async def _unavailable_model(*_args: Any) -> Any:
    raise AssertionError("只读工具不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model, structured=_unavailable_model, tools=_unavailable_model
)


@dataclass
class ReadHarness:
    """一次测试的迁移库、工具图与注入上下文。"""

    db: Database
    graph: CompiledStateGraph
    context: TrainingHarnessContext

    async def call(self, name: str, args: Mapping[str, Any] | None = None) -> ToolMessage:
        """经真实 ToolNode 调用一个工具，返回它生成的 ToolMessage。"""
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": dict(args or {}),
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }
        result = await self.graph.ainvoke(state, context=self.context)
        message = result["messages"][-1]
        assert isinstance(message, ToolMessage)
        return message

    async def payload(self, name: str, args: Mapping[str, Any] | None = None) -> Any:
        """调用工具并解码成功输出。"""
        message = await self.call(name, args)
        assert message.status == "success", message.content
        return json.loads(message.content)

    async def counts(self) -> dict[str, int]:
        """七张业务表的行数快照。"""

        async def op(conn: Any) -> dict[str, int]:
            counts: dict[str, int] = {}
            for table in BUSINESS_TABLES:
                async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    counts[table] = int((await cursor.fetchone())[0])
            return counts

        return await self.db.under_lock(op)

    async def seed_plan(self, content: Mapping[str, Any]) -> int:
        """预置一个 active 计划行，返回计划身份。"""
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO plans (version, status, source_plan_id, structured_content,"
                " created_at, confirmed_at) VALUES (1, 'active', NULL, ?, ?, ?)",
                (json.dumps(content, ensure_ascii=False), CREATED_AT, CREATED_AT),
            )
            try:
                return int(cursor.lastrowid or 0)
            finally:
                await cursor.close()

    async def seed_sessions(self, plan_id: int, days: Sequence[date]) -> None:
        """按训练日写入计划日程（真实 PlanRepo 写原语，外层事务由测试持有）。"""
        async with self.db.transaction() as conn:
            await PlanRepo(self.db).create_sessions_in_transaction(
                conn, plan_id, scheduled_on=days
            )

    async def cancel_sessions(self, plan_id: int, *, from_on: date) -> None:
        """取消某个计划从 ``from_on`` 起的日程。"""
        async with self.db.transaction() as conn:
            await PlanRepo(self.db).cancel_sessions_in_transaction(
                conn, plan_id, business_day=from_on, cancelled_at=CANCELLED_AT
            )

    async def plan_sessions(self, plan_id: int) -> tuple[PlanSession, ...]:
        return await PlanRepo(self.db).list_sessions(plan_id)

    async def seed_workout(
        self,
        performed_on: date,
        sets: Sequence[WorkoutSetInput],
        *,
        plan_session_id: int | None = None,
    ) -> int:
        """写入一次训练（真实 RecordsService），返回训练身份。"""
        services = build_services(
            build_repositories(self.db),
            self.db,
            SqliteHealthProbe(self.db, self.db.path.parent),
        )
        session = await services.records.create(
            performed_on, sets, plan_session_id=plan_session_id
        )
        return session.id


@asynccontextmanager
async def _harness(
    tmp_path: Path,
    tools: Sequence[Any] = (*SCHEDULE_TOOLS, *PROGRESS_TOOLS),
) -> AsyncIterator[ReadHarness]:
    db = Database(tmp_path / "fit_agent.db")
    await db.open()
    await db.migrate()
    try:
        repositories = build_repositories(db)
        services = build_services(
            repositories,
            db,
            SqliteHealthProbe(db, db.path.parent),
        )
        catalog_revision = await db.pragma_value("user_version")
        if not isinstance(catalog_revision, int):
            raise RuntimeError(
                f"迁移后 user_version 不是整数：{catalog_revision!r}"
            )
        context = TrainingHarnessContext(
            model=_MODEL,
            budget=_UnusedBudget(),
            business_day=BUSINESS_DAY,
            profiles=repositories.profiles,
            plans=PlanRepo(db),
            catalog=ExerciseRepo(db),
            records=services.records,
            stats=services.stats,
            dataset=InMemoryCanonicalExerciseDataset(
                await repositories.exercises.list_all(),
                catalog_revision=catalog_revision,
            ),
        )
        graph = StateGraph(HarnessState, context_schema=TrainingHarnessContext)
        graph.add_node("tools", ToolNode(list(tools)))
        graph.add_edge(START, "tools")
        yield ReadHarness(db=db, graph=graph.compile(), context=context)
    finally:
        await db.close()


def _plan_content(squat_id: str = BARBELL_BACK_SQUAT) -> dict[str, Any]:
    """三练的 active 计划：负重、计时、自重各一，窗口 2026-06-01–07。"""
    return {
        "goal": "增肌",
        "starts_on": "2026-06-01",
        "explanation": "每周三练",
        "weekly_frequency": 3,
        "training_days": [
            {
                "scheduled_on": "2026-06-01",
                "exercises": [
                    {
                        "exercise_id": squat_id,
                        "sets": 3,
                        "prescription": {
                            "type": "weighted_reps",
                            "reps_min": 5,
                            "reps_max": 8,
                            "progression_note": None,
                            "load": {
                                "status": "known",
                                "weight_kg": 60.0,
                                "source_workout_session_id": 1,
                                "source_set_no": 1,
                            },
                        },
                    },
                    {
                        "exercise_id": PLANK,
                        "sets": 2,
                        "prescription": {
                            "type": "timed",
                            "duration_seconds_min": 45,
                            "duration_seconds_max": 60,
                            "progression_note": None,
                        },
                    },
                ],
            },
            {
                "scheduled_on": "2026-06-02",
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
            },
            {
                "scheduled_on": "2026-06-03",
                "exercises": [
                    {
                        "exercise_id": BARBELL_BENCH_PRESS,
                        "sets": 3,
                        "prescription": {
                            "type": "weighted_reps",
                            "reps_min": 8,
                            "reps_max": 10,
                            "progression_note": None,
                            "load": {"status": "needs_calibration"},
                        },
                    }
                ],
            },
        ],
    }


def _squat_set(*, weight_kg: float = 60.0, reps: int = 5) -> WorkoutSetInput:
    """杠铃背蹲的一个工作组（外加负重口径与重量同现）。"""
    return WorkoutSetInput(
        exercise_id=BARBELL_BACK_SQUAT,
        set_no=1,
        reps=reps,
        set_type="work",
        load_convention="barbell_includes_bar_total",
        weight_kg=weight_kg,
    )


def _plank_set() -> WorkoutSetInput:
    """平板支撑的一个计时工作组：只有持续秒数。"""
    return WorkoutSetInput(
        exercise_id=PLANK,
        set_no=1,
        reps=None,
        set_type="work",
        duration_seconds=45,
    )


def _leaf_values(value: Any) -> list[Any]:
    """递归取出 JSON 结构的全部叶子值（用于断言只有标准 JSON 标量）。"""
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaf_values(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaf_values(item)]
    return [value]


async def test_read_active_plan_rejects_undeclared_arguments(tmp_path: Path) -> None:
    """无参工具也必须拒绝未声明字段：业务日只能来自注入上下文。"""
    async with _harness(tmp_path) as h:
        message = await h.call("read_active_plan", {EXTRA_ARGUMENT: "2026-06-02"})

        assert message.status == "error"
        assert EXTRA_ARGUMENT in message.content
        assert "Extra inputs are not permitted" in message.content


async def test_read_training_calendar_accepts_declared_boundaries(tmp_path: Path) -> None:
    """年份 2000–2100、月份 1–12 的四个端点都合法，空月只回空 days。"""
    async with _harness(tmp_path) as h:
        assert await h.payload("read_training_calendar", {"year": 2000, "month": 1}) == {
            "month": "2000-01",
            "from_on": "2000-01-01",
            "to_on": "2000-01-31",
            "days": [],
        }
        assert await h.payload("read_training_calendar", {"year": 2100, "month": 12}) == {
            "month": "2100-12",
            "from_on": "2100-12-01",
            "to_on": "2100-12-31",
            "days": [],
        }


async def test_read_training_calendar_rejects_out_of_range_arguments(
    tmp_path: Path,
) -> None:
    """越界的年份与月份由 Schema 边界拦下，进入可修正的参数校验错误路径。"""
    async with _harness(tmp_path) as h:
        cases = (
            ({"year": 1999, "month": 1}, "year"),
            ({"year": 2101, "month": 1}, "year"),
            ({"year": 2026, "month": 0}, "month"),
            ({"year": 2026, "month": 13}, "month"),
        )
        for args, field in cases:
            message = await h.call("read_training_calendar", args)
            assert message.status == "error", message.content
            assert field in message.content
            assert "Extra inputs are not permitted" not in message.content


async def test_read_training_calendar_reports_plan_and_workout_statuses(
    tmp_path: Path,
) -> None:
    """月历同时给出计划日程状态（complete／incomplete／cancelled）与实际训练事实。"""
    async with _harness(tmp_path) as h:
        plan_id = await h.seed_plan(_plan_content())
        days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
        await h.seed_sessions(plan_id, days)
        sessions = await h.plan_sessions(plan_id)
        linked_id = await h.seed_workout(
            days[0], [_squat_set()], plan_session_id=sessions[0].id
        )
        await h.cancel_sessions(plan_id, from_on=days[2])
        extra_id = await h.seed_workout(date(2026, 6, 5), [_plank_set()])

        payload = await h.payload("read_training_calendar", {"year": 2026, "month": 6})

        assert payload["month"] == "2026-06"
        assert payload["from_on"] == "2026-06-01"
        assert payload["to_on"] == "2026-06-30"
        calendar_days = {day["date"]: day for day in payload["days"]}
        assert sorted(calendar_days) == [
            "2026-06-01",
            "2026-06-02",
            "2026-06-03",
            "2026-06-05",
        ]
        assert calendar_days["2026-06-01"]["plan_sessions"] == [
            {
                "plan_session_id": sessions[0].id,
                "scheduled_on": "2026-06-01",
                "status": "complete",
                "workout_session_id": linked_id,
                "actual_performed_on": "2026-06-01",
            }
        ]
        assert calendar_days["2026-06-01"]["workouts"] == [
            {
                "workout_session_id": linked_id,
                "performed_on": "2026-06-01",
                "plan_session_id": sessions[0].id,
            }
        ]
        assert calendar_days["2026-06-02"]["plan_sessions"] == [
            {
                "plan_session_id": sessions[1].id,
                "scheduled_on": "2026-06-02",
                "status": "incomplete",
                "workout_session_id": None,
                "actual_performed_on": None,
            }
        ]
        assert calendar_days["2026-06-03"]["plan_sessions"] == [
            {
                "plan_session_id": sessions[2].id,
                "scheduled_on": "2026-06-03",
                "status": "cancelled",
                "workout_session_id": None,
                "actual_performed_on": None,
            }
        ]
        assert calendar_days["2026-06-05"]["plan_sessions"] == []
        assert calendar_days["2026-06-05"]["workouts"] == [
            {
                "workout_session_id": extra_id,
                "performed_on": "2026-06-05",
                "plan_session_id": None,
            }
        ]


async def test_read_training_calendar_rejects_undeclared_arguments(tmp_path: Path) -> None:
    """月历工具同样拒绝未声明字段。"""
    async with _harness(tmp_path) as h:
        message = await h.call(
            "read_training_calendar", {"year": 2026, "month": 6, EXTRA_ARGUMENT: "x"}
        )

        assert message.status == "error"
        assert "Extra inputs are not permitted" in message.content


async def test_read_training_history_rejects_undeclared_arguments(tmp_path: Path) -> None:
    """最近训练工具同样拒绝未声明字段。"""
    async with _harness(tmp_path) as h:
        message = await h.call("read_training_history", {EXTRA_ARGUMENT: "2026-06-01"})

        assert message.status == "error"
        assert "Extra inputs are not permitted" in message.content


async def test_read_progress_rejects_undeclared_arguments(tmp_path: Path) -> None:
    """进展工具同样拒绝未声明字段。"""
    async with _harness(tmp_path) as h:
        message = await h.call("read_progress", {EXTRA_ARGUMENT: "2026-06-01"})

        assert message.status == "error"
        assert "Extra inputs are not permitted" in message.content


def test_read_tool_schemas_forbid_undeclared_fields_and_hide_injected_context() -> None:
    """六个计划只读 Tool 都带 additionalProperties: false；runtime、身份与 revision 不进模型可见 Schema。"""
    assert [tool.name for tool in SCHEDULE_TOOLS] == [
        "read_active_plan",
        "read_training_calendar",
    ]
    assert [tool.name for tool in PROGRESS_TOOLS] == [
        "read_progress",
        "read_training_history",
    ]
    # 身份与 revision 由 Runtime 注入上下文，模型参数不得出现同名字段（ST-01）。
    injected = {f.name for f in fields(ToolExecutionContext)} | {"revision"}
    visible: dict[str, set[str]] = {}
    for tool in PLANNING_TOOLS:
        assert tool.args_schema.model_json_schema()["additionalProperties"] is False
        properties = tool.tool_call_schema.model_json_schema()["properties"]
        assert "runtime" not in properties
        assert injected.isdisjoint(properties)
        visible[tool.name] = set(properties)
    assert visible == {
        "read_user_profile": set(),
        "read_active_plan": set(),
        "read_training_calendar": {"year", "month"},
        "read_training_history": {"from_on", "to_on", "exercise_ids", "limit"},
        "read_progress": {"window_days"},
        "search_exercises": {
            "query",
            "muscle_groups",
            "equipment",
            "movement_patterns",
            "limit",
        },
    }


async def test_every_read_tool_keeps_business_table_rows_unchanged(
    tmp_path: Path,
) -> None:
    """四个工具逐个调用：七张业务表行数在每次调用前后完全一致。"""
    async with _harness(tmp_path) as h:
        await _seed_full_facts(h)

        for name, args in _all_tool_calls():
            before = await h.counts()

            message = await h.call(name, args)

            assert message.status == "success", message.content
            assert await h.counts() == before


async def test_every_read_tool_returns_standard_json_encodable_text(
    tmp_path: Path,
) -> None:
    """四个工具的输出都是标准 json.dumps 直接可编码的文本：只有 JSON 标量，日期为 ISO 文本。"""
    async with _harness(tmp_path) as h:
        await _seed_full_facts(h)

        for name, args in _all_tool_calls():
            message = await h.call(name, args)

            assert message.status == "success", message.content
            payload = json.loads(message.content)
            assert json.dumps(payload, ensure_ascii=False) == message.content
            assert {type(leaf) for leaf in _leaf_values(payload)} <= {
                str,
                int,
                float,
                bool,
                type(None),
            }

        assert (await h.payload("read_active_plan", {}))["business_day"] == "2026-06-01"
        assert (await h.payload("read_progress", {}))["business_day"] == "2026-06-01"


async def _seed_full_facts(h: ReadHarness) -> None:
    """给四个工具都铺满事实：active 计划、日程、关联训练、额外训练、身体指标。"""
    plan_id = await h.seed_plan(_plan_content())
    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    await h.seed_sessions(plan_id, days)
    sessions = await h.plan_sessions(plan_id)
    await h.seed_workout(days[0], [_squat_set()], plan_session_id=sessions[0].id)
    await h.seed_workout(days[2], [_plank_set()])
    metrics = build_services(
        build_repositories(h.db),
        h.db,
        SqliteHealthProbe(h.db, h.db.path.parent),
    ).body_metrics
    await metrics.create(date(2026, 5, 25), 74.8, 18.9)
    await metrics.create(date(2026, 5, 30), 74.2, 18.5)


def _all_tool_calls() -> tuple[tuple[str, dict[str, Any]], ...]:
    """四个工具的调用清单：无参工具显式传空参数。"""
    return (
        ("read_active_plan", {}),
        ("read_training_calendar", {"year": 2026, "month": 6}),
        ("read_training_history", {}),
        ("read_progress", {}),
    )


async def test_read_user_profile_returns_the_profile_facts(tmp_path: Path) -> None:
    """画像 Tool 只读三态事实；未建档时明确返回 null，不编造字段。"""
    async with _harness(tmp_path, tools=(read_user_profile,)) as h:
        assert await h.payload("read_user_profile") == {"profile": None}
        services = build_services(
            build_repositories(h.db),
            h.db,
            SqliteHealthProbe(h.db, h.db.path.parent),
        )
        await services.profile.update(
            Profile(weekly_frequency=Fact.known(3), training_goal=Fact.known("增肌"))
        )
        payload = await h.payload("read_user_profile")
        assert payload["profile"]["weekly_frequency"] == {
            "state": "known",
            "value": 3,
        }


def test_plan_tool_whitelists_cover_the_required_facts() -> None:
    """两个计划 Node 共用同一只读 Registry：白名单六项，必备事实是白名单子集且调整场景多两项。"""
    names = tuple(tool.name for tool in PLANNING_TOOLS)
    assert names == (
        "read_user_profile",
        "read_active_plan",
        "read_training_calendar",
        "read_training_history",
        "read_progress",
        "search_exercises",
    )
    generate = set(PLAN_REQUIRED_FACTS["generate_plan"])
    adjust = set(PLAN_REQUIRED_FACTS["adjust_plan"])
    assert generate < adjust
    assert adjust - generate == {"read_active_plan", "read_training_calendar"}
    assert adjust <= set(names)
