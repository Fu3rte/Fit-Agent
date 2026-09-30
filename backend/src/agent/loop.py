from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from threading import Event

from src.agent.events import AgentEvent
from src.agent.tool import Tool


def run_turn(
    messages: list[dict],
    complete: Callable[[list[dict], list[dict]], dict],
    tools: dict[str, Tool],
    max_steps: int,
    cancel: Event | None = None,
) -> Iterator[AgentEvent]:
    def checkpoint() -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError("执行已取消")

    if max_steps < 1:
        raise ValueError("max_steps 必须大于 0")
    definitions = [tool.definition() for tool in tools.values()]
    for _ in range(max_steps):
        checkpoint()
        response = complete(messages, definitions)
        checkpoint()
        messages.append(response)
        calls = response.get("tool_calls", [])
        if not calls:
            if not response.get("content"):
                raise RuntimeError("模型返回了空的最终回答")
            yield AgentEvent("message", {"text": response["content"]})
            checkpoint()
            yield AgentEvent("done", {"status": "completed"})
            return
        for call in calls:
            checkpoint()
            function = call["function"]
            tool = tools[function["name"]]
            arguments = tool.arguments.model_validate_json(function["arguments"])
            yield AgentEvent(
                "tool_start",
                {
                    "tool_call_id": call["id"],
                    "name": function["name"],
                    "arguments": arguments.model_dump(),
                },
            )
            checkpoint()
            result = tool.invoke(arguments.model_dump_json())
            checkpoint()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )
            yield AgentEvent(
                "tool_result", {"tool_call_id": call["id"], "content": result}
            )
    raise RuntimeError(f"Agent 达到 {max_steps} 次模型调用上限")
