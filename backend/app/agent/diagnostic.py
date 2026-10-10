from contextlib import aclosing
from time import monotonic

from app.ai.messages import SystemMessage, Tool, ToolCall, UserMessage
from app.ai.stream import stream
from app.ai.types import ModelSpec, StreamOptions

DIAGNOSTIC_TOOL_NAME = "diagnostic_echo"
DIAGNOSTIC_TOKEN = "fit-agent-diagnostic"

_TOOL = Tool(
    name=DIAGNOSTIC_TOOL_NAME,
    description="回显诊断令牌，用于确认模型的原生工具调用能力。",
    parameters={
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "诊断令牌"},
        },
        "required": ["token"],
        "additionalProperties": False,
    },
)

_INSTRUCTIONS = (
    "这是模型配置诊断请求。你必须调用工具 "
    f"{DIAGNOSTIC_TOOL_NAME}，其 token 参数必须精确等于 {DIAGNOSTIC_TOKEN}。"
    "除该工具调用外不要输出其它内容。"
)

# 供应商失败的分类说明：不包含原始响应、授权头或凭据。
_FAILURE_MESSAGES = {
    "AuthenticationError": "模型服务鉴权失败。",
    "PermissionDeniedError": "模型服务拒绝访问。",
    "NotFoundError": "模型或端点不存在。",
    "RateLimitError": "模型服务限流。",
    "APIConnectionError": "无法连接模型服务。",
    "APITimeoutError": "模型服务响应超时。",
}


def _safe_failure(error: BaseException) -> str:
    message = _FAILURE_MESSAGES.get(type(error).__name__)
    return message or f"诊断未通过（{type(error).__name__}）。"


def _elapsed_ms(started: float) -> int:
    return max(0, int((monotonic() - started) * 1000))


async def run_diagnostic(
    *,
    api: str,
    base_url: str,
    model: str,
    provider: str,
    api_key: str,
) -> dict:
    # 独立诊断：仅提供诊断工具声明，不进入业务工具执行链路，不读写业务数据。
    spec = ModelSpec(api=api, provider=provider, id=model, base_url=base_url)
    options: StreamOptions = {"api_key": api_key, "max_tokens": 1024}
    context = {
        "messages": [
            SystemMessage(
                role="system",
                content=_INSTRUCTIONS,
                tools_added=[_TOOL],
                timestamp=0,
            ),
            UserMessage(role="user", content="执行诊断。", timestamp=0),
        ]
    }
    started = monotonic()
    try:
        response = stream(spec, context, options)
        saw_toolcall = False
        async with aclosing(response):
            async for event in response:
                if event["type"] == "toolcall_end":
                    saw_toolcall = True
        message = await response.result()
    except Exception as error:
        return {
            "ok": False,
            "latency_ms": _elapsed_ms(started),
            "message": _safe_failure(error),
        }
    latency = _elapsed_ms(started)
    if message.stop_reason != "toolUse":
        return {"ok": False, "latency_ms": latency, "message": "模型未以工具调用结束响应。"}
    if not saw_toolcall:
        return {"ok": False, "latency_ms": latency, "message": "流式响应缺少工具调用事件。"}
    calls = [block for block in message.content if isinstance(block, ToolCall)]
    if len(calls) != 1:
        return {"ok": False, "latency_ms": latency, "message": "诊断工具调用数量不符合预期。"}
    call = calls[0]
    if call.name != DIAGNOSTIC_TOOL_NAME:
        return {"ok": False, "latency_ms": latency, "message": "诊断工具名称不符合预期。"}
    if call.arguments != {"token": DIAGNOSTIC_TOKEN}:
        return {"ok": False, "latency_ms": latency, "message": "诊断工具参数不符合预期。"}
    return {"ok": True, "latency_ms": latency, "message": "诊断通过：流式响应与原生工具调用正常。"}
