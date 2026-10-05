from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from pydantic import ValidationError

from app.agent.usage import summarize_usage
from app.ai.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    serialize_message,
)


def check() -> None:
    usage = Usage(
        input=7,
        output=5,
        cache_read=3,
        cache_write=2,
        cache_write_1h=1,
        reasoning=2,
        total_tokens=17,
        cost=UsageCost(input=1, output=2, cache_read=3, cache_write=4, total=10),
    )
    assistant = AssistantMessage(
        role="assistant",
        content=[],
        api="openai-completions",
        provider="field-check",
        model="field-check",
        response_id="shared-trace",
        usage=usage,
        stop_reason="stop",
        timestamp=1,
    )
    tool = ToolResultMessage(
        role="toolResult",
        tool_call_id="tool-1",
        tool_name="internal-model",
        content=[],
        usage=usage,
        is_error=False,
        timestamp=2,
    )
    absent_id = AssistantMessage.model_validate(
        assistant.model_dump(exclude_unset=True, exclude={"response_id"})
    )
    messages = [
        SystemMessage(role="system", content="instructions", timestamp=0),
        UserMessage(role="user", content="request", timestamp=1),
        assistant,
        deepcopy(assistant),
        absent_id,
        tool,
    ]
    before = [serialize_message(message) for message in messages]
    totals = summarize_usage(messages)
    assert totals.model_dump(exclude_unset=True) == {
        "input": 28,
        "output": 20,
        "cache_read": 12,
        "cache_write": 8,
        "cache_write_1h": 4,
        "reasoning": 8,
        "total_tokens": 68,
        "cost": {
            "input": 4,
            "output": 8,
            "cache_read": 12,
            "cache_write": 16,
            "total": 40,
        },
    }
    assert [serialize_message(message) for message in messages] == before
    assert totals is not usage and totals.cost is not usage.cost
    unknown = Usage.model_validate(
        usage.model_dump(exclude={"cost"}, exclude_unset=True)
    )
    for index in (2, 5):
        partial = deepcopy(messages)
        partial[index].usage = unknown
        summary = summarize_usage(partial)
        assert summary.total_tokens == 68
        assert "cost" not in summary.model_dump(exclude_unset=True)
    no_usage = ToolResultMessage.model_validate(
        tool.model_dump(exclude_unset=True, exclude={"usage"})
    )
    assert summarize_usage([no_usage]).total_tokens == 0
    empty = summarize_usage([]).model_dump(exclude_unset=True)
    assert empty == {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "total_tokens": 0,
    }
    minimal = Usage(input=1, output=2, cache_read=0, cache_write=0, total_tokens=3)
    plain = deepcopy(assistant)
    plain.usage = minimal
    assert summarize_usage([plain]).model_dump(
        exclude_unset=True
    ) == minimal.model_dump(exclude_unset=True)
    mixed = summarize_usage([plain, assistant])
    assert mixed.reasoning == 2 and mixed.cache_write_1h == 1 and mixed.total_tokens == 20
    assert "cost" not in mixed.model_fields_set
    zero = deepcopy(assistant)
    zero.usage.cost = UsageCost(input=0, output=0, cache_read=0, cache_write=0, total=0)
    assert summarize_usage([zero]).cost.total == 0
    pending = deepcopy(assistant)
    pending.stop_reason = "pending"
    null_cost = deepcopy(assistant)
    null_cost.usage.cost = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        for duplicate in (tool, no_usage):
            failure = executor.submit(summarize_usage, [tool, duplicate]).exception()
            assert isinstance(failure, ValueError) and "重复工具结果" in str(failure)
        failure = executor.submit(summarize_usage, [pending]).exception()
        assert isinstance(failure, ValueError) and "完整助手消息" in str(failure)
        failure = executor.submit(summarize_usage, [null_cost]).exception()
        assert isinstance(failure, ValidationError)
    print("用量字段结构、按条目统计、工具重复拒绝、子计数、费用缺省与原值保护检查通过")


if __name__ == "__main__":
    check()
