import asyncio
import re
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.ai.messages import Message, SystemMessage, serialize_message
from app.application.session.service import (
    CREDENTIAL_SAFE_MESSAGE,
    SessionService,
)
from app.domain.session.errors import (
    CredentialDetected,
    EntryNotFound,
    InvalidTargetEntry,
    OperationExpired,
    SessionConflict,
    SessionNotFound,
)
from app.domain.session.models import (
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    SendCommand,
    SendRequest,
    Session,
    SessionMessageEntry,
    SteeringCommand,
    SteeringRequest,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository

_adapter = TypeAdapter(Message)

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "session-store"

SEND_OP = str(uuid4())
ST2_OP = str(uuid4())
CONC_OP = str(uuid4())
RESTORE_OP = str(uuid4())
BLOCKED_OP = str(uuid4())
REJECTED_OP = str(uuid4())

_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

SYSTEM_SOURCE = {
    "role": "system",
    "content": [
        {"type": "text", "text": "instructions", "text_signature": "sig"}
    ],
    "sections": {"active": "指令片段", "removed": None},
    "tools_added": [
        {
            "name": "lookup",
            "description": "lookup data",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "examples": ["a", {"b": 1}]}
                },
                "required": ["query"],
                "additionalProperties": False,
                "x-provider": {"nested": [None, True, 2.5]},
            },
            "constrained_sampling": {
                "type": "grammar",
                "variants": {"openai_regex": "[a-z]+"},
            },
        }
    ],
    "tools_removed": [{"name": "old-tool"}],
    "timestamp": 1.5,
}
USER_SOURCE = {
    "role": "user",
    "content": [
        {"type": "text", "text": "问题"},
        {"type": "image", "data": "base64", "mime_type": "image/png"},
    ],
    "timestamp": 2,
}
ASSISTANT_SOURCE = {
    "role": "assistant",
    "content": [
        {
            "type": "thinking",
            "thinking": "plan",
            "thinking_signature": "enc",
            "redacted": True,
        },
        {
            "type": "toolCall",
            "id": "call-1",
            "name": "lookup",
            "arguments": {"query": "a", "nested": {"values": [1, None]}},
            "thought_signature": "thought-signature",
            "namespace": "tools",
        },
        {
            "type": "text",
            "text": "done",
            "text_signature": '{"v":1,"id":"response-1"}',
        },
    ],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "response_model": "actual-model",
    "response_id": "response-1",
    "usage": {
        "input": 1,
        "output": 2,
        "cache_read": 3,
        "cache_write": 4,
        "cache_write_1h": 5,
        "reasoning": 1,
        "total_tokens": 10,
        "cost": {
            "input": 0.1,
            "output": 0.2,
            "cache_read": 0.3,
            "cache_write": 0.4,
            "total": 1,
        },
    },
    "stop_reason": "toolUse",
    "diagnostics": [
        {
            "type": "provider",
            "timestamp": 3,
            "error": {
                "name": "Error",
                "message": "safe",
                "stack": "redacted-stack",
                "code": 7.5,
            },
            "details": {"response": {"status": 429}},
        }
    ],
    "deferred": {
        "provider": "example",
        "model_id": "model-1",
        "api": "custom",
        "id": "task-1",
        "expires_at": 123.5,
        "poll_after_ms": 2.5,
        "data": {"cursor": [1, {"next": None}]},
    },
    "timestamp": 3,
}
ASSISTANT_FINAL_SOURCE = {
    "role": "assistant",
    "content": [{"type": "text", "text": "done", "text_signature": '{"v":1}'}],
    "api": "openai-completions",
    "provider": "example",
    "model": "model-1",
    "usage": {
        "input": 1,
        "output": 2,
        "cache_read": 3,
        "cache_write": 4,
        "total_tokens": 10,
    },
    "stop_reason": "stop",
    "timestamp": 5,
}
TOOL_RESULT_SOURCE = {
    "role": "toolResult",
    "tool_call_id": "call-1",
    "tool_name": "lookup",
    "content": [
        {"type": "text", "text": "result"},
        {"type": "image", "data": "base64", "mime_type": "image/jpeg"},
    ],
    "is_error": False,
    "timestamp": 4,
    "details": None,
    "nested_calls": {
        "calls": [
            {
                "id": "nested-1",
                "name": "fetch",
                "arguments": {"id": 9},
                "arguments_bytes": 8.5,
                "status": "ok",
                "duration_ms": 2.5,
                "error": "safe-error",
            }
        ],
        "complete": True,
    },
}


def message(source: dict, **overrides: object) -> Message:
    return _adapter.validate_python({**deepcopy(source), **overrides})


def system_message() -> SystemMessage:
    return _adapter.validate_python(deepcopy(SYSTEM_SOURCE))


def entry(
    session_id: str,
    entry_id: str,
    parent_id: str | None,
    run_id: str | None,
    payload: Message,
    created_at: int,
) -> SessionMessageEntry:
    return SessionMessageEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="message",
        messages=[payload],
        created_at=created_at,
    )


async def assert_foreign_keys(database) -> None:
    async def run(connection):
        cursor = await connection.execute("PRAGMA foreign_key_check")
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    assert await database.read(run) == []


async def open_store(path: Path):
    database = await open_database(path)
    repository = SqliteSessionRepository(database)
    return database, repository, SessionService(repository)


async def scenario_session(service, repository) -> None:
    session = await service.create_session("s-basic", "基础会话")
    assert session.active_leaf_id is None and session.created_at == session.updated_at
    assert (await service.get_session("s-basic")).title == "基础会话"
    assert [item.id for item in await service.list_sessions()] == ["s-basic"]

    # 同 ID 同标题重试保持幂等，返回原会话且不新增记录
    repeat = await service.create_session("s-basic", "基础会话")
    assert repeat.model_dump() == session.model_dump()
    assert [item.id for item in await service.list_sessions()] == ["s-basic"]

    try:
        await service.create_session("s-basic", "重复")
    except SessionConflict:
        pass
    else:
        raise AssertionError("重复会话 ID 未冲突")
    try:
        await service.get_session("missing")
    except SessionNotFound:
        pass
    else:
        raise AssertionError("未知会话未报错")
    assert await repository.get_session("missing") is None

    await service.create_session("s-basic-2", "第二个会话")
    ids = [item.id for item in await service.list_sessions()]
    assert ids == ["s-basic", "s-basic-2"]

    # 并发同 ID 同标题只产生一条会话
    concurrent = await asyncio.gather(
        service.create_session("s-conc-create", "并发创建"),
        service.create_session("s-conc-create", "并发创建"),
    )
    assert concurrent[0].model_dump() == concurrent[1].model_dump()
    assert (
        len(
            [
                item
                for item in await service.list_sessions()
                if item.id == "s-conc-create"
            ]
        )
        == 1
    )


async def scenario_send(service, repository) -> None:
    await service.create_session("s-send", "发送")
    outcome = await service.accept_send(
        SendCommand(
            operation_id=SEND_OP,
            session_id="s-send",
            request=SendRequest(text="你好"),
        ),
        system_message=system_message(),
    )
    assert outcome.created is True and outcome.steering is None
    run = outcome.run
    assert run is not None and run.status == "running" and run.last_entry_id is None

    user = await service.get_entry("s-send", run.request_entry_id)
    assert _UUID_PATTERN.fullmatch(run.id) is not None
    assert _UUID_PATTERN.fullmatch(user.id) is not None
    assert user.messages[0].role == "user" and user.messages[0].content == "你好"
    assert user.parent_id is not None and user.run_id is None
    system_entry = await service.get_entry("s-send", user.parent_id)
    assert system_entry.parent_id is None
    assert serialize_message(system_entry.messages[0]) == serialize_message(
        system_message()
    )
    session = await service.get_session("s-send")
    assert session.active_leaf_id == user.id

    operation = await service.get_operation(SEND_OP)
    assert operation is not None and operation.kind == "send"
    assert operation.run_id == run.id and operation.request == {"text": "你好"}
    assert operation.session_id == "s-send"

    # 同键同请求只产生一份初始数据
    duplicate = await service.accept_send(
        SendCommand(
            operation_id=SEND_OP,
            session_id="s-send",
            request=SendRequest(text="你好"),
        ),
        system_message=system_message(),
    )
    assert duplicate.created is False and duplicate.run.id == run.id
    assert len(await service.list_entries("s-send")) == 2
    assert len(await service.list_runs("s-send")) == 1

    # 同键不同请求冲突，已有数据完整
    try:
        await service.accept_send(
            SendCommand(
                operation_id=SEND_OP,
                session_id="s-send",
                request=SendRequest(text="不同"),
            ),
            system_message=system_message(),
        )
    except SessionConflict:
        pass
    else:
        raise AssertionError("同键不同请求未冲突")
    assert len(await service.list_entries("s-send")) == 2

    # 追加助手消息：节点、指针、last_entry_id 一并推进
    appended = await service.append_entry(
        entry("s-send", "sd-a1", user.id, run.id, message(ASSISTANT_FINAL_SOURCE), 500)
    )
    assert appended.created is True and appended.entry.id == "sd-a1"
    assert (await service.get_session("s-send")).active_leaf_id == "sd-a1"
    assert (await service.get_run("s-send", run.id)).last_entry_id == "sd-a1"

    # 重复节点追加保持原位置与进度
    repeat = await service.append_entry(
        entry("s-send", "sd-a1", user.id, run.id, message(ASSISTANT_FINAL_SOURCE), 999)
    )
    assert repeat.created is False and repeat.entry.id == "sd-a1"
    assert (await service.get_session("s-send")).active_leaf_id == "sd-a1"
    assert (await service.get_run("s-send", run.id)).last_entry_id == "sd-a1"

    # 同键冲突：节点身份一致但内容不同
    try:
        await service.append_entry(
            entry(
                "s-send",
                "sd-a1",
                user.id,
                run.id,
                message(ASSISTANT_FINAL_SOURCE, timestamp=77),
                501,
            )
        )
    except SessionConflict:
        pass
    else:
        raise AssertionError("节点内容不一致未冲突")

    added = await service.append_entry(
        entry("s-send", "sd-a2", "sd-a1", run.id, message(ASSISTANT_FINAL_SOURCE), 502)
    )
    assert added.created is True

    finished = await service.finish_run("s-send", run.id, "completed")
    assert finished.changed is True and finished.run.status == "completed"
    assert finished.run.finished_at is not None
    repeat_finish = await service.finish_run("s-send", run.id, "completed")
    assert repeat_finish.changed is False
    try:
        await service.finish_run("s-send", run.id, "failed")
    except SessionConflict:
        pass
    else:
        raise AssertionError("冲突终态未拒绝")


async def scenario_branching(service) -> None:
    await service.create_session("s-branch", "分支")
    send_op = str(uuid4())
    send = await service.accept_send(
        SendCommand(
            operation_id=send_op,
            session_id="s-branch",
            request=SendRequest(text="初始"),
        ),
        system_message=system_message(),
    )
    run = send.run
    user1 = run.request_entry_id
    system_id = (await service.get_entry("s-branch", user1)).parent_id
    await service.append_entry(
        entry("s-branch", "br-a1", user1, run.id, message(ASSISTANT_FINAL_SOURCE), 600)
    )
    await service.finish_run("s-branch", run.id, "completed")

    # 编辑：删除目标用户消息及全部后续内容，保存修改后的用户节点。
    edit_op = str(uuid4())
    edit = await service.accept_edit(
        EditCommand(
            operation_id=edit_op,
            session_id="s-branch",
            request=EditRequest(target_entry_id=user1, text="修改后"),
        )
    )
    assert edit.created is True
    edited = await service.get_entry("s-branch", edit.run.request_entry_id)
    assert edited.parent_id == system_id
    assert edited.messages[0].content == "修改后"
    for removed in (user1, "br-a1"):
        try:
            await service.get_entry("s-branch", removed)
        except EntryNotFound:
            pass
        else:
            raise AssertionError("编辑未删除被替换内容")
    assert [item.id for item in await service.get_current_branch("s-branch")] == [
        system_id,
        edited.id,
    ]
    assert [item.id for item in await service.list_runs("s-branch")] == [edit.run.id]
    assert await service.get_operation(send_op) is None
    assert await service.get_operation_invalidation(send_op) == "s-branch"
    try:
        await service.resolve_operation(
            send_op, "s-branch", "send", SendRequest(text="初始")
        )
    except OperationExpired:
        pass
    else:
        raise AssertionError("失效编号未拒绝重试")

    await service.append_entry(
        entry(
            "s-branch",
            "br-a2",
            edited.id,
            edit.run.id,
            message(ASSISTANT_FINAL_SOURCE, content=[{"type": "text", "text": "修改回答"}]),
            601,
        )
    )
    await service.finish_run("s-branch", edit.run.id, "completed")

    # 重新生成：保留目标及其祖先，删除目标之后的全部内容。
    regenerate = await service.accept_regenerate(
        RegenerateCommand(
            operation_id=str(uuid4()),
            session_id="s-branch",
            request=RegenerateRequest(target_entry_id=edited.id),
        )
    )
    assert regenerate.created is True
    assert (await service.get_session("s-branch")).active_leaf_id == edited.id
    assert regenerate.run.request_entry_id == edited.id
    try:
        await service.get_entry("s-branch", "br-a2")
    except EntryNotFound:
        pass
    else:
        raise AssertionError("重新生成未删除目标之后的回答")
    # 目标之后的内容删除后已无留存节点引用原编辑运行：运行及其操作记录一并清理并失效。
    assert [item.id for item in await service.list_runs("s-branch")] == [
        regenerate.run.id
    ]
    assert await service.get_operation(edit_op) is None
    assert await service.get_operation_invalidation(edit_op) == "s-branch"
    try:
        await service.get_run("s-branch", edit.run.id)
    except SessionNotFound:
        pass
    else:
        raise AssertionError("无留存节点的旧运行未清理")
    assert [item.id for item in await service.get_current_branch("s-branch")] == [
        system_id,
        edited.id,
    ]

    await service.append_entry(
        entry(
            "s-branch",
            "br-a3",
            edited.id,
            regenerate.run.id,
            message(ASSISTANT_FINAL_SOURCE, content=[{"type": "text", "text": "重新生成"}]),
            602,
        )
    )
    assert [item.id for item in await service.get_current_branch("s-branch")] == [
        system_id,
        edited.id,
        "br-a3",
    ]
    await service.finish_run("s-branch", regenerate.run.id, "completed")

    # 目标节点校验：非用户消息拒绝，缺失节点明确报错。
    try:
        await service.accept_edit(
            EditCommand(
                operation_id=str(uuid4()),
                session_id="s-branch",
                request=EditRequest(target_entry_id=system_id, text="非法"),
            )
        )
    except InvalidTargetEntry:
        pass
    else:
        raise AssertionError("非用户目标未拒绝")
    try:
        await service.accept_regenerate(
            RegenerateCommand(
                operation_id=str(uuid4()),
                session_id="s-branch",
                request=RegenerateRequest(target_entry_id=str(uuid4())),
            )
        )
    except EntryNotFound:
        pass
    else:
        raise AssertionError("缺失目标未报错")

    # 已消费 Steering 用户节点作为重新生成目标：祖先及同运行留存的助手节点保留。
    await scenario_steering_target(service)


async def scenario_steering_target(service) -> None:
    await service.create_session("s-steer-target", "转向目标")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-steer-target",
            request=SendRequest(text="开始"),
        ),
        system_message=system_message(),
    )
    run = send.run
    await service.append_entry(
        entry(
            "s-steer-target",
            "st-a1",
            run.request_entry_id,
            run.id,
            message(ASSISTANT_FINAL_SOURCE),
            700,
        )
    )
    steering = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id="s-steer-target",
            request=SteeringRequest(target_run_id=run.id, text="追加"),
        )
    )
    consumed = await service.consume_steering(
        "s-steer-target", run.id, steering.steering.id
    )
    assert consumed.created is True and consumed.entry is not None
    steering_user = consumed.entry.id
    await service.append_entry(
        entry(
            "s-steer-target",
            "st-a2",
            steering_user,
            run.id,
            message(ASSISTANT_FINAL_SOURCE),
            701,
        )
    )
    await service.finish_run("s-steer-target", run.id, "completed")

    regenerate = await service.accept_regenerate(
        RegenerateCommand(
            operation_id=str(uuid4()),
            session_id="s-steer-target",
            request=RegenerateRequest(target_entry_id=steering_user),
        )
    )
    assert regenerate.created is True
    assert regenerate.run.request_entry_id == steering_user
    # 目标保留；目标之后的助手节点删除；同运行的祖先助手节点保留，运行进度回退。
    assert (await service.get_entry("s-steer-target", steering_user)).messages[0].content == "追加"
    try:
        await service.get_entry("s-steer-target", "st-a2")
    except EntryNotFound:
        pass
    else:
        raise AssertionError("转向目标之后的回答未删除")
    retained = await service.get_run("s-steer-target", run.id)
    assert retained.last_entry_id == steering_user
    await service.finish_run("s-steer-target", regenerate.run.id, "completed")


async def scenario_unpaired(service, repository) -> None:
    await service.create_session("s-unpaired", "未配对")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-unpaired",
            request=SendRequest(text="调用工具"),
        ),
        system_message=system_message(),
    )
    run = send.run
    await service.append_entry(
        entry(
            "s-unpaired",
            "up-a1",
            run.request_entry_id,
            run.id,
            message(ASSISTANT_SOURCE),
            700,
        )
    )
    # 历史读取允许工具调用暂缺结果
    leaf = (await service.get_session("s-unpaired")).active_leaf_id
    context = await service.get_context("s-unpaired", leaf)
    assert len(context) == 3
    await service.finish_run("s-unpaired", run.id, "cancelled")

    before = len(await service.list_entries("s-unpaired"))
    try:
        await service.accept_send(
            SendCommand(
                operation_id=BLOCKED_OP,
                session_id="s-unpaired",
                request=SendRequest(text="继续"),
            ),
            system_message=system_message(),
        )
    except SessionConflict:
        pass
    else:
        raise AssertionError("未配对分支未拒绝新执行")
    assert len(await service.list_entries("s-unpaired")) == before
    assert await service.get_operation(BLOCKED_OP) is None
    assert len(await service.list_runs("s-unpaired")) == 1
    assert (await service.get_session("s-unpaired")).active_leaf_id == "up-a1"


async def scenario_steering(service, repository) -> None:
    await service.create_session("s-steer", "转向")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-steer",
            request=SendRequest(text="开始"),
        ),
        system_message=system_message(),
    )
    run = send.run

    recv1 = await service.accept_steering(
        SteeringCommand(
            operation_id=ST2_OP,
            session_id="s-steer",
            request=SteeringRequest(target_run_id=run.id, text="补充一"),
        )
    )
    assert recv1.created is True and recv1.steering.status == "pending"
    assert recv1.run is None
    recv2 = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id="s-steer",
            request=SteeringRequest(target_run_id=run.id, text="补充二"),
        )
    )
    duplicate = await service.accept_steering(
        SteeringCommand(
            operation_id=ST2_OP,
            session_id="s-steer",
            request=SteeringRequest(target_run_id=run.id, text="补充一"),
        )
    )
    assert duplicate.created is False and duplicate.steering.id == recv1.steering.id
    assert len(await service.list_steering("s-steer", run.id)) == 2
    # 接收不推进位置
    assert (await service.get_session("s-steer")).active_leaf_id == run.request_entry_id
    assert (await service.get_run("s-steer", run.id)).last_entry_id is None

    consumed = await service.consume_steering("s-steer", run.id, recv1.steering.id)
    assert consumed.created is True and consumed.steering.status == "consumed"
    assert consumed.entry.messages[0].content == "补充一"
    assert (
        consumed.entry.messages[0].timestamp
        == recv1.steering.message.timestamp
    )
    assert (await service.get_session("s-steer")).active_leaf_id == consumed.entry.id
    assert (await service.get_run("s-steer", run.id)).last_entry_id == consumed.entry.id
    again = await service.consume_steering("s-steer", run.id, recv1.steering.id)
    assert again.created is False and again.entry.id == consumed.entry.id

    withdrawn = await service.withdraw_steering("s-steer", run.id, recv2.steering.id)
    assert withdrawn.changed is True and withdrawn.steering.status == "withdrawn"
    assert (
        await service.withdraw_steering("s-steer", run.id, recv2.steering.id)
    ).changed is False
    assert (
        await service.consume_steering("s-steer", run.id, recv2.steering.id)
    ).entry is None
    try:
        await service.withdraw_steering("s-steer", run.id, recv1.steering.id)
    except SessionConflict:
        pass
    else:
        raise AssertionError("已消费输入未拒绝撤回")

    # 每条输入最多生成一个节点
    steering_nodes = [
        item
        for item in await service.list_entries("s-steer")
        if item.run_id == run.id and item.messages[0].role == "user"
    ]
    assert [item.id for item in steering_nodes] == [consumed.entry.id]

    recv3 = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id="s-steer",
            request=SteeringRequest(target_run_id=run.id, text="补充三"),
        )
    )
    await service.finish_run("s-steer", run.id, "completed")
    discarded = await service.get_steering("s-steer", recv3.steering.id)
    assert discarded.status == "discarded" and discarded.reason == "completed"

    send2 = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-steer",
            request=SendRequest(text="第二轮"),
        ),
        system_message=system_message(),
    )
    run2 = send2.run
    recv4 = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id="s-steer",
            request=SteeringRequest(target_run_id=run2.id, text="中断前"),
        )
    )
    recovered = await service.recover_interrupted()
    assert [item.id for item in recovered] == [run2.id]
    interrupted = await service.get_run("s-steer", run2.id)
    assert interrupted.status == "interrupted" and interrupted.finished_at is None
    dropped = await service.get_steering("s-steer", recv4.steering.id)
    assert dropped.status == "discarded" and dropped.reason == "interrupted"
    assert await service.recover_interrupted() == []


async def scenario_roundtrip(service) -> None:
    await service.create_session("s-rt", "往返")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-rt",
            request=SendRequest(text="问"),
        ),
        system_message=system_message(),
    )
    run = send.run
    user = await service.get_entry("s-rt", run.request_entry_id)
    system_id = user.parent_id
    await service.append_entry(
        entry("s-rt", "rt-a1", user.id, run.id, message(ASSISTANT_SOURCE), 800)
    )
    await service.append_entry(
        entry("s-rt", "rt-t1", "rt-a1", run.id, message(TOOL_RESULT_SOURCE), 801)
    )
    await service.append_entry(
        entry("s-rt", "rt-a2", "rt-t1", run.id, message(ASSISTANT_FINAL_SOURCE), 802)
    )

    stored = {item.id: item for item in await service.list_entries("s-rt")}
    expected = {
        system_id: system_message(),
        user.id: message(
            USER_SOURCE, content="问", timestamp=user.messages[0].timestamp
        ),
        "rt-a1": message(ASSISTANT_SOURCE),
        "rt-t1": message(TOOL_RESULT_SOURCE),
        "rt-a2": message(ASSISTANT_FINAL_SOURCE),
    }
    for key, source in expected.items():
        assert serialize_message(stored[key].messages[0]) == serialize_message(source)
        assert stored[key].messages[0].model_dump(exclude_unset=True) == (
            source.model_dump(exclude_unset=True)
        )

    assistant = stored["rt-a1"].messages[0]
    assert assistant.diagnostics[0].error.code == 7.5
    assert assistant.deferred.data == {"cursor": [1, {"next": None}]}
    tool = stored["rt-t1"].messages[0]
    dumped = tool.model_dump(exclude_unset=True)
    assert "details" in dumped and dumped["details"] is None
    assert "usage" not in dumped
    system = stored[system_id].messages[0]
    assert system.sections == {"active": "指令片段", "removed": None}
    assert system.tools_removed[0].name == "old-tool"
    await service.finish_run("s-rt", run.id, "completed")


async def scenario_credentials(service, repository) -> None:
    await service.create_session("s-cred", "凭据")
    # 用户请求命中：拒绝且不落库，目标运行保持原状态
    try:
        await service.accept_send(
            SendCommand(
                operation_id=REJECTED_OP,
                session_id="s-cred",
                request=SendRequest(text="secret-key-123"),
            ),
            system_message=system_message(),
            credentials=["secret-key-123"],
        )
    except CredentialDetected:
        pass
    else:
        raise AssertionError("用户请求凭据未拒绝")
    assert await service.get_operation(REJECTED_OP) is None
    assert await service.list_entries("s-cred") == []

    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-cred",
            request=SendRequest(text="正常"),
        ),
        system_message=system_message(),
    )
    run = send.run

    # Steering 命中：目标运行状态不变
    try:
        await service.accept_steering(
            SteeringCommand(
                operation_id=str(uuid4()),
                session_id="s-cred",
                request=SteeringRequest(target_run_id=run.id, text="secret-key-123"),
            ),
            credentials=["secret-key-123"],
        )
    except CredentialDetected:
        pass
    else:
        raise AssertionError("Steering 凭据未拒绝")
    assert await service.list_steering("s-cred", run.id) == []
    assert (await service.get_run("s-cred", run.id)).status == "running"

    pending = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id="s-cred",
            request=SteeringRequest(target_run_id=run.id, text="待消费"),
        )
    )
    # 模型输出命中：消息不入库，运行保存安全失败，pending 被丢弃
    outcome = await service.append_entry(
        entry(
            "s-cred",
            "cr-a1",
            run.request_entry_id,
            run.id,
            message(ASSISTANT_FINAL_SOURCE, content=[{"type": "text", "text": "secret-key-123"}]),
            900,
        ),
        credentials=["secret-key-123"],
    )
    assert outcome.credential_detected is True and outcome.entry is None
    assert await repository.get_entry("s-cred", "cr-a1") is None
    failed = await service.get_run("s-cred", run.id)
    assert failed.status == "failed"
    assert failed.error_code == "credential_detected"
    assert failed.error_message == CREDENTIAL_SAFE_MESSAGE
    dropped = await service.get_steering("s-cred", pending.steering.id)
    assert dropped.status == "discarded" and dropped.reason == "failed"


async def scenario_transaction(service, repository, database) -> None:
    async def failing():
        async with repository.transaction():
            await repository.insert_session(
                Session(
                    id="tx-fail",
                    title="x",
                    active_leaf_id=None,
                    created_at=1,
                    updated_at=1,
                )
            )
            raise RuntimeError("boom")

    try:
        await failing()
    except RuntimeError:
        pass
    else:
        raise AssertionError("事务内异常未传播")
    assert await repository.get_session("tx-fail") is None

    # 协程取消：整体回滚并释放事务占用
    entered = asyncio.Event()

    async def hanging():
        async with repository.transaction():
            await repository.insert_session(
                Session(
                    id="tx-cancel",
                    title="x",
                    active_leaf_id=None,
                    created_at=1,
                    updated_at=1,
                )
            )
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(hanging())
    await entered.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("取消未传播")
    assert await repository.get_session("tx-cancel") is None
    await service.create_session("tx-ok", "锁释放")
    assert (await repository.get_session("tx-ok")) is not None

    # 同连接事务不交错
    order: list[tuple[str, int]] = []

    async def worker(index: int) -> None:
        async with repository.transaction():
            order.append(("enter", index))
            await asyncio.sleep(0)
            order.append(("exit", index))

    await asyncio.gather(worker(1), worker(2))
    assert order in (
        [("enter", 1), ("exit", 1), ("enter", 2), ("exit", 2)],
        [("enter", 2), ("exit", 2), ("enter", 1), ("exit", 1)],
    ), order

    # 节点、指针、last_entry_id 原子回滚
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="tx-ok",
            request=SendRequest(text="原子"),
        ),
        system_message=system_message(),
    )
    run = send.run
    leaf = (await service.get_session("tx-ok")).active_leaf_id
    payload = entry(
        "tx-ok", "at-a1", run.request_entry_id, run.id, message(ASSISTANT_FINAL_SOURCE), 1000
    )

    async def failing_append():
        async with repository.transaction():
            await repository.insert_entry(payload)
            await repository.update_run(
                run.model_copy(update={"last_entry_id": "at-a1"})
            )
            raise RuntimeError("boom")

    try:
        await failing_append()
    except RuntimeError:
        pass
    else:
        raise AssertionError("追加失败未传播")
    assert await repository.get_entry("tx-ok", "at-a1") is None
    assert (await service.get_session("tx-ok")).active_leaf_id == leaf
    assert (await service.get_run("tx-ok", run.id)).last_entry_id is None

    # 成功后重复写入保持原值
    first = await service.append_entry(payload)
    assert first.created is True
    kept_leaf = (await service.get_session("tx-ok")).active_leaf_id
    kept_run = (await service.get_run("tx-ok", run.id)).last_entry_id
    second = await service.append_entry(payload)
    assert second.created is False and second.entry.id == "at-a1"
    assert (await service.get_session("tx-ok")).active_leaf_id == kept_leaf
    assert (await service.get_run("tx-ok", run.id)).last_entry_id == kept_run
    await service.finish_run("tx-ok", run.id, "cancelled")


async def scenario_concurrency(service) -> None:
    await service.create_session("s-conc", "并发")
    command = SendCommand(
        operation_id=CONC_OP,
        session_id="s-conc",
        request=SendRequest(text="并发请求"),
    )
    first, second = await asyncio.gather(
        service.accept_send(command, system_message=system_message()),
        service.accept_send(command, system_message=system_message()),
    )
    assert first.run.id == second.run.id
    assert sorted([first.created, second.created]) == [False, True]
    assert len(await service.list_entries("s-conc")) == 2
    assert len(await service.list_runs("s-conc")) == 1

    try:
        await service.accept_send(
            SendCommand(
                operation_id=CONC_OP,
                session_id="s-conc",
                request=SendRequest(text="另一个"),
            ),
            system_message=system_message(),
        )
    except SessionConflict:
        pass
    else:
        raise AssertionError("并发同键不同请求未冲突")
    await service.finish_run("s-conc", first.run.id, "completed")


async def scenario_lifespan_recovery(root: Path) -> None:
    from app.infrastructure.persistence.sqlite import database as database_module
    from app.interfaces import http as http_module

    path = root / "lifespan.db"
    database, _, service = await open_store(path)
    await service.create_session("s-life", "生命周期")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-life",
            request=SendRequest(text="遗留运行"),
        ),
        system_message=system_message(),
    )
    run = send.run
    pending = await service.accept_steering(
        SteeringCommand(
            operation_id=str(uuid4()),
            session_id="s-life",
            request=SteeringRequest(target_run_id=run.id, text="遗留输入"),
        )
    )
    await database.close()

    original = database_module.default_database_path
    database_module.default_database_path = lambda: path
    try:
        async with http_module.lifespan(http_module.app):
            pass
    finally:
        database_module.default_database_path = original

    reopened, _, reopened_service = await open_store(path)
    try:
        interrupted = await reopened_service.get_run("s-life", run.id)
        assert interrupted.status == "interrupted" and interrupted.finished_at is None
        dropped = await reopened_service.get_steering("s-life", pending.steering.id)
        assert dropped.status == "discarded" and dropped.reason == "interrupted"
    finally:
        await reopened.close()


async def scenario_restore(path: Path) -> None:
    database, _, service = await open_store(path)
    await service.create_session("s-restore", "恢复")
    send = await service.accept_send(
        SendCommand(
            operation_id=RESTORE_OP,
            session_id="s-restore",
            request=SendRequest(text="第一问"),
        ),
        system_message=system_message(),
    )
    run = send.run
    user_id = run.request_entry_id
    system_id = (await service.get_entry("s-restore", user_id)).parent_id
    await service.append_entry(
        entry(
            "s-restore",
            "rs-a1",
            user_id,
            run.id,
            message(ASSISTANT_FINAL_SOURCE),
            1100,
        )
    )
    await service.finish_run("s-restore", run.id, "completed")
    await database.close()

    reopened, _, reopened_service = await open_store(path)
    try:
        session = await reopened_service.get_session("s-restore")
        assert session.active_leaf_id == "rs-a1"
        branch = await reopened_service.get_current_branch("s-restore")
        assert [item.id for item in branch] == [system_id, user_id, "rs-a1"]
        assert await reopened_service.get_context("s-restore", "rs-a1") is not None
        duplicate = await reopened_service.accept_send(
            SendCommand(
                operation_id=RESTORE_OP,
                session_id="s-restore",
                request=SendRequest(text="第一问"),
            ),
            system_message=system_message(),
        )
        assert duplicate.created is False and duplicate.run.id == run.id
        operation = await reopened_service.get_operation(RESTORE_OP)
        assert operation is not None and operation.run_id == run.id
        assert len(await reopened_service.list_entries("s-restore")) == 3
        await assert_foreign_keys(reopened)
    finally:
        await reopened.close()


async def scenario_request_validation(service) -> None:
    await service.create_session("s-valid", "校验")
    rejections = [
        lambda: SendRequest(text=""),
        lambda: SendRequest(text="\t  \n"),
        lambda: SendRequest(text="x" * 32001),
        lambda: EditRequest(target_entry_id="e", text=""),
        lambda: EditRequest(target_entry_id="e", text=" \n "),
        lambda: RegenerateRequest(),
        lambda: SteeringRequest(target_run_id="r", text=""),
        lambda: SteeringRequest(target_run_id="r", text="\t"),
        lambda: SendCommand(
            operation_id="not-a-uuid",
            session_id="s-valid",
            request=SendRequest(text="ok"),
        ),
        lambda: EditCommand(
            operation_id="1234",
            session_id="s-valid",
            request=EditRequest(target_entry_id="e", text="ok"),
        ),
        lambda: RegenerateCommand(
            operation_id="zzzz",
            session_id="s-valid",
            request=RegenerateRequest(target_entry_id="e"),
        ),
        lambda: SteeringCommand(
            operation_id="",
            session_id="s-valid",
            request=SteeringRequest(target_run_id="r", text="ok"),
        ),
    ]
    for build in rejections:
        try:
            build()
        except ValidationError:
            continue
        raise AssertionError("非法请求结构未被拒绝")
    SendRequest(text="x" * 32000)
    assert SendRequest(text="  ok  ").text == "  ok  "
    SendCommand(
        operation_id=str(uuid4()),
        session_id="s-valid",
        request=SendRequest(text="ok"),
    )


async def scenario_append_roles(service, repository) -> None:
    await service.create_session("s-roles", "入口限制")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-roles",
            request=SendRequest(text="开始"),
        ),
        system_message=system_message(),
    )
    run = send.run
    payloads = [
        entry(
            "s-roles",
            "role-u",
            run.request_entry_id,
            run.id,
            message(USER_SOURCE, content="绕过", timestamp=10),
            10,
        ),
        entry(
            "s-roles",
            "role-s",
            run.request_entry_id,
            run.id,
            system_message(),
            10,
        ),
    ]
    for payload in payloads:
        try:
            await service.append_entry(payload)
        except SessionConflict:
            continue
        raise AssertionError("非助手/工具消息被追加入口接受")
    assert await repository.get_entry("s-roles", "role-u") is None
    assert await repository.get_entry("s-roles", "role-s") is None
    assert (await service.get_run("s-roles", run.id)).last_entry_id is None
    await service.finish_run("s-roles", run.id, "cancelled")


async def scenario_identity_credentials(service, repository) -> None:
    secret = "review-protected-token"
    try:
        await service.create_session(
            "session:" + secret, "凭据", credentials=(secret,)
        )
    except CredentialDetected:
        pass
    else:
        raise AssertionError("会话身份字段凭据未拒绝")
    assert await repository.get_session("session:" + secret) is None

    await service.create_session("s-ident", "身份")
    send = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id="s-ident",
            request=SendRequest(text="正常"),
        ),
        system_message=system_message(),
    )
    run = send.run

    try:
        await service.accept_send(
            SendCommand(
                operation_id=str(uuid4()),
                session_id="s-ident",
                request=SendRequest(text="安全"),
            ),
            system_message=SystemMessage(role="system", content=secret, timestamp=1),
            credentials=(secret,),
        )
    except CredentialDetected:
        pass
    else:
        raise AssertionError("系统消息凭据未拒绝")
    assert (await service.get_run("s-ident", run.id)).status == "running"

    outcome = await service.append_entry(
        entry(
            "s-ident",
            "entry-" + secret,
            run.request_entry_id,
            run.id,
            message(ASSISTANT_FINAL_SOURCE),
            20,
        ),
        credentials=(secret,),
    )
    assert outcome.credential_detected is True and outcome.entry is None
    assert await repository.get_entry("s-ident", "entry-" + secret) is None
    assert (await service.get_run("s-ident", run.id)).status == "failed"


async def scenario_task_isolation(service, repository) -> None:
    await service.create_session("s-task", "任务归属")
    observed: list[bool] = []
    async with repository.transaction():
        await repository.insert_session(
            Session(
                id="task-uncommitted",
                title="x",
                active_leaf_id=None,
                created_at=1,
                updated_at=1,
            )
        )
        assert await repository.get_session("task-uncommitted") is not None
        child = asyncio.create_task(repository.get_session("task-uncommitted"))
        done, _ = await asyncio.wait({child}, timeout=0.1)
        observed.append(child in done)
    assert observed == [False]
    assert await child is not None


async def scenario_cancel_begin(database) -> None:
    loop = asyncio.get_running_loop()
    worker_entered = asyncio.Event()
    release = Event()

    def hold_worker() -> int:
        loop.call_soon_threadsafe(worker_entered.set)
        assert release.wait(5)
        return 1

    await database.connection.create_function("review_hold_worker", 0, hold_worker)
    queued = asyncio.create_task(
        database.connection.execute("SELECT review_hold_worker()")
    )
    await worker_entered.wait()

    entered_body = False

    async def transaction() -> None:
        nonlocal entered_body
        async with database.transaction_scope():
            entered_body = True

    task = asyncio.create_task(transaction())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    release.set()
    results = await asyncio.gather(task, return_exceptions=True)
    cursor = await queued
    await cursor.close()
    assert isinstance(results[0], asyncio.CancelledError)
    assert entered_body is False
    assert not database.connection.in_transaction

    async with database.transaction_scope():
        pass
    assert not database.connection.in_transaction

    async def probe(connection):
        reader = await connection.execute("SELECT 1")
        row = await reader.fetchone()
        await reader.close()
        return row[0]

    assert await database.read(probe) == 1


async def run() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=TEMP_ROOT, ignore_cleanup_errors=True) as directory:
        root = Path(directory)

        database, repository, service = await open_store(root / "main.db")
        try:
            await scenario_session(service, repository)
            await scenario_send(service, repository)
            await scenario_branching(service)
            await scenario_unpaired(service, repository)
            await scenario_steering(service, repository)
            await scenario_roundtrip(service)
            await scenario_credentials(service, repository)
            await scenario_transaction(service, repository, database)
            await scenario_concurrency(service)
            await scenario_request_validation(service)
            await scenario_append_roles(service, repository)
            await scenario_identity_credentials(service, repository)
            await scenario_task_isolation(service, repository)
            await scenario_cancel_begin(database)
            await assert_foreign_keys(database)
        finally:
            await database.close()

        await scenario_restore(root / "restore.db")
        await scenario_lifespan_recovery(root)

    print(
        "会话存储与事务用例：创建、读取、关闭重开、分支恢复、四种受理、"
        "Steering 状态、重复写入、并发幂等、凭据保护、事务回滚与取消自检通过"
    )


def check() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    check()
