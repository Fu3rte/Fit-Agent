import json
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.agent.tool import AgentToolResult
from app.ai.messages import TextContent
from app.domain.business.errors import BusinessError
from app.domain.business.models import BusinessModel

T = TypeVar("T")
MainLoopCall = Callable[[Awaitable[T]], T]


def envelope(payload: object, is_error: bool = False) -> AgentToolResult:
    return AgentToolResult(
        [
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            )
        ],
        is_error=is_error,
    )


def business_result(
    operation: Awaitable[BusinessModel],
    call: MainLoopCall,
    on_success: Callable[[BusinessModel], None] | None = None,
) -> AgentToolResult:
    try:
        response = call(operation)
    except BusinessError as error:
        return envelope(error.detail(), True)
    if on_success is not None:
        on_success(response)
    return envelope(response.model_dump())


def unbound(tool_call_id, params, signal, on_update) -> AgentToolResult:
    raise RuntimeError("业务工具声明实例不可执行")
