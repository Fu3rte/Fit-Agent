"""S4-01：真实调用脚本的 fail-closed 预算预检（纯函数回归；离线、无凭据、无网络）。

背景（评审 P1）：烟雾脚本原先用可见 ``--max-output-tokens`` 估算输出花费，但实测
**可见上限约束不住总输出**（思考 token 同样计费，1 个词的回答实测消耗 15–23 输出 token）。
现改为：输入按 08 已拍的「有效输入上限 250,000」，输出按目录里的**模型总输出上限**
384,000，均取峰价；累计已花费（``--prior-spend-usd``）计入同一 USD 10 上限。
低预算必须 fail-closed（拒绝发起调用），不得降级为“少发一点”。
"""

import math

import pytest

from runtime.models import DEEPSEEK_FLASH
from scripts.deepseek_smoke import enforce_cap, main, worst_case_usd

#: 峰价上界（USD / 1M tokens），与脚本同源（官方定价页 2026-09-12 只读核对）。
_PEAK_INPUT_USD_PER_M = 0.30
_PEAK_OUTPUT_USD_PER_M = 1.20


def test_preflight_bound_covers_model_output_ceiling_and_input_cap() -> None:
    bound = worst_case_usd()
    output_only = DEEPSEEK_FLASH.max_output_tokens * _PEAK_OUTPUT_USD_PER_M / 1_000_000
    input_only = 250_000 * _PEAK_INPUT_USD_PER_M / 1_000_000

    # 输出按模型总上限计（不是可见 max_tokens），输入按已拍有效输入上限计
    assert bound >= output_only
    assert bound >= input_only
    assert bound == pytest.approx(output_only + input_only)
    # 明确大于可见上限口径：可见 16 tokens 的输出价只有不到 1e-5 美元
    assert bound > 16 * _PEAK_OUTPUT_USD_PER_M / 1_000_000


def test_low_cap_is_refused_fail_closed() -> None:
    bound = worst_case_usd()
    # 低预算（评审要求的回归场景）：远低于最坏上界 → 拒绝调用
    with pytest.raises(SystemExit):
        enforce_cap(bound, prior_spend_usd=0.0, cap_usd=0.10)
    # 边界：恰好等于最坏上界放行，低一点点也拒绝
    assert enforce_cap(bound, prior_spend_usd=0.0, cap_usd=bound) == pytest.approx(
        bound
    )
    with pytest.raises(SystemExit):
        enforce_cap(bound, prior_spend_usd=0.0, cap_usd=bound - 1e-9)


def test_prior_spend_counts_toward_the_same_cumulative_cap() -> None:
    bound = worst_case_usd()
    cap = 10.0
    prior = cap - bound + 1e-6  # 单次仍在上限内，但累计已越界
    with pytest.raises(SystemExit):
        enforce_cap(bound, prior_spend_usd=prior, cap_usd=cap)
    # 历史花费被原样计入返回值（不重置、不吞掉）
    assert enforce_cap(bound, prior_spend_usd=0.000073, cap_usd=cap) == pytest.approx(
        0.000073 + bound
    )


def test_negative_prior_spend_is_rejected() -> None:
    with pytest.raises(SystemExit):
        enforce_cap(worst_case_usd(), prior_spend_usd=-1.0, cap_usd=10.0)


# ---------- 非有限值与正上限规则（NaN／±Inf 参与比较会静默失真） ----------


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_worst_case_is_rejected(bad: float) -> None:
    with pytest.raises(SystemExit):
        enforce_cap(bad, prior_spend_usd=0.0, cap_usd=10.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_prior_spend_is_rejected(bad: float) -> None:
    with pytest.raises(SystemExit):
        enforce_cap(worst_case_usd(), prior_spend_usd=bad, cap_usd=10.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_cap_is_rejected(bad: float) -> None:
    with pytest.raises(SystemExit):
        enforce_cap(worst_case_usd(), prior_spend_usd=0.0, cap_usd=bad)


@pytest.mark.parametrize("bad_cap", [0.0, -1.0, -0.0001])
def test_non_positive_cap_is_rejected(bad_cap: float) -> None:
    with pytest.raises(SystemExit):
        enforce_cap(worst_case_usd(), prior_spend_usd=0.0, cap_usd=bad_cap)


def test_positive_cap_rule_accepts_a_positive_cap() -> None:
    # 正上限放行（同一上界值即可），只有非正／非有限上限被拒
    bound = worst_case_usd()
    assert enforce_cap(bound, prior_spend_usd=0.0, cap_usd=bound) == pytest.approx(
        bound
    )


@pytest.mark.parametrize("flag", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("option", ["--cap-usd", "--prior-spend-usd"])
def test_cli_non_finite_flags_are_rejected_before_any_call(
    option: str, flag: str
) -> None:
    """命令行传来的 nan／inf 字符串同样被预检拦住，且预检先于凭据与网络（无付费调用）。"""
    with pytest.raises(SystemExit):
        main(["--max-output-tokens", "16", option, flag])
