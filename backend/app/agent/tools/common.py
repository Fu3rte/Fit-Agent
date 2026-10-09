import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import ConfigDict

from app.agent.tool import (
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
)
from app.ai.messages import TextContent
from app.domain.business.errors import BusinessError
from app.domain.business.models import BusinessContext, BusinessModel

T = TypeVar("T")
MainLoopCall = Callable[[Awaitable[T]], T]


class FrozenBusinessContext(BusinessContext):
    # 批次绑定快照：可信身份与日期固定到执行实例，源上下文后续变化不产生影响。
    model_config = ConfigDict(frozen=True)


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


async def check_business_permission(
    context: BeforeToolCallContext, authorize: Callable, call: MainLoopCall,
) -> BeforeToolCallResult | None:
    if not isinstance(context.trusted_context, FrozenBusinessContext):
        raise TypeError("保存工具缺少可信业务上下文绑定")
    try:
        await asyncio.to_thread(
            call, authorize(context.trusted_context, context.arguments)
        )
    except BusinessError as error:
        return BeforeToolCallResult(
            block=True, reason=json.dumps(error.detail(), ensure_ascii=False)
        )
    return None


def unbound(tool_call_id, params, signal, on_update) -> AgentToolResult:
    raise RuntimeError("业务工具声明实例不可执行")
