from asyncio import Runner
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from threading import Event
from time import time_ns

from src.agent.events import AgentEvent
from src.agent.message_context import (
    ConvertToLlm,
    TransformContext,
    convert_to_llm,
    get_current_tools,
    prepare_message_context,
)
from src.agent.messages import (
    AgentMessage,
    AssistantMessage,
    Message,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from src.agent.tool import Tool


def run_turn(
    messages: list[AgentMessage],
    complete: Callable[[list[Message]], AssistantMessage],
    tools: dict[str, Tool],
    max_steps: int,
    cancel: Event | None = None,
    *,
    transform_context: TransformContext | None = None,
    convert_to_llm: ConvertToLlm = convert_to_llm,
) -> Iterator[AgentEvent]:
    def checkpoint() -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError("执行已取消")

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")
    if any(name != tool.name for name, tool in tools.items()):
        raise ValueError("工具注册表名称不一致")
    with Runner() as runner:
        for _ in range(max_steps):
            checkpoint()
            request_start = len(messages)
            projected = runner.run(
                prepare_message_context(
                    messages,
                    transform_context=transform_context,
                    convert_to_llm=convert_to_llm,
                    signal=cancel,
                )
            )
            declarations = {tool.name: tool for tool in get_current_tools(projected)}
            for name, declaration in declarations.items():
                registered = tools[name].definition()
                if (
                    declaration.description != registered.description
                    or declaration.parameters != registered.parameters
                ):
                    raise ValueError(f"工具声明与执行注册表不一致: {name}")
            checkpoint()
            response = AssistantMessage.model_validate(
                complete(projected).model_dump(exclude_unset=True)
            )
            checkpoint()
            if response.stopReason == "pending":
                raise ValueError("模型调用返回了未完成响应")
            seen = {
                block.id
                for message in messages
                if isinstance(message, AssistantMessage)
                for block in message.content
                if isinstance(block, ToolCall)
            }
            messages.append(response)
            calls = [block for block in response.content if isinstance(block, ToolCall)]
            if response.stopReason == "aborted":
                raise CancelledError("模型响应已取消")
            if response.stopReason not in {"stop", "toolUse"}:
                raise RuntimeError(f"模型未正常完成：{response.stopReason}")
            if response.stopReason == "stop":
                if calls:
                    raise ValueError("stop 响应包含工具调用")
                text = "".join(
                    block.text
                    for block in response.content
                    if isinstance(block, TextContent)
                )
                if not text:
                    raise RuntimeError("模型返回了空的最终回答")
                yield AgentEvent("message", {"text": text})
                checkpoint()
                yield AgentEvent("done", {"status": "completed"})
                return
            if not calls:
                raise ValueError("toolUse 响应缺少工具调用")
            for call in calls:
                if call.id in seen:
                    raise ValueError(f"重复工具调用 ID: {call.id}")
                seen.add(call.id)
                if call.name not in declarations:
                    raise PermissionError(f"工具未声明或已被移除: {call.name}")
            for call in calls:
                checkpoint()
                tool = tools[call.name]
                arguments = tool.arguments.model_validate(call.arguments)
                yield AgentEvent(
                    "tool_start",
                    {
                        "tool_call_id": call.id,
                        "name": call.name,
                        "arguments": arguments.model_dump(),
                    },
                )
                checkpoint()
                active = {
                    item.name: item
                    for item in get_current_tools(
                        [*projected, *messages[request_start:]]
                    )
                }
                if call.name not in active:
                    raise PermissionError(f"工具未声明或已被移除: {call.name}")
                registered = tools[call.name].definition()
                if (
                    active[call.name].parameters != registered.parameters
                    or active[call.name].description != registered.description
                ):
                    raise ValueError(f"工具声明与执行注册表不一致: {call.name}")
                result = tools[call.name].invoke(arguments.model_dump_json())
                checkpoint()
                messages.append(
                    ToolResultMessage(
                        role="toolResult",
                        toolCallId=call.id,
                        toolName=call.name,
                        content=[TextContent(type="text", text=result.content)],
                        isError=result.isError,
                        timestamp=time_ns() // 1_000_000,
                    )
                )
                yield AgentEvent(
                    "tool_result", {"tool_call_id": call.id, "content": result.content}
                )
        raise RuntimeError(f"Agent 达到 {max_steps} 次模型调用上限")
