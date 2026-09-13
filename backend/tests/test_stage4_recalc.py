"""S4-08：按最新数据重新生成草稿（Q1=C／Q2=A）的离线确定性证据。

覆盖：过期 Pending 旧草稿 → Agent Run → 子草稿 ``parent_draft_id`` 关联与最新
``context_version`` 绑定；重复请求返回已有 Pending 子草稿；旧草稿不被修改；子草稿进入终态后
允许再次生成；busy／draining 沿用同一全局单 Run 判定；不作自动触发；跨类别提案被确定性拒绝；
显式取消在 ``propose_*`` 落盘前中断同一执行驱动，不产生子草稿、旧草稿原样；安排草稿重算同样经后置查询
DTO 给出新旧 Diff 与正式数据 Diff；``/recalc`` 的客户端幂等键在子草稿产生前即幂等、幂等先于 busy。

全程离线：模型经 ``FunctionModel`` 脚本桩，无真实 Provider 请求；每例只操作 ``tmp_path``
临时库。执行入口复用 ``build_recalc_run_work``（同一驱动／预算／SSE 路径）。
"""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.dto import arrangement_draft_dto, draft_dto, plan_draft_dto
from app.arrangement_drafts import ArrangementDraftService
from app.confirm import ConfirmService
from app.draft_repo import DraftRepo
from app.drafts import DraftNotCorrectable, DraftService
from app.plan_drafts import PlanDraftService
from config import DATABASE_FILENAME
from domain.plan.repo import PlanRepo
from domain.profile.repo import ProfileRepo
from domain.profile.schema import Fact, profile_to_json
from runtime.agent_factory import build_recalc_run_work
from runtime.events import RunEventStream
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver
from storage.errors import ConversationBusy
from storage.run_repo import RunRepo
from tests.support import dump_framework_message, open_database, text_response
from tests.test_stage3_arrangement_confirm import (
    BENCH_ITEM_KEY,
    REASON,
    _arrangement_count,
    _create_arrangement,
    _profile_and_plan,
    _push_session,
)
from tests.test_stage3_plan_confirm import _confirm_plan, _create_plan_draft
from tests.test_stage3_plan_drafts import _profile
from tests.test_stage3_plan_reads import _update_formal_profile
from tests.test_stage4_agent_wiring import HARNESS
from tests.test_stage4_chat_api import _factory, _wait_terminal
from tests.test_stage4_sse_events import _StreamingStub, _tool_call

CONVERSATION_ID = "c1"
BASE_URL = "http://127.0.0.1"
BUSINESS_DATE = date(2026, 9, 20)


async def _formal_profile_and_parent(
    db, *, draft_id: str = "parent-1", stale: bool = True
):
    """建正式档案（context_version=1）后建一条 Pending 档案草稿：默认基线过期。"""
    await RunRepo(db).create_conversation(CONVERSATION_ID)
    await _update_formal_profile(db, draft_id="formal-0", profile=_profile())
    current = (await ProfileRepo(db).read()).context_version
    return await DraftRepo(db).create_pending(
        draft_id=draft_id,
        conversation_id=CONVERSATION_ID,
        run_id=None,
        base_profile_json=None,
        proposed_profile_json=profile_to_json(_profile()),
        base_business_version=current if not stale else current - 1,
    )


def _service(db, *, draining: str | None = None) -> RunService:
    return RunService(
        RunRepo(db),
        active_execution=None if draining is None else (lambda: draining),
        drafts=DraftRepo(db),
        baseline=DraftService(db),
    )


def _recalc_work(db, model, parent, *, run_id: str = "r-1"):
    return build_recalc_run_work(
        db=db,
        repo=RunRepo(db),
        model=model,
        harness=HARNESS,
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        business_date=BUSINESS_DATE,
        events=RunEventStream(),
        parent=parent,
    )


# ---------- 1. 路由入口（不达标旧草稿不得假成功） ----------


def test_recalc_route_is_exposed_and_rejects_unknown_parent(tmp_path: Path) -> None:
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
    assert ("/api/drafts/{draft_id}/recalc", "POST") in table

    with TestClient(app, base_url=BASE_URL) as client:
        # 缺客户端幂等键 → 400 形状错误，不进入业务（与 /reviews 同口径）
        missing = client.post("/api/drafts/missing-parent/recalc", json={})
        assert missing.status_code == 400, missing.text
        assert missing.json()["error_code"] == "invalid_request"
        response = client.post(
            "/api/drafts/missing-parent/recalc", json={"client_request_id": "k-1"}
        )
        assert response.status_code == 404, response.text
        assert response.json()["error_code"] == "invalid_request"


# ---------- 2. 资格：仅过期 Pending 旧草稿；没有自动触发 ----------


async def test_only_stale_pending_parent_starts_a_recalc_run(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db, stale=False)
        service = _service(db)
        # 未过期（基线 = 当前版本）→ 拒绝，不新建 Run、不产生子草稿
        with pytest.raises(DraftNotCorrectable):
            await service.regenerate_draft(
                parent_draft_id=parent.id, run_id="r-x", client_request_id="k-x"
            )
        assert await DraftRepo(db).find_pending_child(parent.id) is None
        assert await RunRepo(db).list_runs(CONVERSATION_ID) == []

        # 正式档案再次提交推进 context_version → 旧草稿过期；仍必须显式请求才创建 Run
        await _update_formal_profile(
            db, draft_id="formal-1", profile=_profile(body_weight_kg=Fact.known(71.0))
        )
        assert await DraftRepo(db).find_pending_child(parent.id) is None
        assert await RunRepo(db).list_runs(CONVERSATION_ID) == []

        result = await service.regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        assert result["created"] is True
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["kind"] == "recalc"
        # 辅助 Run 不写用户消息（意图来自旧草稿，不进对话历史投影）
        assert await RunRepo(db).get_user_request_text("r-1") is None
        # 旧草稿原样：状态、基线、拟议内容、revision 都不被重算修改
        unchanged = await DraftRepo(db).get(parent.id)
        assert unchanged is not None
        assert unchanged.status == "pending"
        assert unchanged.base_business_version == parent.base_business_version
        assert unchanged.proposed_profile_json == parent.proposed_profile_json
        assert unchanged.revision == 1


# ---------- 3. 重复请求与子草稿终态 ----------


async def test_duplicate_returns_existing_pending_child(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db)
        service = _service(db)
        first = await service.regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        assert first["created"] is True
        # 模拟 Run 内已落盘的 Pending 子草稿（带来源 Run 与父子关联）
        await DraftRepo(db).create_pending(
            draft_id="child-1",
            conversation_id=CONVERSATION_ID,
            run_id="r-1",
            base_profile_json=None,
            proposed_profile_json=profile_to_json(_profile()),
            base_business_version=0,
            parent_draft_id=parent.id,
        )
        second = await service.regenerate_draft(
            parent_draft_id=parent.id, run_id="r-2", client_request_id="k-2"
        )
        assert second["created"] is False
        assert second["run"]["id"] == "r-1"
        assert await RunRepo(db).get_run("r-2") is None

        # 子草稿进入终态后允许再次生成（Q2=A），仍不修改旧草稿
        await RunRepo(db).start_run("r-1")
        await RunRepo(db).complete_run(
            "r-1", [("assistant", dump_framework_message(text_response("完成")))]
        )
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE business_drafts SET status = 'discarded' WHERE id = ?",
                ("child-1",),
            )
        third = await service.regenerate_draft(
            parent_draft_id=parent.id, run_id="r-3", client_request_id="k-3"
        )
        assert third["created"] is True
        run = await RunRepo(db).get_run("r-3")
        assert run is not None and run["kind"] == "recalc"


# ---------- 4. 全局单 Run：活跃 Run 与 draining 名额 ----------


async def test_busy_and_draining_block_regeneration(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db)
        repo = RunRepo(db)
        await repo.create_recalc_run(CONVERSATION_ID, "active-1", "k-active")
        with pytest.raises(ConversationBusy):
            await _service(db).regenerate_draft(
                parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
            )
        # 库内无活跃 Run 但进程内名额未释放（draining）→ 同样 busy（S4-03 语义）
        await repo.start_run("active-1")
        await repo.complete_run(
            "active-1", [("assistant", dump_framework_message(text_response("完成")))]
        )
        with pytest.raises(ConversationBusy):
            await _service(db, draining="active-1").regenerate_draft(
                parent_draft_id=parent.id, run_id="r-2", client_request_id="k-2"
            )
        # 名额释放后可创建
        result = await _service(db).regenerate_draft(
            parent_draft_id=parent.id, run_id="r-3", client_request_id="k-3"
        )
        assert result["created"] is True


# ---------- 5. 执行：最新版本绑定、父子关联、确认前不生效 ----------


async def test_recalc_run_binds_latest_version_and_creates_child(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db)
        service = _service(db)
        created = await service.regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        assert created["created"] is True
        parent_draft = created["parent"]
        proposed = json.loads(profile_to_json(_profile()))

        async def race_commit(step: int) -> None:
            """生成读取前再提交一次正式档案：子草稿必须绑定当刻最新版本（版本竞态）。"""
            if step != 0:
                return
            await _update_formal_profile(
                db,
                draft_id="formal-race",
                profile=_profile(body_weight_kg=Fact.known(72.0)),
            )

        model = _StreamingStub(
            [[_tool_call("propose_profile_draft", {"proposed": proposed})]],
            on_step=race_commit,
        ).model()
        driver = ExecutionDriver(RunRepo(db))
        await driver.start("r-1", _recalc_work(db, model, parent_draft))
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "completed"

        child = await DraftRepo(db).find_pending_child(parent.id)
        assert child is not None
        assert child.parent_draft_id == parent.id
        assert child.kind == "profile_update"
        assert child.run_id == "r-1"
        # 绑定生成读取时的最新 context_version（竞态提交后的版本），不是请求时的旧版本
        current = await ProfileRepo(db).read()
        assert child.base_business_version == current.context_version
        assert child.base_business_version != parent.base_business_version
        # 确认前正式事实不变、旧草稿仍 Pending
        still = await DraftRepo(db).get(parent.id)
        assert still is not None and still.status == "pending"


# ---------- 6. 跨类别提案确定性拒绝（不落库、不换类别） ----------


async def test_recalc_run_rejects_other_draft_kind(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db)
        service = _service(db)
        created = await service.regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        parent_draft = created["parent"]
        model = _StreamingStub(
            [[_tool_call("propose_arrangement_draft", {"scheduled_session_id": "s1"})]]
        ).model()
        driver = ExecutionDriver(RunRepo(db))
        await driver.start("r-1", _recalc_work(db, model, parent_draft))
        assert await DraftRepo(db).find_pending_child(parent.id) is None


# ---------- 7. 重算产物的父子关联、新旧 Diff 与正式数据 Diff；确认子草稿 ----------


async def test_child_exposes_parent_linkage_and_both_diffs_then_confirms(
    tmp_path: Path,
) -> None:
    """过期父草稿 → 重算 → 查询 DTO 给出新旧 Diff 与正式业务变更 → 确认子草稿。"""
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db)
        created = await _service(db).regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        parent_draft = created["parent"]
        # 子草稿拟议与旧草稿不同（旧草稿体重 70.0 → 新草稿 71.0）
        proposed = json.loads(
            profile_to_json(_profile(body_weight_kg=Fact.known(71.0)))
        )
        model = _StreamingStub(
            [[_tool_call("propose_profile_draft", {"proposed": proposed})]]
        ).model()
        driver = ExecutionDriver(RunRepo(db))
        await driver.start("r-1", _recalc_work(db, model, parent_draft))
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "completed"

        child = await DraftRepo(db).find_pending_child(parent.id)
        assert child is not None
        assert child.parent_draft_id == parent.id
        assert child.kind == "profile_update"
        before_confirm = await ProfileRepo(db).read()

        # 后置查询的产品 DTO 必须给出父子关联与两份 Diff（01 1.6）
        view = await DraftService(db).get_draft(child.id)
        assert view is not None
        body = draft_dto(view)
        assert body["parent_draft_id"] == parent.id
        parent_diff = {item["field"]: item for item in body["parent_diff"]}
        assert parent_diff["body_weight_kg"]["changed"] is True
        assert parent_diff["body_weight_kg"]["before"]["value"] == 70.0
        assert parent_diff["body_weight_kg"]["after"]["value"] == 71.0
        formal_diff = {item["field"]: item for item in body["diff"]}
        assert formal_diff["body_weight_kg"]["changed"] is True
        assert formal_diff["body_weight_kg"]["before"]["value"] == 70.0
        assert formal_diff["body_weight_kg"]["after"]["value"] == 71.0

        # 确认前正式事实不变；确认子草稿后生效，旧草稿仍 Pending 且不被改动
        assert before_confirm.context_version == parent.base_business_version + 1
        result = await ConfirmService(db).confirm_profile_draft(
            draft_id=child.id, seen_revision=1
        )
        assert result.committed_revision == 1
        after = await ProfileRepo(db).read()
        assert after.context_version == before_confirm.context_version + 1
        assert after.profile is not None
        assert after.profile.body_weight_kg.value == 71.0
        still = await DraftRepo(db).get(parent.id)
        assert still is not None and still.status == "pending"


# ---------- 8. 计划父草稿：显式重算即长期调整授权，同类子草稿保留父子关联 ----------


async def test_plan_parent_regeneration_creates_same_kind_child(
    tmp_path: Path,
) -> None:
    """计划旧草稿重算不因长期调整追问阻断：模型不传 long_term_adjustment 也能落子草稿。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _update_formal_profile(db, draft_id="formal-0", profile=_profile())
        # 先确认首个正式计划（v1），再留一条待确认旧计划草稿作为重算父体
        await _create_plan_draft(db, draft_id="plan-v1", business_date=BUSINESS_DATE)
        await _confirm_plan(db, draft_id="plan-v1", business_date=BUSINESS_DATE)
        await _create_plan_draft(
            db, draft_id="plan-parent", business_date=BUSINESS_DATE
        )
        parent = await DraftRepo(db).get("plan-parent")
        assert parent is not None
        # 推进正式档案使旧计划草稿过期（Q2=A 资格）
        await _update_formal_profile(
            db,
            draft_id="formal-1",
            profile=_profile(body_weight_kg=Fact.known(71.0)),
        )
        current_plan = await PlanRepo(db).read_current()
        assert current_plan is not None
        created = await _service(db).regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        assert created["created"] is True
        model = _StreamingStub(
            [
                [
                    _tool_call(
                        "propose_plan_draft",
                        {
                            "starts_on": (
                                BUSINESS_DATE + timedelta(days=1)
                            ).isoformat(),
                            "review_on": current_plan.review_on.isoformat(),
                            "adjustments": [
                                {
                                    "item_key": "pull-01",
                                    "disposition": "deload",
                                    "work_sets": 2,
                                }
                            ],
                        },
                    )
                ]
            ]
        ).model()
        driver = ExecutionDriver(RunRepo(db))
        await driver.start("r-1", _recalc_work(db, model, created["parent"]))
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "completed"

        child = await DraftRepo(db).find_pending_child(parent.id)
        assert child is not None, "计划重算必须落同类子草稿（显式重算即长期调整授权）"
        assert child.kind == "plan"
        assert child.parent_draft_id == parent.id
        view = await PlanDraftService(db).get_plan_draft(child.id)
        assert view is not None
        body = plan_draft_dto(view)
        assert body["parent_draft_id"] == parent.id
        assert body["parent_plan_diff"] is not None
        assert any(item["changed"] for item in body["parent_plan_diff"])
        assert any(item["changed"] for item in body["diff"])


# ---------- 9. 取消：propose_* 写入前中断，不产生子草稿、旧草稿原样 ----------


async def test_cancel_before_propose_write_leaves_no_child_and_parent_unchanged(
    tmp_path: Path,
) -> None:
    """取消与 ``propose_*`` 落盘之间的确定性交错（``on_item`` 门控，无 sleep）。

    模型已给出工具调用、工具尚未执行时取消：同一执行任务被中断，不产生子草稿、旧草稿逐字段
    不变、Run 终态 ``cancelled`` 且不启动第二次模型／工具尝试（取消只经驱动显式入口触发）。
    """
    async with open_database(tmp_path / "app.db") as db:
        parent = await _formal_profile_and_parent(db)
        created = await _service(db).regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        assert created["created"] is True
        parent_draft = created["parent"]

        call_emitted = asyncio.Event()
        gate = asyncio.Event()

        async def hold(step: int, index: int) -> None:
            if step == 0 and index == 0:
                call_emitted.set()
                await gate.wait()

        proposed = json.loads(profile_to_json(_profile()))
        stub = _StreamingStub(
            [[_tool_call("propose_profile_draft", {"proposed": proposed})]],
            on_item=hold,
        )
        driver = ExecutionDriver(RunRepo(db))
        task = driver.start("r-1", _recalc_work(db, stub.model(), parent_draft))
        await call_emitted.wait()

        cancelled = await driver.cancel("r-1")
        assert cancelled["status"] == "cancelled"
        gate.set()
        await task

        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "cancelled"
        assert run["error_code"] is None
        # 取消后不启动任何后续尝试：模型只被请求一次，工具链从未落盘子草稿
        assert stub.attempts == 1
        assert await DraftRepo(db).find_pending_child(parent.id) is None
        assert await RunRepo(db).list_run_messages("r-1") == []
        assert [e["event_type"] for e in await RunRepo(db).list_run_events("r-1")] == [
            "cancelled"
        ]
        # 旧草稿仍是 Pending 且逐字段不变（不修改、不确认、不删除）
        unchanged = await DraftRepo(db).get(parent.id)
        assert unchanged is not None
        assert unchanged.status == "pending"
        assert unchanged.revision == parent.revision
        assert unchanged.base_business_version == parent.base_business_version
        assert unchanged.proposed_profile_json == parent.proposed_profile_json
        # 名额释放、执行任务已结算（无后台任务占名额）
        assert driver.active_run_id is None
        assert task.done()


# ---------- 10. 安排父草稿重算：两类 Diff 与旧草稿／正式事实隔离 ----------


async def test_arrangement_parent_regeneration_exposes_both_diffs(
    tmp_path: Path,
) -> None:
    """安排旧草稿 → 重算子草稿：查询 DTO 给出旧→新与拟议→正式两份 Diff，正式事实不变。"""
    async with open_database(tmp_path / "app.db") as db:
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        parent_view = await _create_arrangement(
            db, draft_id="arr-parent", session_id=session.id
        )
        assert parent_view.target.adjustment_reason == REASON
        # 推进正式档案使安排旧草稿过期（Q2=A 资格）
        await _update_formal_profile(
            db, draft_id="formal-1", profile=_profile(body_weight_kg=Fact.known(71.0))
        )
        parent = await DraftRepo(db).get("arr-parent")
        assert parent is not None
        before_confirm = await ProfileRepo(db).read()
        sessions_before = await PlanRepo(db).list_sessions(plan.id)

        created = await _service(db).regenerate_draft(
            parent_draft_id=parent.id, run_id="r-1", client_request_id="k-1"
        )
        assert created["created"] is True
        # 子草稿与旧草稿同类别、但目标不同：卧推 3 组（旧草稿 2 组、正式计划 4 组）
        model = _StreamingStub(
            [
                [
                    _tool_call(
                        "propose_arrangement_draft",
                        {
                            "scheduled_session_id": session.id,
                            "adjustments": [
                                {"item_key": BENCH_ITEM_KEY, "work_sets": 3}
                            ],
                            "adjustment_reason": "重算后按最新数据改为三组",
                        },
                    )
                ]
            ]
        ).model()
        driver = ExecutionDriver(RunRepo(db))
        await driver.start("r-1", _recalc_work(db, model, created["parent"]))
        run = await RunRepo(db).get_run("r-1")
        assert run is not None and run["status"] == "completed"

        child = await DraftRepo(db).find_pending_child(parent.id)
        assert child is not None
        assert child.kind == "arrangement"
        assert child.parent_draft_id == parent.id

        view = await ArrangementDraftService(db).get_arrangement_draft(child.id)
        assert view is not None
        body = arrangement_draft_dto(view)
        assert body["parent_draft_id"] == parent.id

        # 方向一：旧草稿拟议 → 新草稿拟议（结构化 FieldDiff：字段名＋before／after）
        parent_diff = {item["field"]: item for item in body["parent_diff"]}
        assert parent_diff["exercises"]["changed"] is True
        before_items = {
            item["item_key"]: item for item in parent_diff["exercises"]["before"]
        }
        after_items = {
            item["item_key"]: item for item in parent_diff["exercises"]["after"]
        }
        assert before_items[BENCH_ITEM_KEY]["prescription"]["work_sets"] == 2
        assert after_items[BENCH_ITEM_KEY]["prescription"]["work_sets"] == 3
        assert parent_diff["adjustment_reason"]["changed"] is True
        assert parent_diff["adjustment_reason"]["before"] == REASON
        assert parent_diff["adjustment_reason"]["after"] == "重算后按最新数据改为三组"

        # 方向二：新草稿拟议 → 正式业务数据（绑定版本的正式训练日）
        formal_diff = {item["field"]: item for item in body["diff"]}
        assert formal_diff["exercises"]["changed"] is True
        formal_before = {
            item["item_key"]: item for item in formal_diff["exercises"]["before"]
        }
        formal_after = {
            item["item_key"]: item for item in formal_diff["exercises"]["after"]
        }
        assert formal_before[BENCH_ITEM_KEY]["prescription"]["work_sets"] == 4
        assert formal_after[BENCH_ITEM_KEY]["prescription"]["work_sets"] == 3

        # 隔离：确认前正式事实（档案版本／计划／日程／安排修订）与旧草稿的 status／revision／
        # base_business_version／proposed_arrangement_json 四字段不变
        current = await PlanRepo(db).read_current()
        assert current is not None and current.id == plan.id
        assert (await ProfileRepo(db).read()).context_version == (
            before_confirm.context_version
        )
        assert await PlanRepo(db).list_sessions(plan.id) == sessions_before
        assert await _arrangement_count(db) == 0
        unchanged = await DraftRepo(db).get(parent.id)
        assert unchanged is not None
        assert unchanged.status == "pending"
        assert unchanged.revision == parent.revision
        assert unchanged.base_business_version == parent.base_business_version
        assert unchanged.proposed_arrangement_json == parent.proposed_arrangement_json


# ---------- 11. /recalc 客户端幂等键：子草稿产生前的重复请求与 busy ----------


async def _seed_stale_profile_parent(tmp_path: Path) -> None:
    """在应用库文件里预置过期 Pending 档案旧草稿（重算路由真实入口的前提）。"""
    async with open_database(tmp_path / DATABASE_FILENAME) as db:
        await _formal_profile_and_parent(db)


def test_recalc_route_takes_client_request_id_idempotent_before_child_exists(
    tmp_path: Path,
) -> None:
    """POST ``/recalc`` 的 ``client_request_id``：重复键返回同一 Run；不同请求遇活跃 Run busy。"""
    asyncio.run(_seed_stale_profile_parent(tmp_path))

    app = create_app(tmp_path)
    gate_holder: dict[str, Any] = {}
    gate = asyncio.Event()

    def gated_stub() -> _StreamingStub:
        async def on_step(step: int) -> None:
            gate_holder["reached"] = True
            await gate.wait()

        return _StreamingStub([["完成"]], on_step=on_step)

    with TestClient(app, base_url=BASE_URL) as client:
        stub = gated_stub()
        app.state.model_factory = _factory(stub)
        first = client.post(
            "/api/drafts/parent-1/recalc", json={"client_request_id": "k-1"}
        )
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True
        run_id = first.json()["run"]["run_id"]

        # 幂等：相同键在 Run 创建前返回同一 Run（此刻尚无任何子草稿）
        again = client.post(
            "/api/drafts/parent-1/recalc", json={"client_request_id": "k-1"}
        )
        assert again.status_code == 200, again.text
        assert again.json()["created"] is False
        assert again.json()["run"]["run_id"] == run_id

        # 不同请求遇全局活跃 Run → 409 conversation_busy，不创建第二个 Run
        busy = client.post(
            "/api/drafts/parent-1/recalc", json={"client_request_id": "k-2"}
        )
        assert busy.status_code == 409, busy.text
        assert busy.json()["error_code"] == "conversation_busy"

        portal = client.portal
        assert portal is not None
        portal.call(gate.set)
        final = _wait_terminal(client, run_id)
        assert final["status"] == "completed"
        assert stub.attempts == 1  # 幂等重发与 busy 请求都不重跑
        session = client.get("/api/sessions/c1").json()
        assert [item["run_id"] for item in session["runs"]] == [run_id]
