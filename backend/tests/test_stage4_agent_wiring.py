"""S4-04：消息上下文与业务工具接线（stage4.md S4-04 验收）。

覆盖（每项对应 stage4.md S4-04 验收的一条）：

1. 纯问答：当前事实每 Run 只注入一次（与当刻应用层读数逐字相同），只落本次新消息。
2. 事实重读：改正式条件后下一 Run 拿到新事实，历史里没有旧系统事实可冒充最新事实。
3. 四类业务草稿：档案／计划／记录／安排各产出一条 Pending 草稿，落盘前正式事实与
   ``context_version`` 不变；用户确认后才 +1（Run 完成 ≠ 草稿生效）。
4. 缺事实追问：记录/计划工具在事实不足时返回需追问结果、不落库（不补造事实）。
5. 失败／取消后续问：保留用户请求并标注中断；部分回答不进上下文（不当作已完成事实）。
6. 历史重载：已完成 Run 按 ``ModelMessagesTypeAdapter`` 原生往返；构造历史不执行工具，
   已落消息不重复回写。
7. 安全阻断：``PlanReadService`` 复核 ``usable=false`` 时不投影可执行处方（工具与系统事实
   两处一致）。
8. 能力旁路扫描：Agent 工具面只有只读查询与四类 Pending 草稿，没有确认／丢弃／作废／直写
   正式事实／公开建草稿入口。
9. 取消：取消后不落迟到消息、不新建草稿，名额释放。

全程离线确定性桩模型（``FunctionModel``），无真实 Provider 请求；每例只操作 ``tmp_path``
下的临时文件库。业务条件与草稿创建都走 Stage 3 真实应用层链路（档案/计划确认、草稿服务），
用例内不做生产写入旁路。
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from api.app import create_app
from app.confirm import ConfirmService
from config import HarnessConfig, effective_harness_config
from domain.profile.repo import ProfileRepo
from domain.profile.safety import RED_FLAG_BLOCK_ADVICE
from domain.profile.schema import Fact, profile_to_json
from domain.profile.service import ProfileService
from domain.records.schema import (
    DraftExerciseLog,
    ExerciseLogFacts,
    RawLoad,
    RecordDraftPayload,
    SetFacts,
    record_draft_to_json,
)
from runtime.agent_factory import agent_tool_functions, build_run_work
from runtime.context import (
    INTERRUPTION_MARKER,
    facts_prompt,
    read_business_facts,
)
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver, ExecutionFailure
from runtime.tools import BusinessTools, ToolIdentity
from storage.db import Database
from storage.run_repo import RunRepo
from tests.support import open_database
from tests.test_stage3_plan_confirm import (
    _confirm_plan,
    _create_plan_draft,
    _formal_profile,
)
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON, _profile
from tests.test_stage3_plan_reads import _update_formal_profile

CONVERSATION_ID = "c1"
#: 后端根目录（按本文件定位，不依赖 pytest 的工作目录）。
BACKEND_ROOT = Path(__file__).resolve().parents[1]
BUSINESS_DATE = date(2026, 9, 20)
OCCURRED_ON = date(2026, 9, 16)
SQUAT = "barbell-back-squat"
RED_FLAG_LABEL = "胸部异常不适"

#: 本文件用生产默认值的冻结有效配置（S4-05b 每 Run 冻结的输入）；用例不测预算边界。
HARNESS = effective_harness_config(HarnessConfig())

#: 预期工具面（不含任何正式写入能力）：四类只读查询 + 四类 Pending 草稿创建。
EXPECTED_TOOLS = (
    "read_plan_guidance",
    "read_training_records",
    "read_week_completion",
    "read_personal_record",
    "propose_profile_draft",
    "propose_plan_draft",
    "propose_record_draft",
    "propose_arrangement_draft",
)

FORBIDDEN_TOOL_WORDS = (
    "confirm",
    "discard",
    "void",
    "commit",
    "delete",
    "recalc",
    "update",
    "write",
    "revise",
    "approve",
)


class _ScriptedModel:
    """脚本桩模型：一次模型请求 = 一次调用，记录该次请求看到的完整消息。"""

    def __init__(self, script: list[tuple[str, dict[str, Any]] | None]) -> None:
        self.script = script
        self.seen: list[list[ModelMessage]] = []

    def model(
        self,
        *,
        on_request: Callable[[int], Awaitable[None]] | None = None,
    ) -> FunctionModel:
        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            step = len(self.seen)
            self.seen.append(list(messages))
            if on_request is not None:
                await on_request(step)
            step_script = self.script[step] if step < len(self.script) else None
            if step_script is None:
                return ModelResponse(parts=[TextPart("完成")])
            name, args = step_script
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name=name, args=args, tool_call_id=f"call-{step}")
                ]
            )

        return FunctionModel(respond)

    @property
    def attempts(self) -> int:
        return len(self.seen)


# ---------- 准备与读取助手 ----------


async def _formal_profile_and_plan(db: Database) -> str:
    """经真实链路建立正式档案并确认首个计划（会话 ``c1`` 同时建立）。"""
    await _formal_profile(db)
    await _create_plan_draft(db, draft_id="plan-draft-1")
    result = await _confirm_plan(db, draft_id="plan-draft-1", business_date=STARTS_ON)
    return result.plan_version_id


async def _run_agent(
    db: Database,
    model: FunctionModel,
    *,
    run_id: str,
    user_text: str,
    driver: ExecutionDriver | None = None,
) -> ExecutionDriver:
    """提交一次请求并由执行驱动跑完（离线桩模型，无真实 Provider 请求）。"""
    repo = RunRepo(db)
    await RunService(repo).submit_request(
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        client_request_id=f"req-{run_id}",
        text=user_text,
    )
    driver = driver if driver is not None else ExecutionDriver(repo)
    work = build_run_work(
        db=db,
        repo=repo,
        model=model,
        harness=HARNESS,
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        business_date=BUSINESS_DATE,
    )
    await driver.start(run_id, work)
    return driver


async def _run_status(db: Database, run_id: str) -> str:
    run = await RunRepo(db).get_run(run_id)
    assert run is not None, f"Run 不存在: {run_id}"
    return str(run["status"])


async def _message_rows(db: Database, run_id: str) -> list[dict[str, Any]]:
    return await RunRepo(db).list_run_messages(run_id)


async def _conversation_message_count(db: Database) -> int:
    async def op(conn):
        async with conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
            (CONVERSATION_ID,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    return await db.under_lock(op)


async def _draft_rows(
    db: Database, *, run_id: str | None = None
) -> list[dict[str, Any]]:
    """草稿行；给 ``run_id`` 时只取本 Run 创建的草稿（准备阶段的既有草稿不计入）。"""

    async def op(conn):
        if run_id is None:
            async with conn.execute(
                "SELECT id, kind, status, run_id, revision, base_business_version"
                " FROM business_drafts ORDER BY created_at, id"
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]
        async with conn.execute(
            "SELECT id, kind, status, run_id, revision, base_business_version"
            " FROM business_drafts WHERE run_id = ? ORDER BY created_at, id",
            (run_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _formal_fact_snapshot(db: Database) -> tuple[Any, ...]:
    """正式事实快照：档案文本、统一业务版本与计划／日程／安排行（草稿不得改动它们）。"""

    async def op(conn):
        async with conn.execute(
            "SELECT profile_json FROM user_profile WHERE id = 1"
        ) as cursor:
            profile_row = await cursor.fetchone()
        async with conn.execute("SELECT context_version FROM user_profile") as cursor:
            version_row = await cursor.fetchone()
        async with conn.execute(
            "SELECT id, version, payload_json FROM plan_versions ORDER BY id"
        ) as cursor:
            plans = [tuple(row) for row in await cursor.fetchall()]
        async with conn.execute(
            "SELECT id, scheduled_on, cancelled_at, locked_at FROM scheduled_sessions"
            " ORDER BY id"
        ) as cursor:
            sessions = [tuple(row) for row in await cursor.fetchall()]
        async with conn.execute(
            "SELECT id, scheduled_session_id, revision_no FROM arrangement_revisions"
            " ORDER BY id"
        ) as cursor:
            arrangements = [tuple(row) for row in await cursor.fetchall()]
        assert profile_row is not None and version_row is not None
        return (
            profile_row["profile_json"],
            int(version_row[0]),
            plans,
            sessions,
            arrangements,
        )

    return await db.under_lock(op)


async def _context_version(db: Database) -> int:
    return (await ProfileRepo(db).read()).context_version


async def _push_session_id(db: Database, plan_version_id: str) -> str:
    from tests.test_stage3_arrangement_confirm import _push_session

    return (await _push_session(db, plan_version_id)).id


def _record_payload(**overrides: Any) -> dict[str, Any]:
    """一条合法的记录草稿载荷（存储契约形状，与工具入参同一形状）。"""
    payload = RecordDraftPayload(
        occurred_on=OCCURRED_ON,
        training_session_id=None,
        exercises=(
            DraftExerciseLog(
                position=1,
                facts=ExerciseLogFacts(
                    exercise_id=SQUAT,
                    record_type="reps_weight",
                    load_notation="barbell_includes_bar_total",
                ),
                sets=(
                    SetFacts(
                        set_no=1,
                        set_type="work",
                        load=RawLoad("80", "kg"),
                        reps=5,
                    ),
                ),
            ),
        ),
        completion_declared=True,
    )
    decoded: dict[str, Any] = json.loads(record_draft_to_json(payload))
    decoded.update(overrides)
    return decoded


def _system_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, SystemPromptPart)
    ]


def _all_parts(messages: list[ModelMessage]) -> list[Any]:
    return [part for message in messages for part in message.parts]


def _user_texts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for part in _all_parts(messages)
        if type(part).__name__ == "UserPromptPart"
    ]


def _persisted_system_prompts(rows: list[dict[str, Any]]) -> list[str]:
    """已落库框架消息里的系统事实部件（必须为空：当前事实不落库、不冒充历史事实）。"""
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    prompts: list[str] = []
    for row in rows:
        if row["kind"] != "framework":
            continue
        for message in ModelMessagesTypeAdapter.validate_json(str(row["payload_json"])):
            if isinstance(message, ModelRequest):
                prompts.extend(
                    part.content
                    for part in message.parts
                    if isinstance(part, SystemPromptPart)
                )
    return prompts


# ---------- 1–2：当前事实投影与历史 ----------


async def test_pure_qa_injects_current_facts_once_and_persists_only_new_messages(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        model = _ScriptedModel([])

        await _run_agent(db, model.model(), run_id="r1", user_text="这周练得怎么样？")

        assert await _run_status(db, "r1") == "completed"
        rows = await _message_rows(db, "r1")
        assert [row["kind"] for row in rows] == [
            "user_request",
            "framework",
            "framework",
        ]
        # 唯一一条系统事实投影 = 当刻应用层读到的事实（逐字相同，不来自历史或摘要）
        prompts = _system_prompts(model.seen[0])
        assert len(prompts) == 1
        facts = await read_business_facts(
            db, conversation_id=CONVERSATION_ID, business_date=BUSINESS_DATE
        )
        assert prompts[0] == facts_prompt(facts)
        assert '"context_version": 2' in prompts[0]  # 建档 +1、计划确认 +1
        assert SQUAT in prompts[0]  # 计划处方随指导放行一起投影
        # 当前事实不落库：历史里没有可被当成最新事实的旧系统投影
        assert _persisted_system_prompts(rows) == []
        # 只落本次新消息：历史为空时框架把用户提示合并进传入请求，因此只落回答一条
        assert [row["kind"] for row in rows].count("framework") == 2


async def test_each_run_reinjects_latest_facts_and_history_carries_no_old_projection(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        first = _ScriptedModel([])
        await _run_agent(db, first.model(), run_id="r1", user_text="今天练什么？")

        # 正式条件变化（红旗）：下一次 Run 必须拿到新事实，而不是复用上一次的投影
        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(body_conditions=Fact.known((RED_FLAG_LABEL,))),
        )
        second = _ScriptedModel([])
        await _run_agent(
            db, second.model(), run_id="r2", user_text="那我今天还能练吗？"
        )

        prompts = _system_prompts(second.seen[0])
        assert len(prompts) == 1  # 当前投影只注入一次，历史不再夹带旧投影
        facts = await read_business_facts(
            db, conversation_id=CONVERSATION_ID, business_date=BUSINESS_DATE
        )
        assert prompts[0] == facts_prompt(facts)
        assert '"usable": false' in prompts[0]
        assert RED_FLAG_BLOCK_ADVICE in prompts[0]
        # 历史里的旧事实不复存在：上一轮的计划处方文本没有出现在本轮请求里
        assert "今天练什么？" in "".join(_user_texts(second.seen[0]))
        assert "'payload': None" in prompts[0] or '"payload": null' in prompts[0]
        # 本次只追加新消息：会话消息数 = r1 的 3 行 + r2 的用户请求行 + r2 的新框架行
        assert (
            await _conversation_message_count(db)
            == 3 + 1 + len(await _message_rows(db, "r2")) - 1
        )


async def test_current_facts_retry_if_business_version_changes_between_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        first_read = asyncio.Event()
        release = asyncio.Event()
        original = ProfileService.read_formal_profile
        calls = 0

        async def gated_read(service: ProfileService):
            nonlocal calls
            snapshot = await original(service)
            calls += 1
            if calls == 1:
                first_read.set()
                await release.wait()
            return snapshot

        monkeypatch.setattr(ProfileService, "read_formal_profile", gated_read)
        task = asyncio.create_task(
            read_business_facts(
                db,
                conversation_id=CONVERSATION_ID,
                business_date=BUSINESS_DATE,
            )
        )
        await first_read.wait()
        await _update_formal_profile(
            db,
            draft_id="concurrent-profile",
            profile=_profile(body_conditions=Fact.known((RED_FLAG_LABEL,))),
        )
        release.set()
        facts = await task

        assert facts.guidance is not None
        assert facts.snapshot.context_version == facts.guidance.context_version == 3
        assert facts.guidance.safety.is_blocked is True


# ---------- 3–4：四类草稿与缺事实追问 ----------


async def test_all_four_draft_families_stay_pending_and_leave_formal_facts_unchanged(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        plan_version_id = await _formal_profile_and_plan(db)
        session_id = await _push_session_id(db, plan_version_id)
        before = await _formal_fact_snapshot(db)
        baseline_version = before[1]

        model = _ScriptedModel(
            [
                (
                    "propose_profile_draft",
                    {
                        "proposed": json.loads(
                            profile_to_json(_profile(body_weight_kg=Fact.known(71.5)))
                        )
                    },
                ),
                (
                    # 长期调整：用户明确要求 + 生成业务日期的次日生效 + 原复核节点 +
                    # 受限组合（档案补丁与计划一次提交）；修订必须真的改一条计划条目
                    # （档案补丁不能代替计划本身的业务变化）。
                    "propose_plan_draft",
                    {
                        "starts_on": (BUSINESS_DATE + timedelta(days=1)).isoformat(),
                        "review_on": REVIEW_ON.isoformat(),
                        "long_term_adjustment": True,
                        "proposed_profile": json.loads(
                            profile_to_json(_profile(body_weight_kg=Fact.known(71.5)))
                        ),
                        "adjustments": [
                            {
                                "item_key": "push-01",
                                "disposition": "deload",
                                "work_sets": 2,
                            }
                        ],
                    },
                ),
                ("propose_record_draft", {"record": _record_payload()}),
                (
                    "propose_arrangement_draft",
                    {
                        "scheduled_session_id": session_id,
                        "adjustments": [{"item_key": "push-01", "work_sets": 2}],
                        "adjustment_reason": "用户报告睡眠不足",
                    },
                ),
                None,
            ]
        )
        await _run_agent(
            db, model.model(), run_id="r1", user_text="建档、排计划、记训练"
        )

        drafts = await _draft_rows(db, run_id="r1")
        assert [(row["kind"], row["status"]) for row in drafts] == [
            ("profile_update", "pending"),
            ("plan", "pending"),
            ("training_record", "pending"),
            ("arrangement", "pending"),
        ]
        assert all(row["run_id"] == "r1" for row in drafts)  # 草稿来源记录本 Run
        # 落盘前正式事实与 context_version 不变（草稿不是正式写入）
        assert await _formal_fact_snapshot(db) == before
        assert await _context_version(db) == baseline_version
        assert await _run_status(db, "r1") == "completed"

        # Run 完成 ≠ 草稿生效：仍要用户经业务确认接口提交才推进正式版本
        await ConfirmService(db).confirm_profile_draft(
            draft_id=str(drafts[0]["id"]), seen_revision=1
        )
        assert await _context_version(db) == baseline_version + 1


async def test_missing_facts_ask_the_user_instead_of_persisting_a_draft(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        # 尚未建档时要求生成计划：fail-closed，不给处方、不落库
        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(training_goal=Fact.unknown()),
        )
        model = _ScriptedModel(
            [
                (
                    # 明确要求调整长期计划后才走生成侧：事实不足仍 fail-closed 阻断。
                    "propose_plan_draft",
                    {
                        "starts_on": (BUSINESS_DATE + timedelta(days=1)).isoformat(),
                        "review_on": REVIEW_ON.isoformat(),
                        "long_term_adjustment": True,
                    },
                ),
                (
                    "propose_record_draft",
                    {"record": _record_payload(exercises=[{"position": 1}])},
                ),
                None,
            ]
        )
        await _run_agent(db, model.model(), run_id="r1", user_text="给我排计划并记一笔")
        assert await _run_status(db, "r1") == "completed"

        tool_returns = [
            part
            for part in _all_parts(model.seen[-1])
            if isinstance(part, ToolReturnPart)
        ]
        assert tool_returns, "工具结果必须回灌给模型以追问"
        plan_result = json.loads(json.dumps(tool_returns[0].content))
        assert plan_result["created"] is False
        assert plan_result["needs_user_input"] is True
        assert plan_result["block"]["code"] == "incomplete_profile"
        assert plan_result["block"]["missing_fields"] == ["training_goal"]
        record_result = json.loads(json.dumps(tool_returns[1].content))
        assert record_result["created"] is False
        assert record_result["needs_user_input"] is True
        # 事实不足时不落任何草稿，也不给处方
        assert await _draft_rows(db, run_id="r1") == []


# ---------- 5–6：失败／取消续问、历史重载与不重放工具 ----------


async def test_failed_run_keeps_request_and_marks_interruption_but_not_partial_answer(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _formal_profile_and_plan(db)

        async def partial_then_fail(active):
            await repo.save_partial_answer("r1", "先记一半未完成的回答")
            raise ExecutionFailure("model_request_timeout")

        await RunService(repo).submit_request(
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            client_request_id="req-r1",
            text="帮我记一次腿部训练",
        )
        driver = ExecutionDriver(repo)
        await driver.start("r1", partial_then_fail)
        assert await _run_status(db, "r1") == "failed"
        assert (await repo.list_partial_answers("r1"))[0][
            "text"
        ] == "先记一半未完成的回答"

        follow_up = _ScriptedModel([])
        await _run_agent(
            db, follow_up.model(), run_id="r2", user_text="那算了，今天休息"
        )

        texts = _user_texts(follow_up.seen[0])
        assert any("帮我记一次腿部训练" in text for text in texts)
        assert any(INTERRUPTION_MARKER in text for text in texts)
        # 中断的部分回答不当作已完成事实回灌
        assert not any("先记一半未完成的回答" in text for text in texts)


async def test_completed_history_reloads_natively_without_reexecuting_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        executed: list[str] = []
        original = BusinessTools.read_training_records

        async def read_training_records(self):
            # 同一函数名：框架按 __name__ 决定工具名，监控桩不改变工具面
            executed.append("read_training_records")
            return await original(self)

        monkeypatch.setattr(
            BusinessTools, "read_training_records", read_training_records
        )

        first = _ScriptedModel([("read_training_records", {}), None])
        await _run_agent(db, first.model(), run_id="r1", user_text="都有哪些训练记录？")
        r1_rows = await _message_rows(db, "r1")
        assert len(executed) == 1
        total_after_first = await _conversation_message_count(db)

        # 第二次 Run：历史原生往返（工具调用与结果原样回灌），且不重放任何工具
        second = _ScriptedModel([])
        await _run_agent(db, second.model(), run_id="r2", user_text="再简述一下")
        assert executed == ["read_training_records"]  # 载入历史没有执行工具

        seen_parts = _all_parts(second.seen[0])
        assert any(isinstance(part, ToolCallPart) for part in seen_parts)
        assert any(isinstance(part, ToolReturnPart) for part in seen_parts)
        assert "都有哪些训练记录？" in "".join(_user_texts(second.seen[0]))

        # 历史不重复回写：r1 的消息行保持原样，本次只追加 r2 自己的新消息
        assert await _message_rows(db, "r1") == r1_rows
        assert (
            await _conversation_message_count(db)
            == total_after_first + 1 + len(await _message_rows(db, "r2")) - 1
        )


async def test_cancel_after_draft_creation_recovers_verified_draft_in_next_context(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        repo = RunRepo(db)
        driver = ExecutionDriver(repo)
        second_request = asyncio.Event()

        async def on_request(step: int) -> None:
            if step == 1:
                second_request.set()
                await asyncio.Event().wait()

        model = _ScriptedModel(
            [
                (
                    "propose_profile_draft",
                    {"proposed": json.loads(profile_to_json(_profile()))},
                ),
                None,
            ]
        )
        await RunService(repo).submit_request(
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            client_request_id="req-r1",
            text="先创建档案草稿",
        )
        work = build_run_work(
            db=db,
            repo=repo,
            model=model.model(on_request=on_request),
            harness=HARNESS,
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            business_date=BUSINESS_DATE,
        )
        task = driver.start("r1", work)
        await second_request.wait()
        drafts = await _draft_rows(db, run_id="r1")
        assert len(drafts) == 1 and drafts[0]["status"] == "pending"
        await driver.cancel("r1")
        await task

        follow_up = _ScriptedModel([])
        await _run_agent(db, follow_up.model(), run_id="r2", user_text="刚才做到哪了？")
        prompt = _system_prompts(follow_up.seen[0])[0]
        assert str(drafts[0]["id"]) in prompt
        assert '"status": "pending"' in prompt
        assert '"run_id": "r1"' in prompt
        assert not any(
            isinstance(part, (ToolCallPart, ToolReturnPart))
            for part in _all_parts(follow_up.seen[0])
            if str(drafts[0]["id"]) in str(part)
        )


# ---------- 7：安全阻断 ----------


async def test_plan_guidance_usable_false_blocks_executable_prescription(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        await _update_formal_profile(
            db,
            draft_id="profile-draft-2",
            profile=_profile(body_conditions=Fact.known((RED_FLAG_LABEL,))),
        )
        model = _ScriptedModel([("read_plan_guidance", {}), None])
        await _run_agent(db, model.model(), run_id="r1", user_text="今天按计划怎么练？")

        tool_returns = [
            part
            for part in _all_parts(model.seen[-1])
            if isinstance(part, ToolReturnPart)
        ]
        guidance = json.loads(json.dumps(tool_returns[0].content))
        assert guidance["safety"]["usable"] is False
        assert guidance["safety"]["red_flag_blocked"] is True
        assert any(
            RED_FLAG_BLOCK_ADVICE in reason for reason in guidance["safety"]["reasons"]
        )
        assert guidance["plan"]["payload"] is None  # usable=false 不给可执行处方
        assert guidance["plan"]["is_current"] is True  # 计划与历史仍可查看
        # 系统事实投影同一口径：阻断时同样不投影处方
        assert '"usable": false' in _system_prompts(model.seen[0])[0]
        assert '"payload": null' in _system_prompts(model.seen[0])[0]


# ---------- 8：能力旁路扫描 ----------


def test_agent_tool_surface_is_read_and_pending_draft_only() -> None:
    tools = BusinessTools(None, ToolIdentity("c1", "r1", BUSINESS_DATE))  # type: ignore[arg-type]
    names = tuple(function.__name__ for function in agent_tool_functions(tools))
    assert names == EXPECTED_TOOLS
    for name in names:
        assert not any(word in name for word in FORBIDDEN_TOOL_WORDS), name

    # 非草稿写入能力不在工具面：只读 + 草稿创建两类，且草稿服务只出现在 tools.py
    source = (BACKEND_ROOT / "runtime" / "tools.py").read_text(encoding="utf-8") + (
        BACKEND_ROOT / "runtime" / "agent_factory.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "ConfirmService",
        "confirm_",
        "discard",
        "void_record",
        "confirm.py",
    ):
        assert forbidden not in source, forbidden


def test_no_public_draft_creation_route_is_exposed(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    # FastAPI 0.141 把 include_router 保留为 _IncludedRouter：必须展开子路由，
    # 否则直接遍历 app.routes 看不到任何子路由，本守卫会空转（S4-07 发现）。
    routes: set[tuple[str, str]] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        included = getattr(route, "original_router", None)
        if included is not None:
            stack.extend(included.routes)
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        for method in getattr(route, "methods", ()) or ():
            routes.add((path, method))
    # 非空断言：路由表确实被展开（上面的展开口径本身可验证）
    assert ("/api/drafts/{draft_id}", "GET") in routes
    assert not any(
        path == "/api/drafts" and method in ("POST", "PUT", "PATCH")
        for path, method in routes
    )


# ---------- 9：取消 ----------


async def test_cancel_prevents_late_messages_and_new_drafts(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile_and_plan(db)
        repo = RunRepo(db)
        driver = ExecutionDriver(repo)
        turning_point = asyncio.Event()
        cancelled = asyncio.Event()

        async def on_request(step: int) -> None:
            if step == 0:
                turning_point.set()
                await cancelled.wait()

        model = _ScriptedModel(
            [
                (
                    "propose_profile_draft",
                    {"proposed": json.loads(profile_to_json(_profile()))},
                )
            ]
        )
        await RunService(repo).submit_request(
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            client_request_id="req-r1",
            text="把体重改成 71.5",
        )
        work = build_run_work(
            db=db,
            repo=repo,
            model=model.model(on_request=on_request),
            harness=HARNESS,
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            business_date=BUSINESS_DATE,
        )
        task = driver.start("r1", work)
        await turning_point.wait()
        await driver.cancel("r1")
        cancelled.set()
        await task

        assert await _run_status(db, "r1") == "cancelled"
        assert driver.active_run_id is None  # 名额已释放
        # 取消后不落迟到框架消息、不新建草稿
        assert [row["kind"] for row in await _message_rows(db, "r1")] == [
            "user_request"
        ]
        assert await _draft_rows(db, run_id="r1") == []

        # 取消语境可恢复：下一次请求保留被取消的请求事实并带中断标注
        follow_up = _ScriptedModel([])
        await _run_agent(db, follow_up.model(), run_id="r2", user_text="那先不改了")
        texts = _user_texts(follow_up.seen[0])
        assert any("把体重改成 71.5" in text for text in texts)
        assert any(INTERRUPTION_MARKER in text for text in texts)
