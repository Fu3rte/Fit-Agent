from src.agent.message_context import convert_to_llm
from src.agent.messages import AgentMessage, AssistantMessage, ToolResultMessage, Usage


def summarize_usage(messages: list[AgentMessage]) -> Usage:
    records: list[Usage] = []
    tool_ids: set[str] = set()
    for message in convert_to_llm(messages):
        if isinstance(message, AssistantMessage):
            if message.stopReason == "pending":
                raise ValueError("用量汇总仅接受完整助手消息")
            records.append(message.usage)
        elif isinstance(message, ToolResultMessage):
            if message.toolCallId in tool_ids:
                raise ValueError(f"重复工具结果: {message.toolCallId}")
            tool_ids.add(message.toolCallId)
            if message.usage is not None:
                records.append(message.usage)
    totals = {
        field: sum(getattr(usage, field) for usage in records)
        for field in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    }
    for field in ("reasoning", "cacheWrite1h"):
        reported = [
            getattr(usage, field)
            for usage in records
            if field in usage.model_fields_set
        ]
        if reported:
            totals[field] = sum(reported)
    if records and all(usage.cost is not None for usage in records):
        totals["cost"] = {
            field: sum(getattr(usage.cost, field) for usage in records)
            for field in ("input", "output", "cacheRead", "cacheWrite", "total")
        }
    return Usage.model_validate(totals)
