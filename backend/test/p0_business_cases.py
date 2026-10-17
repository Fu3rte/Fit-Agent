import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from app.application.business.catalog import Catalog
from app.application.session.service import SessionService
from app.domain.business.models import (
    PlanContent,
    PlanRecord,
    ProfileContent,
    WorkoutContent,
    WorkoutRecord,
)
from app.domain.session.attachments import TMP_ROOT
from app.infrastructure.persistence.sqlite import database as database_module
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test.p0_contract import (
    CaseRoundObservation,
    CheckResult,
    P0Case,
    PreconditionEvidence,
)

# check_id 形如 <CASE>-R<round>-<n>：判定归属的回合由此段声明。
ROUND_MARK = "-R"

_MIGRATION_TS = 1_700_000_000_000


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


# ---------- 隔离库定位 ----------


def _database_path(service, database_path: str | None) -> Path:
    if database_path:
        return Path(database_path)
    provided = getattr(service, "database_path", None)
    if provided is not None:
        return Path(provided)
    return Path(database_module.default_database_path())


def _run_in_own_loop(coroutine_factory):
    # 私有事件循环运行：调用方可能持有运行中的 loop，前置构造使用独立连接避免冲突。
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(coroutine_factory())).result()


# ---------- 前置状态构造 ----------


def _validate_profile(content: dict, catalog: Catalog) -> ProfileContent:
    model = ProfileContent.model_validate(content)
    if model.unavailable_equipment:
        for item in model.unavailable_equipment:
            if item not in catalog.equipment:
                raise ValueError(f"器械标识不在动作目录：{item}")
    if model.forbidden_exercise_ids:
        for item in model.forbidden_exercise_ids:
            if item not in catalog.exercise_ids:
                raise ValueError(f"动作 ID 不在动作目录：{item}")
    return model


def _performed_on(business_date: str, offset: int) -> str:
    return (date.fromisoformat(business_date) + timedelta(days=offset)).isoformat()


async def _build(declaration: dict, database_path: Path, business_date: str):
    catalog = Catalog.load()
    database = await open_database(database_path)
    repository = SqliteBusinessRepository(database)
    sessions = SessionService(SqliteSessionRepository(database))
    detail: dict = {"database_path": str(database_path), "business_date": business_date}
    try:
        async with repository.transaction():
            for index, content in enumerate(declaration.get("profile_versions", []), 1):
                await repository.save_profile(
                    _validate_profile(content, catalog), index, _MIGRATION_TS + index
                )
            for index, content in enumerate(declaration.get("plans", [])):
                record = PlanRecord(
                    id=str(uuid4()),
                    is_current=True,
                    content=PlanContent.model_validate(content),
                    created_at=_MIGRATION_TS + 1000 + index,
                )
                await repository.insert_plan(record)
        for index, item in enumerate(declaration.get("workouts", [])):
            record = WorkoutRecord(
                id=str(uuid4()),
                performed_on=_performed_on(business_date, int(item["performed_on_offset"])),
                version=1,
                content=WorkoutContent.model_validate(item["content"]),
                created_at=_MIGRATION_TS + 2000 + index,
                updated_at=_MIGRATION_TS + 2000 + index,
            )
            async with repository.transaction():
                await repository.insert_workout(record)
        session_ids: dict[str, str] = {}
        for title in declaration.get("sessions", []):
            session_id = str(uuid4())
            created, _ = await sessions.create_session_result(session_id, title)
            if not created:
                raise RuntimeError(f"会话未创建：{title}")
            session_ids[title] = session_id
        detail["profile_versions"] = len(declaration.get("profile_versions", []))
        detail["plans"] = len(declaration.get("plans", []))
        detail["workouts"] = [
            _performed_on(business_date, int(item["performed_on_offset"]))
            for item in declaration.get("workouts", [])
        ]
        detail["sessions"] = session_ids
        profile = await repository.get_profile()
        detail["profile_version"] = None if profile is None else profile.version
        return detail
    finally:
        await database.close()


def _seed_files(files: list[dict], session_id: str, session_ids: dict[str, str]) -> list[str]:
    seeded: list[str] = []
    for item in files:
        target = session_ids.get(item.get("session", "case"), session_id)
        workspace = TMP_ROOT / "sessions" / target / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / item["name"]
        path.write_text(item["text"], encoding="utf-8")
        seeded.append(f"sessions/{target}/workspace/{item['name']}")
    return seeded


def build_precondition(
    case: P0Case, *, service, business_context, session_id: str
) -> PreconditionEvidence:
    declaration = dict(case.precondition or {})
    database_path = _database_path(service, None)
    business_date = business_context.business_date
    detail = _run_in_own_loop(
        lambda: _build(declaration, database_path, business_date)
    )
    detail["files"] = _seed_files(
        declaration.get("files", []), session_id, detail.get("sessions", {})
    )
    detail["case_id"] = case.case_id
    return PreconditionEvidence(case_id=case.case_id, built=True, detail=detail)


# ---------- 真实持久化状态读取 ----------


def _loads(value: str | None):
    return None if value is None else json.loads(value)


def _workout_sets_total(content: dict) -> int:
    return sum(len(exercise.get("sets", [])) for exercise in content.get("exercises", []))


def _plan_day_counts(content: dict | None) -> tuple[int, int]:
    if content is None:
        return (0, 0)
    training = sum(1 for day in content.get("days", []) if day.get("kind") == "training")
    rest = sum(1 for day in content.get("days", []) if day.get("kind") == "rest")
    return (training, rest)


def _plan_exercise_ids(content: dict | None) -> list[str]:
    if content is None:
        return []
    found = []
    for day in content.get("days", []):
        for exercise in day.get("exercises", []):
            if exercise.get("exercise_id"):
                found.append(exercise["exercise_id"])
    return found


def _unique_pending(rows: list[dict]) -> dict | None:
    pending = [row for row in rows if row["status"] == "pending"]
    if len(pending) != 1:
        return None
    row = pending[0]
    return row


def _workout_node(row) -> dict:
    content = _loads(row["content"])
    return {
        "id": row["id"],
        "performed_on": row["performed_on"],
        "version": row["version"],
        "content": content,
        "sets_total": _workout_sets_total(content),
        "dates": row["performed_on"],
    }


def read_state(database_path: str | Path) -> dict:
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    try:
        profile_row = connection.execute(
            "SELECT version, content FROM profile WHERE id = 1"
        ).fetchone()
        snapshots = [
            {
                "proposal_id": row["proposal_id"],
                "status": row["status"],
                "payload": _loads(row["payload"]),
                "display_entry_id": row["display_entry_id"],
                "confirmation_entry_id": row["confirmation_entry_id"],
                "base_profile_version": row["base_profile_version"],
            }
            for row in connection.execute(
                "SELECT proposal_id, status, payload, display_entry_id, "
                "confirmation_entry_id, base_profile_version FROM profile_snapshots"
            )
        ]
        plan_snapshots = [
            {
                "proposal_id": row["proposal_id"],
                "status": row["status"],
                "payload": _loads(row["payload"]),
                "base_profile_version": row["base_profile_version"],
                "base_plan_id": row["base_plan_id"],
                "display_entry_id": row["display_entry_id"],
                "preparation_kind": row["preparation_kind"],
            }
            for row in connection.execute(
                "SELECT proposal_id, status, payload, base_profile_version, base_plan_id, "
                "display_entry_id, preparation_kind FROM plan_snapshots"
            )
        ]
        workout_snapshots = [
            {
                "proposal_id": row["proposal_id"],
                "status": row["status"],
                "payload": _loads(row["payload"]),
                "performed_on": row["performed_on"],
                "base_workout_id": row["base_workout_id"],
                "base_workout_version": row["base_workout_version"],
                "display_entry_id": row["display_entry_id"],
                "sets_total": _workout_sets_total(_loads(row["payload"])),
            }
            for row in connection.execute(
                "SELECT proposal_id, status, payload, performed_on, base_workout_id, "
                "base_workout_version, display_entry_id FROM workout_snapshots"
            )
        ]
        plans = [
            {
                "id": row["id"],
                "is_current": bool(row["is_current"]),
                "content": _loads(row["content"]),
                "created_at": row["created_at"],
            }
            for row in connection.execute(
                "SELECT id, is_current, content, created_at FROM plans "
                "ORDER BY created_at DESC, id DESC"
            )
        ]
        for snapshot in plan_snapshots:
            training, rest = _plan_day_counts(snapshot["payload"])
            snapshot["training_day_count"] = training
            snapshot["rest_day_count"] = rest
        workouts = [
            _workout_node(row)
            for row in connection.execute(
                "SELECT id, performed_on, version, content FROM workouts "
                "ORDER BY performed_on DESC, id DESC"
            )
        ]
        by_date = {record["performed_on"]: record for record in workouts}
        current = next((plan for plan in plans if plan["is_current"]), None)
        counts = {
            "profile_save_records": connection.execute(
                "SELECT COUNT(*) FROM profile_save_records"
            ).fetchone()[0],
            "plan_save_records": connection.execute(
                "SELECT COUNT(*) FROM plan_save_records"
            ).fetchone()[0],
            "workout_save_records": connection.execute(
                "SELECT COUNT(*) FROM workout_save_records"
            ).fetchone()[0],
            "sessions": connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "session_attachments": connection.execute(
                "SELECT COUNT(*) FROM session_attachments"
            ).fetchone()[0],
        }
    finally:
        connection.close()

    def status_counts(rows: list[dict]) -> dict:
        result = {"count": len(rows), "pending_count": 0, "saved_count": 0, "invalidated_count": 0}
        for row in rows:
            if row["status"] == "pending":
                result["pending_count"] += 1
            elif row["status"] == "saved":
                result["saved_count"] += 1
            elif row["status"] == "invalidated":
                result["invalidated_count"] += 1
        result["pending"] = _unique_pending(rows)
        return result

    catalog = Catalog.load()
    plan_exercise_ids = sorted(
        {
            *_plan_exercise_ids(None if current is None else current["content"]),
            *[
                exercise
                for snapshot in plan_snapshots
                for exercise in _plan_exercise_ids(snapshot["payload"])
            ],
        }
    )
    all_exercise_ids = sorted(
        {
            *plan_exercise_ids,
            *[
                exercise["exercise_id"]
                for record in workouts
                for exercise in record["content"].get("exercises", [])
                if exercise.get("exercise_id")
            ],
            *[
                exercise["exercise_id"]
                for snapshot in workout_snapshots
                for exercise in snapshot["payload"].get("exercises", [])
                if exercise.get("exercise_id")
            ],
        }
    )
    plan_equipment = sorted(
        {
            equipment
            for exercise_id in plan_exercise_ids
            if (equipment := _equipment_of(catalog, exercise_id)) is not None
        }
    )
    return {
        "profile": {
            "count": 0 if profile_row is None else 1,
            "version": None if profile_row is None else profile_row["version"],
            "content": None if profile_row is None else _loads(profile_row["content"]),
        },
        "profile_snapshots": status_counts(snapshots),
        "profile_save_records": {"count": counts["profile_save_records"]},
        "plans": {
            "count": len(plans),
            "current_id": None if current is None else current["id"],
            "current": None if current is None else current["content"],
            "versions": [plan["id"] for plan in plans],
            "current_training_day_count": _plan_day_counts(
                None if current is None else current["content"]
            )[0],
            "current_rest_day_count": _plan_day_counts(
                None if current is None else current["content"]
            )[1],
        },
        "plan_snapshots": status_counts(plan_snapshots),
        "plan_save_records": {"count": counts["plan_save_records"]},
        "workouts": {
            "count": len(workouts),
            "total": len(workouts),
            "dates": [record["performed_on"] for record in workouts],
            "by_date": by_date,
        },
        "workout_snapshots": status_counts(workout_snapshots),
        "workout_save_records": {"count": counts["workout_save_records"]},
        "sessions": {"count": counts["sessions"]},
        "session_attachments": {"count": counts["session_attachments"]},
        "derived": {
            "all_exercise_ids": all_exercise_ids,
            "plan_exercise_ids": plan_exercise_ids,
            "plan_equipment": plan_equipment,
        },
    }


# ---------- 检查项判定 ----------


_MISSING = object()


def _resolve_date(token: str, business_date: str) -> str:
    base = date.fromisoformat(business_date)
    if token in ("@today", "@business_date"):
        return base.isoformat()
    if token == "@yesterday":
        return (base - timedelta(days=1)).isoformat()
    if token == "@tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if token.startswith("@+") or token.startswith("@-"):
        return (base + timedelta(days=int(token[1:]))).isoformat()
    raise ValueError(f"未知日期标记：{token}")


def _resolve_expectation(expectation, business_date: str):
    if isinstance(expectation, str) and expectation.startswith("@"):
        return _resolve_date(expectation, business_date)
    if isinstance(expectation, list):
        return [_resolve_expectation(item, business_date) for item in expectation]
    return expectation


def _resolve_value(path: str, state: dict, business_date: str):
    value = state
    for part in path.split("."):
        if part.startswith("@"):
            part = _resolve_date(part, business_date)
        if isinstance(value, dict):
            if part not in value:
                return _MISSING
            value = value[part]
        elif isinstance(value, list):
            if not part.lstrip("-").isdigit():
                return _MISSING
            index = int(part)
            if index >= len(value) or index < -len(value):
                return _MISSING
            value = value[index]
        else:
            return _MISSING
    return value


def _content_exercises(value) -> list[dict]:
    if not isinstance(value, dict):
        return []
    if "exercises" in value:
        return list(value["exercises"])
    found: list[dict] = []
    for day in value.get("days", []):
        found.extend(day.get("exercises", []))
    return found


def _equipment_of(catalog: Catalog, exercise_id: str) -> str | None:
    found = catalog.get(exercise_id)
    return None if found is None else found.equipment


def _equipment_check(value, expectation, catalog: Catalog) -> tuple[bool, str]:
    exercised = _content_exercises(value)
    equipment = sorted(
        {
            equipment
            for exercise in exercised
            if exercise.get("exercise_id")
            and (equipment := _equipment_of(catalog, exercise["exercise_id"])) is not None
        }
    )
    if isinstance(expectation, dict):
        excluded = set(expectation.get("exclude", []))
        overlap = sorted(set(equipment) & excluded)
        return (not overlap, f"出现被排除器械 {overlap}" if overlap else f"器件 {equipment}")
    allowed = set(expectation)
    outside = sorted(set(equipment) - allowed)
    return (not outside, f"出现目录外器械 {outside}" if outside else f"器械 {equipment}")


def _contains(value, expectation) -> tuple[bool, str]:
    items = set(expectation) if isinstance(expectation, list) else {expectation}
    actual = set(value or [])
    return (items <= actual, f"实际 {sorted(actual)}")


def _excludes(value, expectation) -> tuple[bool, str]:
    items = set(expectation) if isinstance(expectation, list) else {expectation}
    actual = set(value or [])
    overlap = sorted(items & actual)
    return (not overlap, f"命中 {overlap}" if overlap else f"实际 {sorted(actual)}")


def _contains_text(value, expectation) -> tuple[bool, str]:
    needles = expectation if isinstance(expectation, list) else [expectation]
    text = "" if value is None else str(value)
    missing = [needle for needle in needles if needle not in text]
    return (not missing, f"缺少 {missing}" if missing else "文本包含全部片段")


def _excludes_text(value, expectation) -> tuple[bool, str]:
    needles = expectation if isinstance(expectation, list) else [expectation]
    text = "" if value is None else str(value)
    hit = [needle for needle in needles if needle in text]
    return (not hit, f"命中 {hit}" if hit else "文本未包含排除片段")


def _catalog_ids_only(value, expectation, catalog: Catalog) -> tuple[bool, str]:
    unknown = [item for item in (value or []) if item not in catalog.exercise_ids]
    return (not unknown, f"目录外动作 {unknown}" if unknown else "动作均在目录内")


def _tool_calls(observation: CaseRoundObservation) -> list[dict]:
    calls: list[dict] = []
    for turn in observation.turns:
        calls.extend(turn.tool_calls)
    return calls


def _observation_text(observation: CaseRoundObservation) -> str:
    parts: list[str] = []
    for turn in observation.turns:
        for call in turn.tool_calls:
            parts.append(json.dumps(call, ensure_ascii=False, default=str))
        for event in turn.events:
            parts.append(json.dumps(event, ensure_ascii=False, default=str))
    return "\n".join(parts)


def _tool_called_successfully(expectation, observation) -> tuple[bool, str]:
    names = expectation if isinstance(expectation, list) else [expectation]
    succeeded = {
        call.get("name") for call in _tool_calls(observation) if not call.get("is_error")
    }
    missing = [name for name in names if name not in succeeded]
    return (not missing, f"未成功调用 {missing}" if missing else "已成功调用")


def _tool_not_called_successfully(expectation, observation) -> tuple[bool, str]:
    names = expectation if isinstance(expectation, list) else [expectation]
    succeeded = {
        call.get("name") for call in _tool_calls(observation) if not call.get("is_error")
    }
    hit = [name for name in names if name in succeeded]
    return (not hit, f"出现成功调用 {hit}" if hit else "未出现成功调用")


def _output_excludes(expectation, observation) -> tuple[bool, str]:
    needles = expectation if isinstance(expectation, list) else [expectation]
    text = _observation_text(observation)
    hit = [needle for needle in needles if needle in text]
    return (not hit, f"命中敏感片段 {hit}" if hit else "未出现敏感片段")


def _apply_rule(check, value, expectation, observation, catalog: Catalog) -> tuple[bool, str]:
    rule = check.rule
    if rule == "equals":
        return (value == expectation, f"实际={value!r} 期望={expectation!r}")
    if rule == "not_equals":
        return (value != expectation, f"实际={value!r} 禁止={expectation!r}")
    if rule == "is_null":
        return (value is None, f"实际={value!r} 期望为空")
    if rule == "not_null":
        return (value is not None, f"实际={value!r} 期望有值")
    if rule == "contains_all":
        passed, detail = _contains(value, expectation)
        return (passed, detail)
    if rule == "excludes_all":
        return _excludes(value, expectation)
    if rule == "set_equals":
        return (set(value or []) == set(expectation), f"实际={sorted(set(value or []))}")
    if rule == "subset_of":
        outside = sorted(set(value or []) - set(expectation))
        return (not outside, f"超出 {outside}" if outside else "全部在允许集合内")
    if rule == "gte":
        return (value is not None and value >= expectation, f"实际={value!r} 下界={expectation!r}")
    if rule == "lte":
        return (value is not None and value <= expectation, f"实际={value!r} 上界={expectation!r}")
    if rule == "length_equals":
        return (
            isinstance(value, (list, str)) and len(value) == expectation,
            f"实际长度={None if value is None else len(value)} 期望={expectation}",
        )
    if rule == "contains_text":
        return _contains_text(value, expectation)
    if rule == "excludes_text":
        return _excludes_text(value, expectation)
    if rule == "equipment_within":
        return _equipment_check(value, expectation, catalog)
    if rule == "catalog_ids_only":
        return _catalog_ids_only(value, expectation, catalog)
    if rule == "tool_called_successfully":
        return _tool_called_successfully(expectation, observation)
    if rule == "tool_not_called_successfully":
        return _tool_not_called_successfully(expectation, observation)
    if rule == "output_excludes":
        return _output_excludes(expectation, observation)
    raise ValueError(f"未知判定规则：{rule}")


def _check_round(check_id: str) -> int:
    marker = check_id.find(ROUND_MARK)
    if marker < 0:
        return 1
    remainder = check_id[marker + len(ROUND_MARK):]
    digits = ""
    for character in remainder:
        if character.isdigit():
            digits += character
        else:
            break
    return int(digits) if digits else 1


def evaluate(
    case: P0Case, *, observation: CaseRoundObservation, service, database_path: str,
    business_context,
) -> list[CheckResult]:
    state = read_state(database_path)
    catalog = Catalog.load()
    business_date = business_context.business_date
    results: list[CheckResult] = []
    for check in case.checks:
        if _check_round(check.check_id) != observation.round_index:
            continue
        if check.kind == "manual":
            results.append(
                CheckResult(
                    case_id=case.case_id,
                    round_index=observation.round_index,
                    check_id=check.check_id,
                    kind="manual",
                    status="pending_review",
                    reason=check.rule,
                    evidence_refs=[check.target],
                )
            )
            continue
        if check.rule in _CHECKS_NEEDING_OBSERVATION:
            value = None
        else:
            value = _resolve_value(check.target, state, business_date)
        if value is _MISSING:
            results.append(
                CheckResult(
                    case_id=case.case_id,
                    round_index=observation.round_index,
                    check_id=check.check_id,
                    kind="deterministic",
                    status="fail",
                    reason=f"状态路径无法解析：{check.target}",
                    evidence_refs=[check.target, f"database={database_path}"],
                )
            )
            continue
        expectation = _resolve_expectation(check.expectation, business_date)
        passed, detail = _apply_rule(check, value, expectation, observation, catalog)
        results.append(
            CheckResult(
                case_id=case.case_id,
                round_index=observation.round_index,
                check_id=check.check_id,
                kind="deterministic",
                status="pass" if passed else "fail",
                reason=f"{check.rule} via {check.target}: {detail}",
                evidence_refs=[check.target, f"database={database_path}"],
            )
        )
    return results


# 有效性校验：目标路径必须能解析到状态 schema，规则必须已注册。

PROBE_BUSINESS_DATE = "2026-01-10"
PROBE_DATE = "2026-01-09"

_RULES = (
    "equals",
    "not_equals",
    "is_null",
    "not_null",
    "contains_all",
    "excludes_all",
    "set_equals",
    "subset_of",
    "gte",
    "lte",
    "length_equals",
    "contains_text",
    "excludes_text",
    "equipment_within",
    "catalog_ids_only",
    "tool_called_successfully",
    "tool_not_called_successfully",
    "output_excludes",
)
RULES = frozenset(_RULES)

_CHECKS_NEEDING_OBSERVATION = frozenset(
    {
        "tool_called_successfully",
        "tool_not_called_successfully",
        "output_excludes",
    }
)


def _probe_exercise() -> dict:
    return {
        "exercise_id": "0025",
        "name": "杠铃卧推",
        "sets": 4,
        "reps": 8,
        "duration_seconds": None,
        "weight_kg": 60.0,
        "load_convention": "barbell_total",
        "rest_seconds": 120.0,
    }


def probe_state() -> dict:
    # 覆盖全部目标路径分支的样例状态，用于校验目标表达式可解析。
    plan_content = {
        "repeat": True,
        "days": [
            {"kind": "training", "focus": "推", "exercises": [_probe_exercise(), _probe_exercise()], "notes": None},
            {"kind": "training", "focus": "拉", "exercises": [_probe_exercise()], "notes": None},
            {"kind": "training", "focus": "腿", "exercises": [_probe_exercise()], "notes": None},
            {"kind": "rest", "focus": None, "exercises": [], "notes": None},
        ],
        "notes": "样例",
        "suggested_fields": ["/days"],
    }
    workout_content = {
        "exercises": [
            {
                "exercise_id": "0025",
                "name": "杠铃卧推",
                "load_convention": "barbell_total",
                "sets": [{"reps": 8, "weight_kg": 60.0, "duration_seconds": None}],
            }
        ],
        "notes": None,
    }
    profile_content = {
        "goal": "增肌",
        "experience": "新手",
        "environment": "普通商业健身房",
        "availability": "每周三次",
        "health_notes": None,
        "movement_restrictions": None,
        "unavailable_equipment": [],
        "forbidden_exercise_ids": [],
    }
    workout_node = {
        "id": "00000000-0000-4000-8000-000000000010",
        "performed_on": PROBE_DATE,
        "version": 1,
        "content": workout_content,
        "sets_total": 1,
        "dates": PROBE_DATE,
    }
    return {
        "profile": {"count": 1, "version": 1, "content": profile_content},
        "profile_snapshots": {
            "count": 1,
            "pending_count": 1,
            "saved_count": 0,
            "invalidated_count": 0,
            "pending": {
                "proposal_id": "00000000-0000-4000-8000-000000000001",
                "status": "pending",
                "payload": profile_content,
                "base_profile_version": 1,
                "display_entry_id": None,
                "confirmation_entry_id": None,
            },
        },
        "profile_save_records": {"count": 0},
        "plans": {
            "count": 1,
            "current_id": "00000000-0000-4000-8000-000000000002",
            "current": plan_content,
            "versions": ["00000000-0000-4000-8000-000000000002"],
            "current_training_day_count": 3,
            "current_rest_day_count": 1,
        },
        "plan_snapshots": {
            "count": 1,
            "pending_count": 1,
            "saved_count": 0,
            "invalidated_count": 0,
            "pending": {
                "proposal_id": "00000000-0000-4000-8000-000000000003",
                "status": "pending",
                "payload": plan_content,
                "base_profile_version": 1,
                "base_plan_id": "00000000-0000-4000-8000-000000000002",
                "display_entry_id": None,
                "preparation_kind": "adjustment",
                "training_day_count": 3,
                "rest_day_count": 1,
            },
        },
        "plan_save_records": {"count": 0},
        "workouts": {
            "count": 1,
            "total": 1,
            "dates": [PROBE_DATE],
            "by_date": {PROBE_DATE: workout_node, PROBE_BUSINESS_DATE: workout_node},
        },
        "workout_snapshots": {
            "count": 1,
            "pending_count": 1,
            "saved_count": 0,
            "invalidated_count": 0,
            "pending": {
                "proposal_id": "00000000-0000-4000-8000-000000000004",
                "status": "pending",
                "payload": workout_content,
                "performed_on": PROBE_DATE,
                "base_workout_id": None,
                "base_workout_version": None,
                "display_entry_id": None,
                "sets_total": 1,
            },
        },
        "workout_save_records": {"count": 0},
        "sessions": {"count": 2},
        "session_attachments": {"count": 0},
        "derived": {
            "all_exercise_ids": ["0025"],
            "plan_exercise_ids": ["0025"],
            "plan_equipment": ["barbell"],
        },
    }


def resolve_path(path: str, state: dict, business_date: str):
    return _resolve_value(path, state, business_date)


def target_is_resolvable(path: str) -> bool:
    return (
        _resolve_value(path, probe_state(), PROBE_BUSINESS_DATE) is not _MISSING
    )
