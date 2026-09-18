"""Stage 6：自然语言打卡的提取链路、SSE 双载荷与确认写入（T2.1／T2.2）。

依据：``refactor-log/stage6.md`` §2.1（链路）、§2.2（提取与校验 Schema）、§2.3（提交与状态）、
§2.4.2（``confirm-workout`` 端点）、§2.4.3（五类 SSE 事件与 ``waiting`` 双载荷）、§3.1／§3.2
（目标实现）、§4.1–§4.3（行为验收）、§5.1（确定性测试最小集）；已合入源码 ``graph/workflow.py``
（提取→校验→候选日程→可读摘要）、``api/routes_agent.py``（``/api/agent/confirm-workout``）、
``api/dto.py``（DTO 与错误映射）、``domain/records/service.py``（唯一写入路径）。

用真实 ``create_app`` ＋ 真实 lifespan 驱动 ``POST /api/agent/run`` 与
``POST /api/agent/confirm-workout``：只把运行入口的模型换成固定替身，计划／记录／统计服务仍是
生产装配的那一份。计划日程用直接 SQL 预置（与 ``test_stage1_api_records.py`` 同一口径，日程候选只查
``plan_sessions``）。整份文件不调真实模型、不依赖任何 ``MODEL_*`` 环境变量。
"""

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import current_business_date
from api.routes_agent import INVALID_MODEL_RESPONSE_MESSAGE
from config import MODEL_API_KEY_ENV, MODEL_BASE_URL_ENV, MODEL_MODEL_ENV
from graph.nodes import (
    GeneratePlanRun,
    ModelRequestBudget,
    ModelRequestBudgetExceeded,
)
from graph.state import WorkflowState
from graph.workflow import (
    NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
    NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
    SAFETY_STOP_MESSAGE,
    _natural_language_record,
)
from storage.db import Database

#: Stage 6 契约正本：§2.4.3 的 ``waiting`` 载荷 JSON 块是这类事件形状的冻结出处。
STAGE6_PLAN_PATH = (
    Path(__file__).resolve().parents[2] / "refactor-log" / "stage6.md"
)

#: 本次 Run 与确认请求注入的业务日（业务日期一律由服务端注入，客户端不传）。
BUSINESS_DAY = date(2026, 6, 1)
#: 训练发生日（与业务日同一天，日程候选就在这一天）与另一个有日程的日期。
DAY = BUSINESS_DAY
OTHER_DAY = date(2026, 6, 5)
CONVERSATION_ID = "6a1c0f3e-2b47-4d90-8e5a-0c3f7b1d9a24"
CREATED_AT = "2026-06-01T08:00:00+00:00"

#: 目录动作：外加负重型（口径＋重量＋次数）、纯自重（只有次数）、计时（只有秒数）。
SQUAT = "barbell-back-squat"
SQUAT_CONVENTION = "barbell_includes_bar_total"
PULL_UP = "pull-up"
PLANK = "plank"

#: 固定替身生成的摘要文本：``message`` 只作可读展示，前端不从它反向解析确认字段（§2.4.3）。
SUMMARY = "9 月 1 日：杠铃背蹲 60kg，1 组 × 5 次。"

#: §3.8／§2.4.3：事件名集合封闭为五类；``waiting`` 有两条路径、两种载荷（计划路径与自然语言打卡路径）。
SSE_EVENT_KEYS: dict[str, tuple[frozenset[str], ...]] = {
    "node": (frozenset({"name"}),),
    "message": (frozenset({"text"}),),
    "waiting": (
        frozenset({"draft_plan_id"}),
        frozenset({"workout", "candidate_plan_sessions"}),
    ),
    "done": (frozenset({"ok", "intent", "termination_reason", "draft_plan_id"}),),
    "error": (frozenset({"message"}),),
}

#: ``waiting.workout`` 的字段集合（确认 UI 的编辑数据源；stage6.md §2.2）。
WORKOUT_KEYS = frozenset({"performed_on", "sets", "plan_session_id", "auto_link"})

#: 提取结果里一条组的字段集合（模型输出 Schema；stage6.md §2.2）。
EXTRACTED_SET_KEYS = frozenset(
    {
        "exercise_id",
        "set_no",
        "set_type",
        "load_convention",
        "weight_kg",
        "reps",
        "duration_seconds",
    }
)

#: 生成 ``waiting`` 前必须先落库的表集合：确认前任何一张变化都失败（§2.1「未确认不写业务库」）。
_TRACKED_TABLES = ("plans", "plan_sessions", "workout_sessions", "workout_sets")


class ScriptedModel:
    """按系统提示词分派固定响应的模型替身；未脚本化的提示词即失败（分类模型必须 0 次调用）。"""

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

    @property
    def prompts(self) -> list[str]:
        """本次 Run 的系统提示词调用序列（分类／提取／摘要各算一次）。"""
        return [prompt for prompt, _ in self.calls]

    def payload(self, system_prompt: str, index: int = 0) -> dict[str, Any]:
        """第 ``index`` 次该类调用的用户载荷（JSON 文本解码）。"""
        calls = [call for call in self.calls if call[0] == system_prompt]
        return json.loads(calls[index][1])


def _extracted_set(
    exercise_id: str,
    *,
    set_no: int = 1,
    set_type: str = "work",
    reps: int | None = None,
    load_convention: str | None = None,
    weight_kg: float | None = None,
    duration_seconds: int | None = None,
) -> dict[str, Any]:
    """一条提取结果：只给出该动作实际记录的字段（未给字段不出现在 JSON 里）。"""
    item: dict[str, Any] = {
        "exercise_id": exercise_id,
        "set_no": set_no,
        "set_type": set_type,
    }
    if reps is not None:
        item["reps"] = reps
    if load_convention is not None:
        item["load_convention"] = load_convention
    if weight_kg is not None:
        item["weight_kg"] = weight_kg
    if duration_seconds is not None:
        item["duration_seconds"] = duration_seconds
    return item


def _squat_extraction(
    *, weight_kg: float = 60.0, reps: int = 5, set_no: int = 1
) -> dict[str, Any]:
    """外加负重型动作的合法提取结果（口径与重量同现）。"""
    return _extracted_set(
        SQUAT,
        set_no=set_no,
        reps=reps,
        load_convention=SQUAT_CONVENTION,
        weight_kg=weight_kg,
    )


def _record_scripts(
    sets: Sequence[Mapping[str, Any]],
    *,
    performed_on: date = DAY,
    message: str = SUMMARY,
) -> dict[str, list[str]]:
    """自然语言打卡成功路径的两份脚本：结构化提取 ＋ 可读摘要。"""
    return {
        NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT: [
            json.dumps(
                {"performed_on": performed_on.isoformat(), "sets": list(sets)},
                ensure_ascii=False,
            )
        ],
        NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [message],
    }


def _waiting_payload(frames: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """自然语言打卡路径的 ``waiting`` 载荷（恰含 ``workout`` 与 ``candidate_plan_sessions``）。"""
    waiting = [data for name, data in frames if name == "waiting"]
    assert len(waiting) == 1, f"成功路径必须恰有一个 waiting：{waiting}"
    return waiting[0]


def _parse_frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    """SSE 文本 → ``(事件名, data)``；逐帧校验事件名属于五类，且载荷键是契约给出的形状之一。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        assert len(lines) == 2, f"SSE 帧必须恰为 event ＋ data 两行：{block!r}"
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        name = lines[0][len("event: ") :]
        assert name in SSE_EVENT_KEYS, f"SSE 事件名不在五类产品事件里：{name}"
        data = json.loads(lines[1][len("data: ") :])
        assert set(data) in SSE_EVENT_KEYS[name], f"{name} 的 data 键不符：{sorted(data)}"
        frames.append((name, data))
    return frames


def _sse_frames(response: Any) -> list[tuple[str, dict[str, Any]]]:
    """SSE 响应 → 事件列表（HTTP 报文形态 ＋ 逐帧契约校验）。"""
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return _parse_frames(response.text)


def _event_names(frames: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _ in frames]


def _node_names(frames: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    return [data["name"] for name, data in frames if name == "node"]


def _without_id(record: Mapping[str, Any]) -> dict[str, Any]:
    """落库训练事实去掉自增身份后的领域结果（表单路径与确认路径的同源对比）。"""
    return {key: value for key, value in record.items() if key != "id"}


async def _seed_plan_session(
    db: Database, *, version: int, status: str, scheduled_on: date
) -> tuple[int, int]:
    """直接 SQL 预置一个计划版本与其一个日程，返回 ``(plan_id, plan_session_id)``。

    ``structured_content`` 用 ``'{}'``：自然语言打卡链路不解析计划内容，日程候选只查
    ``plan_sessions``（``domain/records/repo.py::list_unfinished_plan_sessions``）。
    """
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "INSERT INTO plans (version, status, structured_content, created_at)"
            " VALUES (?, ?, '{}', ?)",
            (version, status, CREATED_AT),
        )
        plan_id = int(cursor.lastrowid or 0)
        await cursor.close()
        cursor = await conn.execute(
            "INSERT INTO plan_sessions (plan_id, scheduled_on) VALUES (?, ?)",
            (plan_id, scheduled_on.isoformat()),
        )
        session_id = int(cursor.lastrowid or 0)
        await cursor.close()
    return plan_id, session_id


async def _row_counts(db: Database) -> dict[str, int]:
    """业务行数快照：未确认／校验失败时任何一张表变化都失败。"""

    async def op(conn: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for table in _TRACKED_TABLES:
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                counts[table] = int((await cursor.fetchone())[0])
        return counts

    return await db.under_lock(op)


async def _plan_rows(db: Database) -> list[dict[str, Any]]:
    """``plans`` 全列快照（按版本升序）：打卡确认不得改变任何计划状态。"""

    async def op(conn: Any) -> list[dict[str, Any]]:
        async with conn.execute(
            "SELECT id, version, status, source_plan_id, structured_content,"
            " created_at, confirmed_at, archived_at FROM plans ORDER BY version"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


async def _plan_session_rows(db: Database) -> list[dict[str, Any]]:
    """``plan_sessions`` 全列快照（按身份升序）：日程本身不因打卡确认被改写。"""

    async def op(conn: Any) -> list[dict[str, Any]]:
        async with conn.execute(
            "SELECT id, plan_id, scheduled_on, cancelled_at FROM plan_sessions"
            " ORDER BY id"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    return await db.under_lock(op)


@dataclass
class _AgentApp:
    """一次测试的 app 客户端、固定替身模型与业务库（预置事实用直接 SQL）。"""

    client: TestClient
    model: ScriptedModel
    db: Database

    def run(self, request: str) -> list[tuple[str, dict[str, Any]]]:
        """经真实端点跑一次 Agent Run（业务日由 app 的依赖覆盖固定）。"""
        return _sse_frames(
            self.client.post(
                "/api/agent/run",
                json={"conversation_id": CONVERSATION_ID, "request": request},
            )
        )

    def confirm_workout(self, body: Mapping[str, Any]) -> Any:
        """``POST /api/agent/confirm-workout``：确认载荷原样提交（含用户修改后的完整值）。"""
        return self.client.post("/api/agent/confirm-workout", json=dict(body))

    def seed_plan_session(
        self, *, version: int, status: str, scheduled_on: date
    ) -> tuple[int, int]:
        return self.client.portal.call(
            partial(
                _seed_plan_session,
                version=version,
                status=status,
                scheduled_on=scheduled_on,
            ),
            self.db,
        )

    def records(self) -> list[dict[str, Any]]:
        """既有表单读取端点：确认写入的落库事实与表单路径看到的是同一张表。"""
        response = self.client.get("/api/records")
        assert response.status_code == 200, response.text
        return response.json()["records"]

    def counts(self) -> dict[str, int]:
        return self.client.portal.call(_row_counts, self.db)

    def plans(self) -> list[dict[str, Any]]:
        return self.client.portal.call(_plan_rows, self.db)

    def plan_sessions(self) -> list[dict[str, Any]]:
        return self.client.portal.call(_plan_session_rows, self.db)

    def personal_bests(self) -> list[dict[str, Any]]:
        """既有 Stats 只读端点：PB 由确定性统计现算，不落表。"""
        response = self.client.get("/api/stats/personal-bests")
        assert response.status_code == 200, response.text
        return response.json()["personal_bests"]


@contextmanager
def _app_client(tmp_path: Path) -> Iterator[TestClient]:
    """真实 app（临时数据目录 ＋ 真实 lifespan）＋ 固定业务日期。"""
    app = create_app(tmp_path / "data", frontend_dist=tmp_path / "dist")
    app.dependency_overrides[current_business_date] = lambda: BUSINESS_DAY
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


@contextmanager
def _scripted_app(
    tmp_path: Path, *, scripts: Mapping[str, Sequence[str]] | None = None
) -> Iterator[_AgentApp]:
    """只替换运行入口的模型 callable（生产装配的其余服务原样保留），不调真实模型、不读环境变量。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        model = ScriptedModel(scripts)
        runtime = client.app.state.agent_runtime
        client.app.state.agent_runtime = replace(
            runtime, run_deps=replace(runtime.run_deps, model=model)
        )
        yield _AgentApp(client=client, model=model, db=db)


def _confirm_body(
    sets: Sequence[Mapping[str, Any]],
    *,
    performed_on: date = DAY,
    plan_session_id: int | None = None,
    auto_link: bool = False,
    conversation_id: str = CONVERSATION_ID,
) -> dict[str, Any]:
    """确认提交载荷：用户可修改后的完整值（日期 ＋ 组 ＋ 日程关联两字段）。"""
    return {
        "conversation_id": conversation_id,
        "performed_on": performed_on.isoformat(),
        "sets": list(sets),
        "plan_session_id": plan_session_id,
        "auto_link": auto_link,
    }


# ---------- §2.4.3 冻结契约：waiting 双载荷 ----------


def test_natural_language_waiting_payload_matches_the_frozen_stage6_contract() -> None:
    """§2.4.3：自然语言打卡 ``waiting`` 载荷的键集合来自契约文档的 JSON 块，不在这里另立形状。"""
    text = STAGE6_PLAN_PATH.read_text(encoding="utf-8")
    section = text[text.index("#### 2.4.3 SSE 事件") : text.index("### 2.5 前端")]
    blocks = re.findall(r"```json\n(.*?)```", section, re.DOTALL)
    contract = next(
        (json.loads(block) for block in blocks if "workout" in json.loads(block)), None
    )

    assert contract is not None, "stage6.md §2.4.3 缺少自然语言打卡 waiting 载荷 JSON 块"
    assert set(contract) == {"workout", "candidate_plan_sessions"}
    assert set(contract["workout"]) == WORKOUT_KEYS


def test_workflow_state_stays_free_of_natural_language_parse_fields() -> None:
    """§2.3／§3.1：``WorkflowState`` 字段集合保持 Stage 3–5 冻结契约，不塞解析状态或候选日程。

    字段清单出处：``graph/state.py::WorkflowState``（Stage 3 冻结；stage5.md §3.9 未增字段），
    自然语言打卡的提取结果与候选日程只在本次 Run 的返回值与 ``waiting`` 载荷里，不进 State。
    """
    assert set(WorkflowState.__annotations__) == {
        "conversation_id",
        "request",
        "intent",
        "context",
        "loaded_skill",
        "draft_plan_id",
        "draft_plan",
        "evaluation",
        "revision_count",
        "confirmation",
        "termination_reason",
    }


# ---------- §2.1／§2.4.3：提取链路与 SSE 双载荷（T2.1） ----------


def test_natural_language_record_emits_node_message_waiting_done_without_writing(
    tmp_path: Path,
) -> None:
    """§2.1／§2.4.3：成功路径事件顺序恰为 ``node`` → ``message`` → ``waiting`` → ``done``。

    模型只做提取与摘要（分类模型 0 次调用）；``waiting.workout`` 是确认 UI 的完整数据源，
    ``candidate_plan_sessions`` 来自数据库；确认前 ``workout_sessions``／``workout_sets`` 无新行。
    """
    with _scripted_app(tmp_path, scripts=_record_scripts([_squat_extraction()])) as agent:
        plan_id, session_id = agent.seed_plan_session(
            version=1, status="active", scheduled_on=DAY
        )
        before = agent.counts()

        frames = agent.run("记录今天杠铃背蹲60kg5次")

        assert _event_names(frames) == ["node", "message", "waiting", "done"]
        assert _node_names(frames) == ["natural_language_record"]
        assert frames[1][1] == {"text": SUMMARY}
        waiting = _waiting_payload(frames)
        assert set(waiting) == {"workout", "candidate_plan_sessions"}
        assert set(waiting["workout"]) == WORKOUT_KEYS
        assert waiting["workout"]["performed_on"] == DAY.isoformat()
        assert set(waiting["workout"]["sets"][0]) == EXTRACTED_SET_KEYS
        assert waiting["workout"]["sets"] == [
            {
                "exercise_id": SQUAT,
                "set_no": 1,
                "set_type": "work",
                "load_convention": SQUAT_CONVENTION,
                "weight_kg": 60.0,
                "reps": 5,
                "duration_seconds": None,
            }
        ]
        # 恰一个未完成日程：默认值仍是「不指定、允许自动关联」，由用户在确认 UI 改选（§2.1 硬边界）。
        assert waiting["workout"]["plan_session_id"] is None
        assert waiting["workout"]["auto_link"] is True
        assert waiting["candidate_plan_sessions"] == [
            {"id": session_id, "plan_id": plan_id, "scheduled_on": DAY.isoformat()}
        ]
        assert frames[-1][1] == {
            "ok": True,
            "intent": "natural_language_record",
            "termination_reason": None,
            "draft_plan_id": None,
        }
        assert agent.model.prompts == [
            NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
            NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
        ]
        assert agent.records() == []
        assert agent.counts() == before


def test_waiting_candidates_come_from_the_database_query_for_the_extracted_day(
    tmp_path: Path,
) -> None:
    """§2.1／§2.2：``candidate_plan_sessions`` 是按提取出的 ``performed_on`` 查库的结果。

    模型输入里有可读动作目录（稳定 ``exercise_id``）；候选身份只从数据库查询来，模型看不到别的日期，
    也不生成候选 ID。
    """
    with _scripted_app(tmp_path, scripts=_record_scripts([_squat_extraction()])) as agent:
        plan_id, session_id = agent.seed_plan_session(
            version=1, status="active", scheduled_on=DAY
        )
        other_plan_id, other_session_id = agent.seed_plan_session(
            version=2, status="archived", scheduled_on=OTHER_DAY
        )

        frames = agent.run("记录今天杠铃背蹲60kg5次")

        waiting = _waiting_payload(frames)
        assert waiting["candidate_plan_sessions"] == [
            {"id": session_id, "plan_id": plan_id, "scheduled_on": DAY.isoformat()}
        ]
        assert waiting["candidate_plan_sessions"] == [
            {
                "id": row["id"],
                "plan_id": row["plan_id"],
                "scheduled_on": row["scheduled_on"],
            }
            for row in agent.plan_sessions()
            if row["scheduled_on"] == DAY.isoformat() and row["cancelled_at"] is None
        ]
        assert other_session_id not in [
            item["id"] for item in waiting["candidate_plan_sessions"]
        ]
        # 摘要生成用的是同一份数据库候选与同一份已校验训练事实（模型不重算、不引入新候选）。
        message_payload = agent.model.payload(NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT)
        assert message_payload["workout"] == waiting["workout"]
        assert (
            message_payload["candidate_plan_sessions"]
            == waiting["candidate_plan_sessions"]
        )
        extraction_payload = agent.model.payload(
            NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT
        )
        assert extraction_payload["business_day"] == DAY.isoformat()
        assert all(
            set(row) == {"exercise_id", "standard_name_zh", "record_type", "load_convention"}
            for row in extraction_payload["actions"]
        )
        assert {row["exercise_id"] for row in extraction_payload["actions"]} >= {
            SQUAT,
            PULL_UP,
            PLANK,
        }
        # 另一个日期的日程与计划都不在候选里，也不出现在任何一份模型输入里。
        assert other_plan_id != plan_id
        assert [item["id"] for item in waiting["candidate_plan_sessions"]] == [
            session_id
        ]
        assert OTHER_DAY.isoformat() not in json.dumps(
            [extraction_payload, message_payload], ensure_ascii=False
        )


def test_candidate_plan_sessions_come_from_the_database_even_when_the_model_invents_ids(
    tmp_path: Path,
) -> None:
    """§2.1「候选 ID」硬边界／§5.1：模型不得生成不存在的候选 ``plan_session_id``。

    摘要文本里写一个目录里不存在的日程 ID：``waiting.candidate_plan_sessions`` 仍是按 ``performed_on``
    查库的结果，那个 ID 不出现在任何 SSE 载荷里；按 ``waiting`` 默认值确认后关联的仍是数据库候选。
    提取阶段模型更早：提取载荷里没有任何日程身份，Schema 也拒绝 ``plan_session_id`` 额外字段
    （``INVALID_EXTRACTIONS`` 的「多出未声明字段」）。
    """
    invented = 999999
    summary = f"9 月 1 日：杠铃背蹲 60kg，1 组 × 5 次；已关联日程 #{invented}。"
    with _scripted_app(
        tmp_path,
        scripts=_record_scripts([_squat_extraction()], message=summary),
    ) as agent:
        plan_id, session_id = agent.seed_plan_session(
            version=1, status="active", scheduled_on=DAY
        )

        frames = agent.run("记录今天杠铃背蹲60kg5次")

        waiting = _waiting_payload(frames)
        assert waiting["candidate_plan_sessions"] == [
            {"id": session_id, "plan_id": plan_id, "scheduled_on": DAY.isoformat()}
        ]
        assert frames[-1][1]["draft_plan_id"] is None
        # 编造的 ID 只可能出现在可读摘要里；结构化载荷（waiting／done）只有数据库事实。
        structured = json.dumps(
            [data for name, data in frames if name != "message"], ensure_ascii=False
        )
        assert str(invented) not in structured
        extraction_payload = agent.model.payload(
            NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT
        )
        assert set(extraction_payload) == {"request", "business_day", "actions"}
        assert "plan_session" not in json.dumps(extraction_payload, ensure_ascii=False)

        confirmed = agent.confirm_workout(
            {"conversation_id": CONVERSATION_ID, **waiting["workout"]}
        )

        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["workout_session"]["plan_session_id"] == session_id


def test_confirm_submission_uses_waiting_fields_not_the_visible_message(
    tmp_path: Path,
) -> None:
    """§2.4.3／§5.1：``message`` 只作可读展示，确认提交数据来自 ``waiting`` 结构化字段。

    摘要文本故意写出与结构化事实不符的数值（999kg／99 组 × 99 次）与一个编造的日程 ID：这些内容只
    在 ``message`` 里；确认提交只用 ``waiting.workout`` 的字段，落库事实逐字段等于结构化载荷，
    可见文本里的数值一个都不进数据库。
    """
    decoy = "9 月 1 日：杠铃背蹲 999kg，99 组 × 99 次；已关联日程 #424242。"
    with _scripted_app(
        tmp_path, scripts=_record_scripts([_squat_extraction()], message=decoy)
    ) as agent:
        _, session_id = agent.seed_plan_session(
            version=1, status="active", scheduled_on=DAY
        )

        frames = agent.run("记录今天杠铃背蹲60kg5次")

        assert frames[1][1] == {"text": decoy}
        waiting = _waiting_payload(frames)
        assert waiting["workout"]["sets"] == [
            {
                "exercise_id": SQUAT,
                "set_no": 1,
                "set_type": "work",
                "load_convention": SQUAT_CONVENTION,
                "weight_kg": 60.0,
                "reps": 5,
                "duration_seconds": None,
            }
        ]

        response = agent.confirm_workout(
            {"conversation_id": CONVERSATION_ID, **waiting["workout"]}
        )

        assert response.status_code == 200, response.text
        written = response.json()["workout_session"]
        assert written["plan_session_id"] == session_id
        assert written["sets"] == waiting["workout"]["sets"]
        assert {row["weight_kg"] for row in written["sets"]} == {60.0}
        assert agent.records() == [written]


#: 结构层非法的提取结果：Pydantic Schema 不通过即运行错误（stage6.md §3.1「非法输出为 Run error」）。
INVALID_EXTRACTIONS: tuple[tuple[str, str], ...] = (
    ("缺少必填 performed_on", json.dumps({"sets": [_squat_extraction()]})),
    (
        "多出未声明字段",
        json.dumps(
            {
                "performed_on": DAY.isoformat(),
                "sets": [_squat_extraction()],
                "plan_session_id": 7,
            }
        ),
    ),
    (
        "动作项多出未声明字段",
        json.dumps(
            {
                "performed_on": DAY.isoformat(),
                "sets": [{**_squat_extraction(), "rir": 2}],
            }
        ),
    ),
    (
        "组类型不在三态内",
        json.dumps(
            {
                "performed_on": DAY.isoformat(),
                "sets": [{**_squat_extraction(), "set_type": "rest"}],
            }
        ),
    ),
    ("不是 JSON", "不是 JSON"),
    ("不是对象", json.dumps([1, 2, 3])),
)


@pytest.mark.parametrize("case, extraction", INVALID_EXTRACTIONS)
def test_structural_extraction_failure_is_one_run_error_and_writes_nothing(
    tmp_path: Path, case: str, extraction: str
) -> None:
    """§2.2／§3.1：Pydantic 结构校验失败是运行错误——只发一个 ``error``，不发 ``waiting``、不写库。"""
    scripts = {
        NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT: [extraction],
        NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT: [SUMMARY],
    }
    with _scripted_app(tmp_path, scripts=scripts) as agent:
        agent.seed_plan_session(version=1, status="active", scheduled_on=DAY)
        before = agent.counts()

        frames = agent.run("记录今天杠铃背蹲60kg5次")

        assert _event_names(frames) == ["node", "error"], case
        assert frames[-1][1] == {"message": INVALID_MODEL_RESPONSE_MESSAGE}, case
        assert agent.model.prompts == [NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT], case
        assert agent.records() == [], case
        assert agent.counts() == before, case


#: 领域／目录口径非法的提取结果：形状合法，但事实不合规（stage6.md §2.2 第二层）。
INVALID_DOMAIN_FACTS: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "动作不在目录内",
        _extracted_set("not-in-catalog", reps=5),
        "动作身份不在目录内",
    ),
    (
        "负重口径与目录不符",
        _extracted_set(
            SQUAT,
            reps=5,
            load_convention="external_added_weight",
            weight_kg=60.0,
        ),
        "负重口径",
    ),
    (
        "外加负重型缺重量与口径",
        _extracted_set(SQUAT, reps=5),
        "负重口径 None 与目录动作",
    ),
    (
        "纯自重缺次数",
        _extracted_set(PULL_UP),
        "reps_bodyweight 型动作必须记录次数",
    ),
    (
        "外加负重型多给时长",
        _extracted_set(
            SQUAT,
            reps=5,
            load_convention=SQUAT_CONVENTION,
            weight_kg=60.0,
            duration_seconds=60,
        ),
        "不得记录持续秒数",
    ),
    (
        "计时动作缺时长",
        _extracted_set(PLANK),
        "计时动作必须记录持续秒数",
    ),
    (
        "组序号越界",
        _squat_extraction(set_no=0),
        "组序号必须在",
    ),
    (
        "次数越界",
        _squat_extraction(reps=0),
        "单组次数必须在",
    ),
    (
        "重量越界",
        _squat_extraction(weight_kg=2000.0),
        "重量必须在",
    ),
)


@pytest.mark.parametrize("case, extracted_set, expected", INVALID_DOMAIN_FACTS)
def test_domain_invalid_extraction_returns_readable_text_without_waiting_or_writes(
    tmp_path: Path, case: str, extracted_set: dict[str, Any], expected: str
) -> None:
    """§2.2／§3.1：领域规则与目录口径复验失败以可读文本返回，不发 ``waiting``、不写库。"""
    with _scripted_app(tmp_path, scripts=_record_scripts([extracted_set])) as agent:
        agent.seed_plan_session(version=1, status="active", scheduled_on=DAY)
        before = agent.counts()

        frames = agent.run("记录今天杠铃背蹲60kg5次")

        assert _event_names(frames) == ["node", "message", "done"], case
        text = frames[1][1]["text"]
        assert "未通过校验" in text and expected in text, case
        assert frames[-1][1]["intent"] == "natural_language_record", case
        # 校验未通过就不再生成摘要，也不查候选日程。
        assert agent.model.prompts == [NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT], case
        assert agent.records() == [], case
        assert agent.counts() == before, case


def test_natural_language_record_shares_the_run_model_request_budget(
    tmp_path: Path,
) -> None:
    """§2.4.3：打卡链路与 Router／计划链路共享同一份每 Run 预算：提取与摘要各扣一次请求。

    预算只剩一次时，摘要生成被既有 ``ModelRequestBudgetExceeded`` 拒绝（运行错误、不写库）。
    正本：``graph/nodes.py::ModelRequestBudget.begin_request``（上限 5／60s／180s 未改）与
    ``graph/workflow.py::_request_model``。
    """
    with _scripted_app(tmp_path, scripts=_record_scripts([_squat_extraction()])) as agent:
        deps = agent.client.app.state.agent_runtime.run_deps
        before = agent.counts()

        async def op() -> int:
            run = GeneratePlanRun(
                business_day=BUSINESS_DAY, budget=ModelRequestBudget(max_requests=1)
            )
            with pytest.raises(ModelRequestBudgetExceeded):
                await _natural_language_record(
                    "记录今天杠铃背蹲60kg5次", run=run, deps=deps
                )
            return run.budget.used

        used = agent.client.portal.call(op)

        assert used == 1
        assert agent.model.prompts == [NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT]
        assert agent.records() == []
        assert agent.counts() == before


def test_acute_keyword_still_stops_before_the_natural_language_branch(tmp_path: Path) -> None:
    """§2.6／§4.3：急性关键词命中先于 Router——不解析打卡、不调模型、不写库，仍走 ``safety_stop``。"""
    with _scripted_app(tmp_path) as agent:
        before = agent.counts()

        frames = agent.run("记录今天杠铃背蹲60kg5次，但最近晕厥")

        assert _node_names(frames) == ["safety_check", "safety_stop"]
        assert _event_names(frames) == ["node", "node", "message", "done"]
        assert frames[-2][1] == {"text": SAFETY_STOP_MESSAGE}
        assert frames[-1][1] == {
            "ok": True,
            "intent": None,
            "termination_reason": "safety_stop",
            "draft_plan_id": None,
        }
        assert agent.model.calls == []
        assert agent.records() == []
        assert agent.counts() == before


#: §2.1「零候选」行／§2.4.2：零候选与「多候选未选择」同口径，只有用户显式选择才写额外训练。
#: 摘要提示词里承诺自动写入额外训练的表述出现过即失败。
AUTO_EXTRA_TRAINING_PROMISES = (
    "将记为额外训练",
    "自动记为额外训练",
    "自动标记为额外训练",
    "自动写为额外训练",
)


def test_zero_candidate_run_cannot_promise_an_automatic_extra_training_write(
    tmp_path: Path,
) -> None:
    """§2.1「零候选」行／§2.4.2／§5.1：零候选不得承诺自动写额外训练，只有用户显式选择才写。

    摘要模型的指令与载荷、以及 Run 返回的 ``waiting.workout`` 都不承诺自动写入：把这份 ``waiting``
    载荷原样提交（``plan_session_id=null`` 且 ``auto_link=true``）走既有领域规则产生日程歧义错误、
    不写库；用户显式选择「额外训练」（``auto_link=false``）才写为 ``plan_session_id=null``。
    依据：stage6.md §2.1「零候选」行、§2.4.2 日程关联规则、§5.1；讨论总结 §9 第 290 行（仅当当天恰有
    一个未完成日程可自动关联，否则必须由用户选择）；已合入源码 ``domain/records/service.py::create``
    的 ``_resolve_link_in_transaction``。
    """
    assert "零个时" in NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT
    zero_candidate_clause = NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT.split(
        "零个时", 1
    )[1].split("；", 1)[0]
    assert "显式选择" in zero_candidate_clause
    assert "额外训练" in zero_candidate_clause
    assert not any(
        promise in NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT
        for promise in AUTO_EXTRA_TRAINING_PROMISES
    )

    request = "记录今天杠铃背蹲60kg5次"
    with _scripted_app(tmp_path, scripts=_record_scripts([_squat_extraction()])) as agent:
        before = agent.counts()

        frames = agent.run(request)

        waiting = _waiting_payload(frames)
        assert waiting["candidate_plan_sessions"] == []
        # 默认值仍是「不指定、允许自动关联」：零候选也等用户在确认 UI 显式改选额外训练。
        assert waiting["workout"]["plan_session_id"] is None
        assert waiting["workout"]["auto_link"] is True
        # 摘要模型的输入就是这份零候选载荷：没有候选 ID 可引用，也没有任何自动写入许诺。
        assert agent.model.payload(NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT) == {
            "request": request,
            "workout": waiting["workout"],
            "candidate_plan_sessions": [],
        }
        # 这份载荷原样提交即歧义：既有领域规则不写库。
        ambiguous = agent.confirm_workout(
            {"conversation_id": CONVERSATION_ID, **waiting["workout"]}
        )
        assert ambiguous.status_code == 409, ambiguous.text
        assert "候选不是恰好一个（0）" in ambiguous.json()["message"]
        assert agent.records() == []
        assert agent.counts() == before
        # 用户显式选择「额外训练」后才写为额外训练（plan_session_id=null 且 auto_link=false）。
        written = agent.confirm_workout(
            {
                "conversation_id": CONVERSATION_ID,
                **waiting["workout"],
                "auto_link": False,
            }
        )
        assert written.status_code == 200, written.text
        assert written.json()["workout_session"]["plan_session_id"] is None


# ---------- §2.4.2：confirm-workout 端点（T2.2） ----------


def test_confirm_workout_writes_through_the_same_domain_path_as_the_form_api(
    tmp_path: Path,
) -> None:
    """§2.1／§3.2：确认写入与表单写入同源——同一载荷经两条 API 得到一致的领域结果。

    用显式额外训练（``plan_session_id=null`` 且 ``auto_link=false``）提交同一份事实：两条路都落在
    同一张表、同一套组行，且返回的领域结果除自增身份外逐字段相等。
    """
    with _app_client(tmp_path) as client:
        form_body = {
            "performed_on": DAY.isoformat(),
            "plan_session_id": None,
            "auto_link": False,
            "sets": [
                {
                    "exercise_id": SQUAT,
                    "set_type": "work",
                    "reps": 5,
                    "load_convention": SQUAT_CONVENTION,
                    "weight_kg": 60.0,
                }
            ],
        }
        confirm_body = _confirm_body([_squat_extraction()])

        form_response = client.post("/api/records", json=form_body)
        confirm_response = client.post(
            "/api/agent/confirm-workout", json=confirm_body
        )

        assert form_response.status_code == 200, form_response.text
        assert confirm_response.status_code == 200, confirm_response.text
        form_record = form_response.json()["record"]
        confirmed = confirm_response.json()["workout_session"]
        assert _without_id(confirmed) == _without_id(form_record)
        assert confirmed["plan_session_id"] is None
        assert client.get("/api/records").json()["records"] == [
            form_record,
            confirmed,
        ]


def test_confirm_workout_returns_the_reread_deterministic_personal_bests(
    tmp_path: Path,
) -> None:
    """§2.4.2：写入成功后返回落库训练事实与既有 Stats 服务重查的 PB；恰一个候选时自动关联。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        _, session_id = client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body(
                [_squat_extraction(weight_kg=62.5, reps=6)],
                auto_link=True,
            ),
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["workout_session"]["plan_session_id"] == session_id
        assert data["workout_session"]["performed_on"] == DAY.isoformat()
        stats_bests = client.get("/api/stats/personal-bests").json()["personal_bests"]
        assert data["personal_bests"] == stats_bests
        assert [
            (best["exercise_id"], best["pb_type"], best["value"])
            for best in data["personal_bests"]
        ] == [(SQUAT, "weight_pb", 62.5)]


def test_confirm_workout_auto_links_only_the_unfinished_session_of_that_day(
    tmp_path: Path,
) -> None:
    """§2.1／§4.2：``plan_session_id=null`` 且 ``auto_link=true`` 时关联当天的唯一未完成日程。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        _, session_id = client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )
        client.portal.call(
            partial(
                _seed_plan_session,
                version=2,
                status="archived",
                scheduled_on=OTHER_DAY,
            ),
            db,
        )

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body([_squat_extraction()], auto_link=True),
        )

        assert response.status_code == 200, response.text
        assert response.json()["workout_session"]["plan_session_id"] == session_id
        assert client.get("/api/records").json()["records"][0][
            "plan_session_id"
        ] == session_id


def test_confirm_workout_zero_candidates_auto_link_is_ambiguous_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """§2.1／§4.2：当天没有未完成日程时系统也不替用户选择：``auto_link=true`` 产生歧义错误且不写库。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        counts_before = client.portal.call(_row_counts, db)

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body([_squat_extraction()], auto_link=True),
        )

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["http_status"] == 409 and body["error_code"] == "invalid_request"
        assert "候选不是恰好一个（0）" in body["message"]
        assert client.get("/api/records").json()["records"] == []
        assert client.portal.call(_row_counts, db) == counts_before


def test_confirm_workout_zero_candidates_explicit_extra_training_writes_null_link(
    tmp_path: Path,
) -> None:
    """§2.4.2：零候选时用户显式选择「额外训练」（``plan_session_id=null`` 且 ``auto_link=false``）即写为额外训练。"""
    with _app_client(tmp_path) as client:
        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body([_squat_extraction()], auto_link=False),
        )

        assert response.status_code == 200, response.text
        assert response.json()["workout_session"]["plan_session_id"] is None
        records = client.get("/api/records").json()["records"]
        assert len(records) == 1 and records[0]["plan_session_id"] is None


def test_confirm_workout_multi_candidate_auto_link_is_ambiguous_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """§2.1／§4.2：多候选时系统不自动选择，确认接口产生日程歧义错误（409）且不写库。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )
        client.portal.call(
            partial(
                _seed_plan_session, version=2, status="archived", scheduled_on=DAY
            ),
            db,
        )
        counts_before = client.portal.call(_row_counts, db)

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body([_squat_extraction()], auto_link=True),
        )

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["http_status"] == 409 and body["error_code"] == "invalid_request"
        assert "候选不是恰好一个" in body["message"]
        assert client.get("/api/records").json()["records"] == []
        assert client.portal.call(_row_counts, db) == counts_before


@pytest.mark.parametrize("auto_link", [False, True])
def test_confirm_workout_explicit_session_choice_links_that_session(
    tmp_path: Path, auto_link: bool
) -> None:
    """§2.1／§4.2：显式选择某一 ``plan_session_id`` 即关联该日程；显式身份优先于 ``auto_link``。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )
        _, chosen = client.portal.call(
            partial(
                _seed_plan_session, version=2, status="archived", scheduled_on=DAY
            ),
            db,
        )

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body(
                [_squat_extraction()],
                plan_session_id=chosen,
                auto_link=auto_link,
            ),
        )

        assert response.status_code == 200, response.text
        assert response.json()["workout_session"]["plan_session_id"] == chosen


def test_confirm_workout_multi_candidate_explicit_extra_training_writes_null_link(
    tmp_path: Path,
) -> None:
    """§2.1／§4.2：多候选时用户可显式标记为额外训练（``plan_session_id=null`` 且 ``auto_link=false``）。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        for version in (1, 2):
            client.portal.call(
                partial(
                    _seed_plan_session,
                    version=version,
                    status="active" if version == 1 else "archived",
                    scheduled_on=DAY,
                ),
                db,
            )

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body([_squat_extraction()], auto_link=False),
        )

        assert response.status_code == 200, response.text
        assert response.json()["workout_session"]["plan_session_id"] is None


def test_confirm_workout_writes_the_full_payload_the_user_edited(tmp_path: Path) -> None:
    """§2.3／§2.4.2／§2.4.3／§5.1：用户修改解析结果后，确认端点按修改后的完整载荷重新校验并写入。

    解析出的原始事实是「6 月 1 日 杠铃背蹲 60kg×5」；用户在确认 UI 里把日期改到另一个有未完成日程
    的日子、重量与次数改成 70kg×3，并保持 ``plan_session_id=null`` 且 ``auto_link=true``：落库的是
    修改后的值，自动关联的是修改后日期的唯一候选，原始日期与原值都不落库。
    """
    with _scripted_app(tmp_path, scripts=_record_scripts([_squat_extraction()])) as agent:
        day_plan_id, day_session = agent.seed_plan_session(
            version=1, status="active", scheduled_on=DAY
        )
        other_plan_id, other_session = agent.seed_plan_session(
            version=2, status="archived", scheduled_on=OTHER_DAY
        )

        waiting = _waiting_payload(agent.run("记录今天杠铃背蹲60kg5次"))

        assert waiting["workout"]["performed_on"] == DAY.isoformat()
        assert waiting["candidate_plan_sessions"] == [
            {
                "id": day_session,
                "plan_id": day_plan_id,
                "scheduled_on": DAY.isoformat(),
            }
        ]

        edited = {
            "conversation_id": CONVERSATION_ID,
            **waiting["workout"],
            "performed_on": OTHER_DAY.isoformat(),
            "sets": [_squat_extraction(weight_kg=70.0, reps=3)],
        }
        response = agent.confirm_workout(edited)

        assert response.status_code == 200, response.text
        written = response.json()["workout_session"]
        assert written["performed_on"] == OTHER_DAY.isoformat()
        assert written["plan_session_id"] == other_session
        assert written["sets"] == [
            {
                "exercise_id": SQUAT,
                "set_no": 1,
                "set_type": "work",
                "load_convention": SQUAT_CONVENTION,
                "weight_kg": 70.0,
                "reps": 3,
                "duration_seconds": None,
            }
        ]
        assert agent.records() == [written]
        # 修改后的完整载荷是唯一写入源：原始值与原日期都不在落库事实里。
        assert other_plan_id != day_plan_id
        assert written["sets"] != waiting["workout"]["sets"]
        assert DAY.isoformat() not in json.dumps(agent.records(), ensure_ascii=False)


def test_confirm_workout_rejects_a_session_already_linked_by_another_workout(
    tmp_path: Path,
) -> None:
    """§4.2：同一日程最多被一次有效训练关联；第二个显式关联返回 409 且不写第二行。

    正本：``domain/records/service.py::_require_linkable_in_transaction``
    （``PlanSessionLinkUnavailable`` → ``api/dto.py`` 的 409 映射）。
    """
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        _, session_id = client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )
        first = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body([_squat_extraction()], plan_session_id=session_id),
        )
        assert first.status_code == 200, first.text
        counts_after_first = client.portal.call(_row_counts, db)

        second = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body(
                [_squat_extraction(weight_kg=65.0)], plan_session_id=session_id
            ),
        )

        assert second.status_code == 409, second.text
        body = second.json()
        assert body["http_status"] == 409 and body["error_code"] == "invalid_request"
        assert "已被其他训练关联" in body["message"]
        assert len(client.get("/api/records").json()["records"]) == 1
        assert client.portal.call(_row_counts, db) == counts_after_first


#: 用户可修改后的确认载荷里非法的情况：形状层（400）与领域层（422）都不写库。
INVALID_CONFIRM_PAYLOADS: tuple[tuple[str, dict[str, Any], int], ...] = (
    (
        "修改后的次数越界",
        _confirm_body([_squat_extraction(reps=0)]),
        422,
    ),
    (
        "修改后的动作不在目录内",
        _confirm_body([_extracted_set("not-in-catalog", reps=5)]),
        422,
    ),
    (
        "修改后的负重口径不符",
        _confirm_body(
            [
                _extracted_set(
                    SQUAT,
                    reps=5,
                    load_convention="external_added_weight",
                    weight_kg=60.0,
                )
            ]
        ),
        422,
    ),
    ("修改后没有任何组", _confirm_body([]), 422),
    (
        "缺少组序号",
        _confirm_body(
            [
                {
                    "exercise_id": SQUAT,
                    "set_type": "work",
                    "reps": 5,
                    "load_convention": SQUAT_CONVENTION,
                    "weight_kg": 60.0,
                }
            ]
        ),
        400,
    ),
    (
        "多出未声明字段",
        {**_confirm_body([_squat_extraction()]), "draft_plan_id": 3},
        400,
    ),
    (
        "日期不是自然日",
        {
            **_confirm_body([_squat_extraction()]),
            "performed_on": "0601-2026",
        },
        400,
    ),
)


@pytest.mark.parametrize("case, body, expected_status", INVALID_CONFIRM_PAYLOADS)
def test_confirm_workout_revalidates_the_edited_payload_and_writes_nothing(
    tmp_path: Path, case: str, body: dict[str, Any], expected_status: int
) -> None:
    """§2.2／§2.4.2：确认端点对修改后的完整载荷重新执行 DTO 与领域校验，未通过即不写库。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        _, session_id = client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )
        counts_before = client.portal.call(_row_counts, db)
        body = {**body, "plan_session_id": session_id, "auto_link": False}

        response = client.post("/api/agent/confirm-workout", json=body)

        assert response.status_code == expected_status, response.text
        assert response.json()["error_code"] == "invalid_request", case
        assert client.get("/api/records").json()["records"] == [], case
        assert client.portal.call(_row_counts, db) == counts_before, case


@pytest.mark.parametrize("conversation_id", ["not-a-uuid", "", 7])
def test_confirm_workout_requires_a_uuid_conversation_id_before_processing(
    tmp_path: Path, conversation_id: Any
) -> None:
    """§2.4.2：``conversation_id`` 必须为 UUID，非法形状在处理前返回既有 JSON 错误形状。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        counts_before = client.portal.call(_row_counts, db)

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body(
                [_squat_extraction()], conversation_id=conversation_id
            ),
        )

        assert response.status_code == 400, response.text
        body = response.json()
        assert body["http_status"] == 400 and body["error_code"] == "invalid_request"
        assert client.portal.call(_row_counts, db) == counts_before


def test_confirm_workout_needs_no_prior_natural_language_parse_in_that_session(
    tmp_path: Path,
) -> None:
    """§2.4.2：本端点不要求服务端证明该 ``conversation_id`` 此前完成过一次自然语言解析。

    这个会话从未跑过 ``/api/agent/run``（没有 checkpoint、没有解析事实）；载荷完全由客户端给出，
    端点照样写入，且确认请求内一次模型调用都没有（模型替身没有脚本，被调即失败）。
    """
    with _scripted_app(tmp_path) as agent:
        _, session_id = agent.seed_plan_session(
            version=1, status="active", scheduled_on=DAY
        )

        response = agent.confirm_workout(
            _confirm_body(
                [_squat_extraction()], plan_session_id=session_id, auto_link=False
            )
        )

        assert response.status_code == 200, response.text
        assert response.json()["workout_session"]["plan_session_id"] == session_id
        assert agent.model.calls == []


def test_confirm_workout_never_changes_plan_state(tmp_path: Path) -> None:
    """§2.3／§4.3：打卡确认是独立链路——不改变任何 ``plans`` 状态，也不改写既有的 ``plan_sessions``。"""
    with _app_client(tmp_path) as client:
        db: Database = client.app.state.db
        plan_id, session_id = client.portal.call(
            partial(
                _seed_plan_session, version=1, status="active", scheduled_on=DAY
            ),
            db,
        )
        plans_before = client.portal.call(_plan_rows, db)
        sessions_before = client.portal.call(_plan_session_rows, db)

        response = client.post(
            "/api/agent/confirm-workout",
            json=_confirm_body(
                [_squat_extraction()], plan_session_id=session_id, auto_link=False
            ),
        )

        assert response.status_code == 200, response.text
        assert client.portal.call(_plan_rows, db) == plans_before
        assert client.portal.call(_plan_session_rows, db) == sessions_before
        assert plans_before[0]["id"] == plan_id and plans_before[0]["status"] == "active"


def test_natural_language_path_never_echoes_provider_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.4.2／§2.4.3：响应、SSE 与错误详情都不回显 API Key、Base URL、模型名或环境变量名。"""
    api_key = "stage6-api-key-must-not-leak"
    base_url = "https://stage6-provider.invalid/v1"
    model_name = "stage6-model-must-not-leak"
    monkeypatch.setenv(MODEL_API_KEY_ENV, api_key)
    monkeypatch.setenv(MODEL_BASE_URL_ENV, base_url)
    monkeypatch.setenv(MODEL_MODEL_ENV, model_name)

    with _scripted_app(tmp_path, scripts=_record_scripts([_squat_extraction()])) as agent:
        run_response = agent.client.post(
            "/api/agent/run",
            json={
                "conversation_id": CONVERSATION_ID,
                "request": "记录今天杠铃背蹲60kg5次",
            },
        )
        failure = agent.confirm_workout(
            _confirm_body([_extracted_set("not-in-catalog", reps=5)])
        )

    assert run_response.status_code == 200, run_response.text
    assert failure.status_code == 422, failure.text
    for forbidden in (
        api_key,
        base_url,
        model_name,
        MODEL_API_KEY_ENV,
        MODEL_BASE_URL_ENV,
        MODEL_MODEL_ENV,
    ):
        assert forbidden not in run_response.text, forbidden
        assert forbidden not in failure.text, forbidden
