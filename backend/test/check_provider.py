import base64
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns

from pydantic import ValidationError

from src.agent.loop import run_turn
from src.agent.message_context import validate_tool_pairs
from src.agent.messages import (
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolReference,
    ToolResultMessage,
    UserMessage,
    serialize_message,
)
from src.agent.providers.openai import (
    REASONING_FIELDS,
    complete,
    from_openai_response,
    from_openai_usage,
    to_openai_request,
)
from src.agent.tools.bash import create_bash_tool
from src.agent.tools.files import WORKSPACE, create_file_tools
from src.agent.usage import summarize_usage
from src.model_config import load_model_config


def check() -> None:
    registry = {**create_file_tools(), "bash": create_bash_tool()}
    config = load_model_config()
    timestamp = time_ns() // 1_000_000
    system = SystemMessage(
        role="system",
        content="按用户请求执行。",
        timestamp=timestamp,
        sections={"current": "工具参数必须遵循声明。", "gone": None},
        toolsAdded=[registry["read"].definition()],
    )
    with (
        TemporaryDirectory(dir=WORKSPACE) as directory,
        config.create_client() as client,
    ):
        root = Path(directory)
        path = root / "input.txt"
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
            mimeType="image/png",
        )
        plain = [
            SystemMessage(
                role="system", content="按用户要求回答。", timestamp=timestamp
            ),
            UserMessage(role="user", content="只回答完成。", timestamp=timestamp),
        ]
        text = complete(client, config, plain)
        assert text.stopReason == "stop" and any(
            isinstance(block, TextContent) and block.text for block in text.content
        )
        assert (
            text.provider == config.OPENAI_PROVIDER and text.api == "openai-completions"
        )
        assert (
            text.model == config.OPENAI_MODEL and text.responseId and text.responseModel
        )
        assert text.timestamp > 0 and text.usage.totalTokens > 0
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
        viewed = complete(client, config, visual)
        assert viewed.stopReason == "stop" and "红" in "".join(
            block.text for block in viewed.content if isinstance(block, TextContent)
        )

        history = [
            system,
            UserMessage(
                role="user",
                content=f"使用 read 读取 {root.name}/input.txt，只调用一次。",
                timestamp=timestamp,
            ),
        ]
        raw = client.chat.completions.create(
            **to_openai_request(history, config),
            tool_choice={"type": "function", "function": {"name": "read"}},
        )
        assistant = from_openai_response(raw, config)
        assert (
            assistant.stopReason == "toolUse"
            and assistant.rawStopReason == "tool_calls"
        )
        assert assistant.responseId == raw.id and assistant.responseModel == raw.model
        assert assistant.timestamp == raw.created * 1000
        assert assistant.usage.totalTokens == raw.usage.total_tokens
        assert (
            assistant.usage.input
            + assistant.usage.cacheRead
            + assistant.usage.cacheWrite
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
        fields = raw.choices[0].message.model_dump(exclude_none=True)
        thoughts = [
            block for block in assistant.content if isinstance(block, ThinkingContent)
        ]
        for field in REASONING_FIELDS:
            if field in fields and fields[field] != "":
                assert thoughts[0].thinking == fields[field]
                assert thoughts[0].thinkingSignature == field
                assert (
                    sum(
                        block.thinkingSignature in REASONING_FIELDS
                        for block in thoughts
                    )
                    == 1
                )
                break
        replay_fields = [field for field in REASONING_FIELDS if fields.get(field)][:1]
        if "reasoning_details" in fields:
            replay_fields.append("reasoning_details")
            assert any(
                block.redacted
                and json.loads(block.thinkingSignature) == fields["reasoning_details"]
                for block in thoughts
            )
        if not replay_fields:
            assert not thoughts
        assert all(
            "textSignature" not in block.model_fields_set
            for block in assistant.content
            if isinstance(block, TextContent)
        )
        calls = [block for block in assistant.content if isinstance(block, ToolCall)]
        assert len(calls) == 1 and isinstance(calls[0].arguments, dict)
        assert "thoughtSignature" not in calls[0].model_fields_set
        assert "namespace" not in calls[0].model_fields_set
        result = registry[calls[0].name].invoke(json.dumps(calls[0].arguments))
        assert result.isError is False and "provider-check" in result.content
        history.extend(
            [
                assistant,
                ToolResultMessage(
                    role="toolResult",
                    toolCallId=calls[0].id,
                    toolName=calls[0].name,
                    content=[TextContent(type="text", text=result.content)],
                    isError=result.isError,
                    timestamp=time_ns() // 1_000_000,
                ),
            ]
        )
        replay = deepcopy(history)
        call = next(block for block in replay[2].content if isinstance(block, ToolCall))
        call.id = "call|" + "长 ID / " * 25
        replay[3].toolCallId = call.id
        replay[3].content.append(image)
        replay.append(
            UserMessage(
                role="user",
                content="报告工具读取的文本及附带图片的主要颜色。无需使用工具。",
                timestamp=timestamp,
            )
        )
        before = [serialize_message(message) for message in replay]
        request = to_openai_request(replay, config)
        api_assistant = next(
            message for message in request["messages"] if message["role"] == "assistant"
        )
        api_tool = next(
            message for message in request["messages"] if message["role"] == "tool"
        )
        assert api_assistant["tool_calls"][0]["id"] == api_tool["tool_call_id"]
        for field in replay_fields:
            assert api_assistant[field] == fields[field]
        assert (
            api_tool["tool_call_id"] != call.id and len(api_tool["tool_call_id"]) <= 40
        )
        assert (
            json.loads(api_assistant["tool_calls"][0]["function"]["arguments"])
            == call.arguments
        )
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
        answer = complete(client, config, replay)
        answer_text = "".join(
            block.text for block in answer.content if isinstance(block, TextContent)
        )
        assert "provider-check" in answer_text and "红" in answer_text
        assert [serialize_message(message) for message in replay] == before
        validate_tool_pairs(replay)

        request_model = partial(complete, client, config)
        removed = deepcopy(history[:2])
        iterator = run_turn(removed, request_model, registry, 8)
        started = next(iterator)
        assert started.event == "tool_start" and started.data["name"] == "read"
        removed.append(
            SystemMessage(
                role="system",
                content="",
                toolsRemoved=[ToolReference(name="read")],
                timestamp=time_ns() // 1_000_000,
            )
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(list, iterator).exception()
        assert isinstance(failure, PermissionError)
        assert not any(isinstance(message, ToolResultMessage) for message in removed)
        assert path.read_text(encoding="utf-8") == "provider-check"

        invalid = deepcopy(history[:2])
        invalid[0].toolsAdded[0].parameters = {}
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                list, run_turn(invalid, request_model, registry, 8)
            ).exception()
        assert isinstance(failure, ValueError) and "注册表" in str(failure)
        assert len(invalid) == 2

        bounded = [
            plain[0],
            UserMessage(
                role="user",
                content="请按顺序输出数字1至100，用逗号分隔。",
                timestamp=timestamp,
            ),
        ]

        def limited(messages):
            response = client.chat.completions.create(
                **to_openai_request(messages, config), max_tokens=1
            )
            return from_openai_response(response, config)

        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(
                list, run_turn(bounded, limited, registry, 1)
            ).exception()
        assert isinstance(failure, RuntimeError) and "length" in str(failure)
        assert (
            isinstance(bounded[-1], AssistantMessage)
            and bounded[-1].stopReason == "length"
        )
        assert bounded[-1].responseId and bounded[-1].usage.totalTokens > 0

        errors = [
            SystemMessage(
                role="system",
                content="仅执行用户指定的 Bash 命令，保留实际退出结果。",
                toolsAdded=[registry["bash"].definition()],
                timestamp=timestamp,
            ),
            UserMessage(
                role="user",
                content="必须用 bash 执行精确命令 printf failure; exit 7，执行一次后说明实际结果，不重试。",
                timestamp=timestamp,
            ),
        ]
        events = list(run_turn(errors, request_model, registry, 8))
        assert events[-1].event == "done"
        failed_results = [
            message for message in errors if isinstance(message, ToolResultMessage)
        ]
        assert failed_results and all(
            message.isError is True for message in failed_results
        )
        assert all(
            "Command exited with code 7" in message.content[0].text
            for message in failed_results
        )
        validate_tool_pairs(errors)
        totals = summarize_usage([*history, answer])
        assert (
            totals.totalTokens == assistant.usage.totalTokens + answer.usage.totalTokens
        )
        assert "cost" not in totals.model_dump(exclude_unset=True)
        evidence = WORKSPACE / "message-integration"
        evidence.mkdir(exist_ok=True)
        (evidence / "verified-messages.json").write_text(
            json.dumps(
                {
                    "verification": {
                        "source": "真实 StepFun Chat Completions 请求",
                        "replay_fields": replay_fields,
                        "reasoning_replay": "passed"
                        if replay_fields
                        else "not_observed",
                        "textSignature": "field_roundtrip_only_not_reported",
                        "thoughtSignature": "field_roundtrip_only_not_reported",
                        "field_validation": "实际 usage 字段的缺失、计数不一致和 null cost 负向检查",
                    },
                    "tool_response": json.loads(serialize_message(assistant)),
                    "raw_usage": raw_usage,
                    "raw_message": raw.choices[0].message.model_dump(
                        exclude_unset=True
                    ),
                    "summary": totals.model_dump(exclude_unset=True),
                    "text": json.loads(serialize_message(text)),
                    "image": json.loads(serialize_message(viewed)),
                    "replay": json.loads(serialize_message(answer)),
                    "length": json.loads(serialize_message(bounded[-1])),
                    "bash": [
                        json.loads(serialize_message(message)) for message in errors
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(
        "真实文本、图片、工具图片回放、ID 映射、用量、工具移除、length 与 Bash 错误结果检查通过"
    )
    print(
        f"真实思考回放字段：{replay_fields}；textSignature/thoughtSignature 为字段往返验证"
    )


if __name__ == "__main__":
    check()
