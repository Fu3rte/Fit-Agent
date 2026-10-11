import asyncio
import json
from pathlib import Path

import anthropic
import httpx
import openai

from app.agent import compaction as C
from app.agent import usage as U
from app.agent.agent_loop import recover_context_overflow
from app.agent.compaction import CompactionSummaryError, ContextBudgetExceeded
from app.agent.config import CompactionHooks, LoopCompactionResult
from app.ai.context import get_current_system_message
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    Usage,
    UserMessage,
)
from app.ai.overflow import (
    ZERO_USAGE,
    ContextOverflowError,
    build_overflow_message,
    is_context_overflow_error,
)
from app.ai.types import ModelSpec
from app.application.session.service import SessionService
from app.domain.session.models import (
    CompactionEntry,
    EditCommand,
    EditRequest,
    RegenerateCommand,
    RegenerateRequest,
    Session,
    SessionMessageEntry,
    SessionRun,
)
from app.infrastructure.persistence.sqlite.database import open_database
from app.infrastructure.persistence.sqlite.repository import SqliteSessionRepository
from app.interfaces.http import (
    CONTEXT_OVERFLOW_MESSAGE,
    CredentialDetectedError,
    terminal_decision,
)

BACKEND = Path(__file__).resolve().parents[1]
TEMP_ROOT = BACKEND / "temp" / "compaction-overflow-recovery"

SID = "11111111-1111-4111-8111-111111111111"
USAGE = Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2)

_clock = [1000]


def tick() -> int:
    _clock[0] += 1
    return _clock[0]


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def system_message(text: str = "系统指令") -> SystemMessage:
    return SystemMessage(role="system", content=text, timestamp=tick())


def user(text: str) -> UserMessage:
    return UserMessage(role="user", content=text, timestamp=tick())


def assistant(text: str = "回答") -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text=text)],
        api="openai-completions",
        provider="test",
        model="test-model",
        usage=USAGE,
        stop_reason="stop",
        timestamp=tick(),
    )


def pending_output(usage: Usage | None = None) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[],
        api="openai-completions",
        provider="test",
        model="test-model",
        usage=usage,
        stop_reason="pending",
        timestamp=tick(),
    )


def failed_message(text: str = "") -> AssistantMessage:
    content = [TextContent(type="text", text=text)] if text else []
    return AssistantMessage(
        role="assistant",
        content=content,
        api="openai-completions",
        provider="test",
        model="test-model",
        usage=ZERO_USAGE,
        stop_reason="error",
        error_message="maximum context length exceeded",
        context_overflow=True,
        timestamp=tick(),
    )


def entry(
    entry_id: str,
    parent_id: str | None,
    message,
    *,
    model_omitted: bool = False,
    run_id: str | None = None,
    session_id: str = SID,
) -> SessionMessageEntry:
    return SessionMessageEntry(
        session_id=session_id,
        id=entry_id,
        parent_id=parent_id,
        run_id=run_id,
        type="message",
        messages=[message],
        created_at=tick(),
        model_omitted=model_omitted,
    )


def failed_path() -> list[SessionMessageEntry]:
    return [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("问题一" * 200)),
        entry(
            "fail",
            "u1",
            failed_message("失败的上下文溢出尝试"),
            model_omitted=True,
            run_id="run-1",
        ),
        entry("a1", "fail", assistant("回答一")),
        entry("u2", "a1", user("问题二" * 400)),
    ]


def sdk_error(cls, status: int, message: str, *, code: str | None = None):
    body: dict = {"error": {"message": message}}
    if code is not None:
        body["error"]["code"] = code
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"),
    )
    return cls(message, response=response, body=body)


def sdk_connection_error():
    return openai.APIConnectionError(
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    )


def check_classification() -> None:
    # 真实 SDK 异常类型与固定输入：容量文案与结构化容量码命中。
    capacity = [
        sdk_error(
            openai.BadRequestError,
            400,
            "This model's maximum context length is 128000 tokens. "
            "However, your messages resulted in 200000 tokens.",
        ),
        sdk_error(openai.BadRequestError, 400, "Bad request", code="context_length_exceeded"),
        sdk_error(anthropic.BadRequestError, 400, "prompt is too long: 210000 tokens > 200000 maximum"),
        sdk_error(
            anthropic.BadRequestError,
            400,
            "input length and `max_tokens` exceed context limit: 200000 + 4000 > 204000",
        ),
        sdk_error(
            openai.BadRequestError,
            413,
            "request input exceeds the context window of this model",
        ),
    ]
    for error in capacity:
        assert is_context_overflow_error(error) is True, repr(error)
    # 排除规则优先：限流、鉴权、权限、资源缺失、连接、超时、普通参数与混合文案。
    excluded = [
        sdk_error(openai.BadRequestError, 400, "Invalid parameter: context length must be positive"),
        sdk_error(
            openai.BadRequestError,
            400,
            "Rate limit exceeded: too many tokens, please wait before trying again",
        ),
        sdk_error(
            openai.BadRequestError,
            400,
            "Invalid parameter: max_tokens must be less than or equal to 4096",
        ),
        sdk_error(openai.BadRequestError, 400, "Invalid parameter: image exceeds maximum size of 20MB"),
        sdk_error(openai.RateLimitError, 429, "rate limit exceeded"),
        sdk_error(openai.AuthenticationError, 401, "invalid api key"),
        sdk_error(openai.PermissionDeniedError, 403, "permission denied"),
        sdk_error(openai.NotFoundError, 404, "model not found"),
        sdk_error(openai.InternalServerError, 500, "internal server error"),
        sdk_error(anthropic.RateLimitError, 429, "rate limit exceeded"),
        sdk_connection_error(),
        TimeoutError("request timed out"),
    ]
    for error in excluded:
        assert is_context_overflow_error(error) is False, repr(error)
    print("PASS: 容量错误识别与普通错误排除")


def check_failed_message() -> None:
    built = build_overflow_message(
        pending_output(), ProviderError(400, "maximum context length exceeded")
    )
    assert built.stop_reason == "error" and built.context_overflow is True
    assert built.usage is not None
    assert built.usage.input == 0 and built.usage.total_tokens == 0
    assert "cost" not in built.usage.model_fields_set
    assert built.error_message == "maximum context length exceeded"
    assert built.model == "test-model" and built.provider == "test"

    real = Usage(input=100, output=5, cache_read=0, cache_write=0, total_tokens=105)
    preserved = build_overflow_message(
        pending_output(real), ProviderError(400, "context length exceeded")
    )
    assert preserved.usage == real
    assert "cost" not in preserved.usage.model_fields_set
    print("PASS: 零值失败消息构造与真实 usage 保真")


def check_zero_value_usage() -> None:
    normal = assistant()
    failed = failed_message()
    totals = U.summarize_usage([normal, failed])
    assert totals.total_tokens == 2
    assert "cost" not in totals.model_fields_set
    zero_only = U.summarize_usage([failed_message()])
    assert zero_only.total_tokens == 0
    assert "cost" not in zero_only.model_fields_set
    print("PASS: 零值失败用量不计费")


def check_terminal_decision() -> None:
    overflow = ContextOverflowError("x")
    assert terminal_decision(overflow, False) == (
        "failed",
        "context_overflow",
        CONTEXT_OVERFLOW_MESSAGE,
    )
    # 停止与第二次容量拒绝同时到达：按已确认取消裁决，凭据命中仍最高优先级。
    assert terminal_decision(overflow, True) == ("cancelled", "cancelled", "执行已取消。")
    assert terminal_decision(CredentialDetectedError(), True)[1] == "credential_detected"
    assert terminal_decision(CompactionSummaryError("x"), False)[1] == "compaction_failed"
    assert terminal_decision(ContextBudgetExceeded("x"), False)[1] == "context_budget_exceeded"
    print("PASS: context_overflow 与取消/凭据终态裁决")


def check_projection_omission() -> None:
    path = failed_path()
    context = C.build_effective_context(path)
    assert "fail" not in context.source_entry_ids
    assert len(context.messages) == len(context.source_entry_ids)
    assert not any(getattr(message, "content", "") == "失败的上下文溢出尝试" for message in context.messages)

    preparation = C.prepare_compaction(
        path, 128000, 16384, C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=50)
    )
    summarized = " ".join(
        str(getattr(message, "content", "")) for message in preparation.messages_to_summarize
    )
    assert "失败" not in summarized
    assert preparation.first_kept_entry_id == "u2"
    print("PASS: 失败尝试在投影与摘要准备中省略")


def check_omission_guard() -> None:
    try:
        entry("bad", "u1", assistant("普通回答"), model_omitted=True)
    except Exception as error:
        assert "省略标记" in str(error)
    else:
        raise AssertionError("省略标记接受了非失败助手消息")
    print("PASS: 省略标记仅接受上下文溢出失败助手消息")


async def open_store(path: Path):
    database = await open_database(path)
    return database, SqliteSessionRepository(database)


async def seed_store(
    path: Path,
    entries: list[SessionMessageEntry],
    *,
    request_entry_id: str = "u1",
    last_entry_id: str = "fail",
    run_status: str = "completed",
) -> None:
    database, repository = await open_store(path)
    try:
        async with repository.transaction():
            await repository.defer_foreign_keys()
            await repository.insert_session(
                Session(id=SID, title="溢出恢复", active_leaf_id=None, created_at=1, updated_at=1)
            )
            run = SessionRun(
                session_id=SID,
                id="run-1",
                request_entry_id=request_entry_id,
                last_entry_id=last_entry_id,
                status=run_status,
                started_at=2,
                finished_at=3,
                error_code=None,
                error_message=None,
            )
            for item in entries:
                await repository.insert_entry(item)
            await repository.insert_run(run)
            session = await repository.get_session(SID)
            await repository.update_session(
                session.model_copy(update={"active_leaf_id": entries[-1].id, "updated_at": 1})
            )
    finally:
        await database.close()


async def scenario_persistence_and_restart() -> None:
    path = TEMP_ROOT / "overflow-restart.sqlite"
    if path.exists():
        path.unlink()
    await seed_store(path, failed_path())

    database, repository = await open_store(path)
    service = SessionService(repository)
    projection = await service.get_projection(SID, "u2")
    assert "fail" not in projection.context.source_entry_ids
    stored = await repository.get_entry(SID, "fail")
    assert stored is not None and stored.model_omitted is True
    await database.close()

    database, repository = await open_store(path)
    service = SessionService(repository)
    projection = await service.get_projection(SID, "u2")
    assert "fail" not in projection.context.source_entry_ids
    stored = await repository.get_entry(SID, "fail")
    assert stored is not None and stored.model_omitted is True
    await database.close()
    print("PASS: 省略标记持久化与重启一致")


async def scenario_branch_isolation() -> None:
    path = TEMP_ROOT / "overflow-branch.sqlite"
    if path.exists():
        path.unlink()
    entries = failed_path()
    entries.append(entry("u3", "a1", user("另一分支")))
    await seed_store(path, entries)

    database, repository = await open_store(path)
    service = SessionService(repository)
    with_fail = await service.get_projection(SID, "u2")
    other = await service.get_projection(SID, "u3")
    assert "fail" not in with_fail.context.source_entry_ids
    assert "fail" not in other.context.source_entry_ids
    assert "u3" in other.context.source_entry_ids
    assert "u2" not in other.context.source_entry_ids
    await database.close()
    print("PASS: 省略不跨分支污染")


async def scenario_edit_regenerate() -> None:
    path = TEMP_ROOT / "overflow-edit.sqlite"
    if path.exists():
        path.unlink()
    await seed_store(path, failed_path())

    database, repository = await open_store(path)
    service = SessionService(repository)
    outcome = await service.accept_regenerate(
        RegenerateCommand(
            operation_id="33333333-3333-4333-8333-333333333333",
            session_id=SID,
            request=RegenerateRequest(target_entry_id="u1"),
        )
    )
    assert outcome.created is True
    assert await repository.get_entry(SID, "fail") is None
    await database.close()
    print("PASS: 重新生成移除省略节点及其子树")


async def scenario_edit_replaces() -> None:
    path = TEMP_ROOT / "overflow-edit2.sqlite"
    if path.exists():
        path.unlink()
    await seed_store(path, failed_path())

    database, repository = await open_store(path)
    service = SessionService(repository)
    outcome = await service.accept_edit(
        EditCommand(
            operation_id="44444444-4444-4444-8444-444444444444",
            session_id=SID,
            request=EditRequest(target_entry_id="u2", text="修改后的请求"),
        )
    )
    assert outcome.created is True
    # 编辑目标 u2 的父链仍包含省略节点；省略语义保持，投影继续跳过。
    projection = await service.get_projection(SID, outcome.run.request_entry_id)
    assert "fail" not in projection.context.source_entry_ids
    await database.close()
    print("PASS: 编辑后省略语义一致")


async def scenario_budget_recheck() -> None:
    path = TEMP_ROOT / "overflow-budget.sqlite"
    if path.exists():
        path.unlink()
    # 合法切点：历史可总结、近期保留原文；压缩后保留原文加摘要仍超已知预算。
    entries = [
        entry("sys", None, system_message()),
        entry("u1", "sys", user("历史问题" * 8000)),
        entry("u2", "u1", user("近期保留原文" * 4000), run_id="run-1"),
    ]
    await seed_store(
        path, entries, request_entry_id="u1", last_entry_id="u2", run_status="running"
    )

    spec = ModelSpec(
        api="openai-completions",
        provider="test",
        id="test-model",
        base_url="http://localhost:1",
        context_window=20000,
        max_tokens=16384,
    )
    settings = C.CompactionSettings(reserve_tokens=16384, keep_recent_tokens=1000)
    dynamic = SystemMessage(
        role="system", content="", sections={"business_context": "{}"}, timestamp=tick()
    )
    database, repository = await open_store(path)
    try:
        service = SessionService(repository)
        position = {"id": "u2"}
        committed: list[str] = []

        async def compact(compaction_id, signal):
            projection = await service.get_projection(SID, position["id"])
            preparation = C.prepare_compaction(
                projection.path, spec.context_window, spec.max_tokens, settings
            )
            system_state = get_current_system_message(projection.context.messages)
            checkpoint = CompactionEntry(
                session_id=SID,
                id=compaction_id,
                parent_id=position["id"],
                run_id="run-1",
                type="compaction",
                summary="固定摘要",
                first_kept_entry_id=preparation.first_kept_entry_id,
                tokens_before=0,
                usage=USAGE,
                system_message=system_state,
                details=None,
                created_at=tick(),
            )
            outcome = await service.commit_compaction(
                checkpoint, prepared_position=checkpoint.parent_id, credentials=()
            )
            assert outcome.credential_detected is False
            position["id"] = outcome.entry.id
            committed.append(outcome.entry.id)
            refreshed = await service.get_projection(SID, outcome.entry.id)
            return LoopCompactionResult(
                entry_id=outcome.entry.id,
                parent_id=outcome.entry.parent_id,
                messages=[*refreshed.context.messages, dynamic],
            )

        async def estimate_request() -> int:
            projection = await service.get_projection(SID, position["id"])
            return C.estimate_context_tokens([*projection.context.messages, dynamic]).tokens

        hooks = CompactionHooks(
            estimate_request=estimate_request,
            compact=compact,
            context_window=spec.context_window,
            reserve_tokens=settings.reserve_tokens,
        )
        events: list[str] = []

        async def emit(event) -> None:
            events.append(event["type"])

        error: BaseException | None = None
        try:
            await recover_context_overflow({"messages": [], "tools": {}}, hooks, None, emit)
        except ContextBudgetExceeded as exc:
            error = exc
        assert error is not None
        # 失败码、已提交检查点保留、未发送恢复模型请求。
        assert terminal_decision(error, False)[1] == "context_budget_exceeded"
        assert events == ["compaction_start", "compaction_end"]
        assert len(committed) == 1
        assert await repository.get_entry(SID, committed[0]) is not None
        assert await repository.get_entry(SID, "u1") is not None
    finally:
        await database.close()
    print("PASS: 恢复后预算复核未通过时结束为 context_budget_exceeded")


def check() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    evidence_dir = BACKEND.parent / "tmp" / "agent-compaction-overflow-recovery"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    check_classification()
    check_failed_message()
    check_zero_value_usage()
    check_terminal_decision()
    check_projection_omission()
    check_omission_guard()
    asyncio.run(scenario_persistence_and_restart())
    asyncio.run(scenario_branch_isolation())
    asyncio.run(scenario_edit_regenerate())
    asyncio.run(scenario_edit_replaces())
    asyncio.run(scenario_budget_recheck())
    evidence = {
        "classification": "real SDK errors; capacity hit; param/rate-limit/auth/connection/service excluded",
        "zero_value_usage": {"cost": None, "total_tokens": 0},
        "projection_omission": "failed attempt excluded from projection and summary",
        "persistence": "model_omitted flag persisted and survives restart",
        "branch": "omission does not leak across branches",
        "edit_regenerate": "omission node removed with subtree / semantics preserved",
        "terminal": "context_overflow -> failed/context_overflow; cancelled wins; credential first",
        "budget_recheck": "post-compaction over budget -> context_budget_exceeded, checkpoint kept, no model request",
    }
    (evidence_dir / "deterministic-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 溢出恢复确定性检查全部通过")


if __name__ == "__main__":
    check()
