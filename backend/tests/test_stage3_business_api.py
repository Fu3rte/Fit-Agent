"""Stage 3 S3-14：业务 API 与契约交接（HTTP／ASGI 层）。

验收对照（stage3.md §5 S3-14、§6「HTTP」「旁路扫描」组）：

- 计划／日程／记录／统计／复盘只读端点走既有路由位置，只调应用层读取入口（计划投影复用
  ``PlanReadService``，不在 ``api/`` 重建第二份投影）；无正式数据时给 ``null``，不伪造空事实。
- 计划／记录草稿纠错／确认／丢弃（含记录作废）按 ``kind`` 分派到既有应用层入口；计划确认的
  业务日期由服务端注入（测试用依赖覆盖固定时钟）。
- 整份计划安全复核取证：限制冲突与红旗各自可读；目录身份读不到时对外给已拍的具体阻断
  ``plan_action_unavailable``（不降级为「需澄清」、不当作「无冲突」）。
- 错误形状沿用 S2-07（``http_status``／``error_code``／``message``）：记录作废未绑定既有身份
  是客户端内容错误（422 ``invalid_request``），不是 500；不存在身份 404；终态 409。
- 不开放公开建草稿／重算／直写正式事实／假 Run／聊天／模型路由；Host／Origin 边界对新端点
  同样生效；响应不含堆栈或 SQL 片段。

实现方式：``tmp_path`` 临时文件库 + 进程内 ASGI 调用（同一 ``create_app`` 装配与中间件栈）；
夹具经真实确认链路（档案／计划／安排／记录）在测试库准备，不开放测试专用生产路由。
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from starlette.types import Message, Scope

from api.deps import current_business_date
from app.confirm import ConfirmService
from app.review_store import ReviewStore
from domain.profile.schema import ActionRestriction, Fact, Profile
from domain.stats.schema import ReviewStatSnapshot
from storage.db import Database
from storage.run_repo import RunRepo
from tests.test_stage2_business_api import _served_app
from tests.test_stage3_arrangement_confirm import (
    _confirm_arrangement,
    _create_arrangement,
    _profile_and_plan,
    _push_session,
)
from tests.test_stage3_plan_confirm import (
    CONVERSATION_ID,
    _confirm_plan,
    _create_plan_draft,
    _formal_profile,
)
from tests.test_stage3_plan_drafts import REVIEW_ON, STARTS_ON
from tests.test_stage3_plan_reads import _update_formal_profile
from tests.test_stage3_record_drafts import OCCURRED_ON
from tests.test_stage3_record_drafts import _create as _create_record_draft

BENCH_EXERCISE_ID = "barbell-bench-press"
LOAD_NOTATION = "barbell_includes_bar_total"
LOOPBACK_HOST = "127.0.0.1"


async def _asgi_json(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: object | None = None,
    host: str = LOOPBACK_HOST,
    origin: str | None = None,
) -> tuple[int, Any]:
    """进程内驱动 ASGI 应用（与 Stage 2 同一装配与中间件栈），支持查询串。

    与 ``tests.test_stage2_business_api._asgi_json`` 同形，只是把 ``?a=b`` 拆成 path 与
    ``query_string``（只读端点用查询串传 plan_version_id／week_no 等）。
    """
    raw_path, _, query = path.partition("?")
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
    headers = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if body:
        headers.append((b"content-type", b"application/json"))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 80),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    await app(scope, receive, send)
    status = int(messages[0]["status"])
    payload = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    if not payload:
        return status, None
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload.decode("utf-8", "replace")


@asynccontextmanager
async def _served(
    tmp_path: Path, *, business_date_value: date | None = None
) -> AsyncIterator[tuple[FastAPI, Database]]:
    """启动进程内应用；给定时把业务日期依赖固定为注入值（不依赖真实「今天」）。"""
    async with _served_app(tmp_path) as (app, db):
        if business_date_value is not None:
            app.dependency_overrides[current_business_date] = lambda: (
                business_date_value
            )
        yield app, db


async def _confirmed_plan(db: Database, draft_id: str = "plan-draft-1") -> str:
    """经真实链路建立正式档案并确认首个计划，返回计划版本 id。"""
    await _formal_profile(db)
    await _create_plan_draft(db, draft_id=draft_id)
    result = await _confirm_plan(db, draft_id=draft_id, business_date=STARTS_ON)
    return result.plan_version_id


# ---------- 只读：计划／日程 ----------


async def test_plan_read_endpoints_report_absent_plan_and_history(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        assert (await _asgi_json(app, "GET", "/api/plan")) == (200, {"plan": None})
        assert (await _asgi_json(app, "GET", "/api/plan/guidance")) == (
            200,
            {"guidance": None},
        )
        status, body = await _asgi_json(app, "GET", "/api/plans/plan-missing")
        assert status == 404
        assert body["error_code"] == "invalid_request"

        plan_version_id = await _confirmed_plan(db)

        status, body = await _asgi_json(app, "GET", "/api/plan")
        assert status == 200
        plan = body["plan"]
        assert plan["id"] == plan_version_id
        assert plan["is_current"] is True
        assert plan["version"] == 1
        assert plan["starts_on"] == STARTS_ON.isoformat()
        assert plan["review_on"] == REVIEW_ON.isoformat()
        assert plan["plan"]["schema_version"] == 1
        assert plan["plan"]["template_key"] == "ppl"
        workouts = plan["plan"]["plan_workouts"]
        assert {item["workout_key"] for item in workouts} == {"push", "pull", "legs"}
        # 日程只含应训练日（休息槽不生成），区间为 [starts_on, review_on)
        assert plan["schedules"]
        for entry in plan["schedules"]:
            assert (
                STARTS_ON.isoformat() <= entry["scheduled_on"] < (REVIEW_ON.isoformat())
            )
            assert entry["plan_workout_key"] in {"push", "pull", "legs"}
            assert (
                entry["weekday"]
                == date.fromisoformat(entry["scheduled_on"]).isoweekday()
            )
        # 固定业务日期 = starts_on：当天即到期锁定（存储标记未写也判为已锁定），
        # 之后的应训练日尚未到期
        first, last = plan["schedules"][0], plan["schedules"][-1]
        assert first["scheduled_on"] == STARTS_ON.isoformat()
        assert first["lock"] == {
            "stored": False,
            "by_business_date": True,
            "effective": True,
        }
        assert first["status"] == "locked"
        assert last["lock"]["effective"] is False
        assert last["status"] == "scheduled"

        # 历史／按身份读取：同一形状，is_current 区分
        status, body = await _asgi_json(app, "GET", f"/api/plans/{plan_version_id}")
        assert status == 200
        assert body["plan"] == plan


async def test_plan_guidance_endpoint_maps_conflicts_red_flag_and_unavailable(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        await _confirmed_plan(db)

        status, body = await _asgi_json(app, "GET", "/api/plan/guidance")
        assert status == 200
        safety = body["guidance"]["safety"]
        assert safety["usable"] is True
        assert safety["red_flag_blocked"] is False
        assert safety["conflicts"] == []
        assert safety["action_unavailable"] is False
        assert safety["block_code"] is None
        assert all(
            entry["cancelled"] is False
            for entry in body["guidance"]["plan"]["schedules"]
        )

        # 命中最新限制：整份计划阻断，冲突项给出稳定身份
        restricted = await _profile_kwargs(db)
        restricted["action_restrictions"] = Fact.known(
            (ActionRestriction(scope="specific_action", target=BENCH_EXERCISE_ID),)
        )
        await _update_formal_profile(
            db, draft_id="profile-draft-2", profile=Profile(**restricted)
        )
        status, body = await _asgi_json(app, "GET", "/api/plan/guidance")
        safety = body["guidance"]["safety"]
        assert safety["usable"] is False
        assert safety["red_flag_blocked"] is False
        assert [item["exercise_id"] for item in safety["conflicts"]] == [
            BENCH_EXERCISE_ID
        ]
        assert safety["conflicts"][0]["restriction"] == {
            "scope": "specific_action",
            "target": BENCH_EXERCISE_ID,
        }
        assert safety["block_code"] is None  # 限制冲突由 conflicts 表达，不是目录缺失
        assert safety["reasons"]

        # 红旗独立阻断
        flagged = await _profile_kwargs(db)
        flagged["body_conditions"] = Fact.known(("锐痛",))
        await _update_formal_profile(
            db, draft_id="profile-draft-3", profile=Profile(**flagged)
        )
        status, body = await _asgi_json(app, "GET", "/api/plan/guidance")
        safety = body["guidance"]["safety"]
        assert safety["usable"] is False
        assert safety["red_flag_blocked"] is True

        # 目录身份读不到：已拍的具体阻断，不降级为「需澄清」、不当作无冲突
        async with db.transaction() as conn:
            await conn.execute(
                "DELETE FROM exercises WHERE id = ?", (BENCH_EXERCISE_ID,)
            )
        status, body = await _asgi_json(app, "GET", "/api/plan/guidance")
        safety = body["guidance"]["safety"]
        assert safety["usable"] is False
        assert safety["action_unavailable"] is True
        assert safety["block_code"] == "plan_action_unavailable"
        assert safety["unknown_exercise_ids"] == [BENCH_EXERCISE_ID]
        assert safety["reasons"]


async def _profile_kwargs(db: Database) -> dict[str, Any]:
    """当前正式档案的字段 → 构造更新档案用的字段字典（逐字段复用当前取值）。"""
    from dataclasses import fields as dataclass_fields

    from domain.profile.repo import ProfileRepo

    snapshot = await ProfileRepo(db).read()
    assert snapshot.profile is not None
    return {
        item.name: getattr(snapshot.profile, item.name)
        for item in dataclass_fields(snapshot.profile)
    }


async def test_arrangement_guidance_endpoint_uses_bound_version_and_conditions(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(
            db, draft_id="arr-draft-1", session_id=session.id, work_sets=None
        )
        result = await _confirm_arrangement(db, draft_id="arr-draft-1")

        status, body = await _asgi_json(
            app,
            "GET",
            f"/api/plan/guidance?arrangement_revision_id={result.arrangement_revision_id}",
        )
        assert status == 200
        guidance = body["guidance"]
        assert guidance["plan"]["id"] == plan.id
        assert guidance["safety"]["usable"] is True

        status, body = await _asgi_json(
            app, "GET", "/api/plan/guidance?arrangement_revision_id=arr-missing"
        )
        assert status == 404
        assert body["error_code"] == "invalid_request"


# ---------- 只读：记录／统计／复盘 ----------


async def _confirmed_record(db: Database, *, draft_id: str = "record-draft-1") -> str:
    if await RunRepo(db).get_conversation(CONVERSATION_ID) is None:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
    await _create_record_draft(db, draft_id=draft_id)
    result = await ConfirmService(db).confirm_record_draft(
        draft_id=draft_id, seen_revision=1
    )
    return result.training_session_id


async def test_records_endpoints_return_current_revision_facts(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path) as (app, db):
        session_id = await _confirmed_record(db)

        status, body = await _asgi_json(app, "GET", "/api/records")
        assert status == 200
        assert [record["id"] for record in body["records"]] == [session_id]
        record = body["records"][0]
        assert record["revision"]["status"] == "valid"
        assert record["revision"]["revision_no"] == 1
        assert record["revision"]["occurred_on"] == OCCURRED_ON.isoformat()
        assert record["record"]["exercises"][0]["exercise_id"] == "barbell-back-squat"

        status, body = await _asgi_json(app, "GET", f"/api/records/{session_id}")
        assert status == 200
        assert body["record"]["id"] == session_id

        status, body = await _asgi_json(app, "GET", "/api/records/session-missing")
        assert status == 404
        assert body["error_code"] == "invalid_request"

        # 无对照安排：三桶不判定（全零），不要求补造目标
        status, body = await _asgi_json(
            app, "GET", f"/api/records/{session_id}/judgement"
        )
        assert status == 200
        judgement = body["judgement"]
        assert judgement["has_comparison"] is False
        assert judgement["is_return_phase"] is False
        assert judgement["buckets"] == {"met": 0, "unmet": 0, "pending": 0}


async def test_stats_and_review_read_endpoints(tmp_path: Path) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        plan_version_id = await _confirmed_plan(db)
        await _confirmed_record(db)

        status, body = await _asgi_json(
            app,
            "GET",
            f"/api/stats/completion?plan_version_id={plan_version_id}&week_no=1",
        )
        assert status == 200
        completion = body["completion"]
        assert completion["planned"] >= 1
        assert completion["completed"] == 0
        assert completion["rate"] == 0.0

        # 未来周没有已到期名额：发 null（「暂无」），不是 0%
        status, body = await _asgi_json(
            app,
            "GET",
            f"/api/stats/completion?plan_version_id={plan_version_id}&week_no=99",
        )
        assert (status, body) == (200, {"completion": None})

        status, body = await _asgi_json(
            app,
            "GET",
            f"/api/stats/completion?plan_version_id={plan_version_id}&week_no=x",
        )
        assert status == 400
        assert body["error_code"] == "invalid_request"

        # PR 现算：该口径最高重量与该重量下单组最高次数
        status, body = await _asgi_json(
            app,
            "GET",
            "/api/stats/pr?exercise_id=barbell-back-squat&load_notation="
            + LOAD_NOTATION,
        )
        assert status == 200
        assert body["pr"]["max_load_kg_key"] == 60000
        assert body["pr"]["best_reps"] is None
        status, body = await _asgi_json(
            app,
            "GET",
            "/api/stats/pr?exercise_id=barbell-back-squat&load_notation="
            + LOAD_NOTATION
            + "&load_kg_key=60000",
        )
        assert body["pr"]["best_reps"] == 8

        # 复盘查询：保存经内部 seam，读取经 HTTP；正文与 stale 现算
        review = await ReviewStore(db).save_review(
            body_markdown="# 本周复盘", basis=ReviewStatSnapshot()
        )
        status, body = await _asgi_json(app, "GET", "/api/reviews")
        assert status == 200
        assert [item["id"] for item in body["reviews"]] == [review.id]
        assert body["reviews"][0]["body_markdown"] == "# 本周复盘"
        assert body["reviews"][0]["stale"] is False
        assert body["reviews"][0]["basis"]["schema_version"] == 1

        status, body = await _asgi_json(app, "GET", f"/api/reviews/{review.id}")
        assert status == 200
        assert body["review"]["id"] == review.id
        status, body = await _asgi_json(app, "GET", "/api/reviews/review-missing")
        assert status == 404


async def test_stats_endpoints_missing_required_query_params_use_unified_shape(
    tmp_path: Path,
) -> None:
    """缺必填查询参数给 S2-07 统一 400，不借用 FastAPI 默认缺参 422 ``{"detail": [...]}``。"""
    async with _served(tmp_path) as (app, db):
        cases = (
            "/api/stats/completion",
            "/api/stats/completion?week_no=1",
            "/api/stats/completion?plan_version_id=pv",
            "/api/stats/pr",
            "/api/stats/pr?exercise_id=barbell-back-squat",
            "/api/stats/pr?load_notation=barbell_includes_bar_total",
        )
        for path in cases:
            status, body = await _asgi_json(app, "GET", path)

            assert status == 400, path
            assert set(body) == {"http_status", "error_code", "message"}, path
            assert body["http_status"] == 400, path
            assert body["error_code"] == "invalid_request", path
            # 不得回 FastAPI 默认校验形状
            assert "detail" not in body, path


# ---------- 草稿：计划／记录纠错-确认-丢弃 ----------


async def test_plan_draft_revise_confirm_discard_over_http(tmp_path: Path) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")

        status, draft = await _asgi_json(app, "GET", "/api/drafts/plan-draft-1")
        assert status == 200
        assert draft["kind"] == "plan"
        assert draft["payload"]["mode"] == "regular"
        assert draft["payload"]["plan"]["template_key"] == "ppl"
        assert draft["payload"]["schedules"]
        assert draft["diff"]

        # 纠错：只改日期与 D9 payload（这里保持内容不变，验证 revision+1 与 kind 分派）
        response = await _asgi_json(
            app,
            "POST",
            "/api/drafts/plan-draft-1/revise",
            json_body={
                "revision": 1,
                "payload": {
                    "starts_on": draft["payload"]["starts_on"],
                    "review_on": draft["payload"]["review_on"],
                    "plan": draft["payload"]["plan"],
                },
            },
        )
        assert response[0] == 200
        assert response[1]["draft"]["revision"] == 2

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/plan-draft-1/confirm",
            json_body={"revision": 2},
        )
        assert status == 200
        assert body["status"] == "committed"
        assert body["plan_version"] == 1
        assert body["plan_version_id"]

        # 重复确认幂等：同一份结果，不重复建版本
        status, retry = await _asgi_json(
            app,
            "POST",
            "/api/drafts/plan-draft-1/confirm",
            json_body={"revision": 2},
        )
        assert (status, retry) == (200, body)

        # 丢弃只改草稿状态；终态不可确认
        await _create_plan_draft(db, draft_id="plan-draft-2")
        status, body = await _asgi_json(app, "POST", "/api/drafts/plan-draft-2/discard")
        assert (status, body) == (
            200,
            {"draft_id": "plan-draft-2", "status": "discarded"},
        )
        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/plan-draft-2/confirm",
            json_body={"revision": 1},
        )
        assert status == 409


async def test_record_draft_revise_confirm_and_void_over_http(tmp_path: Path) -> None:
    async with _served(tmp_path) as (app, db):
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _create_record_draft(db, draft_id="record-draft-1")

        status, draft = await _asgi_json(app, "GET", "/api/drafts/record-draft-1")
        assert status == 200
        assert draft["kind"] == "training_record"
        assert draft["payload"]["status"] == "valid"
        assert draft["payload"]["record"]["exercises"][0]["exercise_id"] == (
            "barbell-back-squat"
        )
        assert draft["diff"]

        status, draft = await _asgi_json(
            app,
            "POST",
            "/api/drafts/record-draft-1/revise",
            json_body={
                "revision": 1,
                "payload": draft["payload"]["record"],
            },
        )
        assert status == 200
        assert draft["draft"]["revision"] == 2

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/record-draft-1/confirm",
            json_body={"revision": 2},
        )
        assert status == 200
        assert body["revision_status"] == "valid"
        session_id = body["training_session_id"]
        assert body["training_session_id"]

        # 更正／作废：向同一身份追加 voided 修订，不物理删除
        await _create_record_draft(
            db,
            draft_id="record-draft-2",
            training_session_id=session_id,
        )
        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/record-draft-2/void",
            json_body={"revision": 1},
        )
        assert status == 200
        assert body["training_session_id"] == session_id
        assert body["revision_status"] == "voided"
        assert body["revision_no"] == 2

        status, body = await _asgi_json(
            app, "GET", f"/api/records/{session_id}/judgement"
        )
        assert (status, body) == (200, {"judgement": None})

        # 丢弃：只改草稿状态，正式修订不变
        await _create_record_draft(db, draft_id="record-draft-3")
        status, body = await _asgi_json(
            app, "POST", "/api/drafts/record-draft-3/discard"
        )
        assert status == 200
        assert body["status"] == "discarded"
        status, fetched = await _asgi_json(app, "GET", "/api/drafts/record-draft-3")
        assert fetched["status"] == "discarded"


async def test_record_void_without_existing_identity_is_client_error(
    tmp_path: Path,
) -> None:
    """作废未绑定既有身份：领域拒绝 → 422 统一形状（不是 500，也不新增 error_code）。"""
    async with _served(tmp_path) as (app, db):
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _create_record_draft(db, draft_id="record-draft-1")  # 新增一次训练

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/record-draft-1/void",
            json_body={"revision": 1},
        )

        assert status == 422
        assert body["http_status"] == 422
        assert body["error_code"] == "invalid_request"
        assert "Traceback" not in str(body)
        # 草稿仍 Pending，未落任何正式事实
        status, fetched = await _asgi_json(app, "GET", "/api/drafts/record-draft-1")
        assert fetched["status"] == "pending"
        assert (await _asgi_json(app, "GET", "/api/records"))[1] == {"records": []}


# ---------- 传输形状、身份与旁路扫描 ----------


async def test_draft_endpoints_reject_unknown_identity_and_bad_revision(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path) as (app, db):
        cases = (
            ("GET", "/api/drafts/d-missing", None),
            ("POST", "/api/drafts/d-missing/confirm", {"revision": 1}),
            ("POST", "/api/drafts/d-missing/discard", None),
            ("POST", "/api/drafts/d-missing/void", {"revision": 1}),
        )
        for method, path, json_body in cases:
            status, body = await _asgi_json(app, method, path, json_body=json_body)
            assert status == 404, path
            assert body == {
                "http_status": 404,
                "error_code": "invalid_request",
                "message": "草稿不存在：d-missing",
            }, path

        # 形状错误先于身份查找
        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d-missing/revise", json_body={"revision": 1}
        )
        assert status == 400
        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d-missing/confirm", json_body={"revision": 0}
        )
        assert status == 400


async def test_plan_and_record_revise_reject_bad_payload_shape(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        await RunRepo(db).create_conversation("c2")
        await _create_record_draft(
            db, draft_id="record-draft-1", occurred_on=OCCURRED_ON
        )

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/plan-draft-1/revise",
            json_body={"revision": 1, "payload": {"starts_on": "2026-09-14"}},
        )
        assert status == 400, body
        assert body["error_code"] == "invalid_request"

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/record-draft-1/revise",
            json_body={"revision": 1, "payload": {"occurred_on": "2026-09-16"}},
        )
        assert status == 400, body

        # 未改动：两份草稿都仍是 revision 1 / Pending
        for draft_id in ("plan-draft-1", "record-draft-1"):
            status, fetched = await _asgi_json(app, "GET", f"/api/drafts/{draft_id}")
            assert (fetched["revision"], fetched["status"]) == (1, "pending")


async def test_session_drafts_endpoint_lists_all_kinds(tmp_path: Path) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        await _formal_profile(db)
        await _create_plan_draft(db, draft_id="plan-draft-1")
        await _create_record_draft(db, draft_id="record-draft-1")

        status, body = await _asgi_json(app, "GET", "/api/sessions/c1/drafts")

        assert status == 200
        assert [(draft["id"], draft["kind"]) for draft in body] == [
            ("profile-draft-1", "profile_update"),
            ("plan-draft-1", "plan"),
            ("record-draft-1", "training_record"),
        ]
        # 不因其他 kind 而让整张列表失败，也不按档案形状解码
        assert body[1]["payload"]["plan"]["template_key"] == "ppl"
        assert body[2]["payload"]["record"]["exercises"]


async def test_loopback_boundaries_and_bypass_scan_cover_stage3_endpoints(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        await _confirmed_plan(db)

        cases = (
            ("GET", "/api/plan"),
            ("GET", "/api/plan/guidance"),
            ("GET", "/api/records"),
            ("GET", "/api/reviews"),
        )
        for method, path in cases:
            status, body = await _asgi_json(
                app, method, path, host="evil.example", origin=None
            )
            assert status == 403, path
            assert body == {"detail": "forbidden"}
            status, body = await _asgi_json(
                app, method, path, origin="http://evil.example"
            )
            assert status == 403, path
        status, _ = await _asgi_json(
            app,
            "POST",
            "/api/drafts/plan-draft-1/discard",
            host="evil.example",
        )
        assert status == 403

        # 回环 Host／Origin 正常放行（含端口）
        status, _ = await _asgi_json(
            app,
            "GET",
            "/api/plan",
            host="localhost:8000",
            origin="http://127.0.0.1:5173",
        )
        assert status == 200

        bypass = (
            ("POST", "/api/drafts"),  # 公开创建草稿
            ("GET", "/api/drafts"),
            ("POST", "/api/drafts/plan-draft-1/recalc"),  # 假重算
            ("PUT", "/api/plan"),  # 直写正式事实
            ("POST", "/api/plan"),
            ("POST", "/api/records"),
            ("PUT", "/api/records/anything"),
            ("POST", "/api/reviews"),  # 公开生成／保存复盘正文
            ("POST", "/api/runs"),  # 假 Run／聊天／模型路由
            ("GET", "/api/runs/active"),
            ("POST", "/api/chat"),
            ("GET", "/api/models"),
            ("GET", "/api/events"),  # SSE 事件流（Stage 4；本轮无路由）
        )
        for method, path in bypass:
            status, _ = await _asgi_json(app, method, path, json_body={})
            assert status in (404, 405), (method, path)


async def test_responses_expose_no_stack_trace_or_sql(tmp_path: Path) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        await _confirmed_record(db)
        cases = (
            ("GET", "/api/plans/plan-missing"),
            ("GET", "/api/records/session-missing"),
            ("GET", "/api/reviews/review-missing"),
            ("POST", "/api/drafts/d-missing/confirm", {"revision": 1}),
            ("GET", "/api/stats/completion?plan_version_id=p&week_no=x"),
        )
        for case in cases:
            method, path = case[0], case[1]
            kwargs: dict[str, Any] = {"json_body": case[2]} if len(case) > 2 else {}
            _, body = await _asgi_json(cast(FastAPI, app), method, path, **kwargs)
            text = str(body)
            for leaked in (
                "Traceback",
                "sqlite3",
                "SELECT",
                "INSERT",
                "api_key",
                "sk-",
            ):
                assert leaked not in text, (path, leaked)


def test_no_duplicate_plan_projection_in_api_layer() -> None:
    """计划／日程投影只有一份：``api/`` 只调用 ``PlanReadService``，不重建投影。"""
    import api.routes_readonly as routes

    assert hasattr(routes, "PlanReadService")
    source = Path(routes.__file__).read_text(encoding="utf-8")
    assert "PlanRepo" not in source
    assert "scheduled_sessions" not in source


async def test_arrangement_draft_query_maps_target_without_formal_write(
    tmp_path: Path,
) -> None:
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        plan = await _profile_and_plan(db)
        session = await _push_session(db, plan.id)
        await _create_arrangement(
            db, draft_id="arr-draft-1", session_id=session.id, work_sets=2
        )

        status, body = await _asgi_json(app, "GET", "/api/drafts/arr-draft-1")
        assert status == 200
        assert body["kind"] == "arrangement"
        target = body["payload"]["target"]
        assert target["scheduled_session_id"] == session.id
        assert target["plan_version_id"] == plan.id
        assert body["plan_version"] == {"id": plan.id, "version": 1}
        # 原计划（4 组）与当次目标（2 组）都在传输对象里
        assert body["payload"]["planned_workout"]["workout_key"] == "push"
        bench = next(
            item
            for item in target["exercises"]
            if item["exercise_id"] == BENCH_EXERCISE_ID
        )
        assert bench["prescription"]["work_sets"] == 2


async def test_plan_version_endpoint_after_replacement_keeps_history(
    tmp_path: Path,
) -> None:
    """替换后旧版本仍可按身份读取（历史不重激活），当前计划指向新版本。"""
    async with _served(tmp_path, business_date_value=STARTS_ON) as (app, db):
        first_id = await _confirmed_plan(db)
        await _create_plan_draft(db, draft_id="plan-draft-2")
        second = await _confirm_plan(
            db, draft_id="plan-draft-2", business_date=STARTS_ON + timedelta(days=7)
        )

        status, body = await _asgi_json(app, "GET", "/api/plan")
        assert body["plan"]["id"] == second.plan_version_id
        assert body["plan"]["is_current"] is True

        status, body = await _asgi_json(app, "GET", f"/api/plans/{first_id}")
        assert status == 200
        assert body["plan"]["is_current"] is False
        assert body["plan"]["version"] == 1

        # 旧版未来未锁定日程被取消（已取消行保留在历史投影里）
        assert any(entry["cancelled"] for entry in body["plan"]["schedules"])
