from app.agent.message_context import convert_to_llm
from app.ai.messages import AssistantMessage, Message, ToolResultMessage, Usage


def summarize_usage(messages: list[Message]) -> Usage:
    records: list[Usage] = []
    tool_ids: set[str] = set()
    for message in convert_to_llm(messages):
        if isinstance(message, AssistantMessage):
            if message.stop_reason == "pending" or message.usage is None:
                raise ValueError("用量汇总仅接受完整助手消息")
            records.append(message.usage)
        elif isinstance(message, ToolResultMessage):
            if message.tool_call_id in tool_ids:
                raise ValueError(f"重复工具结果: {message.tool_call_id}")
            tool_ids.add(message.tool_call_id)
            if message.usage is not None:
                records.append(message.usage)
    totals = {
        field: sum(getattr(usage, field) for usage in records)
        for field in ("input", "output", "cache_read", "cache_write", "total_tokens")
    }
    for field in ("reasoning", "cache_write_1h"):
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
            for field in ("input", "output", "cache_read", "cache_write", "total")
        }
    return Usage.model_validate(totals)
