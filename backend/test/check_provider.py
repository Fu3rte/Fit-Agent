import asyncio
import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails
from pydantic import ValidationError

from app.agent.agent_loop import run_agent_loop
from app.agent.config import AgentLoopConfig
from app.agent.tools.files import create_file_tools
from app.agent.usage import summarize_usage
from app.ai.api.openai_completions import (
    REASONING_FIELDS,
    from_openai_response,
    from_openai_usage,
    to_openai_request,
)
from app.ai.context import validate_tool_pairs
from app.ai.messages import (
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)
from app.ai.stream import complete, stream
from app.ai.types import ModelSpec, StreamOptions
from app.model_config import load_model_config
from test.regression_support import TEST_SESSION, run_tool, session_workspace


def check() -> None:
    config = load_model_config()
    spec = ModelSpec(
        api="openai-completions",
        provider=config.OPENAI_PROVIDER,
        id=config.OPENAI_MODEL,
        base_url=config.OPENAI_BASE_URL,
    )
    options: StreamOptions = {"api_key": config.OPENAI_API_KEY}
    timestamp = time_ns() // 1_000_000
    with (
        TemporaryDirectory() as directory,
        config.create_client() as client,
    ):
        root = Path(directory)
        registry = create_file_tools(TEST_SESSION, tmp_root=root)
        workspace, prefix = session_workspace(root)
        system = SystemMessage(
            role="system",
            content="按用户请求执行。",
            timestamp=timestamp,
            sections={"current": "工具参数必须遵循声明。", "gone": None},
            tools_added=[registry["read"].definition()],
        )
        path = workspace / "input.txt"
        path.write_text("provider-check", encoding="utf-8")
        image_path = root / "red.png"
        drawing = (
            "Add-Type -AssemblyName System.Drawing; "
            "$bitmap = [System.Drawing.Bitmap]::new(64, 64); "
            "$graphics = [System.Drawing.Graphics]::FromImage($bitmap); "
            "$graphics.Clear([System.Drawing.Color]::Red); "
            f"$bitmap.Save('{image_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
            "$graphics.Dispose(); $bitmap.Dispose()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", drawing],
            check=True,
            capture_output=True,
        )
        image = ImageContent(
            type="image",
            data=base64.b64encode(image_path.read_bytes()).decode(),
            mime_type="image/png",
        )
        plain = [
            SystemMessage(
                role="system", content="按用户要求回答。", timestamp=timestamp
            ),
            UserMessage(role="user", content="只回答完成。", timestamp=timestamp),
        ]
        text = asyncio.run(complete(spec, {"messages": plain}, options))
        assert text.stop_reason == "stop" and any(
            isinstance(block, TextContent) and block.text for block in text.content
        )
        assert (
            text.provider == config.OPENAI_PROVIDER and text.api == "openai-completions"
        )
        assert (
            text.model == config.OPENAI_MODEL and text.response_id and text.response_model
        )
        assert text.timestamp > 0 and text.usage.total_tokens > 0
        assert "cost" not in json.loads(serialize_message(text))["usage"]
        visual = [
            plain[0],
            UserMessage(
                role="user",
                content=[
                    TextContent(
                        type="text", text="这张图片的主要颜色是什么？只回答中文颜色名。"
                    ),
                    image,
                ],
                timestamp=timestamp,
            ),
        ]
        viewed = asyncio.run(complete(spec, {"messages": visual}, options))
        assert viewed.stop_reason == "stop" and "红" in "".join(
            block.text for block in viewed.content if isinstance(block, TextContent)
        )

        history = [
            system,
            UserMessage(
                role="user",
                content=f"使用 read 读取 {prefix}/input.txt，只调用一次。",
                timestamp=timestamp,
            ),
        ]
        raw = client.chat.completions.create(
            **to_openai_request(history, spec, options),
            tool_choice={"type": "function", "function": {"name": "read"}},
        )
        assistant = from_openai_response(raw, spec)
        assert (
            assistant.stop_reason == "toolUse"
            and assistant.raw_stop_reason == "tool_calls"
        )
        assert assistant.response_id == raw.id and assistant.response_model == raw.model
        assert assistant.timestamp == raw.created * 1000
        assert assistant.usage.total_tokens == raw.usage.total_tokens
        assert (
            assistant.usage.input
            + assistant.usage.cache_read
            + assistant.usage.cache_write
            == raw.usage.prompt_tokens
        )
        assert assistant.usage.output == raw.usage.completion_tokens
        raw_usage = raw.usage.model_dump(exclude_unset=True)
        assert from_openai_usage(raw_usage) == assistant.usage
        with ThreadPoolExecutor(max_workers=1) as executor:
            for field in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_tokens_details",
            ):
                missing = deepcopy(raw_usage)
                del missing[field]
                failure = executor.submit(from_openai_usage, missing).exception()
                assert isinstance(failure, KeyError)
            missing = deepcopy(raw_usage)
            del missing["prompt_tokens_details"]["cached_tokens"]
            assert isinstance(
                executor.submit(from_openai_usage, missing).exception(), KeyError
            )
            invalid = deepcopy(raw_usage)
            invalid["total_tokens"] += 1
            assert isinstance(
                executor.submit(from_openai_usage, invalid).exception(), ValueError
            )
            invalid = {**raw_usage, "cost": None}
            assert isinstance(
                executor.submit(from_openai_usage, invalid).exception(), ValidationError
            )

        def completion(finish_reason: str) -> ChatCompletion:
            return ChatCompletion(
                id="response-limit",
                created=1,
                model=config.OPENAI_MODEL,
                object="chat.completion",
                choices=[
                    Choice(
                        index=0,
                        finish_reason=finish_reason,
                        message=ChatCompletionMessage(
                            role="assistant", content="已生成内容"
                        ),
                    )
                ],
                usage=CompletionUsage(
                    prompt_tokens=4,
                    completion_tokens=2,
                    total_tokens=6,
                    prompt_tokens_details=PromptTokensDetails(cached_tokens=0),
                ),
            )

        limited_message = from_openai_response(completion("length"), spec)
        assert limited_message.stop_reason == "length"
        assert limited_message.raw_stop_reason == "length"
        assert limited_message.usage is not None
        assert "".join(
            block.text
            for block in limited_message.content
            if isinstance(block, TextContent)
        ) == "已生成内容"
        blocked = from_openai_response(completion("content_filter"), spec)
        assert blocked.stop_reason == "error"
        assert blocked.raw_stop_reason == "content_filter"
        assert blocked.error_message == "Provider finish_reason: content_filter"
        assert blocked.usage is not None and blocked.content
        fields = raw.choices[0].message.model_dump(exclude_none=True)
        thoughts = [
            block for block in assistant.content if isinstance(block, ThinkingContent)
        ]
        replay_fields = [field for field in REASONING_FIELDS if fields.get(field)][:1]
        if replay_fields:
            assert thoughts[0].thinking == fields[replay_fields[0]]
            assert thoughts[0].thinking_signature == replay_fields[0]
        if "reasoning_details" in fields:
            replay_fields.append("reasoning_details")
            assert any(
                block.redacted
                and json.loads(block.thinking_signature) == fields["reasoning_details"]
                for block in thoughts
            )
        if not replay_fields:
            assert not thoughts
        calls = [block for block in assistant.content if isinstance(block, ToolCall)]
        assert len(calls) == 1 and isinstance(calls[0].arguments, dict)
        assert "thought_signature" not in calls[0].model_fields_set
        tool_result = run_tool(
            registry[calls[0].name], calls[0].arguments, tool_call_id=calls[0].id
        )
        assert tool_result.is_error is False and "provider-check" in tool_result.content[0].text
        history.extend([assistant, tool_result])
        replay = deepcopy(history)
        replay[3].content.append(image)
        replay.append(
            UserMessage(
                role="user",
                content="报告工具读取的文本及附带图片的主要颜色。无需使用工具。",
                timestamp=timestamp,
            )
        )
        before = [serialize_message(message) for message in replay]
        request = to_openai_request(replay, spec, options)
        api_tool = next(
            message for message in request["messages"] if message["role"] == "tool"
        )
        assert api_tool["tool_call_id"] == calls[0].id
        assert (
            request["messages"][0]["content"]
            == "按用户请求执行。\n\n工具参数必须遵循声明。"
        )
        assert any(
            message["role"] == "user"
            and isinstance(message["content"], list)
            and any(part["type"] == "image_url" for part in message["content"])
            for message in request["messages"]
        )
        answer = asyncio.run(complete(spec, {"messages": replay}, options))
        answer_text = "".join(
            block.text for block in answer.content if isinstance(block, TextContent)
        )
        assert "provider-check" in answer_text and "红" in answer_text
        assert [serialize_message(message) for message in replay] == before
        validate_tool_pairs(replay)

        loop_config = AgentLoopConfig(model=config, max_turns=8)

        async def noop(event):
            pass

        invalid = deepcopy(history[:2])
        invalid[0].tools_added[0].parameters = {}
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                asyncio.run,
                run_agent_loop(
                    [], {"messages": invalid, "tools": registry}, loop_config, noop
                ),
            ).exception()
        assert isinstance(failure, ValueError) and "注册表" in str(failure)

        bounded = [
            plain[0],
            UserMessage(
                role="user",
                content="请按顺序输出数字1至100，用逗号分隔。",
                timestamp=timestamp,
            ),
        ]
        bounded_before = [serialize_message(message) for message in bounded]

        def limited(model, context, stream_options):
            return stream(model, context, {**stream_options, "max_tokens": 1})

        limited_events = []

        async def collect_limited(event):
            limited_events.append(event)

        limited_messages = asyncio.run(
            run_agent_loop(
                [], {"messages": bounded, "tools": registry}, loop_config,
                collect_limited, None, limited,
            )
        )
        assert len(limited_messages) == 1
        final = limited_messages[0]
        assert isinstance(final, AssistantMessage)
        assert final.stop_reason == final.raw_stop_reason == "length"
        assert final.usage is not None and final.usage.output > 0
        assert not any(isinstance(block, ToolCall) for block in final.content)
        assert not any(event["type"] in {"tool_start", "tool_result"} for event in limited_events)
        assert [event["message"] for event in limited_events if event["type"] == "message_end"] == [final]
        assert limited_events[-1]["type"] == "trace_end"
        assert limited_events[-1]["status"] == "completed"
        assert next(event for event in limited_events if event["type"] == "turn_end")["status"] == "completed"
        validate_tool_pairs([*bounded, *limited_messages])
        assert [serialize_message(message) for message in bounded] == bounded_before

        errors = [
            SystemMessage(
                role="system",
                content="仅按用户指定路径读取文件，保留真实读取结果。",
                tools_added=[registry["read"].definition()],
                timestamp=timestamp,
            ),
            UserMessage(
                role="user",
                content=f"必须用 read 读取 {prefix}/absent.txt，"
                "执行一次后说明实际结果，不重试也不改用其他工具。",
                timestamp=timestamp,
            ),
        ]
        events = []

        async def collect(event):
            events.append(event)

        committed = asyncio.run(
            run_agent_loop([], {"messages": errors, "tools": registry}, loop_config, collect)
        )
        assert (
            events[-1]["type"] == "trace_end" and events[-1]["status"] == "completed"
        )
        failed_results = [
            message
            for message in [*errors, *committed]
            if isinstance(message, ToolResultMessage)
        ]
        assert failed_results and all(
            message.is_error is True for message in failed_results
        )
        assert all(
            "工具执行失败" in message.content[0].text for message in failed_results
        )
        validate_tool_pairs([*errors, *committed])
        totals = summarize_usage([*history, answer])
        assert (
            totals.total_tokens == assistant.usage.total_tokens + answer.usage.total_tokens
        )
        assert "cost" not in totals.model_dump(exclude_unset=True)
    print(
        "真实文本、图片、工具图片回放、ID 映射、用量、声明校验、length 与真实文件读取失败结果检查通过"
    )
    print(
        f"真实思考回放字段：{replay_fields}；text_signature/thought_signature 为字段往返验证"
    )


if __name__ == "__main__":
    check()
