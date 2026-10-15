import asyncio
import inspect
import json
import traceback
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import CancelledError as FuturesCancelled
from copy import deepcopy
from dataclasses import dataclass
from threading import Event
from time import perf_counter, time_ns
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.agent.telemetry import TraceSpan
from app.ai.messages import (
    JsonObject,
    TextContent,
    ToolCall,
    ToolResultMessage,
    text_projection,
)
from app.ai.messages import Tool as ToolDeclaration

ExecutionMode = Literal["parallel", "sequential"]


def _inline_refs(schema: JsonObject) -> JsonObject:
    # Pydantic 用 $defs/$ref 表达嵌套模型；部分模型接口不解析引用，这里就地内联为自包含 schema。
    definitions = schema.get("$defs") or {}

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = definitions.get(ref.removeprefix("#/$defs/"))
                if target is not None:
                    return resolve(target)
            return {
                key: resolve(value) for key, value in node.items() if key != "$defs"
            }
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


class CredentialDetectedError(Exception):
    """工具结果命中已知模型凭据：禁止公开与保存，运行以 credential_detected 终止。"""


class _ResultProtocolError(TypeError):
    pass


class _UpdatePublicationError(RuntimeError):
    pass


class _CancelledExecution(asyncio.CancelledError):
    def __init__(self, result):
        self.result = result


@dataclass(frozen=True)
class AgentToolResult:
    content: list[TextContent]
    is_error: bool = False


UpdateCallback = Callable[[AgentToolResult], None]
ExecuteFunction = Callable[
    [str, BaseModel, Event | None, UpdateCallback | None],
    AgentToolResult | Awaitable[AgentToolResult],
]
PrepareArguments = Callable[[JsonObject], JsonObject]
CredentialCheck = Callable[[str], bool]
OnToolUpdate = Callable[[str, str, AgentToolResult], None]


@dataclass(frozen=True)
class BeforeToolCallContext:
    tool_call: ToolCall
    arguments: BaseModel
    trusted_context: BaseModel | None = None


@dataclass(frozen=True)
class BeforeToolCallResult:
    block: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class AfterToolCallContext:
    tool_call: ToolCall
    arguments: BaseModel
    result: AgentToolResult
    is_error: bool
    duration_ms: float


@dataclass(frozen=True)
class AfterToolCallResult:
    content: list[TextContent] | None = None
    is_error: bool | None = None


BeforeToolCall = Callable[
    [BeforeToolCallContext, Event | None], Awaitable[BeforeToolCallResult | None]
]
AfterToolCall = Callable[
    [AfterToolCallContext, Event | None], Awaitable[AfterToolCallResult | None]
]


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    arguments: type[BaseModel]
    execute: ExecuteFunction
    execution_mode: ExecutionMode = "parallel"
    prepare_arguments: PrepareArguments | None = None
    max_output_chars: int | None = 50_000
    trusted_context: BaseModel | None = None
    format_error: Callable[[str], AgentToolResult] | None = None
    validate_output: Callable[[AgentToolResult], None] | None = None

    def definition(self) -> ToolDeclaration:
        return ToolDeclaration(
            name=self.name,
            description=self.description,
            parameters=_inline_refs(self.arguments.model_json_schema()),
        )


@dataclass(frozen=True)
class PreparedToolCall:
    index: int
    tool_call: ToolCall
    tool: AgentTool
    arguments: BaseModel
    timestamp: int


@dataclass(frozen=True)
class FinalizedToolCall:
    index: int
    message: ToolResultMessage
    duration_ms: float | None = None
    postprocess_ms: float | None = None
    # 结构化取消：准备阶段因取消返回的结果，定稿时据此将工具 Span 结算为 cancelled。
    cancelled: bool = False


@dataclass(frozen=True)
class ToolBatchResult:
    messages: list[ToolResultMessage]
    prepare_started_at: float
    first_result_at: float | None
    finalized_at: float
    failure: BaseException | None = None


OnToolStart = Callable[[ToolCall], Awaitable[None]]
OnToolFinalized = Callable[[FinalizedToolCall], Awaitable[None]]


def prepare_arguments(tool: AgentTool, tool_call: ToolCall) -> JsonObject:
    arguments = deepcopy(tool_call.arguments)
    return tool.prepare_arguments(arguments) if tool.prepare_arguments else arguments


def validate_arguments(tool: AgentTool, arguments: JsonObject) -> BaseModel:
    return tool.arguments.model_validate(arguments)


async def prepare_tool_call(
    index: int,
    tool_call: ToolCall,
    *,
    tools: Mapping[str, AgentTool],
    declared: Mapping[str, ToolDeclaration],
    before_tool_call: BeforeToolCall | None = None,
    signal: Event | None = None,
    contains_credentials: CredentialCheck | None = None,
) -> PreparedToolCall | FinalizedToolCall:
    timestamp = time_ns() // 1_000_000
    call_name = tool_call.name
    tool = tools.get(call_name)
    if tool is None:
        return _finalized(index, tool_call, f"工具不存在：{call_name}", timestamp)
    declaration = declared.get(call_name)
    if declaration is None:
        return _finalized(
            index, tool_call, f"工具未授权或已被移除：{call_name}", timestamp,
            tool=tool, error_code="tool_not_authorized",
            contains_credentials=contains_credentials,
        )
    expected = tool.definition()
    if (
        declaration.description != expected.description
        or declaration.parameters != expected.parameters
    ):
        raise ValueError(f"工具声明与执行注册表不一致: {call_name}")
    try:
        prepared = prepare_arguments(tool, tool_call)
    except CredentialDetectedError:
        raise
    except Exception as error:
        if tool.format_error is not None:
            _reject_error_credentials(contains_credentials, str(error))
        return _finalized(
            index, tool_call, f"参数预处理失败：{error}", timestamp,
            tool=None if isinstance(error, FuturesCancelled) else tool,
            error_code="argument_preparation_failed",
            contains_credentials=contains_credentials,
        )
    try:
        arguments = validate_arguments(tool, prepared)
    except ValidationError as error:
        if tool.format_error is not None:
            _reject_error_credentials(
                contains_credentials, json.dumps(tool_call.arguments, ensure_ascii=False)
            )
        return _finalized(
            index,
            tool_call,
            "参数校验失败"
            if tool.format_error is not None
            else _validation_message(call_name, tool_call.arguments, error, declaration.parameters),
            timestamp,
            tool=tool,
            error_code=_validation_error_code(tool, error) if tool.format_error is not None else "invalid_arguments",
            contains_credentials=contains_credentials,
        )
    if before_tool_call is not None:
        try:
            decision = await before_tool_call(
                BeforeToolCallContext(
                    tool_call.model_copy(deep=True),
                    arguments.model_copy(deep=True),
                    deepcopy(tool.trusted_context),
                ),
                signal,
            )
        except CredentialDetectedError:
            raise
        except Exception as error:
            if tool.format_error is not None:
                _reject_error_credentials(contains_credentials, str(error))
            return _finalized(
                index, tool_call, f"权限检查失败：{error}", timestamp,
                tool=None if isinstance(error, FuturesCancelled) else tool,
                error_code="permission_check_failed",
                contains_credentials=contains_credentials,
            )
        if _cancelled(signal):
            return _finalized(index, tool_call, "Operation aborted", timestamp, cancelled=True)
        if decision is not None and decision.block:
            if tool.format_error is not None:
                _reject_error_credentials(
                    contains_credentials, decision.reason or "工具执行被拒绝"
                )
            return _finalized(
                index, tool_call, decision.reason or "工具执行被拒绝", timestamp,
                tool=tool, error_code="tool_blocked",
                contains_credentials=contains_credentials,
            )
    if _cancelled(signal):
        return _finalized(index, tool_call, "Operation aborted", timestamp, cancelled=True)
    return PreparedToolCall(index, tool_call, tool, arguments, timestamp)


async def execute_prepared_tool_call(
    prepared: PreparedToolCall,
    *,
    after_tool_call: AfterToolCall | None = None,
    signal: Event | None = None,
    on_update: UpdateCallback | None = None,
    contains_credentials: CredentialCheck | None = None,
) -> FinalizedToolCall:
    tool_call = prepared.tool_call
    tool = prepared.tool
    arguments = prepared.arguments
    started_at = perf_counter()
    cancellation = None
    output_cancelled = False
    framework_error_code = None
    accepting_updates = True

    def update(partial: AgentToolResult) -> None:
        if accepting_updates and on_update is not None:
            try:
                on_update(partial)
            except (CredentialDetectedError, _ResultProtocolError):
                raise
            except Exception as error:
                if contains_credentials is not None and contains_credentials(
                    "".join(traceback.format_exception(error))
                ):
                    raise CredentialDetectedError() from None
                raise _UpdatePublicationError("工具进度发布失败") from error

    try:
        result = await _execute_with_cancel(
            tool_call.id, tool, arguments, signal,
            update if on_update is not None else None,
        )
    except (CredentialDetectedError, _ResultProtocolError, _UpdatePublicationError):
        raise
    except _CancelledExecution as error:
        cancellation = error
        result = error.result
        if result is None:
            result = AgentToolResult([], is_error=True)
        _validate_result(result)
        result = AgentToolResult(
            [*result.content, TextContent(type="text", text="Operation aborted；工具执行已收尾，副作用可能已发生，请核对原操作。")],
            is_error=True,
        )
    except Exception as error:
        output_cancelled = isinstance(error, FuturesCancelled)
        if not output_cancelled:
            framework_error_code = "tool_execution_failed"
        result = AgentToolResult(
            [TextContent(type="text", text=f"工具执行失败：{error}")], is_error=True
        )
    finally:
        accepting_updates = False
    _validate_result(result)
    executed_at = perf_counter()
    # 完整结果（截断前）先过凭据检查，避免截断区内的凭据逃过公开与保存检查。
    _reject_credentials(contains_credentials, result)
    if framework_error_code is not None and tool.format_error is not None:
        result = _formatted_error(tool, framework_error_code)
        _reject_credentials(contains_credentials, result)
    result = _truncate(tool, result)
    if after_tool_call is not None:
        duration_ms = (executed_at - started_at) * 1000
        try:
            override = await after_tool_call(
                AfterToolCallContext(
                    tool_call.model_copy(deep=True),
                    arguments.model_copy(deep=True),
                    deepcopy(result),
                    result.is_error,
                    duration_ms,
                ),
                signal,
            )
        except CredentialDetectedError:
            raise
        except Exception as error:
            output_cancelled = output_cancelled or isinstance(error, FuturesCancelled)
            result = AgentToolResult(
                [
                    *result.content,
                    TextContent(
                        type="text",
                        text=(
                            f"\n\n工具后处理失败：{error}。"
                            "工具可能已产生副作用，请勿重复执行。"
                        ),
                    ),
                ],
                is_error=True,
            )
            if (
                tool.format_error is not None
                and cancellation is None
                and not isinstance(error, FuturesCancelled)
            ):
                _reject_credentials(contains_credentials, result)
                result = _formatted_error(tool, "tool_postprocess_failed")
        else:
            if override is not None:
                result = AgentToolResult(
                    deepcopy(override.content)
                    if override.content is not None
                    else result.content,
                    result.is_error if override.is_error is None else override.is_error,
                )
        _validate_result(result)
        _reject_credentials(contains_credentials, result)
    if cancellation is not None:
        raise cancellation
    if tool.validate_output is not None and not output_cancelled:
        tool.validate_output(result)
    finalized_at = perf_counter()
    message = ToolResultMessage(
        role="toolResult",
        tool_call_id=tool_call.id,
        tool_name=tool_call.name,
        content=result.content,
        is_error=result.is_error,
        timestamp=prepared.timestamp,
    )
    return FinalizedToolCall(
        prepared.index,
        message,
        duration_ms=(executed_at - started_at) * 1000,
        postprocess_ms=(
            (finalized_at - executed_at) * 1000 if after_tool_call is not None else None
        ),
    )


async def run_tool_call(
    tool_call: ToolCall,
    *,
    tools: Mapping[str, AgentTool],
    declared: Mapping[str, ToolDeclaration],
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
    signal: Event | None = None,
    on_update: UpdateCallback | None = None,
    contains_credentials: CredentialCheck | None = None,
) -> ToolResultMessage:
    outcome = await prepare_tool_call(
        0,
        tool_call,
        tools=tools,
        declared=declared,
        before_tool_call=before_tool_call,
        signal=signal,
        contains_credentials=contains_credentials,
    )
    if isinstance(outcome, FinalizedToolCall):
        _reject_credentials(contains_credentials, AgentToolResult(outcome.message.content, True))
        return outcome.message

    def update(result: AgentToolResult) -> None:
        _validate_result(result)
        _reject_credentials(contains_credentials, result)
        if on_update is not None:
            on_update(result)

    finalized = await execute_prepared_tool_call(
        outcome,
        after_tool_call=after_tool_call,
        signal=signal,
        on_update=update if on_update is not None or contains_credentials is not None else None,
        contains_credentials=contains_credentials,
    )
    return finalized.message


async def run_tool_batch(
    tool_calls: list[ToolCall],
    *,
    tools: Mapping[str, AgentTool],
    declared: Mapping[str, ToolDeclaration],
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
    signal: Event | None = None,
    execution_mode: ExecutionMode = "parallel",
    on_tool_start: OnToolStart | None = None,
    on_tool_finalized: OnToolFinalized | None = None,
    on_tool_update: OnToolUpdate | None = None,
    contains_credentials: CredentialCheck | None = None,
    telemetry_span: TraceSpan | None = None,
) -> ToolBatchResult:
    if len({call.id for call in tool_calls}) != len(tool_calls):
        raise ValueError("工具调用ID重复")
    messages: list[ToolResultMessage | None] = [None] * len(tool_calls)
    first_result_at: float | None = None
    prepare_started_at = perf_counter()
    notify_start = on_tool_start or _noop_event
    notify_finalized = on_tool_finalized or _noop_event
    failure: BaseException | None = None
    # 每个实际工具调用各自建立 tool Span：从准备开始，至执行与后处理收尾结算。
    tool_spans: list[TraceSpan | None] = [None] * len(tool_calls)

    def open_tool_span(index: int, call: ToolCall) -> None:
        if telemetry_span is None:
            return
        tool = tools.get(call.name)
        # tool_name 使用核实后的注册名，未核实的调用保持 null。
        tool_spans[index] = telemetry_span.open(
            "tool",
            {
                "tool_call_id": call.id,
                "tool_name": tool.name if tool is not None else None,
            },
        )

    def settle_tool_span(finalized: FinalizedToolCall) -> None:
        handle = tool_spans[finalized.index]
        if handle is None:
            return
        tool_spans[finalized.index] = None
        attributes: dict[str, object] = {}
        # 复用真实执行与后处理耗时；准备阶段拒绝执行时两者保持未知。
        if finalized.duration_ms is not None:
            attributes["execution_ms"] = finalized.duration_ms
        if finalized.postprocess_ms is not None:
            attributes["postprocess_ms"] = finalized.postprocess_ms
        handle.set_attributes(attributes)
        handle.set_status(
            "cancelled"
            if finalized.cancelled
            else "failed"
            if finalized.message.is_error
            else "completed"
        )
        handle.settle()

    def settle_unfinished(cancelled: bool) -> None:
        # 尚未定稿的调用（取消或框架失败打断调度）：按真实控制流结算为 cancelled 或 failed。
        status = "cancelled" if cancelled else "failed"
        for position, handle in enumerate(tool_spans):
            if handle is not None:
                tool_spans[position] = None
                handle.set_status(status)
                handle.settle()

    def update_for(tool_call_id: str, tool_name: str):
        # 进度回调绑定真实调用身份；执行结束后的迟到更新忽略；公开前先过凭据检查。
        active = [True]
        if on_tool_update is None and contains_credentials is None:
            return None, active

        def update(result: AgentToolResult) -> None:
            if not active[0]:
                return
            _validate_result(result)
            _reject_credentials(contains_credentials, result)
            if on_tool_update is not None:
                on_tool_update(tool_call_id, tool_name, result)

        return update, active

    async def finalize(finalized: FinalizedToolCall) -> None:
        nonlocal first_result_at
        # 定稿前先经过完成通知（含凭据检查）；命中时该结果不入结果集，随后停止调度。
        _reject_credentials(
            contains_credentials,
            AgentToolResult(finalized.message.content, finalized.message.is_error),
        )
        await notify_finalized(finalized)
        messages[finalized.index] = finalized.message
        if first_result_at is None:
            first_result_at = perf_counter()
        settle_tool_span(finalized)

    def stop() -> None:
        if signal is not None:
            signal.set()

    sequential = execution_mode == "sequential" or any(
        (tool := tools.get(call.name)) is not None
        and tool.execution_mode == "sequential"
        for call in tool_calls
    )
    if sequential:
        for index, call in enumerate(tool_calls):
            if _cancelled(signal):
                break
            try:
                await notify_start(call)
                if _cancelled(signal):
                    break
                open_tool_span(index, call)
                outcome = await prepare_tool_call(
                    index,
                    call,
                    tools=tools,
                    declared=declared,
                    before_tool_call=before_tool_call,
                    signal=signal,
                    contains_credentials=contains_credentials,
                )
                if isinstance(outcome, FinalizedToolCall):
                    finalized = outcome
                elif _cancelled(signal):
                    break
                else:
                    update, active = update_for(call.id, call.name)
                    try:
                        finalized = await execute_prepared_tool_call(
                            outcome,
                            after_tool_call=after_tool_call,
                            signal=signal,
                            on_update=update,
                            contains_credentials=contains_credentials,
                        )
                    finally:
                        active[0] = False
                await finalize(finalized)
            except BaseException as error:
                failure = error
                stop()
                break
    else:
        placements: list[PreparedToolCall | FinalizedToolCall | None] = [
            None
        ] * len(tool_calls)
        for index, call in enumerate(tool_calls):
            if _cancelled(signal):
                break
            try:
                await notify_start(call)
                if _cancelled(signal):
                    break
                open_tool_span(index, call)
                placements[index] = await prepare_tool_call(
                    index,
                    call,
                    tools=tools,
                    declared=declared,
                    before_tool_call=before_tool_call,
                    signal=signal,
                    contains_credentials=contains_credentials,
                )
            except BaseException as error:
                failure = error
                stop()
                break
        prepared_calls: list[PreparedToolCall] = []
        if failure is None:
            for placement in placements:
                if isinstance(placement, FinalizedToolCall):
                    try:
                        await finalize(placement)
                    except BaseException as error:
                        failure = error
                        stop()
                        break
                elif isinstance(placement, PreparedToolCall):
                    prepared_calls.append(placement)
        if failure is None and prepared_calls and not _cancelled(signal):
            first_failure: list[BaseException] = []

            async def run(prepared: PreparedToolCall) -> None:
                update, active = update_for(
                    prepared.tool_call.id, prepared.tool_call.name
                )
                try:
                    finalized = await execute_prepared_tool_call(
                        prepared,
                        after_tool_call=after_tool_call,
                        signal=signal,
                        on_update=update,
                        contains_credentials=contains_credentials,
                    )
                finally:
                    active[0] = False
                await finalize(finalized)

            def observe(task: asyncio.Task) -> None:
                # 首个框架失败立即通知在途工具停止；聚合仍等待它们收尾。
                if task.cancelled() or first_failure:
                    return
                error = task.exception()
                if error is not None:
                    first_failure.append(error)
                    stop()

            tasks = [asyncio.create_task(run(prepared)) for prepared in prepared_calls]
            for task in tasks:
                task.add_done_callback(observe)
            try:
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError as error:
                failure = error
            else:
                for outcome in outcomes:
                    if isinstance(outcome, BaseException) and not first_failure:
                        first_failure.append(outcome)
                if first_failure:
                    failure = first_failure[0]

    # 未定稿调用：任务取消与协作取消记 cancelled，框架失败记 failed。
    settle_unfinished(
        isinstance(failure, asyncio.CancelledError)
        or (failure is None and _cancelled(signal))
    )
    return ToolBatchResult(
        [message for message in messages if message is not None],
        prepare_started_at,
        first_result_at,
        perf_counter(),
        failure,
    )


async def _execute_with_cancel(
    tool_call_id: str,
    tool: AgentTool,
    arguments: BaseModel,
    signal: Event | None,
    on_update: UpdateCallback | None,
) -> AgentToolResult:
    # 协程 execute 直接在当前事件循环执行；同步 execute 放入线程池，两者共用取消收尾。
    if inspect.iscoroutinefunction(tool.execute):
        pending: asyncio.Future = asyncio.ensure_future(
            tool.execute(tool_call_id, arguments, signal, on_update)
        )
    else:
        pending = asyncio.get_running_loop().run_in_executor(
            None, tool.execute, tool_call_id, arguments, signal, on_update
        )
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # 任务取消桥接到工具信号，等待在途工作完成收尾并接收结果后再向外传播。
        if signal is not None:
            signal.set()
        while not pending.done():
            try:
                await asyncio.wait((pending,))
            except asyncio.CancelledError:
                continue
        try:
            result = pending.result()
        except (CredentialDetectedError, _ResultProtocolError, _UpdatePublicationError):
            raise
        except asyncio.CancelledError:
            result = None
        except Exception as error:
            result = AgentToolResult(
                [TextContent(type="text", text=f"工具执行失败：{error}")], is_error=True
            )
        raise _CancelledExecution(result)


def _validate_result(result: AgentToolResult) -> None:
    if (
        not isinstance(result, AgentToolResult)
        or type(result.is_error) is not bool
        or not isinstance(result.content, list)
        or any(not isinstance(block, TextContent) for block in result.content)
    ):
        raise _ResultProtocolError("工具返回值违反AgentToolResult协议")


def _reject_credentials(
    contains_credentials: CredentialCheck | None, result: AgentToolResult
) -> None:
    if contains_credentials is not None and contains_credentials(
        text_projection(result.content)
    ):
        raise CredentialDetectedError()


def _reject_error_credentials(check: CredentialCheck | None, text: str) -> None:
    _reject_credentials(check, AgentToolResult([TextContent(type="text", text=text)], True))


def _cancelled(signal: Event | None) -> bool:
    return signal is not None and signal.is_set()


async def _noop_event(_event: object) -> None:
    return None


def _formatted_error(tool: AgentTool, code: str) -> AgentToolResult:
    result = tool.format_error(code)
    _validate_result(result)
    if not result.is_error:
        raise _ResultProtocolError("错误格式器必须返回 is_error=True")
    return result


def _validation_error_code(tool: AgentTool, error: ValidationError) -> str:
    # 仅公开模型已知字段；未知键与列表下标均不进入错误响应。
    location = error.errors(include_input=False, include_context=False)[0]["loc"]
    root = location[0] if location else None
    field = root if root in tool.arguments.model_fields else "arguments"
    return f"invalid_arguments.{field}"


def _finalized(
    index: int,
    tool_call: ToolCall,
    text: str,
    timestamp: int,
    *,
    cancelled: bool = False,
    tool: AgentTool | None = None,
    error_code: str = "tool_failed",
    contains_credentials: CredentialCheck | None = None,
) -> FinalizedToolCall:
    result = (
        _formatted_error(tool, error_code)
        if tool is not None and tool.format_error is not None and not cancelled
        else AgentToolResult([TextContent(type="text", text=text)], is_error=True)
    )
    if tool is not None and tool.validate_output is not None and not cancelled:
        _validate_result(result)
        _reject_credentials(contains_credentials, result)
        tool.validate_output(result)
    return FinalizedToolCall(
        index,
        ToolResultMessage(
            role="toolResult",
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            content=result.content,
            is_error=True,
            timestamp=timestamp,
        ),
        cancelled=cancelled,
    )


def _validation_message(
    call_name: str, arguments: JsonObject, error: ValidationError, parameters: JsonObject
) -> str:
    constraints = {}
    properties = parameters.get("properties", {})
    for item in error.errors():
        root = str(item["loc"][0]) if item["loc"] else "root"
        constraints[root] = properties[root] if root in properties else parameters
    allowed = json.dumps(constraints, ensure_ascii=False, indent=2)
    details = "\n".join(
        f"  - {'.'.join(str(part) for part in item['loc']) or 'root'}: {item['msg']}"
        for item in error.errors()
    )
    received = json.dumps(arguments, ensure_ascii=False, indent=2)
    return f'工具 "{call_name}" 参数校验失败：\n{details}\n允许的参数约束：\n{allowed}\n收到的参数：\n{received}'


def _truncate(tool: AgentTool, result: AgentToolResult) -> AgentToolResult:
    if tool.max_output_chars is None:
        return result
    text = "".join(block.text for block in result.content)
    if len(text) <= tool.max_output_chars:
        return result
    return AgentToolResult(
        [
            TextContent(
                type="text",
                text=text[: tool.max_output_chars]
                + f"\n[输出截断：超过 {tool.max_output_chars} 字符]",
            )
        ],
        result.is_error,
    )
