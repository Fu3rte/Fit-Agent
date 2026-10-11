import pytest

from app.agent import compaction as C
from app.ai.messages import AssistantMessage, SystemMessage, TextContent, Tool, Usage
from test import check_compaction_core as T


def usage(total: int) -> Usage:
    return Usage(input=total, output=0, cache_read=0, cache_write=0, total_tokens=total)


def base_path() -> list:
    return [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题")),
        T.entry("a1", "u1", T.assistant("回答", usage=usage(999))),
    ]


def swapped_system_context(path: list, system: SystemMessage) -> C.EffectiveContext:
    return C.EffectiveContext(
        messages=[system, *path[1].messages, *path[2].messages],
        source_entry_ids=["sys", "u1", "a1"],
    )


def test_consistent_projection_keeps_usage_and_trailing() -> None:
    path = base_path()
    estimate = C.estimate_projected_context_tokens(C.build_effective_context(path), path)
    assert estimate.last_usage_index == 2
    assert estimate.usage_tokens == 999
    assert estimate.trailing_tokens == 0
    assert estimate.tokens == 999

    extended = [*path, T.entry("u2", "a1", T.user("追问"))]
    estimate = C.estimate_projected_context_tokens(
        C.build_effective_context(extended), extended
    )
    trailing = C.estimate_tokens(extended[3].messages[0])
    assert estimate.last_usage_index == 2
    assert estimate.usage_tokens == 999
    assert estimate.trailing_tokens == trailing
    assert estimate.tokens == 999 + trailing


def test_changed_system_state_invalidates_old_usage() -> None:
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("问题")),
        T.entry("a1", "u1", T.assistant("回答")),
    ]
    context = C.EffectiveContext(
        messages=[T.system_message("新系统状态" * 100000), path[1].messages[0], path[2].messages[0]],
        source_entry_ids=["sys", "u1", "a1"],
    )
    estimate = C.estimate_projected_context_tokens(context, path)
    assert estimate.last_usage_index is None
    assert estimate.tokens >= C.estimate_tokens(context.messages[0])


def test_changed_sections_invalidate_old_usage() -> None:
    path = base_path()
    changed = SystemMessage(
        role="system",
        content="系统指令",
        sections={"business_context": "动态上下文" * 4000},
        timestamp=T.tick(),
    )
    context = swapped_system_context(path, changed)
    estimate = C.estimate_projected_context_tokens(context, path)
    assert estimate.last_usage_index is None
    assert estimate.usage_tokens == 0
    assert estimate.tokens == sum(
        C.estimate_tokens(message) for message in context.messages
    )


def test_changed_tool_schema_invalidates_old_usage() -> None:
    path = base_path()
    changed = SystemMessage(
        role="system",
        content="系统指令",
        tools_added=[
            Tool(
                name="echo",
                description="回显",
                parameters={
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": ["n"],
                },
            )
        ],
        timestamp=T.tick(),
    )
    context = swapped_system_context(path, changed)
    estimate = C.estimate_projected_context_tokens(context, path)
    assert estimate.last_usage_index is None
    assert estimate.tokens >= C.estimate_tokens(changed)


def test_compaction_invalidates_retained_usage() -> None:
    path = base_path()
    context = C.EffectiveContext(
        messages=[*path[1].messages, *path[2].messages],
        source_entry_ids=["u1", "a1"],
    )
    fresh = [*path, T.compaction_entry("c1", "a1", "u1")]
    estimate = C.estimate_projected_context_tokens(context, fresh)
    assert estimate.last_usage_index is None
    assert estimate.usage_tokens == 0
    assert estimate.tokens == sum(
        C.estimate_tokens(message) for message in context.messages
    )


def test_missing_or_unusable_usage_uses_full_estimate() -> None:
    question = T.user("问题")
    errored = AssistantMessage(
        role="assistant",
        content=[TextContent(type="text", text="回答")],
        api="openai-completions",
        provider="test",
        model="test",
        usage=T.USAGE,
        stop_reason="error",
        error_message="boom",
        timestamp=T.tick(),
    )
    states = (
        T.assistant("回答", usage=T.USAGE, stop_reason="aborted"),
        errored,
        T.assistant("回答", usage=usage(0)),
        T.assistant("回答", usage=None, stop_reason="pending"),
    )
    for last in states:
        messages = [question, last]
        estimate = C.estimate_context_tokens(messages)
        assert estimate.last_usage_index is None
        assert estimate.usage_tokens == 0
        assert estimate.tokens == sum(
            C.estimate_tokens(message) for message in messages
        )


def test_inserted_prefix_invalidates_usage_then_new_assistant_reuses_it() -> None:
    old = T.assistant("旧回答", usage=usage(5000))
    inserted = T.user("插入的摘要")
    prompt = T.user("新问题")
    fresh = T.assistant("新回答", usage=usage(2000))
    assert old.timestamp < inserted.timestamp < prompt.timestamp < fresh.timestamp

    stale = C.estimate_context_tokens([inserted, old])
    assert stale.last_usage_index is None
    assert stale.usage_tokens == 0

    reused = C.estimate_context_tokens([inserted, old, prompt, fresh])
    assert reused.last_usage_index == 3
    assert reused.usage_tokens == 2000
    assert reused.trailing_tokens == 0


def test_post_compaction_assistant_usage_reused() -> None:
    path = [
        T.entry("sys", None, T.system_message()),
        T.entry("u1", "sys", T.user("历史问题")),
        T.entry("a1", "u1", T.assistant("历史回答", usage=usage(4000))),
        T.compaction_entry("c1", "a1", "u1"),
        T.entry("u2", "c1", T.user("新问题")),
        T.entry("a2", "u2", T.assistant("新回答", usage=usage(700))),
    ]
    context = C.build_effective_context(path)
    estimate = C.estimate_projected_context_tokens(context, path)
    assert context.source_entry_ids == ["c1", "c1", "u1", "a1", "u2", "a2"]
    assert estimate.last_usage_index == 5
    assert estimate.usage_tokens == 700
    assert estimate.trailing_tokens == 0


def test_illegal_source_mapping_rejected() -> None:
    path = base_path()
    with pytest.raises(ValueError, match="数量不一致"):
        C.estimate_projected_context_tokens(
            C.EffectiveContext(messages=path[1].messages, source_entry_ids=[]), path
        )
    with pytest.raises(ValueError, match="不在当前路径"):
        C.estimate_projected_context_tokens(
            C.EffectiveContext(messages=path[1].messages, source_entry_ids=["ghost"]), path
        )


def check() -> None:
    test_consistent_projection_keeps_usage_and_trailing()
    test_changed_system_state_invalidates_old_usage()
    test_changed_sections_invalidate_old_usage()
    test_changed_tool_schema_invalidates_old_usage()
    test_compaction_invalidates_retained_usage()
    test_missing_or_unusable_usage_uses_full_estimate()
    test_inserted_prefix_invalidates_usage_then_new_assistant_reuses_it()
    test_post_compaction_assistant_usage_reused()
    test_illegal_source_mapping_rejected()
    print("usage 基准：一致投影保留、状态变化失效、压缩重算、完整估算、重启用与非法映射拒绝通过")


if __name__ == "__main__":
    check()
