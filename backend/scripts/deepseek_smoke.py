"""真实 deepseek-flash 最小联调（S4-01 证据脚本；仅在明确授权下手工运行）。

用途：在**生产路径**（``runtime.provider.build_model`` → pydantic-ai + openai SDK + DeepSeek
官方兼容端点）上证明实际连通性、profile 生效与 usage 可得性。不进入自动化测试（默认套件必须
离线），不进业务库，不写任何日志文件。

安全与成本护栏（运行前必须满足，脚本自身强制）：

- 凭据只从环境变量 ``DEEPSEEK_API_KEY`` 读取；脚本不打印、不落盘、不回显任何凭据或请求头。
- 单次请求、单次请求时限 120 秒（08 已拍）；``--max-output-tokens`` 只限可见回答，
  **不得用于预算预检**（实测：思考 token 同样计入输出，可见上限约束不住总输出）。
- 预检按需失败（fail-closed）：输入按 08 已拍的「有效输入上限 250,000 tokens」，
  输出按目录里的**模型总输出上限** 384,000 tokens，均取峰价；
  最坏花费与 ``--prior-spend-usd``（累计已花费）之和超过 ``--cap-usd`` 则直接拒绝发起调用。
- 价格依据（2026-09-12 只读核对官方页面 https://api-docs.deepseek.com/quick_start/pricing，
  ``deepseek-flash``）：输入 cache miss 峰 $0.30 谷 $0.15、输出 峰 $1.20 谷 $0.60（USD/1M），
  峰时段 01:00–04:00 / 06:00–10:00 UTC 周一至周五。取峰价做上界。
- 报告的 usage 缺失时：本次调用已发生，按上述预检上界保守计入，然后停止后续真实调用。

用法（在 ``backend/`` 下，凭据经环境变量注入，不经命令行参数）：

    DEEPSEEK_API_KEY=... PYTHONPATH=. .venv/bin/python scripts/deepseek_smoke.py \\
        --max-output-tokens 16 --prior-spend-usd 0.000073
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys

from pydantic_ai import Agent

from runtime.models import DEEPSEEK_FLASH, ModelSpec
from runtime.provider import build_model

#: 峰价上界（USD / 1M tokens），来源见模块 docstring。
_PEAK_INPUT_USD_PER_M = 0.30
_PEAK_OUTPUT_USD_PER_M = 1.20

#: 预检输入上界：08 已拍的「有效输入上限」250,000 tokens（单次请求不可能超这个预算）。
_PREFLIGHT_INPUT_BOUND_TOKENS = 250_000

#: 固定业务时限：单次模型请求 120 秒（08 已拍参数；真实配置归 S4-05）。
_REQUEST_TIMEOUT_SECONDS = 120.0

#: 非敏感、极短提示（证据只需证明连通与 usage，不需要业务内容）。
_PROMPT = "Reply with exactly one word: ok"


def worst_case_usd(spec: ModelSpec = DEEPSEEK_FLASH) -> float:
    """单次真实调用的最坏花费（fail-closed 预检上界）。

    输入按 08 已拍的有效输入上限，输出按**模型总输出上限**（思考 token 同样计费，
    实测可见 ``max_tokens`` 约束不住总输出），两顶均取峰价——不用 ``--max-output-tokens``
    作预检，因为它不是总输出的上界。
    """
    return (
        _PREFLIGHT_INPUT_BOUND_TOKENS * _PEAK_INPUT_USD_PER_M
        + spec.max_output_tokens * _PEAK_OUTPUT_USD_PER_M
    ) / 1_000_000


def enforce_cap(worst_case: float, *, prior_spend_usd: float, cap_usd: float) -> float:
    """累计费用护栏：最坏花费 + 已花费 > 上限即拒绝发起调用（fail-closed）。

    校验先于算术：三项都必须是**有限数值**（NaN／±Inf 参与比较会静默失真，`nan > x` 恒假），
    且上限必须是正数；任何一项不合规均直接拒绝，不做降级、不取默认值。
    返回累计上界（已花费 + 本次最坏）供报告使用；拒绝时抛 ``SystemExit``（不降级为“少发一点”）。
    """
    for name, value in (
        ("单次最坏花费", worst_case),
        ("累计已花费", prior_spend_usd),
        ("费用上限", cap_usd),
    ):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SystemExit(f"拒绝调用：{name}必须是有限数值（实际 {value!r}）")
    if prior_spend_usd < 0:
        raise SystemExit("累计已花费不能为负")
    if cap_usd <= 0:
        raise SystemExit(f"拒绝调用：费用上限必须是正数（实际 {cap_usd!r}）")
    cumulative_worst = prior_spend_usd + worst_case
    if cumulative_worst > cap_usd:
        raise SystemExit(
            f"拒绝调用：单次最坏 ${worst_case:.6f} + 已花费 ${prior_spend_usd:.6f}"
            f" = ${cumulative_worst:.6f} 超过累计上限 ${cap_usd:.2f}"
        )
    return cumulative_worst


async def _run(
    max_output_tokens: int, cap_usd: float, prior_spend_usd: float
) -> dict[str, object]:
    # 预检先于凭据检查：护栏不依赖任何秘密，缺凭据时上抛的信息也不含凭据内容
    worst_case = worst_case_usd()
    cumulative_worst = enforce_cap(
        worst_case, prior_spend_usd=prior_spend_usd, cap_usd=cap_usd
    )

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key.strip():
        raise SystemExit(
            "缺少 DEEPSEEK_API_KEY（只从环境变量读取；不写命令行、不写文件）"
        )

    model = build_model(
        api_key,
        model_id=DEEPSEEK_FLASH.model_id,
        timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
    )
    agent = Agent(model=model, retries={"tools": 0, "output": 0}, name="s401-smoke")
    result = await agent.run(_PROMPT, model_settings={"max_tokens": max_output_tokens})

    usage = result.usage
    text = result.output if isinstance(result.output, str) else str(result.output)
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    actual_usd = (
        input_tokens * _PEAK_INPUT_USD_PER_M + output_tokens * _PEAK_OUTPUT_USD_PER_M
    ) / 1_000_000
    return {
        "model_id_requested": DEEPSEEK_FLASH.model_id,
        "model_name_reported": getattr(result.response, "model_name", None),
        "provider_reported": getattr(result.response, "provider_name", None),
        "context_window_profile": model.profile.get("context_window"),
        "profile_thinking_flags": {
            "supports_thinking": model.profile.get("supports_thinking"),
            "thinking_always_enabled": model.profile.get("thinking_always_enabled"),
            "openai_reasoning_enabled_by_default": model.profile.get(
                "openai_reasoning_enabled_by_default"
            ),
        },
        "sdk_max_retries": model.client.max_retries
        if hasattr(model, "client")
        else None,
        "requests": usage.requests,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_cost_field": None if usage.cost is None else str(usage.cost),
        "response_chars": len(text),
        "response_snippet": text[:80],
        # 默认行为证据：服务端是否返回思考内容（2026-09-12 已拍：保持默认开启）
        "response_part_types": [type(part).__name__ for part in result.response.parts],
        "thinking_present": any(
            "Thinking" in type(part).__name__ for part in result.response.parts
        ),
        "preflight_worst_case_usd": round(worst_case, 8),
        "preflight_basis": {
            "input_bound_tokens": _PREFLIGHT_INPUT_BOUND_TOKENS,
            "output_bound_tokens": DEEPSEEK_FLASH.max_output_tokens,
            "peak_input_usd_per_m": _PEAK_INPUT_USD_PER_M,
            "peak_output_usd_per_m": _PEAK_OUTPUT_USD_PER_M,
        },
        "estimated_actual_usd": round(actual_usd, 8),
        "prior_spend_usd": prior_spend_usd,
        "cumulative_worst_case_usd": round(cumulative_worst, 8),
        "cumulative_actual_usd": round(prior_spend_usd + actual_usd, 8),
        "cap_usd": cap_usd,
        "cap_respected": cumulative_worst <= cap_usd
        and (prior_spend_usd + actual_usd) <= cap_usd,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deepseek-flash 单次最小真实调用")
    parser.add_argument("--max-output-tokens", type=int, default=16)
    parser.add_argument("--cap-usd", type=float, default=10.0)
    parser.add_argument(
        "--prior-spend-usd",
        type=float,
        default=0.0,
        help="此前真实调用已花费的估算值（累计上限口径；预算预检不含凭据）",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.max_output_tokens <= 256:
        raise SystemExit(
            "--max-output-tokens 必须在 1..256（本脚本不允许放开输出上限）"
        )
    report = asyncio.run(
        _run(args.max_output_tokens, args.cap_usd, args.prior_spend_usd)
    )
    # 只输出脱敏报告：无凭据、无请求头、无原始响应体
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
