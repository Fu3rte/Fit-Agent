# 费用护栏离线验证：预留、结算、自动停、硬顶、异常防呆。
# 全部为合成数据；真实调用必须在这些用例全过之后。

import datetime as dt
from decimal import Decimal

import pytest

from spike_lib.fee_guard import (
    ANOMALY_CHECK_AFTER_CALLS,
    AUTO_STOP_USD,
    HARD_CAP_USD,
    MAX_TOKENS_LIMIT,
    FeeGuard,
    StopSpike,
    is_peak,
    max_rate,
    rate,
)

OFF_PEAK = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.timezone.utc)  # 周日 → 恒 off-peak
PEAK = dt.datetime(2026, 9, 4, 2, 0, tzinfo=dt.timezone.utc)  # 周五 02:00 UTC → peak
PEAK2 = dt.datetime(2026, 9, 4, 7, 0, tzinfo=dt.timezone.utc)  # 周五 07:00 UTC → peak


def test_peak_windows_match_official_definition():
    assert is_peak(PEAK) and is_peak(PEAK2)
    assert not is_peak(OFF_PEAK)  # 周日
    assert not is_peak(dt.datetime(2026, 9, 4, 5, 0, tzinfo=dt.timezone.utc))  # 04:00-06:00 空档
    assert not is_peak(dt.datetime(2026, 9, 4, 11, 0, tzinfo=dt.timezone.utc))
    # 边界含起点不含终点
    assert is_peak(dt.datetime(2026, 9, 4, 1, 0, tzinfo=dt.timezone.utc))
    assert not is_peak(dt.datetime(2026, 9, 4, 4, 0, tzinfo=dt.timezone.utc))
    assert not is_peak(dt.datetime(2026, 9, 4, 10, 0, tzinfo=dt.timezone.utc))


def test_rate_table_matches_official_prices():
    # 官方 Models & Pricing（2026-08-16 起峰谷分段）：USD per 1M（见 evidence/pricing-source.md）
    assert rate("deepseek-v4-flash", "input_hit", OFF_PEAK) == Decimal("0.007")
    assert rate("deepseek-v4-flash", "input_miss", OFF_PEAK) == Decimal("0.22")
    assert rate("deepseek-v4-flash", "output", OFF_PEAK) == Decimal("0.66")
    assert rate("deepseek-v4-flash", "input_hit", PEAK) == Decimal("0.014")
    assert rate("deepseek-v4-pro", "input_miss", OFF_PEAK) == Decimal("0.66")
    assert rate("deepseek-v4-pro", "output", PEAK) == Decimal("3.96")
    # vision-exp 是官方价格表独立列，数值与 flash 一致（非 starts_with 推断）
    assert rate("deepseek-v4-flash-vision-exp", "input_hit", OFF_PEAK) == Decimal("0.007")
    assert rate("deepseek-v4-flash-vision-exp", "output", PEAK) == Decimal("1.32")


def test_reserve_worst_case_uses_max_of_peak_and_offpeak_regardless_of_call_time():
    """P0 修复：预留按峰谷最大价，与调用时间无关。off-peak 发起的预留必须等于 peak 预留。"""
    g = FeeGuard()
    res_peak = g.reserve("deepseek-v4-flash", input_tokens_reserve=100_000, max_output=256)
    expected_peak = (
        Decimal("0.44") / 1_000_000 * 100_000 + Decimal("1.32") / 1_000_000 * 256
    ).quantize(Decimal("0.000001"))
    assert res_peak.usd.quantize(Decimal("0.000001")) == expected_peak
    assert max_rate("deepseek-v4-flash", "input_miss") == Decimal("0.44")

    g2 = FeeGuard()
    res_offpeak = g2.reserve("deepseek-v4-flash", input_tokens_reserve=100_000, max_output=256)
    # 同样输入规模：off-peak 时刻预留不得低于 peak 预留（保守最坏成本）
    assert res_offpeak.usd == res_peak.usd


def test_reserve_rejects_max_output_over_limit():
    g = FeeGuard()
    with pytest.raises(StopSpike, match="256"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000, max_output=257)
    with pytest.raises(StopSpike, match="256"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000, max_output=0)


def test_settle_hit_miss_output_no_double_billing():
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=100_000)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "prompt_cache_hit_tokens": 700,
        "prompt_cache_miss_tokens": 300,
    }
    cost = g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    expected = (
        Decimal("0.007") / 1_000_000 * 700
        + Decimal("0.22") / 1_000_000 * 300
        + Decimal("0.66") / 1_000_000 * 100
    ).quantize(Decimal("0.000001"))
    assert cost.quantize(Decimal("0.000001")) == expected
    assert g.reserved_usd == 0  # 预留已解除，未重复计入
    assert g.settled_usd.quantize(Decimal("0.000001")) == expected


def test_settle_falls_back_to_cached_tokens_when_raw_hit_missing():
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
    usage = {"prompt_tokens": 500, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 200}}
    cost = g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    expected = (
        Decimal("0.007") / 1_000_000 * 200
        + Decimal("0.22") / 1_000_000 * 300
        + Decimal("0.66") / 1_000_000 * 10
    ).quantize(Decimal("0.000001"))
    assert cost.quantize(Decimal("0.000001")) == expected


def test_usage_missing_keeps_reservation_not_zero():
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=50_000)
    before = g.settled_usd
    cost = g.settle(res, None, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    assert cost == res.usd
    assert g.settled_usd == before + res.usd  # 预留全额保留，不是 0
    assert g.calls[-1].settled_usd is None


def test_missing_usage_crossing_five_dollars_stops_and_blocks_reserve():
    """P0 修复：usage 缺失路径同样触发 $5 自动停，禁止下一次预留。"""
    g = FeeGuard()
    res = g.reserve("deepseek-v4-pro", input_tokens_reserve=3_800_000)  # pro miss peak $1.32/1M → ~$5.02
    assert res.usd > AUTO_STOP_USD
    with pytest.raises(StopSpike, match="自动停"):
        g.settle(res, None, now_utc=OFF_PEAK, model="deepseek-v4-pro")  # usage 缺失 → 预留保留
    assert g.settled_usd >= AUTO_STOP_USD
    assert g.stopped is not None and "自动停" in g.stopped
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_cancel_keeps_reservation_not_zero():
    g = FeeGuard()
    res = g.reserve("deepseek-v4-pro", input_tokens_reserve=50_000)
    before = g.settled_usd
    g.cancel(res, reason="user cancel")
    assert g.settled_usd == before + res.usd
    assert g.calls[-1].settled_usd is None


def test_cancel_crossing_five_dollars_stops_and_blocks_reserve():
    """P0 修复：取消路径同样触发 $5 自动停（设置停止位，后续 reserve 拒绝）。"""
    g = FeeGuard()
    # 先累计到接近 $5，再一次取消跨过
    res1 = g.reserve("deepseek-v4-pro", input_tokens_reserve=3_000_000)  # ~$3.96+0.86
    g.cancel(res1, "user cancel")
    assert g.settled_usd < AUTO_STOP_USD
    res2 = g.reserve("deepseek-v4-pro", input_tokens_reserve=1_000_000)
    g.cancel(res2, "user cancel")
    assert g.settled_usd >= AUTO_STOP_USD
    assert g.stopped is not None and "自动停" in g.stopped
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_settle_exceeding_reservation_stops_immediately():
    """P1 修复：实际结算超预留 → 立即停止并显式报错（输入上界估算不足）。"""
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=1000, max_output=256)
    # 构造远超预留的实际 usage（预留按 1000 输入，实际 500k）
    usage = {"prompt_tokens": 500_000, "completion_tokens": 200, "prompt_cache_hit_tokens": 0}
    with pytest.raises(StopSpike, match="超过预留"):
        g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    assert g.stopped is not None
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_total_exceeding_hard_cap_after_settle_raises():
    """P1 修复：结算使总额超硬顶 → 显式报错并停止。预留上界约束使“超硬顶”必然伴随
    “超预留”（实际费用超过预留即停），两条件任一触发都立即报错。"""
    g = FeeGuard()
    g.settled_usd = Decimal("9.00")  # 模拟既有累计（接近硬顶）
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)  # 最坏预留 ~$0.0008，可获准
    usage = {"prompt_tokens": 500_000, "completion_tokens": 200, "prompt_cache_hit_tokens": 0}
    with pytest.raises(StopSpike) as exc_info:
        g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    assert "超过预留" in str(exc_info.value) or "硬顶" in str(exc_info.value)
    assert g.stopped is not None
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_hard_cap_rejects_reservation():
    g = FeeGuard()
    g.settled_usd = Decimal("9.90")
    with pytest.raises(StopSpike, match="硬顶"):
        g.reserve("deepseek-v4-pro", input_tokens_reserve=1_000_000)  # 最坏 ~$1.32+ > $10-9.9


def test_auto_stop_at_five_dollars():
    g = FeeGuard()
    # 构造能一次跨过 $5 的结算：pro peak miss $1.32/1M × 4M = $5.28
    res = g.reserve("deepseek-v4-pro", input_tokens_reserve=4_200_000)
    usage = {"prompt_tokens": 4_000_000, "completion_tokens": 256, "prompt_cache_hit_tokens": 0}
    with pytest.raises(StopSpike, match="自动停"):
        g.settle(res, usage, now_utc=PEAK, model="deepseek-v4-pro")
    assert g.settled_usd >= AUTO_STOP_USD


def test_hit_and_miss_inconsistency_stops():
    """P1 修复：服务端同时提供 hit+miss 且与 prompt 矛盾 → 停止人工核查，不静默生成费用。"""
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 5,
        "prompt_cache_hit_tokens": 700,
        "prompt_cache_miss_tokens": 500,  # 700+500 != 1000
    }
    with pytest.raises(StopSpike, match="分项矛盾"):
        g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    assert g.stopped is not None


def test_negative_usage_stops():
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
    with pytest.raises(StopSpike, match="非法"):
        g.settle(
            res,
            {"prompt_tokens": -5, "completion_tokens": 5, "prompt_cache_hit_tokens": 0},
            now_utc=OFF_PEAK,
            model="deepseek-v4-flash",
        )


def test_dual_source_hit_mismatch_stops():
    """P1 修复：raw hit 与 details.cached_tokens 双来源不一致 → 停止。"""
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 5,
        "prompt_cache_hit_tokens": 700,
        "prompt_tokens_details": {"cached_tokens": 300},
    }
    with pytest.raises(StopSpike, match="双来源不一致"):
        g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")


def test_settle_requires_explicit_response_model():
    """P1 修复：settle 的 model 必填——缺失响应身份不得默认为请求模型。"""
    g = FeeGuard()
    res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
    with pytest.raises(TypeError):
        g.settle(res, {"prompt_tokens": 10, "completion_tokens": 5}, now_utc=OFF_PEAK)  # type: ignore[call-arg]


def test_anomaly_undercharge_detected_from_request_scale():
    """P1 修复：第 10 次防呆基于实际请求规模/模型推导，而非固定金额窗口。
    构造"记账费用远低于按实际 token × 最廉价的下限"的异常。"""
    g = FeeGuard()
    for _ in range(ANOMALY_CHECK_AFTER_CALLS - 1):
        res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
        usage = {"prompt_tokens": 8000, "completion_tokens": 100, "prompt_cache_hit_tokens": 0}
        cost = g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
        assert cost > 0
        # 模拟少收费：把结算人为砍到期望下限以下
        g.calls[-1].settled_usd = Decimal("0.0000001")
        g.settled_usd -= cost - Decimal("0.0000001")
    # 第 10 次：正常结算 → 防呆扫描发现早期记录低于期望下限，停止并报错
    res10 = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
    usage10 = {"prompt_tokens": 8000, "completion_tokens": 100, "prompt_cache_hit_tokens": 0}
    with pytest.raises(StopSpike, match="期望下限"):
        g.settle(res10, usage10, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    assert g.stopped is not None and "期望下限" in g.stopped
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_anomaly_zero_total_after_10_calls_stops():
    g = FeeGuard()
    for i in range(ANOMALY_CHECK_AFTER_CALLS):
        res = g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if i < ANOMALY_CHECK_AFTER_CALLS - 1:
            g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
        else:
            # 第 10 次结算时防呆发现累计费用为 0（可能漏记账），停止并报错
            with pytest.raises(StopSpike, match="费用为 0"):
                g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
    assert g.stopped and "费用为 0" in g.stopped
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_anomaly_normal_magnitude_does_not_stop():
    g = FeeGuard()
    for _ in range(ANOMALY_CHECK_AFTER_CALLS):
        res = g.reserve("deepseek-v4-flash", input_tokens_reserve=2000)
        usage = {"prompt_tokens": 1000, "completion_tokens": 128, "prompt_cache_hit_tokens": 0}
        cost = g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
        assert cost > 0
    assert g.stopped is None


def test_stopped_guard_blocks_further_calls():
    g = FeeGuard()
    g.stopped = "manual stop"
    with pytest.raises(StopSpike, match="已停止"):
        g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)


def test_limits_constants():
    assert MAX_TOKENS_LIMIT == 256
    assert AUTO_STOP_USD == Decimal("5.00") and HARD_CAP_USD == Decimal("10.00")


def test_unknown_model_refuses_to_price():
    with pytest.raises(StopSpike, match="未知"):
        FeeGuard().worst_case_cost("deepseek-v4-not-real", input_tokens_reserve=1000, max_output=256)


def test_settle_validation_failure_keeps_reservation_counted_and_stops():
    """P1 修复回归：结算校验失败（畸形/矛盾 usage）→ 预留解除但仍全额计入累计（不算 0），
    留可核查 record；guard 停止并拒绝后续 reserve；不重复计费。
    覆盖：非整数字段（_int_or_stop 只抛异常不置停止位的路径）、prompt_tokens_details 非 dict
    （原实现 AttributeError 而非保守停止）、hit+miss 与 prompt 分项矛盾。"""
    malformed_usages = [
        {"prompt_tokens": 1000, "completion_tokens": 5, "prompt_cache_hit_tokens": "700"},
        {"prompt_tokens": 1000, "completion_tokens": 5, "prompt_tokens_details": 123},
        {
            "prompt_tokens": 1000,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 700,
            "prompt_cache_miss_tokens": 500,
        },
    ]
    for usage in malformed_usages:
        g = FeeGuard()
        res = g.reserve("deepseek-v4-flash", input_tokens_reserve=10_000)
        before = g.settled_usd
        with pytest.raises(StopSpike):
            g.settle(res, usage, now_utc=OFF_PEAK, model="deepseek-v4-flash")
        assert g.stopped is not None, "校验失败后未置停止位，后续 reserve 会被放行"
        assert g.reserved_usd == 0  # 预留已解除，不重复占用
        assert g.settled_usd == before + res.usd  # 原预留仍计入累计，不算 0、不漏记
        rec = g.calls[-1]
        assert rec.note == "settle_validation_failed"  # 可核查记录
        assert rec.settled_usd is None and rec.reserved_usd == res.usd
        assert rec.usage_raw == usage  # 保留原始证据
        with pytest.raises(StopSpike, match="已停止"):
            g.reserve("deepseek-v4-flash", input_tokens_reserve=1000)  # 拒绝后续预留
