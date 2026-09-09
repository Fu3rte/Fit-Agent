# 持久账本离线验证（PLAN 决策 A）：请求前落盘预留、重启保留未结算金额、
# 账本损坏或并发启动即拒绝、异常路径落盘。全部合成数据，不涉真实调用。

import json
from decimal import Decimal

import pytest

from spike_lib.fee_guard import FeeGuard, StopSpike
from spike_lib.ledger import LedgerCorrupt, LedgerLocked, PersistentFeeGuard

FLASH = "deepseek-v4-flash"
PRO = "deepseek-v4-pro"
OFF_PEAK = __import__("datetime").datetime(
    2026, 9, 6, 12, 0, tzinfo=__import__("datetime").timezone.utc
)


def _read_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_reserve_persisted_before_dispatch_and_survives_restart(tmp_path):
    """核心语义：预留返回前账本已落盘（wire 请求在 reserve 返回后才发出）；
    重启后预留金额原样保留（未结算不丢失）。"""
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(FLASH, input_tokens_reserve=10_000)
    on_disk = _read_state(path)
    assert Decimal(on_disk["reserved_usd"]) == res.usd  # 落盘内容与内存一致
    assert on_disk["calls"] == []  # 尚无终态记录
    g.close()
    g2 = PersistentFeeGuard(path)  # 模拟重启
    assert g2.reserved_usd == res.usd
    assert g2.settled_usd == 0
    g2.close()


def test_settle_persisted_across_restart(tmp_path):
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(FLASH, input_tokens_reserve=10_000)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 10,
        "prompt_cache_hit_tokens": 0,
    }
    cost = g.settle(res, usage, now_utc=OFF_PEAK, model=FLASH)
    g.close()
    g2 = PersistentFeeGuard(path)
    assert g2.settled_usd == cost and g2.reserved_usd == 0
    assert len(g2.calls) == 1 and g2.calls[0].usage_raw == usage
    assert g2.calls[0].settled_usd == cost
    g2.close()


def test_cancel_keeps_reservation_across_restart(tmp_path):
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(PRO, input_tokens_reserve=50_000)
    g.cancel(res, "stream_aborted:CancelledError")
    g.close()
    g2 = PersistentFeeGuard(path)
    assert g2.reserved_usd == 0
    assert g2.settled_usd == res.usd  # 取消：预留全额保留计入累计，不算 0
    assert g2.calls[-1].note == "stream_aborted:CancelledError"
    assert g2.calls[-1].settled_usd is None
    g2.close()


def test_stopped_state_persists_and_blocks_after_restart(tmp_path):
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    g.stop("manual stop")
    g.close()
    g2 = PersistentFeeGuard(path)
    with pytest.raises(StopSpike, match="已停止"):
        g2.reserve(FLASH, input_tokens_reserve=1000)
    g2.close()


def test_usage_missing_settle_keeps_reservation_across_restart(tmp_path):
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(FLASH, input_tokens_reserve=10_000)
    g.settle(res, None, now_utc=OFF_PEAK, model=FLASH)  # usage 缺失 → 预留保留
    g.close()
    g2 = PersistentFeeGuard(path)
    assert g2.settled_usd == res.usd and g2.calls[-1].note == "usage_missing"
    g2.close()


def test_crash_before_dispatch_constrains_restart_budget(tmp_path):
    """崩溃于预留后/结算前：重启后未结算预留仍占用预算，硬顶约束生效。"""
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(PRO, input_tokens_reserve=3_800_000)  # 最坏 ≈ $5.017
    assert res.usd > Decimal("5")
    g.close()  # 模拟崩溃（永远等不到结算）
    g2 = PersistentFeeGuard(path)
    with pytest.raises(StopSpike, match="硬顶"):
        g2.reserve(PRO, input_tokens_reserve=3_800_000)  # 5.017 + 5.017 > $10
    g2.close()


def test_corrupt_json_refuses_and_does_not_overwrite(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(LedgerCorrupt, match="拒绝启动"):
        PersistentFeeGuard(path)
    assert path.read_text(encoding="utf-8") == before  # 绝不覆盖坏账本


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s.update(version=2),
        lambda s: s.update(extra="field"),
        lambda s: s.pop("settled_usd"),
        lambda s: s.update(settled_usd="-1.0"),
        lambda s: s.update(settled_usd=0.5),  # JSON 数值：非十进制字符串，拒绝
        lambda s: s.update(reserved_usd="abc"),
        lambda s: s.update(
            calls=[
                {
                    "model": "deepseek-v4-not-real",
                    "reserved_usd": "0",
                    "settled_usd": None,
                    "usage_raw": None,
                    "expected_min_usd": None,
                    "note": None,
                }
            ]
        ),
        lambda s: s.update(calls=[{"model": FLASH}]),
        lambda s: s.update(stopped=123),
        lambda s: s.update(settled_usd="9.9", reserved_usd="0.5"),  # 加载即超硬顶
    ],
)
def test_valid_json_wrong_shape_refuses(tmp_path, mutation):
    path = tmp_path / "ledger.json"
    g = FeeGuard()
    res = g.reserve(FLASH, input_tokens_reserve=1000)
    g.settle(
        res,
        {"prompt_tokens": 10, "completion_tokens": 1, "prompt_cache_hit_tokens": 0},
        now_utc=OFF_PEAK,
        model=FLASH,
    )
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "settled_usd": str(g.settled_usd),
                "reserved_usd": "0",
                "stopped": None,
                "calls": [
                    {
                        "model": FLASH,
                        "reserved_usd": str(res.usd),
                        "settled_usd": str(g.settled_usd),
                        "usage_raw": {"prompt_tokens": 10},
                        "expected_min_usd": None,
                        "note": None,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    state = _read_state(path)
    mutation(state)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(LedgerCorrupt):
        PersistentFeeGuard(path)


def test_concurrent_startup_refused(tmp_path):
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    with pytest.raises(LedgerLocked, match="并发"):
        PersistentFeeGuard(path)
    g.close()
    g2 = PersistentFeeGuard(path)  # 释放后可重新打开
    g2.close()


def test_leftover_tmp_file_ignored(tmp_path):
    path = tmp_path / "ledger.json"
    path.with_name("ledger.json.tmp").write_text("garbage from crash", encoding="utf-8")
    g = PersistentFeeGuard(path)  # 无主账本 → 新建；残留 tmp 被忽略
    g.reserve(FLASH, input_tokens_reserve=1000)
    assert "garbage" not in path.read_text(encoding="utf-8")
    g.close()


def test_anomaly_window_survives_restart(tmp_path):
    """第 10 次量级防呆跨重启生效（calls 全量恢复）。"""
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    for _ in range(9):
        res = g.reserve(FLASH, input_tokens_reserve=1000)
        g.settle(
            res,
            {"prompt_tokens": 0, "completion_tokens": 0},
            now_utc=OFF_PEAK,
            model=FLASH,
        )
    g.close()
    g2 = PersistentFeeGuard(path)
    res10 = g2.reserve(FLASH, input_tokens_reserve=1000)
    with pytest.raises(StopSpike, match="费用为 0"):
        g2.settle(
            res10,
            {"prompt_tokens": 0, "completion_tokens": 0},
            now_utc=OFF_PEAK,
            model=FLASH,
        )
    g2.close()
    g3 = PersistentFeeGuard(path)
    assert g3.stopped is not None and "费用为 0" in g3.stopped  # 停止位已持久化
    g3.close()


def test_threshold_auto_stop_persisted_on_settle(tmp_path):
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(PRO, input_tokens_reserve=3_900_000)  # 一次跨过 $5 自动停
    with pytest.raises(StopSpike, match="自动停"):
        g.settle(
            res, None, now_utc=OFF_PEAK, model=PRO
        )  # usage 缺失 → 预留保留 → 自动停
    g.close()
    g2 = PersistentFeeGuard(path)
    assert g2.stopped is not None and "自动停" in g2.stopped
    with pytest.raises(StopSpike, match="已停止"):
        g2.reserve(FLASH, input_tokens_reserve=1000)
    g2.close()


def test_settle_validation_failure_persisted(tmp_path):
    """结算校验失败（畸形 usage）路径同样落盘：停止位 + 全额计入累计。"""
    path = tmp_path / "ledger.json"
    g = PersistentFeeGuard(path)
    res = g.reserve(FLASH, input_tokens_reserve=10_000)
    with pytest.raises(StopSpike):
        g.settle(
            res,
            {
                "prompt_tokens": 1000,
                "completion_tokens": 5,
                "prompt_cache_hit_tokens": "700",
            },
            now_utc=OFF_PEAK,
            model=FLASH,
        )
    g.close()
    g2 = PersistentFeeGuard(path)
    assert g2.stopped is not None
    assert g2.settled_usd == res.usd and g2.calls[-1].note == "settle_validation_failed"
    g2.close()
