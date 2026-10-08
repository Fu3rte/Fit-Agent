import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import aiosqlite
from pydantic import ValidationError

from app.agent.tool import CredentialDetectedError, run_tool_batch
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.agent.tools.exercises import exercise_tool_declarations
from app.agent.tools.plans import plan_tool_declarations
from app.agent.tools.profile import profile_tool_declarations
from app.agent.tools.workouts import workout_tool_declarations
from app.ai.messages import ToolCall
from app.application.business.catalog import Catalog
from app.domain.business.errors import BusinessError, ProfileUpdateProcessing
from app.domain.business.models import ProfileProposalArguments
from app.domain.session.errors import EntryNotFound
from app.domain.session.models import RegenerateCommand, RegenerateRequest
from app.infrastructure.persistence.sqlite.business_repository import (
    SqliteBusinessRepository,
)
from app.infrastructure.persistence.sqlite.database import (
    _MIGRATIONS,
    SCHEMA_VERSION,
    apply_schema,
    open_database,
)
from test.check_profile_confirmation import (
    DECLARED,
    GET_PROFILE,
    PREPARE,
    SAVE,
    SEARCH_EXERCISES,
    STATUS,
    Fixture,
    envelope,
    message_text,
    new_id,
    payload,
    prepare_arguments,
    propose,
)
from test.regression_support import temporary_root

EVIDENCE = temporary_root("business-profile")

EXERCISE_FIELDS = {
    "id",
    "name",
    "body_part",
    "equipment",
    "target",
    "muscle_group",
    "secondary_muscles",
    "load_convention",
    "steps",
}
WORKOUT_TOOLS = [item.name for item in workout_tool_declarations()]
PLAN_TOOLS = [item.name for item in plan_tool_declarations()]
BUSINESS_TOOLS = {GET_PROFILE, SEARCH_EXERCISES, PREPARE, SAVE, STATUS, *WORKOUT_TOOLS, *PLAN_TOOLS}


async def check_catalog(directory: Path) -> dict:
    catalog = Catalog.load()
    assert len(catalog) == 1324
    first = catalog.get("0001")
    assert first is not None and first.id == "0001"
    assert first.steps.zh and first.steps.en
    assert catalog.get("9999") is None
    bench = catalog.search("卧推", equipment="barbell")
    assert bench and all("卧推" in item.name for item in bench)
    assert all(item.equipment == "barbell" for item in bench)

    database = await open_database(directory / "catalog.db")
    repository = SqliteBusinessRepository(database)
    try:
        assert await repository.count_exercises() == 0
        async with repository.transaction():
            await repository.replace_exercises(catalog.all())
        assert await repository.count_exercises() == 1324
        # 只读目录整体替换：重复导入不叠加行数。
        async with repository.transaction():
            await repository.replace_exercises(catalog.all())
        assert await repository.count_exercises() == 1324
    finally:
        await database.close()
    return {"count": len(catalog), "bench": [item.id for item in bench]}


async def check_declarations() -> dict:
    declarations = business_tool_declarations()
    assert {item.name for item in declarations} == BUSINESS_TOOLS
    assert [item.name for item in declarations] == [
        GET_PROFILE,
        SEARCH_EXERCISES,
        PREPARE,
        SAVE,
        STATUS,
        *WORKOUT_TOOLS,
        *PLAN_TOOLS,
    ]
    profile = profile_tool_declarations()
    exercises = exercise_tool_declarations()
    assert {item.name for item in profile} == {GET_PROFILE, PREPARE, SAVE, STATUS}
    assert [item.name for item in exercises] == [SEARCH_EXERCISES]
    assert {item.name: item for item in profile + exercises + workout_tool_declarations() + plan_tool_declarations()} == DECLARED
    serialized = json.dumps(
        [item.model_dump() for item in profile + exercises + workout_tool_declarations()], ensure_ascii=False
    )
    # 旧确认卡片工具与卡片协议字段整体移除。
    for stale in ("confirm_profile_update", "replaces_id", "confirmation_id", '"kind"'):
        assert stale not in serialized, stale
    prepare = DECLARED[PREPARE].parameters
    target = prepare["properties"]["profile_id"]
    assert target["type"] == "integer"
    assert (target.get("minimum"), target.get("maximum")) == (1, 1)
    assert set(prepare["required"]) == {
        "profile_id",
        "base_profile_version",
        "payload",
    }
    assert prepare["additionalProperties"] is False
    assert prepare["properties"]["payload"]["additionalProperties"] is False
    save = DECLARED[SAVE].parameters
    assert set(save["required"]) == {
        "proposal_id",
        "display_entry_id",
        "confirmation_entry_id",
    }
    assert save["additionalProperties"] is False
    assert set(DECLARED[STATUS].parameters["required"]) == {"proposal_id"}
    return {
        "tools": sorted(item.name for item in declarations),
        "prepare_required": sorted(prepare["required"]),
        "profile_id_schema": target,
    }


async def check_read_paths(root: Path) -> dict:
    fixture = await Fixture(root / "read-paths.db").seeded()
    try:
        session_id = await fixture.session("画像读取会话")
        context = fixture.context(session_id, session_id, session_id)

        empty = await fixture.invoke(GET_PROFILE, {}, context)
        assert empty.is_error is False
        assert envelope(empty) == {"version": None, "content": None}
        assert (await fixture.business.get_profile()).model_dump() == {
            "version": None,
            "content": None,
        }

        searched = await fixture.invoke(
            SEARCH_EXERCISES, {"name": "卧推", "equipment": "barbell"}, context
        )
        assert searched.is_error is False
        exercises = json.loads(message_text(searched))
        assert exercises and all(set(item) == EXERCISE_FIELDS for item in exercises)

        # 检索参数校验：空白名称、缺失名称与未定义字段在参数层拒绝。
        rejected = []
        for arguments in (
            {"name": "   "},
            {"name": "卧推", "kind": "push"},
            {},
        ):
            message = await fixture.invoke(SEARCH_EXERCISES, arguments, context)
            assert message.is_error is True
            rejected.append(message_text(message)[:40])

        # 保存与查询的参数格式：非标准 UUID、缺失字段与多余字段均拒绝。
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        for arguments in (
            {
                "proposal_id": "not-a-uuid",
                "display_entry_id": ids["display"],
                "confirmation_entry_id": ids["confirmation"],
            },
            {"proposal_id": ids["proposal"]},
            {"proposal_id": ids["proposal"], "payload": payload()},
        ):
            message = await fixture.invoke(SAVE, arguments, context)
            assert message.is_error is True
        for arguments in ({}, {"proposal_id": "not-a-uuid"}):
            message = await fixture.invoke(STATUS, arguments, context)
            assert message.is_error is True
        # 参数层拒绝不产生任何画像写入。
        assert (await fixture.business.get_profile()).version is None
        return {"search_count": len(exercises), "rejected": rejected}
    finally:
        await fixture.close()


async def check_argument_preparation(root: Path) -> dict:
    fixture = await Fixture(root / "argument-preparation.db").seeded()
    try:
        session_id = await fixture.session("画像参数预处理")
        arguments = prepare_arguments("None", payload(health_notes="None"))
        original = deepcopy(arguments)
        ids = await propose(fixture, session_id, arguments)
        assert arguments == original
        result = envelope(ids["result_message"])
        assert result["base_profile_version"] is None
        assert result["payload"]["health_notes"] == "None"
        snapshot = await fixture.snapshot(ids["proposal"])
        assert snapshot.base_profile_version is None
        assert snapshot.payload.health_notes == "None"
        assert (await fixture.business.get_profile()).version is None

        context = fixture.save_context(ids)
        tools = bind_business_tools(fixture.business, context, fixture.call, {}, {})
        assert tools[PREPARE].prepare_arguments is not None
        assert tools[PREPARE].prepare_arguments.__module__ == "app.agent.tools.profile"
        assert tools[SEARCH_EXERCISES].execute.__module__ == "app.agent.tools.exercises"
        assert list(tools) == list(DECLARED)
        assert all(
            tool.prepare_arguments is None
            for name, tool in tools.items()
            if name not in {PREPARE, "prepare_workout"}
        )
        for version in (None, 1, 2):
            valid = prepare_arguments(version, payload())
            assert tools[PREPARE].prepare_arguments(deepcopy(valid)) == valid

        invalid = [
            prepare_arguments(value, payload())
            for value in ("null", "none", " None ", "1", 0, True, 1.5)
        ]
        invalid.extend(
            [
                {"profile_id": 1, "payload": payload()},
                {**arguments, "profile_id": "1"},
                {**arguments, "extra": "None"},
                prepare_arguments("None", payload(unavailable_equipment="None")),
            ]
        )
        for value in invalid:
            message = await fixture.invoke(PREPARE, value, context)
            assert message.is_error is True, value
            assert "参数校验失败" in message_text(message), message_text(message)
        assert await fixture.status_of(ids["proposal"]) == "pending"

        saved = envelope(await fixture.tool_save(ids))
        assert saved["version"] == 1
        assert saved["content"] == arguments["payload"]
        assert envelope(await fixture.tool_status(ids))["result"] == saved
        assert envelope(await fixture.tool_save(ids)) == saved
        profile = envelope(await fixture.invoke(GET_PROFILE, {}, context))
        assert profile == {"version": 1, "content": arguments["payload"]}
        return {"converted": result, "rejected": len(invalid), "version": 1}
    finally:
        await fixture.close()


async def check_payload_errors(root: Path) -> dict:
    fixture = await Fixture(root / "payload-errors.db").seeded()
    try:
        session_id = await fixture.session("画像校验会话")
        context = fixture.context(session_id, new_id(), new_id())

        async def errors(base, data: dict) -> list[dict]:
            try:
                await fixture.business.prepare_profile_update(
                    context,
                    ProfileProposalArguments.model_validate(
                        prepare_arguments(base, data)
                    ),
                )
            except BusinessError as error:
                assert error.code == "invalid_business_payload", error.detail()
                return error.detail()["errors"]
            raise AssertionError("非法画像未被拒绝")

        # 全空画像在参数构造阶段拒绝，不进入服务与数据库。
        try:
            ProfileProposalArguments.model_validate(
                prepare_arguments(None, dict.fromkeys(payload()))
            )
        except ValidationError as error:
            assert "首次建档" in str(error)
        else:
            raise AssertionError("全空画像参数未被拒绝")

        # 空白文本字段与重复列表项同样由参数 schema 拒绝。
        schema_rejected = []
        for data in (
            payload(goal="   "),
            payload(unavailable_equipment=["barbell", "barbell"]),
            payload(forbidden_exercise_ids=["0001", "  "]),
        ):
            message = await fixture.invoke(
                PREPARE, prepare_arguments(None, data), context
            )
            assert message.is_error is True
            schema_rejected.append(message_text(message)[:48])

        # 目录引用校验在服务层完成：返回字段路径与说明。
        references = await errors(None, payload(unavailable_equipment=["不存在器械"]))
        assert references[0]["path"] == "/payload/unavailable_equipment/0"
        bad_id = await errors(None, payload(forbidden_exercise_ids=["9999"]))
        assert bad_id[0]["path"] == "/payload/forbidden_exercise_ids/0"

        # 合法目录引用可以保存：器械标识与动作 ID 均存在于目录。
        valid = payload(
            unavailable_equipment=["barbell"], forbidden_exercise_ids=["0001"]
        )
        ids = await propose(fixture, session_id, prepare_arguments(None, valid))
        saved = envelope(await fixture.tool_save(ids))
        assert saved["version"] == 1 and saved["content"] == valid
        profile = await fixture.business.get_profile()
        assert profile.content.model_dump() == valid
        return {
            "paths": [item[0]["path"] for item in (references, bad_id)],
            "schema_rejected": schema_rejected,
            "saved": saved,
        }
    finally:
        await fixture.close()


async def check_long_output_and_credentials(root: Path) -> dict:
    fixture = await Fixture(root / "long-output.db").seeded()
    try:
        session_id = await fixture.session("长画像会话")
        first = await propose(fixture, session_id, prepare_arguments(None, payload()))
        long_notes = "伤病情况" * 15000
        ids = await propose(
            fixture,
            session_id,
            prepare_arguments(None, payload(health_notes=long_notes)),
        )
        context = fixture.save_context(ids)
        tools = bind_business_tools(fixture.business, context, fixture.call, {}, {})
        declared = {name: tool.definition() for name, tool in tools.items()}
        assert declared == DECLARED
        # 画像结果为业务事实：完整可解析，禁止截断。
        assert all(tool.max_output_chars is None for tool in tools.values())
        assert tools[PREPARE].execution_mode == "sequential"
        assert tools[SAVE].execution_mode == "sequential"
        assert tools[STATUS].execution_mode == "parallel"
        assert tools[GET_PROFILE].execution_mode == "parallel"

        # 超过默认截断长度的合法画像：准备结果仍是单个完整 JSON 块。
        prepared = ids["result_message"]
        assert len(prepared.content) == 1
        assert len(message_text(prepared)) > 50_000
        assert len(envelope(prepared)["payload"]["health_notes"]) == len(long_notes)
        # 新快照使同会话同目标画像的旧待确认快照失效。
        assert await fixture.status_of(first["proposal"]) == "invalidated"

        saved = envelope(await fixture.tool_save(ids))
        assert saved["version"] == 1
        assert len(saved["content"]["health_notes"]) == len(long_notes)
        queried = await fixture.invoke(GET_PROFILE, {}, context)
        assert len(queried.content) == 1
        assert len(envelope(queried)["content"]["health_notes"]) == len(long_notes)

        # 截断区之后的模型凭据同样命中：结果不公开也不保存。
        from app.model_config import load_model_config

        secret = load_model_config().OPENAI_API_KEY
        guarded = await run_tool_batch(
            [
                ToolCall(
                    type="toolCall",
                    id="call-guarded",
                    name=PREPARE,
                    arguments=prepare_arguments(
                        1, payload(health_notes="肩" * 60_000 + secret)
                    ),
                )
            ],
            tools=tools,
            declared=declared,
            contains_credentials=lambda text: secret in text,
        )
        assert isinstance(guarded.failure, CredentialDetectedError), guarded.failure
        assert guarded.messages == []
        assert (await fixture.business.get_profile()).version == 1
        return {
            "long_chars": len(message_text(prepared)),
            "credential_guard": type(guarded.failure).__name__,
        }
    finally:
        await fixture.close()


async def check_concurrent_save(root: Path) -> dict:
    fixture = await Fixture(root / "concurrent.db").seeded()
    try:
        session_id = await fixture.session("并发保存会话")
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        outcomes = await asyncio.gather(
            fixture.service_save(ids),
            fixture.service_save(ids),
            return_exceptions=True,
        )
        saved = [item for item in outcomes if hasattr(item, "version")]
        rejected = [item for item in outcomes if isinstance(item, ProfileUpdateProcessing)]
        # 并发确认只落地一次：一方完成保存，另一方按处理中拒绝。
        assert len(saved) == 1 and len(rejected) == 1, outcomes
        assert saved[0].version == 1
        assert await fixture.profile_state() == {
            "version": 1,
            "profile_rows": 1,
            "record_rows": 1,
        }
        assert envelope(await fixture.tool_save(ids)) == saved[0].model_dump()
        return {"saved": saved[0].model_dump(), "loser": rejected[0].code}
    finally:
        await fixture.close()


class _StatementSync:
    # SQLite 语句同步点：连接线程在目标语句执行前暂停，据此把替换意图登记进保存
    # 事务的真实窗口，形成可复现的提交竞争边界。回调异常会被 CPython 吞掉，
    # 超时状态记录后交由主线程断言。
    def __init__(self, prefix: str, nth: int = 1) -> None:
        self.prefix = prefix
        self.nth = nth
        self.entered = Event()
        self.release = Event()
        self.released = False
        self.matches = 0

    def trace(self, sql: str) -> None:
        if self.entered.is_set() or not sql.startswith(self.prefix):
            return
        self.matches += 1
        if self.matches < self.nth:
            return
        self.entered.set()
        self.released = self.release.wait(30)


@asynccontextmanager
async def _paused_save(
    fixture: Fixture, sync: _StatementSync, operation: Callable[[], object]
) -> AsyncIterator[asyncio.Task]:
    # 在保存事务的 COMMIT 语句处暂停：画像写入已落库、提交尚未落地。
    await fixture.database.connection.set_trace_callback(sync.trace)
    task = asyncio.create_task(operation())
    try:
        assert await asyncio.to_thread(sync.entered.wait, 30)
        yield task
    finally:
        await fixture.database.connection.set_trace_callback(None)
        assert sync.released, "SQLite 语句同步点超时"


def _regenerate(fixture: Fixture, session_id: str, target_entry_id: str):
    return fixture.service.accept_regenerate(
        RegenerateCommand(
            operation_id=new_id(),
            session_id=session_id,
            request=RegenerateRequest(target_entry_id=target_entry_id),
        )
    )


async def check_replacement_wins(root: Path) -> dict:
    # 保存事务提交瞬间登记替换意图：COMMIT 被否决、画像写入随事务回滚，
    # 快照随消息移除后按不存在拒绝，画像保持不变。
    fixture = await Fixture(root / "replacement-wins.db").seeded()
    sync = _StatementSync("COMMIT", nth=2)
    try:
        session_id = await fixture.session("替换优先会话")
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        async with _paused_save(
            fixture, sync, lambda: fixture.service_save(ids)
        ) as task:
            assert fixture.database.connection.in_transaction
            async with fixture.replacements.register(session_id):
                assert fixture.replacements.pending_count(session_id) == 1
                replacement = asyncio.create_task(
                    _regenerate(fixture, session_id, ids["request"])
                )
                sync.release.set()
                outcome = await replacement
                await fixture.service.finish_run(session_id, outcome.run.id, "completed")
        detail = await _detail(task)
        assert detail["code"] == "profile_proposal_not_found", detail
        assert "errors" not in detail
        assert await fixture.repository.list_snapshots(session_id) == []
        assert await fixture.repository.get_save_record(ids["proposal"]) is None
        assert await fixture.profile_state() == {
            "version": None,
            "profile_rows": 0,
            "record_rows": 0,
        }
        return {"save_error": detail["code"], "profile_version": None}
    finally:
        await fixture.close()


async def _detail(awaitable) -> dict:
    try:
        await awaitable
    except BusinessError as error:
        return error.detail()
    raise AssertionError("expected BusinessError")


async def check_replacement_fails(root: Path) -> dict:
    # 替换受理事务失败保持原消息与快照完整；提交让位后重新校验并一次完成保存。
    fixture = await Fixture(root / "replacement-fails.db").seeded()
    sync = _StatementSync("COMMIT", nth=2)
    try:
        session_id = await fixture.session("替换失败会话")
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        async with _paused_save(
            fixture, sync, lambda: fixture.service_save(ids)
        ) as task:
            async with fixture.replacements.register(session_id):
                sync.release.set()
                failed = False
                try:
                    await _regenerate(fixture, session_id, new_id())
                except EntryNotFound:
                    failed = True
                assert failed
        result = await task
        assert result.version == 1
        assert await fixture.status_of(ids["proposal"]) == "saved"
        assert await fixture.profile_state() == {
            "version": 1,
            "profile_rows": 1,
            "record_rows": 1,
        }
        return {"replacement_failed": "saved", "version": result.version}
    finally:
        await fixture.close()


async def check_commit_before_replacement(root: Path) -> dict:
    # 提交已落地后才登记替换意图：已完成保存的结果保持有效，
    # 幂等记录随消息删除仍保留归属与固定结果。
    fixture = await Fixture(root / "commit-first.db").seeded()
    try:
        session_id = await fixture.session("提交优先会话")
        ids = await propose(fixture, session_id, prepare_arguments(None, payload()))
        saved = await fixture.service_save(ids)
        assert saved.version == 1
        async with fixture.replacements.register(session_id):
            outcome = await _regenerate(fixture, session_id, ids["request"])
            await fixture.service.finish_run(session_id, outcome.run.id, "completed")
        assert await fixture.repository.list_snapshots(session_id) == []
        record = await fixture.repository.get_save_record(ids["proposal"])
        assert record is not None and record.result.model_dump() == saved.model_dump()
        assert await fixture.business.list_confirmation_bindings(session_id) == {
            ids["confirmation"]: ids["proposal"]
        }
        assert await fixture.business.list_display_bindings(session_id) == {}
        repeated = await fixture.service_save(ids)
        assert repeated.model_dump() == saved.model_dump()
        assert (await fixture.business.get_profile()).version == 1
        return {
            "commit_first": "record_kept",
            "repeat": repeated.model_dump(),
            "record_saved_at": record.saved_at,
        }
    finally:
        await fixture.close()


async def check_migration(root: Path) -> dict:
    # 旧库逐版本迁移后重开，通过当前结构校验。
    path = root / "migrate.db"
    connection = await aiosqlite.connect(path, isolation_level=None)
    try:
        for version in (1, 2, 3):
            await apply_schema(
                connection, _MIGRATIONS[version].read_text(encoding="utf-8"), version
            )
    finally:
        await connection.close()
    database = await open_database(path)
    await database.close()
    reopened = await open_database(path)
    await reopened.close()
    assert SCHEMA_VERSION == 7
    return {"from": 3, "to": SCHEMA_VERSION}


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    with TemporaryDirectory(dir=EVIDENCE, ignore_cleanup_errors=True) as directory:
        root = Path(directory)
        evidence["migration"] = asyncio.run(check_migration(root))
        evidence["catalog"] = asyncio.run(check_catalog(root))
        evidence["declarations"] = asyncio.run(check_declarations())
        evidence["read_paths"] = asyncio.run(check_read_paths(root))
        evidence["argument_preparation"] = asyncio.run(check_argument_preparation(root))
        evidence["payload_errors"] = asyncio.run(check_payload_errors(root))
        evidence["long_output"] = asyncio.run(check_long_output_and_credentials(root))
        evidence["concurrent_save"] = asyncio.run(check_concurrent_save(root))
        evidence["replacement_wins"] = asyncio.run(check_replacement_wins(root))
        evidence["replacement_failed"] = asyncio.run(check_replacement_fails(root))
        evidence["commit_first"] = asyncio.run(check_commit_before_replacement(root))
    (EVIDENCE / "business-profile.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        "PASS: 版本 7 迁移、动作目录导入与检索、统一工具声明与参数 schema、画像读取与"
        "参数校验、None 字符串转换与完整保存流程、目录引用与字段级错误、"
        "超长画像完整输出、凭据保护、并发保存幂等、"
        "提交竞争边界与替换优先级、幂等记录保留"
    )


if __name__ == "__main__":
    check()
