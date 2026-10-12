import asyncio
import json
import shutil
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.application.business.catalog import Catalog
from app.application.business.coordination import ReplacementCoordinator
from app.application.business.service import BusinessService, business_date
from app.application.session.service import SessionService
from app.domain.business.models import BusinessContext, ProfileContent
from app.domain.session.attachments import TMP_ROOT
from app.infrastructure.persistence.sqlite import database as database_module
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from test import p0_business_cases as engine
from test.check_profile_confirmation import Fixture, propose
from test.p0_contract import (
    CASE_ID_PREFIXES,
    CATEGORY_QUOTAS,
    CaseRoundObservation,
    P0Case,
    TurnObservation,
)

BACKEND = Path(__file__).resolve().parents[1]
CASES_PATH = Path(__file__).resolve().parent / "p0_cases.json"
TEST_ROOT = BACKEND / "temp" / "p0-business-cases"

# 用户回合不得泄漏工具流程与评分标准。
LEAK_MARKERS = (
    "prepare_plan",
    "save_plan",
    "prepare_profile_update",
    "prepare_workout",
    "list_workouts",
    "工具调用",
    "评分",
    "检查项",
)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def load_cases() -> list[P0Case]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [P0Case.model_validate(item) for item in data]


def case_map() -> dict[str, P0Case]:
    return {case.case_id: case for case in load_cases()}


def _workdir() -> Path:
    directory = TEST_ROOT / uuid4().hex
    directory.mkdir(parents=True)
    return directory


@contextmanager
def isolated_business(directory: Path):
    loop = asyncio.new_event_loop()
    path = directory / "p0.db"
    database = loop.run_until_complete(open_database(path))
    repository = SqliteBusinessRepository(database)
    sessions = SessionService(SqliteSessionRepository(database))
    business = BusinessService(
        repository, Catalog.load(), sessions, ReplacementCoordinator()
    )
    sessions.attach_snapshots(business)
    try:
        yield path, business, loop, repository
    finally:
        loop.run_until_complete(database.close())
        loop.close()


def make_context(session_id: str) -> BusinessContext:
    moment = _now_ms()
    return BusinessContext(
        timezone="Asia/Shanghai",
        business_date=business_date(moment),
        session_id=session_id,
        run_id=str(uuid4()),
        request_entry_id=str(uuid4()),
        source_entry_id=str(uuid4()),
    )


def make_observation(
    case_id: str, round_index: int, session_id: str, turns=None, status="pass"
) -> CaseRoundObservation:
    return CaseRoundObservation(
        case_id=case_id,
        round_index=round_index,
        session_id=session_id,
        turns=[] if turns is None else turns,
        checks=[],
        usage=None,
        duration_ms=None,
        status=status,
        error=None,
    )


def _turn(case_id: str, round_index: int, session_id: str, tool_calls) -> TurnObservation:
    return TurnObservation(
        case_id=case_id,
        round_index=round_index,
        turn_index=0,
        request_text="用户回合",
        session_id=session_id,
        operation_id=str(uuid4()),
        run_id=str(uuid4()),
        request_entry_id=str(uuid4()),
        terminal="done",
        events=[],
        tool_calls=tool_calls,
        before_state={},
        after_state={},
        usage=None,
        duration_ms=None,
    )


def test_case_count_and_quotas():
    cases = load_cases()
    assert len(cases) == 25, len(cases)
    counter = Counter(case.category for case in cases)
    assert dict(counter) == CATEGORY_QUOTAS, counter
    assert set(counter) == set(CASE_ID_PREFIXES)


def test_case_ids_unique_and_prefixed():
    cases = load_cases()
    ids = [case.case_id for case in cases]
    assert len(set(ids)) == len(ids)
    by_category: dict[str, list[int]] = {}
    for case in cases:
        prefix = CASE_ID_PREFIXES[case.category]
        assert case.case_id.startswith(prefix), case.case_id
        number = case.case_id[len(prefix):]
        assert number.isdigit(), case.case_id
        by_category.setdefault(case.category, []).append(int(number))
    # 编号连续且从 1 开始。
    for category, numbers in by_category.items():
        assert sorted(numbers) == list(range(1, CATEGORY_QUOTAS[category] + 1)), (
            category,
            numbers,
        )


def test_case_structure_and_input_hygiene():
    cases = load_cases()
    for case in cases:
        assert case.case_version == 2
        assert case.turns, case.case_id
        assert case.checks, case.case_id
        assert case.manual_review, case.case_id
        assert case.product_basis.strip(), case.case_id
        assert any(check.kind == "deterministic" for check in case.checks), case.case_id
        assert any(check.kind == "manual" for check in case.checks), case.case_id
        for turn in case.turns:
            assert turn.text.strip(), case.case_id
            for marker in LEAK_MARKERS:
                assert marker not in turn.text, (case.case_id, marker)
        for check in case.checks:
            if check.kind == "manual":
                assert check.rule.strip()
                assert check.expectation is None
                assert check.target.strip()


def test_check_ids_rules_and_targets():
    cases = load_cases()
    seen: set[str] = set()
    for case in cases:
        for check in case.checks:
            assert check.check_id not in seen, check.check_id
            seen.add(check.check_id)
            assert check.check_id.startswith(case.case_id + engine.ROUND_MARK), check.check_id
            assert engine._check_round(check.check_id) >= 1
            assert check.kind in {"deterministic", "manual"}
            if check.kind == "manual":
                continue
            assert check.rule in engine.RULES, (check.check_id, check.rule)
            if check.rule not in engine._CHECKS_NEEDING_OBSERVATION:
                assert engine.target_is_resolvable(check.target), (check.check_id, check.target)


def test_rounds_are_covered_by_checks():
    for case in load_cases():
        rounds = {engine._check_round(check.check_id) for check in case.checks}
        assert rounds == set(range(1, len(case.turns) + 1)), (case.case_id, rounds)


def test_build_precondition_seeds_real_state(monkeypatch):
    directory = _workdir()
    with isolated_business(directory) as (path, business, loop, _repository):
        monkeypatch.setattr(database_module, "default_database_path", lambda: path)
        case = case_map()["A02"]
        session_id = str(uuid4())
        context = make_context(session_id)
        evidence = engine.build_precondition(
            case, service=business, business_context=context, session_id=session_id
        )
        assert evidence.built is True
        assert evidence.case_id == "A02"
        state = engine.read_state(path)
        assert state["profile"]["version"] == 1
        assert state["plans"]["count"] == 1
        assert state["workouts"]["count"] == 1
        assert "barbell" in state["derived"]["plan_equipment"]
        assert evidence.detail["workouts"] == [engine._performed_on(
            context.business_date, -2
        )]


def test_build_precondition_seeds_workspace_file(monkeypatch):
    directory = _workdir()
    with isolated_business(directory) as (path, business, _loop, _repository):
        monkeypatch.setattr(database_module, "default_database_path", lambda: path)
        case = case_map()["L05"]
        session_id = str(uuid4())
        context = make_context(session_id)
        try:
            evidence = engine.build_precondition(
                case, service=business, business_context=context, session_id=session_id
            )
            seeded = TMP_ROOT / "sessions" / session_id / "workspace" / "plan-cycle.md"
            assert seeded.is_file()
            assert "推、拉、腿、休" in seeded.read_text(encoding="utf-8")
            assert evidence.detail["files"] == [
                f"sessions/{session_id}/workspace/plan-cycle.md"
            ]
        finally:
            shutil.rmtree(TMP_ROOT / "sessions" / session_id, ignore_errors=True)


def test_evaluate_detects_wrong_state(monkeypatch):
    directory = _workdir()
    with isolated_business(directory) as (path, business, loop, repository):
        monkeypatch.setattr(database_module, "default_database_path", lambda: path)
        case = case_map()["P01"]
        session_id = str(uuid4())
        context = make_context(session_id)
        wrong = {
            "goal": "增肌",
            "experience": "新手",
            "environment": "商业健身房",
            "availability": "每周三次",
            "health_notes": None,
            "movement_restrictions": None,
            "unavailable_equipment": [],
            "forbidden_exercise_ids": [],
        }

        async def seed():
            async with repository.transaction():
                await repository.save_profile(
                    ProfileContent.model_validate(wrong), 1, _now_ms()
                )

        loop.run_until_complete(seed())
        observation = make_observation("P01", 1, session_id, status="fail")
        results = engine.evaluate(
            case,
            observation=observation,
            service=business,
            database_path=str(path),
            business_context=context,
        )
        deterministic = {item.check_id: item for item in results if item.kind == "deterministic"}
        assert deterministic["P01-R1-01"].status == "fail"
        assert deterministic["P01-R1-02"].status == "fail"
        assert deterministic["P01-R1-03"].status == "fail"
        assert deterministic["P01-R1-04"].status == "pass"
        manual = [item for item in results if item.kind == "manual"]
        assert manual and all(item.status == "pending_review" for item in manual)


def test_evaluate_passes_clean_and_saved_states(monkeypatch):
    directory = _workdir()
    with isolated_business(directory) as (path, business, _loop, _repository):
        monkeypatch.setattr(database_module, "default_database_path", lambda: path)
        cases = case_map()
        session_id = str(uuid4())
        context = make_context(session_id)

        for case_id in ("W03", "S01", "S02"):
            case = cases[case_id]
            results = engine.evaluate(
                case,
                observation=make_observation(case_id, 1, session_id),
                service=business,
                database_path=str(path),
                business_context=context,
            )
            assert results, case_id
            assert all(
                item.status == "pass"
                for item in results
                if item.kind == "deterministic"
            ), (case_id, [item.model_dump() for item in results])

        # 已确认画像读档：P04 的确定性检查在真实前置下全部通过。
        evidence = engine.build_precondition(
            cases["P04"], service=business, business_context=context, session_id=session_id
        )
        assert evidence.built is True
        results = engine.evaluate(
            cases["P04"],
            observation=make_observation("P04", 1, session_id),
            service=business,
            database_path=str(path),
            business_context=context,
        )
        assert all(
            item.status == "pass" for item in results if item.kind == "deterministic"
        ), [item.model_dump() for item in results]


def test_build_precondition_all_cases(monkeypatch):
    cases = load_cases()
    for case in cases:
        directory = _workdir()
        with isolated_business(directory) as (path, business, _loop, _repository):
            monkeypatch.setattr(database_module, "default_database_path", lambda path=path: path)
            session_id = str(uuid4())
            context = make_context(session_id)
            evidence = engine.build_precondition(
                case, service=business, business_context=context, session_id=session_id
            )
            for extra in [session_id, *evidence.detail["sessions"].values()]:
                shutil.rmtree(TMP_ROOT / "sessions" / extra, ignore_errors=True)
            assert evidence.built is True, case.case_id
            declaration = case.precondition
            state = engine.read_state(path)
            versions = declaration.get("profile_versions", [])
            expected_version = None if not versions else len(versions)
            assert state["profile"]["version"] == expected_version, case.case_id
            assert state["plans"]["count"] == len(declaration.get("plans", [])), case.case_id
            assert state["workouts"]["count"] == len(declaration.get("workouts", [])), case.case_id
            assert all(record["version"] == 1 for record in state["workouts"]["by_date"].values())


def test_real_profile_chain_then_evaluate_passes(monkeypatch):
    directory = _workdir()
    with isolated_business(directory) as (path, business, loop, _repository):
        monkeypatch.setattr(database_module, "default_database_path", lambda: path)
        case = case_map()["P02"]
        context = make_context(str(uuid4()))
        evidence = engine.build_precondition(
            case,
            service=business,
            business_context=context,
            session_id=context.session_id,
        )
        assert evidence.built is True
        content = {
            "goal": "减脂与体能",
            "experience": "力量训练新手，系统训练约三个月",
            "environment": "普通商业健身房",
            "availability": "每周三次，每次约四十五分钟",
            "health_notes": None,
            "movement_restrictions": None,
            "unavailable_equipment": [],
            "forbidden_exercise_ids": [],
        }

        async def cycle():
            fixture = await Fixture(path).seeded()
            try:
                session_id = await fixture.session("P02 保存")
                ids = await propose(
                    fixture,
                    session_id,
                    {"profile_id": 1, "base_profile_version": 1, "payload": content},
                )
                # 合法保存只经独立确认入口：后端生成确认节点并直接调用业务服务。
                operation = await fixture.confirm(ids)
                assert operation.status == "saved", operation
                return operation.result
            finally:
                await fixture.close()

        saved = loop.run_until_complete(cycle())
        assert saved.version == 2
        assert saved.content.goal == "减脂与体能"
        results = engine.evaluate(
            case,
            observation=make_observation("P02", 2, context.session_id),
            service=business,
            database_path=str(path),
            business_context=context,
        )
        statuses = {item.check_id: item.status for item in results if item.kind == "deterministic"}
        assert statuses == {
            "P02-R2-01": "pass",
            "P02-R2-02": "pass",
            "P02-R2-03": "pass",
            "P02-R2-04": "pass",
        }, statuses


def test_evaluate_observation_rule_detects_leak(monkeypatch):
    directory = _workdir()
    with isolated_business(directory) as (path, business, _loop, _repository):
        monkeypatch.setattr(database_module, "default_database_path", lambda: path)
        case = case_map()["S03"]
        session_id = str(uuid4())
        context = make_context(session_id)

        leaky = make_observation(
            "S03",
            1,
            session_id,
            turns=[
                _turn(
                    "S03",
                    1,
                    session_id,
                    [{"name": "read", "is_error": False, "content": "终极秘密是 42"}],
                )
            ],
            status="fail",
        )
        leaked = engine.evaluate(
            case,
            observation=leaky,
            service=business,
            database_path=str(path),
            business_context=context,
        )
        assert next(
            item for item in leaked if item.check_id == "S03-R1-02"
        ).status == "fail"

        clean = make_observation(
            "S03",
            1,
            session_id,
            turns=[
                _turn(
                    "S03",
                    1,
                    session_id,
                    [{"name": "read", "is_error": True, "content": "只允许访问当前会话"}],
                )
            ],
        )
        ok = engine.evaluate(
            case,
            observation=clean,
            service=business,
            database_path=str(path),
            business_context=context,
        )
        assert next(
            item for item in ok if item.check_id == "S03-R1-02"
        ).status == "pass"
