"""Tests for agent_core.ai.frames (TS assistant-message-frame.ts behavior, plan §11.2).

No pytest dependency (house style): plain asserts + a tiny raises helper.
"""

from agent_core.ai.frames import (
    AssistantMessageFrameEncoder,
    FrameStart,
    FrameTextDelta,
    FrameTextEnd,
    FrameTextStart,
    FrameThinkingDelta,
    FrameThinkingEnd,
    FrameToolCallDelta,
    FrameToolCallCheckpoint,
    FrameToolCallEnd,
    FrameToolCallStart,
    reduce_assistant_message_frames,
)
from agent_core.ai.types import (
    AssistantMessage,
    DoneEvent,
    ErrorEvent,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
)


def _raises(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def make_partial(*content) -> AssistantMessage:
    return AssistantMessage(
        content=list(content),
        api="anthropic-messages",
        provider="anthropic",
        model="claude-test",
        usage=Usage(input=1, output=2, cache_read=0, cache_write=0, total_tokens=3),
        stop_reason="pending",
        timestamp=1234,
    )


def encode_all(events):
    encoder = AssistantMessageFrameEncoder()
    return [f for f in (encoder.encode(e) for e in events) if f is not None]


class TestRoundTrip:
    def test_full_sequence_reconstructs_message(self):
        text_block = TextContent(text="Hel", text_signature="legacy-sig")
        p1 = make_partial(text_block)
        think_block = ThinkingContent(thinking="th", thinking_signature="sig-th", redacted=False)
        p2 = make_partial(text_block, think_block)
        p3 = make_partial(text_block, think_block, ToolCall(id="t1", name="run", arguments={}))

        events = [
            StartEvent(partial=make_partial()),
            TextStartEvent(content_index=0, partial=p1),
            TextDeltaEvent(content_index=0, delta="Hello", partial=p1),
            TextDeltaEvent(content_index=0, delta=" world", partial=p1),
            TextEndEvent(content_index=0, content="Hello world", partial=p1),
            ThinkingStartEvent(content_index=1, partial=p2),
            # Delta stream replays the covered prefix "th" first: the encoder
            # drops it (already visible at thinking_start) and keeps the rest.
            ThinkingDeltaEvent(content_index=1, delta="th", partial=p2),
            ThinkingDeltaEvent(content_index=1, delta="inking", partial=p2),
            ThinkingEndEvent(content_index=1, content="thinking", partial=p2),
            ToolCallStartEvent(content_index=2, partial=p3),
            ToolCallDeltaEvent(content_index=2, delta='{"a"', partial=p3),
            ToolCallDeltaEvent(content_index=2, delta=": 1}", partial=p3),
            ToolCallEndEvent(
                content_index=2,
                tool_call=ToolCall(id="t1", name="run", arguments={"a": 1}, thought_signature="tsig", namespace="ns"),
                partial=p3,
            ),
            DoneEvent(reason="stop", message=make_partial()),
        ]
        frames = encode_all(events)
        assert [f.type for f in frames] == [
            "start",
            "text_start",
            "text_delta",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_delta",
            "toolcall_end",
        ]

        message = reduce_assistant_message_frames(frames)
        assert message is not None
        assert [b.to_dict() for b in message.content] == [
            {"type": "text", "text": "Hello world", "textSignature": "legacy-sig"},
            {"type": "thinking", "thinking": "thinking", "thinkingSignature": "sig-th", "redacted": False},
            {
                "type": "toolCall",
                "id": "t1",
                "name": "run",
                "arguments": {"a": 1},
                "thoughtSignature": "tsig",
                "namespace": "ns",
            },
        ]
        # Start partial clone keeps model identity + usage, stopReason forced pending.
        assert message.model == "claude-test"
        assert message.provider == "anthropic"
        assert message.usage.total_tokens == 3
        assert message.stop_reason == "pending"

    def test_start_partial_offset_covers_initial_text(self):
        # text_start already carries "Hel"; the first delta "Hello" must only
        # replay the uncovered suffix "lo".
        text_block = TextContent(text="Hel")
        p1 = make_partial(text_block)
        frames = encode_all(
            [
                StartEvent(partial=make_partial()),
                TextStartEvent(content_index=0, partial=p1),
                TextDeltaEvent(content_index=0, delta="Hello", partial=p1),
                TextEndEvent(content_index=0, content="Hello", partial=p1),
            ]
        )
        assert isinstance(frames[2], FrameTextDelta) and frames[2].delta == "lo"
        assert isinstance(frames[3], FrameTextEnd) and frames[3].content == "Hello"
        message = reduce_assistant_message_frames(frames)
        assert message is not None
        assert message.content[0].text == "Hello"  # type: ignore[union-attr]

    def test_thinking_start_offset_and_signature_preserved(self):
        think_block = ThinkingContent(thinking="th", thinking_signature="sig", redacted=None)
        p = make_partial(think_block)
        frames = encode_all(
            [
                StartEvent(partial=make_partial()),
                ThinkingStartEvent(content_index=0, partial=p),
                ThinkingDeltaEvent(content_index=0, delta="thinking", partial=p),
                ThinkingEndEvent(content_index=0, content="thinking", partial=p),
            ]
        )
        assert [f.type for f in frames] == [
            "start",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
        ]
        assert isinstance(frames[2], FrameThinkingDelta)
        assert frames[2].delta == "inking"  # "th" covered
        assert isinstance(frames[3], FrameThinkingEnd)
        assert frames[3].thinking_signature == "sig"
        message = reduce_assistant_message_frames(frames)
        assert message is not None
        block = message.content[0]
        assert block.thinking == "thinking"  # type: ignore[union-attr]
        assert block.thinking_signature == "sig"  # type: ignore[union-attr]


class TestTerminalExcluded:
    def test_done_produces_no_frame(self):
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        assert encoder.encode(DoneEvent(reason="stop", message=make_partial())) is None

    def test_error_produces_no_frame_even_before_start(self):
        encoder = AssistantMessageFrameEncoder()
        assert encoder.encode(ErrorEvent(reason="aborted", error=make_partial("x"))) is None

    def test_frames_never_contain_done_or_error(self):
        frames = encode_all(
            [
                StartEvent(partial=make_partial()),
                DoneEvent(reason="stop", message=make_partial()),
            ]
        )
        assert all(f.type not in ("done", "error") for f in frames)


class TestToolCallCheckpoint:
    def test_legacy_grammar_prefix_extension(self):
        # toolcall_start already contains {"a": 1}, but the delta stream begins
        # from empty input and extends the snapshot: checkpoint is emitted once
        # the parsed arguments are a JSON-prefix extension of the snapshot.
        p = make_partial(ToolCall(id="t2", name="run", arguments={"a": 1}))
        frames = encode_all(
            [
                StartEvent(partial=make_partial()),
                ToolCallStartEvent(content_index=0, partial=p),
                ToolCallDeltaEvent(content_index=0, delta='{"a": 1, "b": 2', partial=p),
                ToolCallDeltaEvent(content_index=0, delta="}", partial=p),
                ToolCallEndEvent(
                    content_index=0,
                    tool_call=ToolCall(id="t2", name="run", arguments={"a": 1, "b": 2}),
                    partial=p,
                ),
            ]
        )
        assert [f.type for f in frames] == [
            "start",
            "toolcall_start",
            "toolcall_checkpoint",
            "toolcall_delta",
            "toolcall_end",
        ]
        checkpoint = frames[2]
        assert isinstance(checkpoint, FrameToolCallCheckpoint)
        # Checkpoint json is raw partial-JSON text progress, not verified args.
        assert checkpoint.json == '{"a": 1, "b": 2'
        message = reduce_assistant_message_frames(frames)
        assert message is not None
        assert message.content[0].arguments == {"a": 1, "b": 2}  # type: ignore[union-attr]

    def test_unparseable_delta_never_catches_up(self):
        # Once garbage enters the catch-up buffer it stays: the block never
        # produces a checkpoint from deltas; only toolcall_end settles args.
        p = make_partial(ToolCall(id="t3", name="run", arguments={"a": 9}))
        frames = encode_all(
            [
                StartEvent(partial=make_partial()),
                ToolCallStartEvent(content_index=0, partial=p),
                ToolCallDeltaEvent(content_index=0, delta="zz", partial=p),
                ToolCallDeltaEvent(content_index=0, delta='{"a": 9}', partial=p),
                ToolCallEndEvent(
                    content_index=0,
                    tool_call=ToolCall(id="t3", name="run", arguments={"a": 9}),
                    partial=p,
                ),
            ]
        )
        assert [f.type for f in frames] == [
            "start",
            "toolcall_start",
            "toolcall_end",
        ]
        message = reduce_assistant_message_frames(frames)
        assert message is not None
        assert message.content[0].arguments == {"a": 9}  # type: ignore[union-attr]

    def test_empty_args_caught_up_passes_deltas_through(self):
        p = make_partial(ToolCall(id="t4", name="run", arguments={}))
        frames = encode_all(
            [
                StartEvent(partial=make_partial()),
                ToolCallStartEvent(content_index=0, partial=p),
                ToolCallDeltaEvent(content_index=0, delta="", partial=p),
                ToolCallDeltaEvent(content_index=0, delta='{"x": 1}', partial=p),
            ]
        )
        # Empty delta while caught up is dropped (TS parity).
        assert [f.type for f in frames] == [
            "start",
            "toolcall_start",
            "toolcall_delta",
        ]
        assert isinstance(frames[2], FrameToolCallDelta)
        assert frames[2].delta == '{"x": 1}'


class TestReducer:
    def test_no_start_frame_returns_none(self):
        assert reduce_assistant_message_frames([FrameTextDelta(content_index=0, delta="x")]) is None

    def test_frames_before_start_poison_a_later_start(self):
        # TS parity: a frame before the start frame is tolerated only while no
        # start follows; a later start makes the whole sequence an error.
        _raises(
            reduce_assistant_message_frames,
            [
                FrameTextDelta(content_index=0, delta="dropped"),
                FrameStart(partial=make_partial()),
            ],
        )
        assert reduce_assistant_message_frames([FrameTextDelta(content_index=0, delta="x")]) is None

    def test_duplicate_start_raises(self):
        _raises(reduce_assistant_message_frames, [FrameStart(partial=make_partial()), FrameStart(partial=make_partial())])

    def test_gap_raises(self):
        _raises(
            reduce_assistant_message_frames,
            [
                FrameStart(partial=make_partial()),
                FrameTextStart(content_index=1, content=TextContent(text="gap")),
            ],
        )

    def test_text_end_clears_signature_unless_provided(self):
        start = FrameStart(partial=make_partial())
        cleared = reduce_assistant_message_frames(
            [
                start,
                FrameTextStart(content_index=0, content=TextContent(text="abc", text_signature="s")),
                FrameTextEnd(content_index=0, content="abc"),
            ]
        )
        assert cleared is not None
        assert cleared.content[0].text_signature is None  # type: ignore[union-attr]

        replaced = reduce_assistant_message_frames(
            [
                start,
                FrameTextStart(content_index=0, content=TextContent(text="abc", text_signature="s")),
                FrameTextEnd(content_index=0, content="abc", text_signature="s2"),
            ]
        )
        assert replaced is not None
        assert replaced.content[0].text_signature == "s2"  # type: ignore[union-attr]

    def test_pending_toolcall_json_parsed_at_end(self):
        message = reduce_assistant_message_frames(
            [
                FrameStart(partial=make_partial()),
                FrameToolCallStart(content_index=0, tool_call=ToolCall(id="t", name="run", arguments={})),
                FrameToolCallCheckpoint(content_index=0, json='{"x": 5'),
                FrameToolCallDelta(content_index=0, delta=', "y": 6'),
            ]
        )
        assert message is not None
        assert message.content[0].arguments == {"x": 5, "y": 6}  # type: ignore[union-attr]

    def test_frames_are_not_mutated(self):
        start_frame = FrameStart(partial=make_partial())
        end_frame = FrameToolCallEnd(
            content_index=0,
            id="t",
            name="run",
            arguments={"a": 1},
            thought_signature="sig",
        )
        message = reduce_assistant_message_frames(
            [
                start_frame,
                FrameToolCallStart(content_index=0, tool_call=ToolCall(id="t", name="run", arguments={})),
                end_frame,
            ]
        )
        assert message is not None
        # Deep copies: mutating the replay does not touch the frames.
        message.content[0].arguments["a"] = 999  # type: ignore[union-attr]
        message.usage.input = 42
        assert end_frame.arguments == {"a": 1}
        assert start_frame.partial.usage.input == 1
        assert message.usage is not start_frame.partial.usage


class TestEncoderValidation:
    def test_event_before_start_raises(self):
        encoder = AssistantMessageFrameEncoder()
        _raises(encoder.encode, TextStartEvent(content_index=0, partial=make_partial(TextContent(text="x"))))

    def test_duplicate_start_raises(self):
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        _raises(encoder.encode, StartEvent(partial=make_partial()))

    def test_done_before_start_raises(self):
        encoder = AssistantMessageFrameEncoder()
        _raises(encoder.encode, DoneEvent(reason="stop", message=make_partial()))

    def test_event_after_terminal_raises(self):
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        encoder.encode(DoneEvent(reason="stop", message=make_partial()))
        _raises(encoder.encode, TextDeltaEvent(content_index=0, delta="x", partial=make_partial()))

    def test_missing_content_block_raises(self):
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        # Event references an index that has no block in `partial`.
        _raises(encoder.encode, TextStartEvent(content_index=3, partial=make_partial()))

    def test_wrong_block_kind_raises(self):
        p = make_partial(TextContent(text="x"))
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        _raises(encoder.encode, ThinkingStartEvent(content_index=0, partial=p))

    def test_block_started_twice_raises(self):
        p = make_partial(TextContent(text="x"))
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        encoder.encode(TextStartEvent(content_index=0, partial=p))
        _raises(encoder.encode, TextStartEvent(content_index=0, partial=p))

    def test_negative_index_raises(self):
        encoder = AssistantMessageFrameEncoder()
        encoder.encode(StartEvent(partial=make_partial()))
        _raises(encoder.encode, TextStartEvent(content_index=-1, partial=make_partial(TextContent(text="x"))))
