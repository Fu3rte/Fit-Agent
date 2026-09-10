"""Stage 2 S2-07：业务 API 与前端契约交接（HTTP／ASGI 层）。

验收对照（stage2.md §5 S2-07、§6「HTTP」「旁路扫描」组）：

- 六个端点（档案只读、会话草稿列表、单草稿查询、纠错、确认、丢弃）走已有路由位置：路由只
  校验传输、调用应用层、映射结果与错误；领域规则与 SQL 不在路由里（本文件不重复领域用例）。
- 非法 JSON／类型、未知字段、不存在身份、非法状态都明确拒绝；确认不携带业务内容，纠错内容
  由应用层重新做领域校验（客户端 payload 不是可信最终事实）；三态事实在响应里不压成空数组
  或默认值。
- 读后可见性：纠错后单草稿查询、确认后档案只读与草稿状态、丢弃后草稿状态都立即读回。
- 响应不含配置凭据或内部异常堆栈；不开放创建草稿、重算、直接写正式档案与假 Run／聊天／
  模型路由；既有 Host／Origin 边界（10.1）对新端点同样生效。

实现方式：``tmp_path`` 下的临时文件库 + 进程内 ASGI 调用（同一个 ``create_app`` 装配与同一
中间件栈，不新开端口、不新增依赖）；草稿由内部应用层（``DraftService``）在测试库准备，
不开放测试专用生产路由。非 Windows 平台结果不作为 Windows 阶段门槛（stage2.md §6）。
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from starlette.types import Message, Scope

from api.app import create_app
from app.drafts import DraftService, DraftView
from domain.profile.repo import ProfileRepo
from domain.profile.schema import (
    FACT_FIELDS,
    ActionRestriction,
    Fact,
    Profile,
    ProfileSnapshot,
)
from domain.profile.service import ProfileService
from storage.db import Database
from storage.run_repo import RunRepo

SEEDED_EXERCISE_ID = "barbell-back-squat"  # 003 迁移种子：合法具体动作身份
LOOPBACK_HOST = "127.0.0.1"


# ---------- 进程内 ASGI 调用与临时库 ----------


async def _asgi_json(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json_body: object | None = None,
    raw_body: bytes | None = None,
    host: str = LOOPBACK_HOST,
    origin: str | None = None,
) -> tuple[int, Any]:
    """进程内驱动 ASGI 应用（与真实入口同一装配与中间件栈），返回 (状态码, 解析后的响应体)。

    直接给 Host／Origin 头，用来回归 10.1 的既有本机访问边界。
    """
    body = raw_body if raw_body is not None else b""
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
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
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
async def _served_app(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, Database]]:
    """启动一次进程内应用（同一 lifespan），产出应用与其唯一连接。"""
    app = create_app(tmp_path / "data")
    async with app.router.lifespan_context(app):
        yield app, app.state.db


# ---------- 档案样本与测试替身 ----------


def _first_time_profile() -> Profile:
    """首次建档样本：八项明确回答齐备（可过首次确认完整性门）。"""
    return Profile(
        training_goal=Fact.known("增肌"),
        training_experience=Fact.known("零基础"),
        weekly_frequency=Fact.known(3),
        session_duration_minutes=Fact.known(60),
        available_equipment=Fact.known(("哑铃",)),
        action_restrictions=Fact.denied(),
        body_conditions=Fact.denied(),
        body_weight_kg=Fact.known(73.0),
    )


def _revised_profile() -> Profile:
    """纠错样本：相对首次建档样本改目标与体重（仍九项齐备）。"""
    return replace(
        _first_time_profile(),
        training_goal=Fact.known("力量"),
        body_weight_kg=Fact.known(75.0),
    )


def _existing_profile() -> Profile:
    """已建档正式档案样本（用于过期与无业务变更场景）。"""
    return replace(
        _first_time_profile(),
        training_goal=Fact.known("力量"),
        body_weight_kg=Fact.known(80.0),
    )


def _weight_changed_profile() -> Profile:
    """相对 :func:`_existing_profile` 只改体重的拟议内容。"""
    return replace(_existing_profile(), body_weight_kg=Fact.known(79.0))


def _goal_and_weight_changed_profile() -> Profile:
    """相对 :func:`_existing_profile` 改目标与体重的拟议内容。"""
    return replace(
        _existing_profile(),
        training_goal=Fact.known("增肌"),
        body_weight_kg=Fact.known(78.0),
    )


def _fact_json(fact: Fact[Any]) -> dict[str, Any]:
    """测试侧独立编码：三态事实 → 请求体形状（不复用生产编码器）。"""
    value = fact.value
    if isinstance(value, tuple):
        value = [
            {"scope": item.scope, "target": item.target}
            if isinstance(item, ActionRestriction)
            else item
            for item in value
        ]
    return {"state": fact.state, "value": value if fact.is_known else None}


def _facts(profile: Profile) -> dict[str, Any]:
    return {name: _fact_json(getattr(profile, name)) for name in FACT_FIELDS}


def _facts_with(field: str, entry: object) -> dict[str, Any]:
    facts = dict(_FIRST_TIME_FACTS)
    facts[field] = entry
    return facts


def _facts_without(field: str) -> dict[str, Any]:
    facts = dict(_FIRST_TIME_FACTS)
    del facts[field]
    return facts


def _revise_body(facts: object = None, revision: object = 1) -> dict[str, Any]:
    return {
        "revision": revision,
        "payload": {"profile": _FIRST_TIME_FACTS if facts is None else facts},
    }


_FIRST_TIME_FACTS = _facts(_first_time_profile())
_REVISED_FACTS = _facts(_revised_profile())


async def _create_draft(
    db: Database,
    *,
    draft_id: str,
    proposed: Profile,
    conversation_id: str = "c1",
    baseline: ProfileSnapshot | None = None,
) -> DraftView:
    """内部应用层在测试库准备 Pending 草稿（不是生产路由）。"""
    drafts = DraftService(db)
    return await drafts.create_profile_draft(
        draft_id=draft_id,
        generation_baseline=(
            baseline
            if baseline is not None
            else await drafts.prepare_generation_baseline()
        ),
        conversation_id=conversation_id,
        run_id=None,
        proposed=proposed,
    )


async def _write_formal_profile(db: Database, profile: Profile) -> None:
    """测试替身：确认事务之外写正式档案并推进一次版本（构造既有档案／后续业务变更）。"""
    async with db.transaction() as conn:
        await ProfileService(db).write_profile_in_transaction(conn, profile)
        await ProfileRepo(db).bump_context_version_in_transaction(conn)


async def _formal(db: Database) -> ProfileSnapshot:
    return await ProfileRepo(db).read()


# ---------- GET /api/profile ----------


async def test_profile_endpoint_reports_unbuilt_profile_as_null_without_writes(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        status, body = await _asgi_json(app, "GET", "/api/profile")

        assert (status, body) == (200, {"context_version": 0, "profile": None})
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)


async def test_profile_endpoint_keeps_unknown_denied_and_known_distinct(
    tmp_path: Path,
) -> None:
    """部分档案不显示成完整档案：八个字段都在，缺失／未知与明确无各不相同。"""
    partial = Profile(
        training_goal=Fact.known("力量"),
        available_equipment=Fact.denied(),
        action_restrictions=Fact.known(
            (ActionRestriction(scope="specific_action", target=SEEDED_EXERCISE_ID),)
        ),
        body_conditions=Fact.known(()),
    )
    async with _served_app(tmp_path) as (app, db):
        await _write_formal_profile(db, partial)

        status, body = await _asgi_json(app, "GET", "/api/profile")

        assert status == 200
        assert body["context_version"] == 1
        facts = body["profile"]
        assert sorted(facts) == sorted(FACT_FIELDS)
        assert facts["training_goal"] == {"state": "known", "value": "力量"}
        # 未收集（unknown）与明确无（denied）分别表达，都不是空数组或默认值
        assert facts["training_experience"] == {"state": "unknown", "value": None}
        assert facts["available_equipment"] == {"state": "denied", "value": None}
        # 显式空集合是第三种表达，不与 denied／unknown 混同
        assert facts["body_conditions"] == {"state": "known", "value": []}
        assert facts["session_duration_minutes"] == {"state": "unknown", "value": None}
        # 限制按稳定身份（scope＋target）表达，展示名不参与身份
        assert facts["action_restrictions"]["value"] == [
            {"scope": "specific_action", "target": SEEDED_EXERCISE_ID}
        ]


# ---------- GET /api/sessions/{session_id}/drafts ----------


async def test_session_drafts_endpoint_lists_persisted_current_state(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        runs = RunRepo(db)
        await runs.create_conversation("c1")
        await runs.create_conversation("c2")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())
        await _create_draft(db, draft_id="d2", proposed=_revised_profile())
        await _create_draft(
            db, draft_id="d3", proposed=_first_time_profile(), conversation_id="c2"
        )

        status, drafts = await _asgi_json(app, "GET", "/api/sessions/c1/drafts")

        assert status == 200
        assert [draft["id"] for draft in drafts] == ["d1", "d2"]
        assert [draft["status"] for draft in drafts] == ["pending", "pending"]
        assert [draft["revision"] for draft in drafts] == [1, 1]
        assert [entry["field"] for entry in drafts[0]["diff"]] == list(FACT_FIELDS)
        assert drafts[0]["payload"]["profile"]["training_goal"] == {
            "state": "known",
            "value": "增肌",
        }
        status, other = await _asgi_json(app, "GET", "/api/sessions/c2/drafts")
        assert [draft["id"] for draft in other] == ["d3"]
        # 该会话没有已持久化草稿：当前状态就是空，不创建草稿、不报假失败
        status, empty = await _asgi_json(app, "GET", "/api/sessions/c-missing/drafts")
        assert (status, empty) == (200, [])


# ---------- GET /api/drafts/{draft_id} ----------


async def test_single_draft_endpoint_returns_content_diff_and_status(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        status, body = await _asgi_json(app, "GET", "/api/drafts/d1")

        assert status == 200
        assert (
            body["id"],
            body["kind"],
            body["status"],
            body["revision"],
            body["base_business_version"],
        ) == ("d1", "profile_update", "pending", 1, 0)
        assert body["payload"]["profile"]["training_experience"] == {
            "state": "known",
            "value": "零基础",
        }
        goal = next(
            entry for entry in body["diff"] if entry["field"] == "training_goal"
        )
        assert goal == {
            "field": "training_goal",
            "before": {"state": "unknown", "value": None},
            "after": {"state": "known", "value": "增肌"},
            "changed": True,
        }
        # 未提交：没有提交凭据，不伪造结果
        assert body["committed_revision"] is None
        assert body["committed_business_version"] is None


async def test_committed_draft_endpoint_reports_persisted_result(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())
        await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )

        status, body = await _asgi_json(app, "GET", "/api/drafts/d1")

        assert status == 200
        assert body["status"] == "committed"
        assert body["committed_revision"] == 1
        assert body["committed_business_version"] == 1


# ---------- 错误形状：不存在身份 ----------


UNKNOWN_IDENTITY_CASES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/api/drafts/d-missing", None),
    (
        "POST",
        "/api/drafts/d-missing/revise",
        _revise_body(),
    ),
    ("POST", "/api/drafts/d-missing/confirm", {"revision": 1}),
    ("POST", "/api/drafts/d-missing/discard", None),
)


async def test_draft_endpoints_reject_unknown_identity_with_api_error_shape(
    tmp_path: Path,
) -> None:
    # 不用 pytest.mark.parametrize：anyio 插件为 async 测试重建 callspec 时会丢弃
    # 参数化（tests/test_provider_settings.py 同一说明），故在循环内逐用例跑。
    async with _served_app(tmp_path) as (app, db):
        for method, path, json_body in UNKNOWN_IDENTITY_CASES:
            status, body = await _asgi_json(app, method, path, json_body=json_body)

            assert status == 404, path
            assert body == {
                "http_status": 404,
                "error_code": "invalid_request",
                "message": "草稿不存在：d-missing",
            }, path
            assert "Traceback" not in json.dumps(body, ensure_ascii=False), path
        status, empty = await _asgi_json(app, "GET", "/api/sessions/c1/drafts")
        assert empty == []  # 不创建新草稿


async def test_transport_shape_is_validated_before_identity_lookup(
    tmp_path: Path,
) -> None:
    """形状错误先于身份查找：非法请求体不因身份不存在改判 404，也不触碰应用层。"""
    async with _served_app(tmp_path) as (app, db):
        cases = (
            ("/api/drafts/d-missing/revise", {"revision": 1}),
            ("/api/drafts/d-missing/confirm", {"revision": 0}),
        )
        for path, body in cases:
            status, response = await _asgi_json(app, "POST", path, json_body=body)
            assert status == 400, path
            assert response["error_code"] == "invalid_request", path


# ---------- POST /api/drafts/{draft_id}/revise ----------


async def test_revise_endpoint_updates_content_and_revision_without_confirming(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())
        before = await _formal(db)

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/d1/revise",
            json_body={"revision": 1, "payload": {"profile": _REVISED_FACTS}},
        )

        assert status == 200
        revised = body["draft"]
        assert (
            revised["revision"],
            revised["status"],
            revised["base_business_version"],
        ) == (
            2,
            "pending",
            0,
        )
        assert revised["payload"]["profile"]["training_goal"] == {
            "state": "known",
            "value": "力量",
        }
        assert revised["payload"]["profile"]["body_weight_kg"] == {
            "state": "known",
            "value": 75.0,
        }
        # Diff 与内容同源重算
        goal = next(
            entry for entry in revised["diff"] if entry["field"] == "training_goal"
        )
        assert goal["before"] == {"state": "unknown", "value": None}
        assert goal["after"] == {"state": "known", "value": "力量"}
        # 读后可见：单草稿查询就是修订后的草稿；纠错不提交、不写正式事实
        status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert (status, fetched) == (200, revised)
        assert await _formal(db) == before


async def test_revise_endpoint_rejects_unseen_revision_without_partial_update(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        status, body = await _asgi_json(
            app,
            "POST",
            "/api/drafts/d1/revise",
            json_body={"revision": 7, "payload": {"profile": _REVISED_FACTS}},
        )

        assert status == 409
        assert body["http_status"] == 409
        assert body["error_code"] == "draft_modified"
        assert "7" in body["message"]
        status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert fetched["revision"] == 1
        assert fetched["payload"]["profile"]["training_goal"] == {
            "state": "known",
            "value": "增肌",
        }


REVISE_SHAPE_CASES = cast(
    "tuple[tuple[str, dict[str, Any]], ...]",
    (
        ("非法 JSON", {"raw_body": b"{not json"}),
        ("请求体非对象", {"json_body": ["not", "an", "object"]}),
        ("未知顶层字段", {"json_body": {**_revise_body(), "status": "committed"}}),
        (
            "未知元数据字段",
            {"json_body": {**_revise_body(), "base_business_version": 9}},
        ),
        ("缺 revision", {"json_body": {"payload": {"profile": _FIRST_TIME_FACTS}}}),
        ("缺 payload", {"json_body": {"revision": 1}}),
        ("revision 非整数", {"json_body": _revise_body(revision="1")}),
        ("revision 为 0", {"json_body": _revise_body(revision=0)}),
        ("revision 为布尔", {"json_body": _revise_body(revision=True)}),
        ("payload 非对象", {"json_body": {"revision": 1, "payload": "x"}}),
        (
            "payload 含多余字段",
            {
                "json_body": {
                    "revision": 1,
                    "payload": {"profile": _FIRST_TIME_FACTS, "restrictions": []},
                }
            },
        ),
        ("profile 非对象", {"json_body": _revise_body("x")}),
        (
            "profile 缺字段",
            {"json_body": _revise_body(_facts_without("body_conditions"))},
        ),
        (
            "profile 未知字段",
            {
                "json_body": _revise_body(
                    {
                        **_FIRST_TIME_FACTS,
                        "body_weight": {"state": "known", "value": 73.0},
                    }
                )
            },
        ),
        (
            "profile 用旧身体情况字段",
            {
                "json_body": _revise_body(
                    {
                        **_FIRST_TIME_FACTS,
                        "body_state": {"state": "denied", "value": None},
                    }
                )
            },
        ),
        (
            "事实不是三态对象",
            {"json_body": _revise_body(_facts_with("training_goal", "增肌"))},
        ),
        (
            "事实对象键不符",
            {
                "json_body": _revise_body(
                    _facts_with(
                        "training_goal",
                        {"state": "known", "value": "增肌", "extra": 1},
                    )
                )
            },
        ),
        (
            "事实状态非法",
            {
                "json_body": _revise_body(
                    _facts_with("training_goal", {"state": "maybe", "value": None})
                )
            },
        ),
        (
            "unknown 带值",
            {
                "json_body": _revise_body(
                    _facts_with("training_goal", {"state": "unknown", "value": "增肌"})
                )
            },
        ),
        (
            "known 无值",
            {
                "json_body": _revise_body(
                    _facts_with("training_goal", {"state": "known", "value": None})
                )
            },
        ),
        (
            "文本数组元素非文本",
            {
                "json_body": _revise_body(
                    _facts_with("available_equipment", {"state": "known", "value": [1]})
                )
            },
        ),
        (
            "限制条目非对象",
            {
                "json_body": _revise_body(
                    _facts_with(
                        "action_restrictions", {"state": "known", "value": ["深蹲"]}
                    )
                )
            },
        ),
        (
            "限制条目键不符",
            {
                "json_body": _revise_body(
                    _facts_with(
                        "action_restrictions",
                        {"state": "known", "value": [{"scope": "movement_pattern"}]},
                    )
                )
            },
        ),
        (
            "限制 scope 非文本",
            {
                "json_body": _revise_body(
                    _facts_with(
                        "action_restrictions",
                        {
                            "state": "known",
                            "value": [{"scope": 1, "target": "深蹲"}],
                        },
                    )
                )
            },
        ),
    ),
)


def test_revise_endpoint_rejects_invalid_transport_shape_marker() -> None:
    """参数化用例集本身非空（避免循环体静默空跑）。"""
    assert REVISE_SHAPE_CASES


async def test_revise_endpoint_rejects_invalid_transport_shape(
    tmp_path: Path,
) -> None:
    # 不用 pytest.mark.parametrize：anyio 插件丢弃参数化（tests/test_provider_settings.py
    # 同一说明），故在循环内逐用例跑；每个用例都在同一份未改动草稿上验证无写入。
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        for reason, kwargs in REVISE_SHAPE_CASES:
            status, body = await _asgi_json(
                app, "POST", "/api/drafts/d1/revise", **kwargs
            )

            assert status == 400, reason
            assert body["http_status"] == 400, reason
            assert body["error_code"] == "invalid_request", reason
            # 传输形状不合法不落任何写入：草稿与正式档案原样
            status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
            assert (fetched["revision"], fetched["status"]) == (1, "pending"), reason
            assert await _formal(db) == ProfileSnapshot(
                profile=None, context_version=0
            ), reason


async def test_revise_endpoint_rejects_non_finite_numbers_without_persisting(
    tmp_path: Path,
) -> None:
    """非有限数值不得进入草稿：非标准 JSON 常量 400，指数溢出（1e999）由领域校验 422。

    Python 的 JSON 解析器默认接受 NaN／Infinity，且 ``1e999`` 会解析为 ``inf``；非有限数值既
    不能可靠落盘（SQLite ``json_valid`` 拒绝 Infinity／NaN）也不能回传（响应编码拒绝非有限
    浮点），必须在传输形状或领域校验处干净拒绝，且不留任何部分更新。
    """
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())
        valid_body = json.dumps(_revise_body(), ensure_ascii=False)
        # 体重值在合法请求体里只出现一次，便于把 JSON 数字换成各种非有限写法
        assert '"value": 73.0' in valid_body

        cases = (
            ("NaN 常量", "NaN", 400),
            ("Infinity 常量", "Infinity", 400),
            ("−Infinity 常量", "-Infinity", 400),
            ("指数溢出 1e999", "1e999", 422),
        )
        for reason, literal, expected in cases:
            raw_body = valid_body.replace("73.0", literal, 1).encode("utf-8")
            status, body = await _asgi_json(
                app, "POST", "/api/drafts/d1/revise", raw_body=raw_body
            )

            assert status == expected, (reason, status, body)
            assert body["error_code"] == "invalid_request", reason
            # 没有部分更新：草稿内容、revision 与正式档案全部原样
            status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
            assert (fetched["revision"], fetched["status"]) == (1, "pending"), reason
            assert fetched["payload"]["profile"]["body_weight_kg"] == {
                "state": "known",
                "value": 73.0,
            }, reason
            assert await _formal(db) == ProfileSnapshot(
                profile=None, context_version=0
            ), reason


async def test_revise_endpoint_handles_extreme_integers_without_500(
    tmp_path: Path,
) -> None:
    """超大整数不得引发 500：不可表示的整数干净拒绝，可表示的照常接受。

    ``math.isfinite(huge_int)`` 会抛 OverflowError；而 number 事实在读写两侧都以 float 表示
    （``domain.profile.schema`` 解码为 float），超出 float 表示范围的整数会落盘成功却读不回来。
    因此按「值必须可表示为有限浮点」判定：不可表示 → 422 且不落任何写入；可表示 → 照常接受
    （不新增加值域阈值：业务阈值未拍）。
    """
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())
        valid_body = json.dumps(_revise_body(), ensure_ascii=False)
        assert '"value": 73.0' in valid_body

        # 400 位整数：合法 JSON 整数，但无法表示为有限 float → 干净拒绝、无部分更新
        raw_body = valid_body.replace("73.0", "1" + "0" * 399, 1).encode("utf-8")
        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d1/revise", raw_body=raw_body
        )
        assert status == 422, body
        assert body["error_code"] == "invalid_request"
        status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert (fetched["revision"], fetched["status"]) == (1, "pending")
        assert fetched["payload"]["profile"]["body_weight_kg"] == {
            "state": "known",
            "value": 73.0,
        }
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)

        # 301 位整数：仍在 float 表示范围内 → 接受（不存在值域阈值），响应与读回都可序列化
        literal = "1" + "0" * 300
        expected = float(int(literal))
        raw_body = valid_body.replace("73.0", literal, 1).encode("utf-8")
        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d1/revise", raw_body=raw_body
        )
        assert status == 200, body
        assert body["draft"]["revision"] == 2
        assert body["draft"]["payload"]["profile"]["body_weight_kg"] == {
            "state": "known",
            "value": expected,
        }
        status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert status == 200
        assert fetched["payload"]["profile"]["body_weight_kg"] == {
            "state": "known",
            "value": expected,
        }
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)


REVISE_DOMAIN_CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "模式词表外",
        _facts_with(
            "action_restrictions",
            {
                "state": "known",
                "value": [{"scope": "movement_pattern", "target": "太极"}],
            },
        ),
    ),
    (
        "目录外动作",
        _facts_with(
            "action_restrictions",
            {
                "state": "known",
                "value": [{"scope": "specific_action", "target": "no-such-action"}],
            },
        ),
    ),
    ("空白文本", _facts_with("training_goal", {"state": "known", "value": "  "})),
    (
        "数值字段给文本",
        _facts_with("weekly_frequency", {"state": "known", "value": "3"}),
    ),
)


async def test_revise_endpoint_rejects_domain_invalid_content_with_422(
    tmp_path: Path,
) -> None:
    """形状合法但内容不合领域规则：由应用层领域校验拒绝，路由只映射错误。"""
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        for reason, facts in REVISE_DOMAIN_CASES:
            status, body = await _asgi_json(
                app, "POST", "/api/drafts/d1/revise", json_body=_revise_body(facts)
            )

            assert status == 422, reason
            assert body["error_code"] == "invalid_request", reason
            status, fetched = await _asgi_json(app, "GET", "/api/drafts/d1")
            assert (fetched["revision"], fetched["status"]) == (1, "pending"), reason


async def test_revise_keeps_identity_source_baseline_and_status_untouched(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        # 元数据字段在请求体里根本不存在：任何尝试改写都被当未登记字段拒绝
        for extra in (
            {"id": "d-other"},
            {"status": "committed"},
            {"base_business_version": 9},
            {"conversation_id": "c9"},
            {"committed_business_version": 11},
        ):
            status, body = await _asgi_json(
                app,
                "POST",
                "/api/drafts/d1/revise",
                json_body={**_revise_body(), **extra},
            )
            assert status == 400, extra
            assert body["error_code"] == "invalid_request", extra

        status, _ = await _asgi_json(
            app,
            "POST",
            "/api/drafts/d1/revise",
            json_body=_revise_body(_REVISED_FACTS),
        )
        assert status == 200
        view = await DraftService(db).get_draft("d1")
        assert view is not None
        assert view.draft.id == "d1"
        assert view.draft.conversation_id == "c1"
        assert view.draft.run_id is None
        assert view.draft.base_business_version == 0
        assert view.draft.status == "pending"
        assert view.draft.committed_revision is None


# ---------- POST /api/drafts/{draft_id}/confirm ----------


async def test_confirm_endpoint_commits_once_and_reads_back_profile(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )

        assert status == 200
        assert body == {
            "draft_id": "d1",
            "status": "committed",
            "committed_revision": 1,
            "committed_business_version": 1,
        }
        # 读后可见：正式档案只读接口表达已提交事实与版本
        status, profile = await _asgi_json(app, "GET", "/api/profile")
        assert status == 200
        assert profile["context_version"] == 1
        assert profile["profile"]["training_goal"] == {
            "state": "known",
            "value": "增肌",
        }
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert draft["status"] == "committed"
        assert draft["committed_business_version"] == 1

        # 重复确认幂等返回原结果，版本只 +1
        status, retry = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )
        assert (status, retry) == (200, body)
        assert (await _formal(db)).context_version == 1

        # 后续业务版本变化不改写已提交草稿的原结果
        await _write_formal_profile(db, _existing_profile())
        status, after_change = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )
        assert (status, after_change) == (200, body)
        assert (await _formal(db)).context_version == 2


async def test_confirm_endpoint_reports_stale_baseline_with_verifiable_changes(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _write_formal_profile(db, _existing_profile())
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()
        for draft_id, proposed in (
            ("d1", _weight_changed_profile()),
            ("d2", _goal_and_weight_changed_profile()),
        ):
            await drafts.create_profile_draft(
                draft_id=draft_id,
                generation_baseline=baseline,
                conversation_id="c1",
                run_id=None,
                proposed=proposed,
            )
        status, _ = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )
        assert status == 200
        formal_after_first = await _formal(db)

        # 同一业务版本的第二份草稿：即使改动字段与先提交的无关也保守拦截
        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d2/confirm", json_body={"revision": 1}
        )

        assert status == 409
        assert body["error_code"] == "draft_stale"
        # 只报告可核实的字段变化：d1 改过的体重，不虚构「谁何时修改」
        assert body["detail"] == "可核实字段变化：body_weight_kg"
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d2")
        assert (draft["status"], draft["revision"]) == ("pending", 1)
        assert await _formal(db) == formal_after_first


async def test_confirm_endpoint_reports_stale_without_field_difference(
    tmp_path: Path,
) -> None:
    """版本变过但快照无字段差异时仍拦截，并明确说明没有可核实差异。"""
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        drafts = DraftService(db)
        baseline = await drafts.prepare_generation_baseline()  # 未建档 V=0
        for draft_id, proposed in (
            ("d1", _first_time_profile()),
            ("d2", _revised_profile()),
        ):
            await drafts.create_profile_draft(
                draft_id=draft_id,
                generation_baseline=baseline,
                conversation_id="c1",
                run_id=None,
                proposed=proposed,
            )
        assert (
            await _asgi_json(
                app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
            )
        )[0] == 200

        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d2/confirm", json_body={"revision": 1}
        )

        assert status == 409
        assert body["error_code"] == "draft_stale"
        # 基线是「未建档」：不把之前未建档摊成逐字段 unknown → known 的假差异
        assert body["detail"] == "业务版本已变化，当前快照无字段差异"
        assert "context_version" in body["message"]


async def test_confirm_endpoint_rejects_unseen_revision(tmp_path: Path) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 2}
        )

        assert status == 409
        assert body["error_code"] == "draft_modified"
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert draft["status"] == "pending"


async def test_confirm_endpoint_rejects_incomplete_first_time_profile(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(
            db,
            draft_id="d1",
            proposed=Profile(
                training_goal=Fact.known("增肌"), body_weight_kg=Fact.known(73.0)
            ),
        )

        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )

        assert status == 422
        assert body["error_code"] == "invalid_request"
        # 缺项不补造：正式档案仍未建档，草稿保持 Pending
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert draft["status"] == "pending"


async def test_confirm_endpoint_rejects_no_business_change(tmp_path: Path) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _write_formal_profile(db, _existing_profile())
        await _create_draft(db, draft_id="d1", proposed=_existing_profile())

        status, body = await _asgi_json(
            app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
        )

        assert status == 409
        assert body["error_code"] == "invalid_request"
        assert "无业务变更" in body["message"]
        # 不提交、版本不变、草稿保持 Pending（可继续纠错或丢弃）
        assert (await _formal(db)).context_version == 1
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert draft["status"] == "pending"


# ---------- POST /api/drafts/{draft_id}/discard ----------


async def test_discard_endpoint_marks_discarded_without_formal_side_effects(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        status, body = await _asgi_json(app, "POST", "/api/drafts/d1/discard")

        assert (status, body) == (200, {"draft_id": "d1", "status": "discarded"})
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert draft["status"] == "discarded"
        assert draft["revision"] == 1
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)

        # 重复丢弃幂等：同一结果、无新增副作用
        status, retry = await _asgi_json(app, "POST", "/api/drafts/d1/discard")
        assert (status, retry) == (200, body)
        status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert draft["status"] == "discarded"


async def test_terminal_draft_states_reject_edits_confirm_and_discard(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())
        await _create_draft(db, draft_id="d2", proposed=_first_time_profile())
        assert (
            await _asgi_json(
                app, "POST", "/api/drafts/d1/confirm", json_body={"revision": 1}
            )
        )[0] == 200
        assert (await _asgi_json(app, "POST", "/api/drafts/d2/discard"))[0] == 200
        formal_after_commit = await _formal(db)

        cases = (
            ("POST", "/api/drafts/d1/revise", _revise_body(_REVISED_FACTS)),
            ("POST", "/api/drafts/d1/discard", None),
            ("POST", "/api/drafts/d2/revise", _revise_body(_REVISED_FACTS)),
            ("POST", "/api/drafts/d2/confirm", {"revision": 1}),
        )
        for method, path, json_body in cases:
            status, body = await _asgi_json(app, method, path, json_body=json_body)
            assert status == 409, (path, body)
            assert body["error_code"] == "invalid_request", path

        # 终态不被改写，正式数据也不变
        status, committed = await _asgi_json(app, "GET", "/api/drafts/d1")
        assert (committed["status"], committed["revision"]) == ("committed", 1)
        status, discarded = await _asgi_json(app, "GET", "/api/drafts/d2")
        assert (discarded["status"], discarded["revision"]) == ("discarded", 1)
        assert await _formal(db) == formal_after_commit


# ---------- 请求体形状：确认与丢弃 ----------


CONFIRM_DISCARD_SHAPE_CASES = cast(
    "tuple[tuple[str, str, dict[str, Any]], ...]",
    (
        ("确认非法 JSON", "/api/drafts/d1/confirm", {"raw_body": b"{not json"}),
        ("确认请求体非对象", "/api/drafts/d1/confirm", {"json_body": [1, 2]}),
        (
            "确认未知字段",
            "/api/drafts/d1/confirm",
            {"json_body": {"revision": 1, "payload": {"profile": _FIRST_TIME_FACTS}}},
        ),
        ("确认缺 revision", "/api/drafts/d1/confirm", {"json_body": {}}),
        (
            "确认 revision 非整数",
            "/api/drafts/d1/confirm",
            {"json_body": {"revision": "1"}},
        ),
        (
            "确认 revision 为小数",
            "/api/drafts/d1/confirm",
            {"json_body": {"revision": 1.5}},
        ),
        (
            "确认 revision 为 0",
            "/api/drafts/d1/confirm",
            {"json_body": {"revision": 0}},
        ),
        (
            "确认 revision 为布尔",
            "/api/drafts/d1/confirm",
            {"json_body": {"revision": True}},
        ),
        ("丢弃带字段", "/api/drafts/d1/discard", {"json_body": {"reason": "x"}}),
        ("丢弃请求体非对象", "/api/drafts/d1/discard", {"json_body": ["x"]}),
    ),
)


async def test_confirm_and_discard_reject_invalid_body_shape(tmp_path: Path) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        for reason, path, kwargs in CONFIRM_DISCARD_SHAPE_CASES:
            status, body = await _asgi_json(app, "POST", path, **kwargs)

            assert status == 400, reason
            assert body["error_code"] == "invalid_request", reason
            status, draft = await _asgi_json(app, "GET", "/api/drafts/d1")
            assert draft["status"] == "pending", reason
            assert await _formal(db) == ProfileSnapshot(
                profile=None, context_version=0
            ), reason


# ---------- 既有本机访问边界与旁路扫描 ----------


async def test_loopback_host_and_origin_boundaries_apply_to_business_endpoints(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        await RunRepo(db).create_conversation("c1")
        await _create_draft(db, draft_id="d1", proposed=_first_time_profile())

        for host, origin in (
            ("evil.example", None),
            (LOOPBACK_HOST, "http://evil.example"),
            (LOOPBACK_HOST, "null"),
        ):
            status, body = await _asgi_json(
                app, "GET", "/api/profile", host=host, origin=origin
            )
            assert status == 403, (host, origin)
            assert body == {"detail": "forbidden"}

        # 写端点同样在边界内，且被拒绝的请求不产生写入
        status, _ = await _asgi_json(
            app,
            "POST",
            "/api/drafts/d1/confirm",
            json_body={"revision": 1},
            host="evil.example",
        )
        assert status == 403
        assert (await _formal(db)).context_version == 0

        # 回环 Host／Origin 正常放行（含端口）
        status, _ = await _asgi_json(
            app,
            "GET",
            "/api/profile",
            host="localhost:8000",
            origin="http://127.0.0.1:5173",
        )
        assert status == 200


SIDE_SCAN_CASES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/drafts"),  # 公开创建草稿
    ("GET", "/api/drafts"),
    ("POST", "/api/drafts/d1/recalc"),  # 重算假成功
    ("PUT", "/api/profile"),  # 直接写正式档案
    ("POST", "/api/profile"),
    ("POST", "/api/runs"),  # 假 Run／聊天／模型路由
    ("GET", "/api/runs/active"),
    ("POST", "/api/chat"),
    ("GET", "/api/models"),
    ("POST", "/api/test/drafts"),  # 测试专用生产路由
)


async def test_no_public_creation_recalc_write_or_fake_run_routes(
    tmp_path: Path,
) -> None:
    async with _served_app(tmp_path) as (app, db):
        for method, path in SIDE_SCAN_CASES:
            status, _ = await _asgi_json(app, method, path, json_body={})

            # 405 = 路径存在但方法未接线（例如 /api/profile 只读）；两者都表示能力未提供
            assert status in (404, 405), (method, path, status)
        assert await _formal(db) == ProfileSnapshot(profile=None, context_version=0)


LEAK_CASES = cast(
    "tuple[tuple[str, str, dict[str, Any]], ...]",
    (
        ("GET", "/api/drafts/d-missing", {}),
        ("POST", "/api/drafts/d1/revise", {"raw_body": b"{oops"}),
        ("POST", "/api/drafts/d1/confirm", {"json_body": {}}),
        ("GET", "/api/profile", {}),
    ),
)


async def test_responses_expose_no_stack_trace_or_credentials(tmp_path: Path) -> None:
    async with _served_app(tmp_path) as (app, db):
        for method, path, kwargs in LEAK_CASES:
            _, body = await _asgi_json(app, method, path, **kwargs)

            text = json.dumps(body, ensure_ascii=False)
            for leaked in (
                "Traceback",
                "sqlite3",
                "SELECT",
                "INSERT",
                "api_key",
                "sk-",
            ):
                assert leaked not in text, (path, leaked)
