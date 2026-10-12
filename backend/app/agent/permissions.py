from threading import Event

from app.agent.tool import BeforeToolCall, BeforeToolCallContext, BeforeToolCallResult
from app.agent.tools.files import check_file_permission


def create_before_tool_call() -> BeforeToolCall:
    # 业务保存由独立确认 HTTP 调用内部业务服务，模型注册表不提供保存工具；
    # 运行权限入口只保留文件写入边界。
    async def before_tool_call(
        context: BeforeToolCallContext, signal: Event | None,
    ) -> BeforeToolCallResult | None:
        if signal is not None and signal.is_set():
            return None
        if context.tool_call.name in {"write", "edit"}:
            return check_file_permission(context)
        return None

    return before_tool_call
