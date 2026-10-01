from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from pydantic import ValidationError

from src.agent.messages import (
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
    serialize_message,
)
from src.agent.usage import summarize_usage


def check() -> None:
    usage = Usage(
        input=7,
        output=5,
        cacheRead=3,
        cacheWrite=2,
        cacheWrite1h=1,
        reasoning=2,
        totalTokens=17,
        cost=UsageCost(input=1, output=2, cacheRead=3, cacheWrite=4, total=10),
    )
    assistant = AssistantMessage(
        role="assistant",
        content=[],
        api="openai-completions",
        provider="field-check",
        model="field-check",
        responseId="shared-trace",
        usage=usage,
        stopReason="stop",
        timestamp=1,
    )
    tool = ToolResultMessage(
        role="toolResult",
        toolCallId="tool-1",
        toolName="internal-model",
        content=[],
        usage=usage,
        isError=False,
        timestamp=2,
    )
    absent_id = AssistantMessage.model_validate(
        assistant.model_dump(exclude_unset=True, exclude={"responseId"})
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
        "cacheRead": 12,
        "cacheWrite": 8,
        "cacheWrite1h": 4,
        "reasoning": 8,
        "totalTokens": 68,
        "cost": {
            "input": 4,
            "output": 8,
            "cacheRead": 12,
            "cacheWrite": 16,
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
        assert summary.totalTokens == 68
        assert "cost" not in summary.model_dump(exclude_unset=True)
    no_usage = ToolResultMessage.model_validate(
        tool.model_dump(exclude_unset=True, exclude={"usage"})
    )
    assert summarize_usage([no_usage]).totalTokens == 0
    empty = summarize_usage([]).model_dump(exclude_unset=True)
    assert empty == {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
    }
    minimal = Usage(input=1, output=2, cacheRead=0, cacheWrite=0, totalTokens=3)
    plain = deepcopy(assistant)
    plain.usage = minimal
    assert summarize_usage([plain]).model_dump(
        exclude_unset=True
    ) == minimal.model_dump(exclude_unset=True)
    mixed = summarize_usage([plain, assistant])
    assert mixed.reasoning == 2 and mixed.cacheWrite1h == 1 and mixed.totalTokens == 20
    assert "cost" not in mixed.model_fields_set
    zero = deepcopy(assistant)
    zero.usage.cost = UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0)
    assert summarize_usage([zero]).cost.total == 0
    pending = deepcopy(assistant)
    pending.stopReason = "pending"
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
