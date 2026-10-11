import asyncio
import json
import os
from pathlib import Path

# 真实 Anthropic Messages 协议溢出恢复探测：使用已授权的 Anthropic 兼容端点。
# 契约要求容量溢出具备明确信号（容量短语或 code=context_length_exceeded）；
# 端点若只返回通用错误，则如实记录阻塞，不改用其它服务代表该协议通过。
os.environ["FIT_AGENT_TEST_MODEL_API"] = "anthropic-messages"
os.environ["FIT_AGENT_TEST_MODEL_BASE_URL"] = "https://api.stepfun.com/step_plan"
os.environ["FIT_AGENT_TEST_MODEL_ID"] = "step-3.7-flash"
os.environ["FIT_AGENT_TEST_MODEL_PROVIDER"] = "stepfun"

from app.ai.messages import UserMessage
from app.ai.model_capabilities import resolve_model_spec
from app.ai.overflow import is_context_overflow_error
from app.ai.stream import stream
from app.model_config import load_model_config
from test import check_overflow_recovery_loop as base
from test.regression_support import install_test_model_config

ROOT = Path(__file__).resolve().parents[2] / "tmp" / "agent-compaction-joint-acceptance" / "anthropic"
PROBE_TOKENS = (320000, 300000, 260000, 250000, 200000)


async def consume(spec, api_key: str, tokens: int):
    context = {"messages": [UserMessage(role="user", content="word " * tokens, timestamp=0)]}
    response = stream(spec, context, {"api_key": api_key, "max_tokens": 32})
    async for _ in response:
        pass
    return await response.result()


def probe(spec, api_key: str, tokens: int) -> dict:
    try:
        message = asyncio.run(consume(spec, api_key, tokens))
    except BaseException as error:
        return {
            "tokens": tokens,
            "outcome": "raised",
            "error_type": type(error).__name__,
            "status_code": getattr(error, "status_code", None),
            "code": getattr(error, "code", None),
            "message": (getattr(error, "message", None) or str(error))[:200],
            "capacity_signal": is_context_overflow_error(error),
        }
    return {
        "tokens": tokens,
        "outcome": "message",
        "stop_reason": message.stop_reason,
        "context_overflow": message.context_overflow,
        "usage_total": message.usage.total_tokens if message.usage is not None else None,
        "capacity_signal": message.context_overflow,
    }


def capacity_probe(spec, api_key: str) -> dict:
    accepted_max: int | None = None
    rejected_min: int | None = None
    samples = []
    for tokens in PROBE_TOKENS:
        result = probe(spec, api_key, tokens)
        samples.append(result)
        if result["capacity_signal"]:
            rejected_min = tokens
        elif result["outcome"] == "message" and accepted_max is None:
            accepted_max = tokens
    return {"accepted_max": accepted_max, "rejected_min": rejected_min, "samples": samples}


def check() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    install_test_model_config()
    config = load_model_config()
    spec = resolve_model_spec(config)
    evidence = {
        "protocol": "anthropic-messages",
        "base_url": spec.base_url,
        "model": spec.id,
        "context_window": spec.context_window,
    }
    probe_result = capacity_probe(spec, config.api_key)
    evidence["capacity_probe"] = probe_result
    if probe_result["rejected_min"] is None:
        evidence["status"] = "blocked"
        evidence["reason"] = (
            "端点对超出输入返回通用 400 input_invalid，不含契约要求的明确容量信号，"
            "无法触发真实 Anthropic 协议溢出恢复"
        )
        (ROOT / "overflow-evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("BLOCKED: Anthropic 端点无明确容量信号，真实溢出恢复未验证")
        return
    base.ROOT = ROOT
    base.TEMP_ROOT = ROOT / "data"
    base.check()
    evidence["status"] = "verified"
    (ROOT / "overflow-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 真实 Anthropic Messages 协议一次溢出恢复与二次溢出终态")


if __name__ == "__main__":
    check()
