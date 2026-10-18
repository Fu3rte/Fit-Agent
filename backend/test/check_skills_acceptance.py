import argparse
import asyncio
import hashlib
import json
import re
import shutil
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from functools import partial
from pathlib import Path
from time import time_ns
from uuid import uuid4

from app.agent.skills import SKILLS_ROOT
from app.application.business.profile import business_date
from app.domain.business.models import (
    PlanContent,
    PlanRecord,
    ProfileContent,
    WorkoutContent,
    WorkoutRecord,
)
from app.infrastructure.persistence.sqlite import database as database_module
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.interfaces import http as interface
from app.interfaces.http_api.routes import agent as agent_routes
from app.model_config import ModelConfigError, ModelNotConfigured, load_model_config
from test.regression_support import (
    Server,
    client,
    temporary_root,
)

EVIDENCE_ROOT = "skills-acceptance"
SAVING_TOOLS = {"save_plan", "save_profile_update", "save_workout", "update_workout"}
PREPARATION_TOOLS = {
    "prepare_profile_update",
    "prepare_workout",
    "prepare_plan",
    "prepare_plan_import",
    "prepare_plan_adjustment",
}
BANNED_USER_TERMS = (
    "ATP",
    "磷酸肌酸",
    "无机磷酸盐",
    "钙离子",
    "糖酵解",
    "氧化磷酸化",
    "交叉桥",
    "兴奋—收缩耦联",
    "代谢产物",
)
READ_CLAIM_MARKERS = ("已成功读取", "读取成功", "已读取到", "成功获取到")
READ_FAILURE_MARKERS = ("失败", "无法读取", "不存在", "读取不了", "报错")

PROMPT_C1 = "练完第二天酸，是不是乳酸没排掉？"
PROMPT_C2 = "是不是每组都要做到做不动才有效？"
PROMPT_C3 = "我想提高一组能做的次数，怎么练？"

PROMPT_D1 = (
    "请先读取 skills/workout-planning/SKILL.md，并按照该文件里指向 "
    "../training-theory/references/methodology.md 的相对引用，实际读取该引用文件。"
    "传给 read 的路径按规则归一化到 skills/ 命名空间。"
    "最后只列出该文件「六步推理协议」的第 1 步和第 2 步标题。"
)
PROMPT_D2 = (
    "请用 read 读取 skills/training-theory/references/training-variables.md，"
    "第一次使用 offset=1、limit=10。如果返回提示仍有后续内容，就按提示继续读取，"
    "直到读完整份文件。最后原样给出该文件的最后一行。"
)
PROMPT_D3 = (
    "请用 read 读取 skills/training-theory/references/does-not-exist.md，"
    "并原样报告工具返回结果。不要改用其他路径，也不要声称已经读取。"
)

PROMPT_E_PLAN = (
    "我想练力量，每周去三次健身房，每次大概四十五分钟，目标是增肌和提升一般力量。"
    "请给我一套能直接执行的训练安排，具体到每个训练日做什么动作、几组、每组几次。"
    "先不要保存，把完整安排展示给我核对。"
)
PROMPT_E_PLAN_CONFIRM = "这份安排可以，确认保存。"
PROMPT_E_ADJUST = (
    "我想把当前计划里第 1 天（推日）第一个动作「杠铃卧推」从 4 组改成 5 组，"
    "其他动作、重量、休息和训练日全部保持不变。请先核对我的画像和当前计划，"
    "再结合我最近的训练记录说明这次调整的依据，把调整后的完整计划展示给我核对，先不要保存。"
)
PROMPT_E_WORKOUT = (
    "今天实际练了杠铃卧推三组：第一组 8 次 60 公斤，第二组 8 次 62.5 公斤，"
    "第三组 6 次 65 公斤。没有其他不适或感受。请查询今天的记录，"
    "把完整训练记录展示给我核对，先不要保存。"
)
PROMPT_E_WORKOUT_CONFIRM = "确认保存这份训练记录。"

SEEDED_PROFILE = {
    "goal": "增肌与一般力量提升",
    "experience": "力量训练新手，系统训练约三个月",
    "environment": "普通商业健身房",
    "availability": "每周三次，每次约四十五分钟",
    "health_notes": "无伤病或不适",
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


def now_ms() -> int:
    return time_ns() // 1_000_000


def skill_path_is_normalized(path) -> bool:
    if not isinstance(path, str):
        return False
    normalized = path.replace("\\", "/")
    if not normalized.startswith("skills/"):
        return False
    return all(part != ".." for part in normalized.split("/"))


def continuation_offset(text) -> int | None:
    if not isinstance(text, str):
        return None
    match = re.search(r"继续读取请使用 offset=(\d+)", text)
    return int(match.group(1)) if match else None


def read_claim(answer: str) -> bool:
    return any(marker in answer for marker in READ_CLAIM_MARKERS)


def read_failure_stated(answer: str) -> bool:
    return any(marker in answer for marker in READ_FAILURE_MARKERS)


def banned_user_terms(text: str) -> list[str]:
    return [term for term in BANNED_USER_TERMS if term in text]


NUMERIC_DOSE_PATTERN = re.compile(
    r"\d+(?:\s*[-–~至]\s*\d+)?\s*(?:组|次|公斤|kg)"
)
NUMERIC_CONTEXT_MARKERS = (
    "建议",
    "每周",
    "每次",
    "每组",
    "每天",
    "保留",
    "留",
    "至少",
    "最多",
    "控制在",
    "先加",
    "再加",
    "加到",
    "做到",
)
NEGATION_MARKERS = (
    "不以此",
    "不据此",
    "不单独",
    "不参与判定",
    "不作为判定",
    "不能单独",
    "不用来判定",
    "不会用",
    "不用这个",
    "而不是",
)
LOAD_MARKERS = ("负荷", "训练量", "练多少", "训练安排", "该练多重")
SIGNAL_MARKERS = (
    "疲劳",
    "力竭",
    "当次状态",
    "单次",
    "酸痛",
    "信号",
    "RIR",
    "RPE",
)
JUDGMENT_MARKERS = ("决定", "调整", "判定", "判断", "确定")


def numeric_prescription_terms(text: str) -> list[str]:
    terms: list[str] = []
    for sentence in re.split(r"[。；\n]", text):
        if any(marker in sentence for marker in NUMERIC_CONTEXT_MARKERS):
            terms.extend(
                match.group(0) for match in NUMERIC_DOSE_PATTERN.finditer(sentence)
            )
    return terms


def system_load_boundary(answer: str) -> bool:
    for sentence in re.split(r"[。！？\n]", answer):
        if not any(marker in sentence for marker in NEGATION_MARKERS):
            continue
        if any(marker in sentence for marker in LOAD_MARKERS):
            return True
        if any(marker in sentence for marker in SIGNAL_MARKERS) and any(
            marker in sentence for marker in JUDGMENT_MARKERS
        ):
            return True
    return False


def endpoint_defined(answer: str) -> bool:
    return any(
        marker in answer
        for marker in ("终点", "可测", "同一标准下", "固定测试条件", "重复测到")
    )


def uncertainty_stated(answer: str) -> bool:
    return any(
        marker in answer
        for marker in (
            "不确定",
            "不能确认",
            "说不清",
            "还缺",
            "无法确认",
            "仍未知",
            "不能保证",
            "无法保证",
            "尚未",
        )
    )


def last_file_line(path: Path) -> str:
    lines = [line.rstrip() for line in Path(path).read_text(encoding="utf-8-sig").splitlines()]
    return next(line for line in reversed(lines) if line)


def tool_records(wire: list[dict]) -> list[dict]:
    starts: dict[str, dict] = {}
    endings: dict[str, dict] = {}
    for event in wire:
        data = event["data"]
        call_id = data.get("tool_call_id")
        if call_id is None:
            continue
        if event["event"] == "tool_start":
            starts[call_id] = data
        elif event["event"] == "tool_execution_end":
            endings[call_id] = data
    return [
        {
            "call_id": call_id,
            "name": start.get("name"),
            "arguments": start.get("arguments"),
            "is_error": endings.get(call_id, {}).get("is_error"),
            "content": endings.get(call_id, {}).get("content"),
        }
        for call_id, start in starts.items()
    ]


def read_records(tools: list[dict]) -> list[dict]:
    records = []
    for tool in tools:
        if tool["name"] != "read":
            continue
        arguments = tool.get("arguments") or {}
        content = tool.get("content") or ""
        records.append(
            {
                "path": arguments.get("path"),
                "offset": arguments.get("offset", 1),
                "limit": arguments.get("limit", 200),
                "is_error": tool.get("is_error"),
                "continuation_offset": continuation_offset(content),
                "content_head": content[:600],
                "content_tail": content[-400:],
            }
        )
    return records


def successful(tools: list[dict], name: str) -> list[dict]:
    return [tool for tool in tools if tool["name"] == name and not tool["is_error"]]


def check(evidence: dict, name: str, passed, detail=None) -> None:
    evidence["checks"].append(
        {"name": name, "passed": bool(passed), "detail": detail}
    )


def turn_evidence(request: str, result: dict) -> dict:
    return {
        "input": request,
        "operation_id": result["operation_id"],
        "run_id": result["run_id"],
        "request_entry_id": result["request_entry_id"],
        "tool_calls": [
            {**tool, "content": (tool.get("content") or "")[:20000]}
            for tool in result["tools"]
        ],
        "read_calls": read_records(result["tools"]),
        "final_answer": result["answer"],
    }


def run_turn(http, session_id: str, request: str) -> dict:
    from test.check_http import events, final_text, validate_events, wait_idle

    operation_id = str(uuid4())
    body = {"session_id": session_id, "operation_id": operation_id, "request": request}
    with http.stream("POST", "/api/agent/run", json=body) as response:
        if response.status_code != 200:
            response.read()
            raise AssertionError(f"运行请求失败 {response.status_code}: {response.text}")
        run_id = response.headers["X-Run-ID"]
        request_entry_id = response.headers["X-Request-Entry-ID"]
        wire = list(events(response))
    wait_idle()
    try:
        validate_events(wire)
    except AssertionError as error:
        observed = sorted(
            {
                str(event["data"].get("run_id"))
                for event in wire
                if event["data"].get("run_id")
            }
        )
        raise AssertionError(
            f"SSE 事件 run_id 不一致：{observed}；X-Run-ID={run_id}"
        ) from error
    if wire[-1]["event"] != "done" or wire[-1]["data"]["status"] != "completed":
        raise AssertionError(f"运行未成功完成：{wire[-1]}")
    return {
        "operation_id": operation_id,
        "run_id": run_id,
        "request_entry_id": request_entry_id,
        "tools": tool_records(wire),
        "answer": final_text(wire),
    }


def create_session(http, title: str) -> str:
    session_id = str(uuid4())
    response = http.post(
        "/api/sessions", json={"session_id": session_id, "title": title}
    )
    assert response.status_code == 201, response.text
    return session_id


def history(http, session_id: str) -> dict:
    response = http.get(f"/api/sessions/{session_id}/history")
    assert response.status_code == 200, response.text
    return response.json()


def find_pending(http, session_id: str, business_kind: str) -> dict | None:
    items = [
        item
        for item in history(http, session_id)["pending_proposals"]
        if item["business_kind"] == business_kind
        and item.get("status", "pending") == "pending"
    ]
    return items[0] if len(items) == 1 else None


def confirmation_body(kind: str, target: dict) -> dict:
    return {
        "operation_id": str(uuid4()),
        "business_kind": kind,
        "proposal_id": target["proposal_id"],
        "display_entry_id": target["display_entry_id"],
    }


def confirm(http, session_id: str, kind: str, target: dict) -> dict:
    response = http.post(
        f"/api/sessions/{session_id}/confirmations",
        json=confirmation_body(kind, target),
    )
    assert response.status_code == 200, response.text
    return response.json()


def current_plan(http) -> dict:
    response = http.get("/api/plans/current")
    assert response.status_code == 200, response.text
    return response.json()


def list_plans(http) -> list:
    response = http.get("/api/plans")
    assert response.status_code == 200, response.text
    return response.json()


def list_workouts(http, query: str = "page_size=100") -> dict:
    response = http.get(f"/api/workouts?{query}")
    assert response.status_code == 200, response.text
    return response.json()


def profile(http) -> dict:
    response = http.get("/api/profile")
    assert response.status_code == 200, response.text
    return response.json()


def seed_database(
    path: Path,
    *,
    with_profile: bool = False,
    with_plan: bool = False,
    with_workouts: bool = False,
) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = date.fromisoformat(business_date(now_ms()))
    plan = seeded_plan() if with_plan else None
    workouts = seeded_workouts(today) if with_workouts else []

    async def build() -> None:
        database = await open_database(path)
        try:
            repository = SqliteBusinessRepository(database)
            async with repository.transaction():
                if with_profile:
                    await repository.save_profile(
                        ProfileContent.model_validate(SEEDED_PROFILE), 1, now_ms()
                    )
                if plan is not None:
                    await repository.insert_plan(
                        PlanRecord(
                            id=plan["id"],
                            is_current=True,
                            content=PlanContent.model_validate(plan["content"]),
                            created_at=now_ms(),
                        )
                    )
            for item in workouts:
                async with repository.transaction():
                    await repository.insert_workout(
                        WorkoutRecord(
                            id=str(uuid4()),
                            performed_on=item["performed_on"],
                            version=1,
                            content=WorkoutContent.model_validate(item["content"]),
                            created_at=now_ms(),
                            updated_at=now_ms(),
                        )
                    )
        finally:
            await database.close()

    asyncio.run(build())
    return {
        "business_date": today.isoformat(),
        "plan_id": None if plan is None else plan["id"],
        "workout_dates": [item["performed_on"] for item in workouts],
    }


def seeded_plan() -> dict:
    return {
        "id": str(uuid4()),
        "content": {
            "repeat": True,
            "days": [
                {
                    "kind": "training",
                    "focus": "推（胸、肩、三头）",
                    "exercises": [
                        {
                            "exercise_id": "0025",
                            "name": "杠铃卧推",
                            "sets": 4,
                            "reps": 8,
                            "duration_seconds": None,
                            "weight_kg": 60.0,
                            "load_convention": "barbell_total",
                            "rest_seconds": 150.0,
                        },
                        {
                            "exercise_id": "0047",
                            "name": "杠铃上斜卧推",
                            "sets": 3,
                            "reps": 10,
                            "duration_seconds": None,
                            "weight_kg": 40.0,
                            "load_convention": "barbell_total",
                            "rest_seconds": 120.0,
                        },
                    ],
                    "notes": "用户提供的现状：胸部训练日。",
                },
                {
                    "kind": "training",
                    "focus": "拉（背、二头）",
                    "exercises": [
                        {
                            "exercise_id": "0027",
                            "name": "杠铃俯身划船",
                            "sets": 4,
                            "reps": 8,
                            "duration_seconds": None,
                            "weight_kg": 50.0,
                            "load_convention": "barbell_total",
                            "rest_seconds": 120.0,
                        },
                        {
                            "exercise_id": "0007",
                            "name": "交替侧向下拉",
                            "sets": 3,
                            "reps": 12,
                            "duration_seconds": None,
                            "weight_kg": None,
                            "load_convention": None,
                            "rest_seconds": 90.0,
                        },
                    ],
                    "notes": None,
                },
                {
                    "kind": "training",
                    "focus": "腿",
                    "exercises": [
                        {
                            "exercise_id": "1436",
                            "name": "杠铃高杠深蹲",
                            "sets": 4,
                            "reps": 6,
                            "duration_seconds": None,
                            "weight_kg": 80.0,
                            "load_convention": "barbell_total",
                            "rest_seconds": 180.0,
                        }
                    ],
                    "notes": "用户提供的现状：腿部训练日，其余动作参数未知。",
                },
                {"kind": "rest", "focus": None, "exercises": [], "notes": None},
            ],
            "notes": "用户提供的推、拉、腿、休四日循环。部分动作参数未知。",
            "suggested_fields": [
                "/days/1/exercises/0/rest_seconds",
                "/days/2/exercises/0/reps",
            ],
        },
    }


def seeded_workouts(today: date) -> list[dict]:
    templates = (
        ("0025", "杠铃卧推", "barbell_total", [(8, 60.0), (8, 62.5), (6, 65.0)]),
        ("1436", "杠铃高杠深蹲", "barbell_total", [(6, 80.0), (6, 85.0), (5, 90.0)]),
        ("0027", "杠铃俯身划船", "barbell_total", [(10, 45.0), (10, 47.5)]),
    )
    records = []
    for offset in range(12):
        performed_on = (today - timedelta(days=offset)).isoformat()
        exercise_id, name, convention, sets = templates[offset % len(templates)]
        records.append(
            {
                "performed_on": performed_on,
                "content": {
                    "exercises": [
                        {
                            "exercise_id": exercise_id,
                            "name": name,
                            "load_convention": convention,
                            "sets": [
                                {
                                    "reps": reps,
                                    "weight_kg": weight,
                                    "duration_seconds": None,
                                }
                                for reps, weight in sets
                            ],
                        }
                    ],
                    "notes": f"验收种子记录，训练日 {performed_on}。",
                },
            }
        )
    return records


def plan_payload_issues(payload: dict) -> list[str]:
    issues = []
    days = payload.get("days")
    if not isinstance(days, list) or not days:
        return ["days 必须为非空数组"]
    training_days = [day for day in days if day.get("kind") == "training"]
    if not training_days:
        issues.append("缺少训练日")
    for index, day in enumerate(days):
        exercises = day.get("exercises")
        if day.get("kind") == "rest":
            if exercises != []:
                issues.append(f"休息日 {index} 的动作列表必须为空")
            continue
        if not exercises:
            issues.append(f"训练日 {index} 缺少动作")
            continue
        for position, exercise in enumerate(exercises):
            sets = exercise.get("sets")
            if not isinstance(sets, int) or isinstance(sets, bool) or sets < 1:
                issues.append(f"训练日 {index} 动作 {position} 的 sets 必须为正整数")
            if exercise.get("reps") is None and exercise.get("duration_seconds") is None:
                issues.append(f"训练日 {index} 动作 {position} 缺少 reps 或 duration_seconds")
            if exercise.get("weight_kg") is not None and not exercise.get("load_convention"):
                issues.append(f"训练日 {index} 动作 {position} 有重量但缺少口径")
    return issues


def workout_sets_match(exercise: dict, expected: list[tuple]) -> bool:
    actual = [
        (item.get("reps"), item.get("weight_kg"))
        for item in (exercise.get("sets") or [])
    ]
    return actual == [(reps, float(weight)) for reps, weight in expected]


def plan_notes(payload: dict) -> str:
    parts = [payload.get("notes") or ""]
    parts.extend(day.get("notes") or "" for day in payload.get("days") or [])
    return "\n".join(parts)


def reference_reads(records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if record["is_error"] is False
        and re.fullmatch(r"skills/[^/]+/references/[^/]+\.md", record["path"] or "")
    ]


def entry_reads(records: list[dict]) -> list[dict]:
    return [
        record
        for record in records
        if record["is_error"] is False
        and re.fullmatch(r"skills/[^/]+/SKILL\.md", record["path"] or "")
    ]


def seeded_plan_matches(actual: dict, seeded: dict) -> list[str]:
    problems = []
    actual_days = actual.get("days") or []
    seeded_days = seeded.get("days") or []
    if len(actual_days) != len(seeded_days):
        return [f"训练日数量 {len(actual_days)} 与种子 {len(seeded_days)} 不一致"]
    for index, (day, source) in enumerate(zip(actual_days, seeded_days)):
        if day.get("kind") != source["kind"]:
            problems.append(f"第 {index + 1} 天 kind 变化")
        for position, (exercise, original) in enumerate(
            zip(day.get("exercises") or [], source["exercises"])
        ):
            for field in (
                "exercise_id",
                "name",
                "reps",
                "duration_seconds",
                "weight_kg",
                "load_convention",
                "rest_seconds",
            ):
                if exercise.get(field) != original.get(field):
                    problems.append(f"第 {index + 1} 天动作 {position + 1} 的 {field} 变化")
        if len(day.get("exercises") or []) != len(source["exercises"]):
            problems.append(f"第 {index + 1} 天动作数量变化")
    return problems


def scenario_c1(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-C1")
    evidence["session_id"] = session_id
    result = run_turn(http, session_id, PROMPT_C1)
    evidence["turns"].append(turn_evidence(PROMPT_C1, result))
    answer = result["answer"]
    tools = result["tools"]
    reads = read_records(tools)
    entries = entry_reads(reads)
    references = reference_reads(reads)

    check(evidence, "读取相关 SKILL.md", bool(entries), [item["path"] for item in entries])
    check(evidence, "按需读取 references", bool(references), [item["path"] for item in references])
    check(evidence, "回答非空", bool(answer.strip()), answer[:200])
    terms = banned_user_terms(answer)
    check(evidence, "无生物学禁用词", not terms, terms)
    check(
        evidence,
        "说明证据强度",
        any(marker in answer for marker in ("有研究支持", "经验推断", "观察指标", "证据")),
        answer,
    )
    check(
        evidence,
        "未产生业务准备或保存",
        not [tool for tool in tools if tool["name"] in PREPARATION_TOOLS | SAVING_TOOLS],
        [tool["name"] for tool in tools],
    )
    evidence["observations"] = {
        "answer": answer,
        "read_paths": [item["path"] for item in reads],
        "business_tools": [tool["name"] for tool in tools if tool["name"] not in {"read", "grep", "find", "ls"}],
    }


def scenario_c2(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-C2")
    evidence["session_id"] = session_id
    result = run_turn(http, session_id, PROMPT_C2)
    evidence["turns"].append(turn_evidence(PROMPT_C2, result))
    answer = result["answer"]
    tools = result["tools"]
    reads = read_records(tools)
    entries = entry_reads(reads)
    references = reference_reads(reads)

    check(evidence, "读取相关 SKILL.md", bool(entries), [item["path"] for item in entries])
    check(evidence, "按需读取 references", bool(references), [item["path"] for item in references])
    check(
        evidence,
        "说明力竭并非必要条件",
        ("力竭" in answer or "做不动" in answer)
        and any(
            marker in answer
            for marker in (
                "不是每组",
                "不是必须",
                "不是必要",
                "并非必要",
                "不必要",
                "不需要",
                "不必",
                "不会额外",
                "并不额外",
            )
        ),
        answer,
    )
    check(
        evidence,
        "说明力竭的代价",
        any(marker in answer for marker in ("代价", "恢复", "疲劳", "更慢")),
        answer,
    )
    check(evidence, "说明系统不以此单独判定负荷", system_load_boundary(answer), answer)
    terms = numeric_prescription_terms(answer)
    check(evidence, "未输出数值处方", not terms, terms)
    check(
        evidence,
        "未产生业务准备或保存",
        not [tool for tool in tools if tool["name"] in PREPARATION_TOOLS | SAVING_TOOLS],
        [tool["name"] for tool in tools],
    )
    evidence["observations"] = {
        "answer": answer,
        "read_paths": [item["path"] for item in reads],
        "numeric_terms": terms,
    }


def scenario_c3(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-C3")
    evidence["session_id"] = session_id
    result = run_turn(http, session_id, PROMPT_C3)
    evidence["turns"].append(turn_evidence(PROMPT_C3, result))
    answer = result["answer"]
    tools = result["tools"]
    reads = read_records(tools)
    entries = entry_reads(reads)
    references = reference_reads(reads)

    check(evidence, "读取相关 SKILL.md", bool(entries), [item["path"] for item in entries])
    check(evidence, "按需读取 references", bool(references), [item["path"] for item in references])
    check(evidence, "给出终点定义", endpoint_defined(answer), answer)
    check(
        evidence,
        "给出单变量对照思路",
        any(
            marker in answer
            for marker in ("一次只改", "单变量", "对照", "只改一个", "只变一件事")
        ),
        answer,
    )
    check(evidence, "说明不确定性", uncertainty_stated(answer), answer)
    promises = [phrase for phrase in ("保证提高", "一定会提高", "必然提高", "肯定能提高") if phrase in answer]
    check(evidence, "未承诺必然提高", not promises, promises)
    terms = numeric_prescription_terms(answer)
    check(evidence, "未输出数值处方", not terms, terms)
    check(
        evidence,
        "未产生业务准备或保存",
        not [tool for tool in tools if tool["name"] in PREPARATION_TOOLS | SAVING_TOOLS],
        [tool["name"] for tool in tools],
    )
    evidence["observations"] = {
        "answer": answer,
        "read_paths": [item["path"] for item in reads],
        "numeric_terms": terms,
    }


def scenario_d1(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-D1")
    evidence["session_id"] = session_id
    result = run_turn(http, session_id, PROMPT_D1)
    evidence["turns"].append(turn_evidence(PROMPT_D1, result))
    tools = result["tools"]
    reads = read_records(tools)
    paths = [item["path"] for item in reads]

    check(
        evidence,
        "读取 workout-planning SKILL.md",
        any(
            item["is_error"] is False
            and item["path"] == "skills/workout-planning/SKILL.md"
            for item in reads
        ),
        paths,
    )
    normalized = [
        item
        for item in reads
        if item["is_error"] is False
        and item["path"] == "skills/training-theory/references/methodology.md"
    ]
    check(evidence, "跨 Skill 相对引用归一化后读取成功", bool(normalized), paths)
    check(evidence, "read 路径不含未归一化的 ..", all(skill_path_is_normalized(path) for path in paths), paths)
    content = "\n".join(item.get("content_head") or "" for item in normalized)
    check(evidence, "获得目标文件内容", "六步推理协议" in content, content[:300])
    check(
        evidence,
        "回答包含六步推理前两步标题",
        "定终点" in result["answer"] and "列候选原因" in result["answer"],
        result["answer"],
    )
    evidence["observations"] = {
        "read_paths": paths,
        "answer": result["answer"],
    }


def scenario_d2(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-D2")
    evidence["session_id"] = session_id
    result = run_turn(http, session_id, PROMPT_D2)
    evidence["turns"].append(turn_evidence(PROMPT_D2, result))
    tools = result["tools"]
    reads = [
        item
        for item in read_records(tools)
        if item["path"] == "skills/training-theory/references/training-variables.md"
    ]
    offsets = [item["offset"] for item in reads]
    hints = [item["continuation_offset"] for item in reads if item["continuation_offset"]]
    expected_last = last_file_line(
        SKILLS_ROOT / "training-theory" / "references" / "training-variables.md"
    )

    check(evidence, "同一路径发生续读", len(reads) >= 2, reads)
    check(
        evidence,
        "续读偏移递增并遵循提示",
        len(offsets) >= 2
        and offsets == sorted(offsets)
        and len(set(offsets)) == len(offsets)
        and any(hint in offsets[1:] for hint in hints),
        {"offsets": offsets, "hints": hints},
    )
    check(
        evidence,
        "首次读取出现续读提示",
        any(item["continuation_offset"] for item in reads),
        [item["continuation_offset"] for item in reads],
    )
    check(
        evidence,
        "读到真实文件末行",
        bool(reads) and expected_last in (reads[-1].get("content_tail") or ""),
        {"expected_last": expected_last, "tail": reads[-1].get("content_tail") if reads else None},
    )
    check(
        evidence,
        "回答使用续读内容",
        expected_last.strip() in result["answer"]
        or "周期化不是必需品" in result["answer"]
        or "三条使用规则" in result["answer"],
        result["answer"],
    )
    evidence["observations"] = {
        "offsets": offsets,
        "hints": hints,
        "expected_last_line": expected_last,
        "answer": result["answer"],
    }


def scenario_d3(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-D3")
    evidence["session_id"] = session_id
    result = run_turn(http, session_id, PROMPT_D3)
    evidence["turns"].append(turn_evidence(PROMPT_D3, result))
    tools = result["tools"]
    reads = [
        item
        for item in read_records(tools)
        if "does-not-exist.md" in (item["path"] or "")
    ]
    answer = result["answer"]

    check(evidence, "对不存在路径发起读取", bool(reads), [item["path"] for item in reads])
    check(evidence, "读取失败状态真实", bool(reads) and all(item["is_error"] is True for item in reads), reads)
    check(evidence, "如实说明读取失败", read_failure_stated(answer), answer)
    check(evidence, "未宣称读取成功", not read_claim(answer), answer)
    check(
        evidence,
        "未用其他读取掩盖失败",
        not [
            item
            for item in read_records(tools)
            if item["is_error"] is False and "does-not-exist.md" in (item["path"] or "")
        ],
        [item["path"] for item in read_records(tools)],
    )
    evidence["observations"] = {
        "read_calls": reads,
        "answer": answer,
    }


def scenario_e_plan(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-计划生成")
    evidence["session_id"] = session_id
    before_plans = list_plans(http)
    before_current = current_plan(http)
    check(evidence, "初始无已保存计划", before_plans == [] and before_current == {"id": None, "content": None}, before_plans)

    result = run_turn(http, session_id, PROMPT_E_PLAN)
    evidence["turns"].append(turn_evidence(PROMPT_E_PLAN, result))
    tools = result["tools"]
    reads = read_records(tools)
    prepared = successful(tools, "prepare_plan")

    check(evidence, "读取画像", bool(successful(tools, "get_profile")), [tool["name"] for tool in tools])
    check(
        evidence,
        "读取 workout-planning SKILL.md",
        any(
            item["is_error"] is False
            and item["path"] == "skills/workout-planning/SKILL.md"
            for item in reads
        ),
        [item["path"] for item in reads],
    )
    check(evidence, "prepare_plan 成功生成提案", len(prepared) == 1, [tool["name"] for tool in tools])
    check(
        evidence,
        "未改用录入或调整工具",
        not successful(tools, "prepare_plan_import")
        and not successful(tools, "prepare_plan_adjustment"),
        [tool["name"] for tool in tools],
    )
    payload = json.loads(prepared[0]["content"])["payload"] if prepared else {}
    issues = plan_payload_issues(payload)
    check(evidence, "提案包含具体动作与组次", not issues, issues)
    check(
        evidence,
        "未出现保存工具",
        not [tool for tool in tools if tool["name"] in SAVING_TOOLS],
        [tool["name"] for tool in tools],
    )
    check(
        evidence,
        "确认前未保存计划",
        list_plans(http) == before_plans and current_plan(http) == before_current,
        {"plans": list_plans(http), "current": current_plan(http)},
    )

    confirm_turn = run_turn(http, session_id, PROMPT_E_PLAN_CONFIRM)
    evidence["turns"].append(turn_evidence(PROMPT_E_PLAN_CONFIRM, confirm_turn))
    check(
        evidence,
        "普通文字确认不提交保存",
        list_plans(http) == before_plans and current_plan(http) == before_current,
        {"plans": list_plans(http), "current": current_plan(http)},
    )

    target = find_pending(http, session_id, "plan")
    check(evidence, "存在待确认计划快照", target is not None, None)
    if target is None:
        evidence["observations"] = {
            "payload": payload,
            "answer": result["answer"],
            "confirm_answer": confirm_turn["answer"],
        }
        return
    operation = confirm(http, session_id, "plan", target)
    saved = operation["result"]
    after_plans = list_plans(http)
    confirmation_tools = prepared or successful(confirm_turn["tools"], "prepare_plan")
    confirmation_payload = (
        json.loads(confirmation_tools[0]["content"])["payload"]
        if confirmation_tools
        else {}
    )
    check(evidence, "按钮确认保存成功", operation["status"] == "saved", operation)
    check(
        evidence,
        "保存内容与准备 payload 一致",
        saved["content"] == confirmation_payload
        and saved["proposal_id"] == target["proposal_id"],
        {"saved": saved["content"], "payload": confirmation_payload},
    )
    check(
        evidence,
        "仅新增一个当前版本",
        len(after_plans) == 1
        and after_plans[0]["id"] == saved["id"]
        and after_plans[0]["is_current"] is True
        and current_plan(http) == {"id": saved["id"], "content": saved["content"]},
        after_plans,
    )

    repeat = http.post(
        f"/api/sessions/{session_id}/confirmations",
        json={
            "operation_id": operation["operation_id"],
            "business_kind": "plan",
            "proposal_id": target["proposal_id"],
            "display_entry_id": target["display_entry_id"],
        },
    )
    check(
        evidence,
        "重复确认保持幂等",
        repeat.status_code == 200
        and repeat.json()["result"] == saved
        and len(list_plans(http)) == 1,
        {"status": repeat.status_code, "body": repeat.json() if repeat.status_code == 200 else repeat.text},
    )
    evidence["observations"] = {
        "payload": payload,
        "saved": saved,
        "confirmation_entry_id": operation.get("confirmation_entry_id"),
        "answer": result["answer"],
        "confirm_answer": confirm_turn["answer"],
    }


def scenario_e_adjust(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-计划调整")
    evidence["session_id"] = session_id
    seeded = evidence["seeded"]
    before_plans = list_plans(http)
    before_current = current_plan(http)
    seeded_content = before_current["content"]

    result = run_turn(http, session_id, PROMPT_E_ADJUST)
    evidence["turns"].append(turn_evidence(PROMPT_E_ADJUST, result))
    tools = result["tools"]
    reads = read_records(tools)
    prepared = successful(tools, "prepare_plan_adjustment")

    check(evidence, "读取画像", bool(successful(tools, "get_profile")), [tool["name"] for tool in tools])
    check(evidence, "读取当前计划", bool(successful(tools, "get_current_plan")), [tool["name"] for tool in tools])
    queries = successful(tools, "list_workouts")
    check(evidence, "查询真实训练记录", bool(queries), [tool["name"] for tool in tools])
    latest = json.loads(queries[-1]["content"]) if queries else {}
    check(
        evidence,
        "训练记录覆盖最近 10 条",
        latest.get("total") == 12 and len(latest.get("items") or []) == 10,
        {"total": latest.get("total"), "items": len(latest.get("items") or [])},
    )
    check(
        evidence,
        "读取计划相关 SKILL.md",
        any(
            item["is_error"] is False
            and item["path"] in {
                "skills/workout-planning/SKILL.md",
                "skills/plan-adjustment/SKILL.md",
            }
            for item in reads
        ),
        [item["path"] for item in reads],
    )
    check(evidence, "prepare_plan_adjustment 成功", len(prepared) == 1, [tool["name"] for tool in tools])
    proposal = json.loads(prepared[0]["content"]) if prepared else {}
    payload = proposal.get("payload") or {}
    check(
        evidence,
        "依据使用真实画像版本与当前计划",
        proposal.get("base_profile_version") == 1
        and proposal.get("base_plan_id") == seeded["plan_id"],
        {"base_profile_version": proposal.get("base_profile_version"), "base_plan_id": proposal.get("base_plan_id")},
    )
    modified = (payload.get("days") or [{}])[0].get("exercises") or [{}]
    check(
        evidence,
        "目标动作改为 5 组",
        bool(modified) and modified[0].get("name") == "杠铃卧推" and modified[0].get("sets") == 5,
        modified[0] if modified else None,
    )
    problems = seeded_plan_matches(payload, seeded_content)
    check(evidence, "未涉及字段保持原值", not problems, problems)
    notes = plan_notes(payload)
    dates = seeded["workout_dates"][:10]
    check(
        evidence,
        "说明使用本次真实训练记录",
        any(day in notes for day in dates),
        {"notes": notes, "dates": dates},
    )
    check(
        evidence,
        "确认前未保存计划",
        list_plans(http) == before_plans and current_plan(http) == before_current,
        {"plans": list_plans(http), "current": current_plan(http)},
    )
    check(
        evidence,
        "未出现保存工具",
        not [tool for tool in tools if tool["name"] in SAVING_TOOLS],
        [tool["name"] for tool in tools],
    )

    target = find_pending(http, session_id, "plan")
    check(evidence, "存在待确认调整快照", target is not None, None)
    if target is None:
        evidence["observations"] = {
            "proposal": proposal,
            "answer": result["answer"],
        }
        return
    operation = confirm(http, session_id, "plan", target)
    saved = operation["result"]
    after_plans = list_plans(http)
    check(
        evidence,
        "按钮确认保存调整版本",
        operation["status"] == "saved"
        and saved["content"] == payload
        and len(after_plans) == 2
        and after_plans[0]["id"] == saved["id"]
        and after_plans[0]["is_current"] is True
        and after_plans[1]["id"] == seeded["plan_id"],
        {"saved": saved, "plans": after_plans},
    )
    evidence["observations"] = {
        "proposal": proposal,
        "saved": saved,
        "answer": result["answer"],
        "confirmation_entry_id": operation.get("confirmation_entry_id"),
    }


def scenario_e_workout(http, evidence: dict) -> None:
    session_id = create_session(http, "Skills 验收-训练记录")
    evidence["session_id"] = session_id
    expected_date = business_date(now_ms())
    before = list_workouts(http)
    check(evidence, "初始无训练记录", before["total"] == 0, before)

    result = run_turn(http, session_id, PROMPT_E_WORKOUT)
    evidence["turns"].append(turn_evidence(PROMPT_E_WORKOUT, result))
    tools = result["tools"]
    computed = successful(tools, "calculate_date")
    first_prepared = successful(tools, "prepare_workout")

    check(
        evidence,
        "使用 calculate_date 换算今天",
        len(computed) == 1
        and computed[0]["arguments"] == {"days_offset": 0},
        [tool["arguments"] for tool in computed],
    )
    computed_date = json.loads(computed[0]["content"])["date"] if computed else None
    check(
        evidence,
        "查询当天真实记录",
        any(
            tool["arguments"].get("date_from") == computed_date
            and tool["arguments"].get("date_to") == computed_date
            for tool in successful(tools, "list_workouts")
        ),
        [tool["arguments"] for tool in successful(tools, "list_workouts")],
    )
    check(
        evidence,
        "首轮创建训练记录快照",
        len(first_prepared) == 1,
        [tool["name"] for tool in tools],
    )
    check(
        evidence,
        "确认前未保存训练记录",
        list_workouts(http)["total"] == 0,
        list_workouts(http),
    )

    confirm_turn = run_turn(http, session_id, PROMPT_E_WORKOUT_CONFIRM)
    evidence["turns"].append(turn_evidence(PROMPT_E_WORKOUT_CONFIRM, confirm_turn))
    check(
        evidence,
        "普通文字确认不提交保存",
        list_workouts(http)["total"] == 0,
        list_workouts(http),
    )
    all_tools = tools + confirm_turn["tools"]
    check(
        evidence,
        "未出现保存工具",
        not [tool for tool in all_tools if tool["name"] in SAVING_TOOLS],
        [tool["name"] for tool in all_tools],
    )

    target = find_pending(http, session_id, "workout")
    check(evidence, "存在待确认训练记录快照", target is not None, None)
    if target is None:
        evidence["observations"] = {
            "first_turn_prepared": bool(first_prepared),
            "computed_date": computed_date,
            "expected_date": expected_date,
            "answer": result["answer"],
            "confirm_answer": confirm_turn["answer"],
        }
        return

    second_prepared = successful(confirm_turn["tools"], "prepare_workout")
    all_prepared = first_prepared + second_prepared
    confirmed_tool = next(
        (
            tool
            for tool in all_prepared
            if json.loads(tool["content"]).get("proposal_id")
            == target["proposal_id"]
        ),
        None,
    )
    check(
        evidence,
        "确认快照可定位到准备结果",
        confirmed_tool is not None,
        {
            "target_proposal_id": target["proposal_id"],
            "prepared_proposal_ids": [
                json.loads(tool["content"]).get("proposal_id")
                for tool in all_prepared
            ],
        },
    )
    if confirmed_tool is None:
        evidence["observations"] = {
            "first_turn_prepared": bool(first_prepared),
            "computed_date": computed_date,
            "expected_date": expected_date,
            "answer": result["answer"],
            "confirm_answer": confirm_turn["answer"],
        }
        return

    proposal = json.loads(confirmed_tool["content"])
    payload = proposal.get("payload") or {}
    exercises = payload.get("exercises") or []
    check(
        evidence,
        "训练日期为业务日期",
        proposal.get("performed_on") == expected_date,
        {"performed_on": proposal.get("performed_on"), "expected_date": expected_date},
    )
    check(
        evidence,
        "新增记录基础字段为空",
        proposal.get("base_workout_id") is None
        and proposal.get("base_workout_version") is None,
        {
            "base_workout_id": proposal.get("base_workout_id"),
            "base_workout_version": proposal.get("base_workout_version"),
        },
    )
    check(
        evidence,
        "保留用户提供的实际组次",
        len(exercises) == 1
        and exercises[0].get("name") == "杠铃卧推"
        and workout_sets_match(exercises[0], [(8, 60.0), (8, 62.5), (6, 65.0)]),
        exercises,
    )
    check(
        evidence,
        "未编造缺失信息",
        all(item.get("duration_seconds") is None for item in exercises)
        and payload.get("notes") is None,
        {
            "duration": [item.get("duration_seconds") for item in exercises],
            "notes": payload.get("notes"),
        },
    )

    operation = confirm(http, session_id, "workout", target)
    saved = operation["result"]
    listing = list_workouts(http)
    check(
        evidence,
        "按钮确认保存训练记录",
        operation["status"] == "saved"
        and listing["total"] == 1
        and listing["items"][0]["id"] == saved["id"]
        and listing["items"][0]["content"] == payload,
        {"saved": saved, "listing": listing, "confirmed_payload": payload},
    )
    repeat = http.post(
        f"/api/sessions/{session_id}/confirmations",
        json={
            "operation_id": operation["operation_id"],
            "business_kind": "workout",
            "proposal_id": target["proposal_id"],
            "display_entry_id": target["display_entry_id"],
        },
    )
    check(
        evidence,
        "重复确认保持幂等",
        repeat.status_code == 200
        and repeat.json()["result"] == saved
        and list_workouts(http)["total"] == 1,
        {
            "status": repeat.status_code,
            "body": repeat.json() if repeat.status_code == 200 else repeat.text,
        },
    )
    evidence["observations"] = {
        "first_turn_prepared": bool(first_prepared),
        "computed_date": computed_date,
        "expected_date": expected_date,
        "confirmed_proposal_id": proposal.get("proposal_id"),
        "payload": payload,
        "saved": saved,
        "answer": result["answer"],
        "confirm_answer": confirm_turn["answer"],
        "confirmation_entry_id": operation.get("confirmation_entry_id"),
    }


def base_evidence(scenario: str, model: dict | None) -> dict:
    return {
        "scenario": scenario,
        "status": "fail",
        "started_at": now_ms(),
        "finished_at": None,
        "model": model,
        "database": None,
        "tmp_root": None,
        "seeded": None,
        "session_id": None,
        "input": None,
        "turns": [],
        "checks": [],
        "observations": {},
        "manual_review": None,
        "failure": None,
        "rerun": None,
    }


@contextmanager
def file_tool_root(root: Path):
    original = agent_routes.create_file_tools
    agent_routes.create_file_tools = partial(original, tmp_root=root)
    try:
        yield
    finally:
        agent_routes.create_file_tools = original


@contextmanager
def isolated_app(database: Path, trace: Path):
    original_database = database_module.default_database_path
    from app.agent import telemetry

    original_trace = telemetry.trace_database_path
    database_module.default_database_path = lambda: database
    telemetry.trace_database_path = lambda: trace
    try:
        yield
    finally:
        database_module.default_database_path = original_database
        telemetry.trace_database_path = original_trace


def run_live(scenario: str, run_root: Path, evidence: dict) -> None:
    evidence["input"] = {
        "c1": PROMPT_C1,
        "c2": PROMPT_C2,
        "c3": PROMPT_C3,
        "d1": PROMPT_D1,
        "d2": PROMPT_D2,
        "d3": PROMPT_D3,
        "e-plan": PROMPT_E_PLAN,
        "e-adjust": PROMPT_E_ADJUST,
        "e-workout": PROMPT_E_WORKOUT,
    }[scenario]
    database = run_root / "dbs" / f"{scenario}.db"
    trace = run_root / "traces" / f"{scenario}.db"
    tmp_root = run_root / "tmp" / scenario
    # 同一 run-id 重跑必须从干净状态开始：只清理本场景的库、轨迹与临时目录。
    for path in (database, trace):
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(str(path) + suffix)
            if target.exists():
                target.unlink()
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    evidence["database"] = str(database)
    evidence["tmp_root"] = str(tmp_root)

    seeded = seed_database(
        database,
        with_profile=scenario in {"e-plan", "e-adjust"},
        with_plan=scenario == "e-adjust",
        with_workouts=scenario == "e-adjust",
    )
    evidence["seeded"] = seeded

    handlers = {
        "c1": scenario_c1,
        "c2": scenario_c2,
        "c3": scenario_c3,
        "d1": scenario_d1,
        "d2": scenario_d2,
        "d3": scenario_d3,
        "e-plan": scenario_e_plan,
        "e-adjust": scenario_e_adjust,
        "e-workout": scenario_e_workout,
    }
    with isolated_app(database, trace), file_tool_root(tmp_root):
        with Server(interface.app) as server, client(server.base_url, timeout=280) as http:
            handlers[scenario](http, evidence)


def evidence_run_ids(evidence: dict) -> list[str]:
    return [
        turn["run_id"] for turn in evidence.get("turns", []) if turn.get("run_id")
    ]


def evidence_answer_digest(evidence: dict) -> str:
    answers = [turn.get("final_answer") or "" for turn in evidence.get("turns", [])]
    return hashlib.sha256("\n---\n".join(answers).encode("utf-8")).hexdigest()


def manual_review_error(evidence: dict) -> str | None:
    review = evidence.get("manual_review")
    if not review:
        return "missing"
    if review.get("status") not in ("pass", "fail"):
        return "invalid_status"
    if list(review.get("run_ids") or []) != evidence_run_ids(evidence):
        return "run_ids_mismatch"
    if review.get("answer_sha256") != evidence_answer_digest(evidence):
        return "answer_sha256_mismatch"
    return None


def finish(evidence: dict) -> None:
    evidence["finished_at"] = now_ms()
    evidence["run_ids"] = evidence_run_ids(evidence)
    evidence["answer_sha256"] = evidence_answer_digest(evidence)
    checks_passed = bool(evidence["checks"]) and all(
        item["passed"] for item in evidence["checks"]
    )
    if evidence.get("failure"):
        evidence["status"] = "fail"
        return
    if not checks_passed:
        evidence["status"] = "fail"
        return
    problem = manual_review_error(evidence)
    if problem == "missing":
        evidence["status"] = "pending_review"
        evidence["manual_review_error"] = problem
        return
    if problem:
        evidence["status"] = "fail"
        evidence["manual_review_error"] = problem
        return
    evidence["manual_review_error"] = None
    evidence["status"] = (
        "pass" if evidence["manual_review"]["status"] == "pass" else "fail"
    )


def load_manual_review(path: Path, scenario: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "scenarios" in data:
        data = data["scenarios"].get(scenario)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"人工核对文件缺少场景 {scenario}")
    return data


def write_evidence(path: Path, evidence: dict, api_key: str | None) -> None:
    text = json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
    if api_key and api_key in text:
        raise RuntimeError("证据包含模型凭据，拒绝写入")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        archive = path.parent / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f"{path.stem}-{now_ms()}.json").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    path.write_text(text, encoding="utf-8")


def model_identity(config) -> dict:
    return {
        "provider": config.provider or "",
        "api": config.api,
        "model": config.model,
        "base_url": config.base_url,
    }


def print_summary(scenario: str, evidence: dict, path: Path) -> None:
    print(
        json.dumps(
            {
                "scenario": scenario,
                "status": evidence["status"],
                "manual_review_error": evidence.get("manual_review_error"),
                "run_ids": evidence.get("run_ids"),
                "answer_sha256": evidence.get("answer_sha256"),
                "session_id": evidence.get("session_id"),
                "checks": [
                    {"name": item["name"], "passed": item["passed"]}
                    for item in evidence["checks"]
                ],
                "evidence": str(path),
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--run-id", default="d-acceptance")
    parser.add_argument("--apply-review")
    arguments = parser.parse_args()
    run_root = temporary_root(EVIDENCE_ROOT) / arguments.run_id
    evidence_dir = run_root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{arguments.scenario}.json"

    if arguments.apply_review:
        if not path.is_file():
            print(f"缺少证据文件，无法应用人工核对：{path}")
            return 2
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["manual_review"] = load_manual_review(
            Path(arguments.apply_review), arguments.scenario
        )
        finish(evidence)
        write_evidence(path, evidence, None)
        print_summary(arguments.scenario, evidence, path)
        return 0 if evidence["status"] == "pass" else 1

    try:
        config = load_model_config()
    except (ModelNotConfigured, ModelConfigError) as error:
        evidence = base_evidence(arguments.scenario, None)
        evidence["status"] = "blocked"
        evidence["failure"] = {"type": type(error).__name__, "message": str(error)}
        evidence["finished_at"] = now_ms()
        write_evidence(path, evidence, None)
        print(f"BLOCKED: 缺少真实模型配置，未执行验收：{error}")
        print(f"Evidence: {path}")
        return 2

    identity = model_identity(config)
    evidence = base_evidence(arguments.scenario, identity)
    try:
        run_live(arguments.scenario, run_root, evidence)
    except BaseException as error:
        evidence["failure"] = {
            "type": f"{type(error).__module__}.{type(error).__qualname__}",
            "message": str(error),
        }
        finish(evidence)
        write_evidence(path, evidence, config.api_key)
        print(f"FAIL: {arguments.scenario}: {type(error).__name__}: {error}")
        print(f"Evidence: {path}")
        return 1
    finish(evidence)
    write_evidence(path, evidence, config.api_key)
    print_summary(arguments.scenario, evidence, path)
    return 0 if evidence["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
