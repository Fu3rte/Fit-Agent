"""S4-08 Q3=B：显式复盘生成 Run（独立入口、确定性冻结、数值完整性与来源竞态回滚）。

覆盖（stage4.md S4-08 验收；architecture/06 6.4；decision Q3=B）：

1. 只有 ``POST /api/reviews`` 显式入口创建 Run：无定时／按周／自动触发，聊天工具面无复盘工具。
2. 成功：Run 完成时以冻结快照 + 精确来源修订保存 Markdown；模型正文数字必须来自冻结事实。
3. 幂等（相同 ``client_request_id`` 返回已有 Run）与全局单 Run busy（幂等先于 busy）。
4. 取消：取消后不保存复盘。
5. 预算：可重试的连接失败耗尽重试池后 Run 失败且不保存（复用 S4-05 边界，无第二套预算）。
6. 数值完整性：编造事实之外的数字 → 终态失败、零写入。
7. 来源竞态：冻结后来源修订被作废 → 保存事务回滚，无复盘行。
8. 重复显式生成追加新行不覆盖旧正文；依据变化后旧正文保留且读为 stale。

全程离线：``FunctionModel`` 脚本桩，无真实 Provider 请求；每例只操作 ``tmp_path`` 临时库。
"""

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from api.app import create_app
from app.review_store import ReviewStore
from config import HarnessConfig, effective_harness_config
from domain.stats.service import StatsService
from runtime.agent_factory import build_review_run_work
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver
from storage.errors import ConversationBusy
from storage.run_repo import REVIEW_CONVERSATION_ID, RunRepo
from tests.support import open_database
from tests.test_stage3_arrangement_confirm import _profile_and_plan, _push_session
from tests.test_stage3_record_confirm import _confirm, _void
from tests.test_stage3_record_drafts import _create as _create_record
from tests.test_stage3_record_drafts import _weight_log, _weight_set
from tests.test_stage3_stats import PUSH_ON, _accept, _throwaway_draft
from tests.test_stage4_chat_api import BASE_URL, _wait_terminal

HARNESS = effective_harness_config(HarnessConfig())
NO_DATA_TEXT = "当前没有可解释的完成率或 PR 数据，建议先确认一次训练记录。"


def _text_model(text: str, *, on_request: Any = None) -> FunctionModel:
    """非流式文本桩：可注入一次请求前回调（确定性制造竞态）。"""

    async def respond(messages: Any, info: AgentInfo) -> ModelResponse:
        if on_request is not None:
            await on_request()
        # 工具面必须为空：复盘生成没有业务工具入口（无聊天工具触发路径）。
        assert info.function_tools == []
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(respond)


def _factory(model: FunctionModel):
    async def factory() -> FunctionModel:
        return model

    return factory


def _service(db: Any, *, draining: str | None = None) -> RunService:
    return RunService(
        RunRepo(db),
        active_execution=None if draining is None else (lambda: draining),
    )


def _review_work(db: Any, model: FunctionModel, *, business_date: date):
    return build_review_run_work(
        db=db,
        model=model,
        harness=HARNESS,
        business_date=business_date,
    )


async def _confirmed_session(db: Any) -> tuple[str, str]:
    """经真实链路（计划→安排→记录）造一条完成安排且进 PR 的训练：返回 (身份 id, 修订 id)。"""
    plan = await _profile_and_plan(db)
    session = await _push_session(db, plan.id)
    arrangement_revision_id = await _accept(
        db, draft_id="arr-draft-1", session_id=session.id
    )
    await _create_record(
        db,
        draft_id="record-draft-1",
        occurred_on=PUSH_ON,
        exercises=(_weight_log(sets=(_weight_set(),)),),
        arrangement_revision_id=arrangement_revision_id,
        completion_declared=True,
    )
    result = await _confirm(db, draft_id="record-draft-1")
    assert result.session_revision_id is not None
    return result.training_session_id, result.session_revision_id


async def _void_source(db: Any, session_id: str) -> None:
    """作废该训练身份当前修订（同 S3-13 口径：另备一条草稿再经作废入口切换指针）。"""
    await _throwaway_draft(
        db,
        draft_id="record-draft-void",
        occurred_on=PUSH_ON,
        training_session_id=session_id,
    )
    await _void(db, draft_id="record-draft-void")


async def _run_to_terminal(db: Any, run_id: str, work: Any) -> None:
    driver = ExecutionDriver(RunRepo(db))
    task = driver.start(run_id, work)
    await task
    assert driver.active_run_id is None


# ---------- 1. 触发面：只有显式入口，没有自动／聊天工具 ----------


def test_review_route_is_the_only_generation_entry(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    table: set[tuple[str, str]] = set()
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
            table.add((path, method))
    assert ("/api/reviews", "POST") in table
    assert ("/api/reviews", "GET") in table  # 只读列表不生成
    assert not any(
        path == "/api/reviews" and method in ("PUT", "PATCH") for path, method in table
    )


def test_getting_reviews_never_starts_a_run(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as client:
        assert client.get("/api/reviews").json() == {"reviews": []}
        assert client.get("/api/reviews/nope").status_code == 404


# ---------- 2. 成功：Run 完成、正文与快照落库 ----------


def test_explicit_review_run_persists_markdown_and_basis(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app, base_url=BASE_URL) as client:
        app.state.model_factory = _factory(_text_model(NO_DATA_TEXT))
        first = client.post("/api/reviews", json={"client_request_id": "rev-1"})
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True
        run = first.json()["run"]
        run_id = run["run_id"]
        # Run 传输字段仍是冻结七项，不因复盘 Run 而加宽
        assert set(run) == {
            "run_id",
            "conversation_id",
            "status",
            "error_code",
            "retry_of_run_id",
            "created_at",
            "updated_at",
        }
        terminal = _wait_terminal(client, run_id)
        assert terminal["status"] == "completed", terminal
        # 幂等重发：相同 client_request_id 返回已有 Run，不新建、不重复生成
        again = client.post("/api/reviews", json={"client_request_id": "rev-1"})
        assert again.status_code == 200
        assert again.json()["created"] is False
        assert again.json()["run"]["run_id"] == run_id

        reviews = client.get("/api/reviews").json()["reviews"]
        assert len(reviews) == 1
        assert reviews[0]["body_markdown"] == NO_DATA_TEXT
        assert reviews[0]["stale"] is False
        assert reviews[0]["basis"]["per_week"] == []
        assert reviews[0]["basis"]["prs"] == []

    # Run 归属专用内部会话，不冒充用户对话
    async def inspect() -> None:
        async with open_database(tmp_path / "app.db") as db:
            stored = await RunRepo(db).get_run(run_id)
            assert stored is not None and stored["kind"] == "review"
            assert stored["conversation_id"] == REVIEW_CONVERSATION_ID
            assert await RunRepo(db).get_user_request_text(run_id) is None

    asyncio.run(inspect())


async def test_review_snapshot_is_frozen_and_reproducible(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        _session_id, revision_id = await _confirmed_session(db)
        stats = StatsService(db)
        basis = await stats.review_basis(business_date=date(2026, 9, 14))
        again = await stats.review_basis(business_date=date(2026, 9, 14))
        assert basis == again, "同一事实两次冻结必须逐字段相同"
        # 快照与现算查询同一口径，不自造统计
        completion = await stats.weekly_completion(
            basis.snapshot.per_week[0].plan_version_id,
            1,
            business_date=date(2026, 9, 14),
        )
        assert basis.snapshot.per_week == (completion,)
        assert completion is not None and completion.numerator == 1
        assert [pr.best_reps for pr in basis.snapshot.prs] == [8]
        assert [pr.load_kg_key for pr in basis.snapshot.prs] == [60000]
        # 来源修订 id 精确指向参与快照的当前修订
        assert revision_id in basis.source_revision_ids

        # 模型正文数字全部来自冻结事实 → 保存成功且快照逐字落库
        text = "第 1 周完成率 1/3，深蹲最好单组 60 kg × 8 次。"
        run = await _service(db).request_review(
            run_id="r-review-1", client_request_id="k-review-1"
        )
        assert run["created"] is True
        await _run_to_terminal(
            db,
            "r-review-1",
            _review_work(db, _text_model(text), business_date=date(2026, 9, 14)),
        )
        view = (await ReviewStore(db).list_reviews())[-1]
        assert view.body_markdown == text
        assert view.basis == basis.snapshot
        assert set(view.source_revision_ids) == set(basis.source_revision_ids)
        assert view.stale is False


# ---------- 3. 幂等与 busy（同一全局单 Run 判定） ----------


async def test_review_idempotency_precedes_busy_and_draining(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        service = _service(db)
        first = await service.request_review(run_id="r-1", client_request_id="k-1")
        assert first["created"] is True
        second = await service.request_review(run_id="r-2", client_request_id="k-1")
        assert second["created"] is False
        assert second["run"]["id"] == "r-1"
        assert await RunRepo(db).get_run("r-2") is None
        # 已有活跃 Run：新请求 busy；同键仍先幂等返回
        with pytest.raises(ConversationBusy):
            await service.request_review(run_id="r-3", client_request_id="k-3")
        same = await service.request_review(run_id="r-4", client_request_id="k-1")
        assert same["created"] is False and same["run"]["id"] == "r-1"
        # draining：库内已无活跃 Run 但进程内名额未释放，同样 busy
        await RunRepo(db).start_run("r-1")
        await RunRepo(db).cancel_run("r-1")
        with pytest.raises(ConversationBusy):
            await _service(db, draining="r-1").request_review(
                run_id="r-5", client_request_id="k-5"
            )


# ---------- 4. 取消：不保存复盘 ----------


async def test_cancel_before_save_leaves_no_review(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _service(db).request_review(run_id="r-1", client_request_id="k-1")
        gate = asyncio.Event()

        async def block() -> None:
            await gate.wait()

        model = _text_model(NO_DATA_TEXT, on_request=block)
        driver = ExecutionDriver(RunRepo(db))
        task = driver.start(
            "r-1", _review_work(db, model, business_date=date(2026, 9, 14))
        )
        await asyncio.sleep(0.01)
        await driver.cancel("r-1")
        await task
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "cancelled"
        assert await ReviewStore(db).list_reviews() == ()
        gate.set()


# ---------- 5. 预算：复用 S4-05 重试池，耗尽即失败且不保存 ----------


async def test_retryable_failure_exhausts_pool_without_saving(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _service(db).request_review(run_id="r-1", client_request_id="k-1")
        attempts = 0

        async def respond(messages: Any, info: AgentInfo) -> ModelResponse:
            nonlocal attempts
            attempts += 1
            raise httpx2.ConnectError("refused")

        await _run_to_terminal(
            db,
            "r-1",
            _review_work(db, FunctionModel(respond), business_date=date(2026, 9, 14)),
        )
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "failed"
        assert run["error_code"] == "model_request_failed"
        assert attempts == 2  # 重试池 1：首次 + 一次重试，不多发
        assert await ReviewStore(db).list_reviews() == ()


# ---------- 6. 数值完整性：不得编造事实之外的数字 ----------


async def test_invented_number_fails_and_writes_nothing(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _service(db).request_review(run_id="r-1", client_request_id="k-1")
        await _run_to_terminal(
            db,
            "r-1",
            _review_work(
                db,
                _text_model("本周已完成 999 次训练。"),
                business_date=date(2026, 9, 14),
            ),
        )
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "failed"
        assert run["error_code"] == "model_request_failed"
        assert await ReviewStore(db).list_reviews() == ()


# ---------- 7. 来源竞态：冻结后来源被作废 → 整体回滚 ----------


async def test_source_revision_race_rolls_back_without_review(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id, _revision = await _confirmed_session(db)
        await _service(db).request_review(run_id="r-1", client_request_id="k-1")

        async def void_source() -> None:
            # 冻结之后、保存之前来源修订被作废并切换指针（并发用户操作）
            await _void_source(db, session_id)

        model = _text_model("已完成唯一一次已到期训练。", on_request=void_source)
        await _run_to_terminal(
            db,
            "r-1",
            _review_work(db, model, business_date=date(2026, 9, 14)),
        )
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "failed"
        assert run["error_code"] == "model_request_failed"
        assert await ReviewStore(db).list_reviews() == ()


# ---------- 8. 追加不覆盖；依据变化后旧正文保留且 stale ----------


async def test_repeated_generation_appends_and_marks_stale(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        session_id, _revision = await _confirmed_session(db)
        await _service(db).request_review(run_id="r-1", client_request_id="k-1")
        await _run_to_terminal(
            db,
            "r-1",
            _review_work(
                db,
                _text_model("第一次生成：已完成 1/3。"),
                business_date=date(2026, 9, 14),
            ),
        )
        await _service(db).request_review(run_id="r-2", client_request_id="k-2")
        await _run_to_terminal(
            db,
            "r-2",
            _review_work(
                db,
                _text_model("第二次生成：仍为 1/3。"),
                business_date=date(2026, 9, 14),
            ),
        )
        reviews = await ReviewStore(db).list_reviews()
        assert len(reviews) == 2
        bodies = [view.body_markdown for view in reviews]
        assert "第一次生成：已完成 1/3。" in bodies
        assert "第二次生成：仍为 1/3。" in bodies

        # 依据变化（作废来源修订）：旧正文原样保留、读为 stale；允许再次生成追加
        await _void_source(db, session_id)
        marked = await ReviewStore(db).list_reviews()
        assert [view.stale for view in marked] == [True, True]
        assert [view.body_markdown for view in marked] == bodies
