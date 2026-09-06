# Fit-Agent PydanticAI spike：费用护栏（离线先行验证，任何真实调用前必须全部通过）。
# 价格来源：DeepSeek 官方 Models & Pricing 全文（Jina Reader 只读抓取，快照时间见
# spike/evidence/pricing-source.md；2026-08-16 起峰谷分段生效）。
# https://api-docs.deepseek.com/quick_start/pricing/
# DeepSeek OpenAI 端点无独立 cache_write 计费类目：计费 = input(hit) + input(miss) + output。
# 不得将平台币价推测成美元；本表单位为官方页面的 USD per 1M tokens。

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

MAX_TOKENS_LIMIT = 256  # 用户硬约束：单次 wire 请求 max_tokens ≤ 256（guard transport 在 wire 层强制）
AUTO_STOP_USD = Decimal("5.00")
HARD_CAP_USD = Decimal("10.00")
ANOMALY_CHECK_AFTER_CALLS = 10


class StopSpike(Exception):
    """达到停止条件：停止真实调用，保留现场人工核查。"""


class FeeModel(str, Enum):
    FLASH = "deepseek-v4-flash"
    PRO = "deepseek-v4-pro"
    VISION_EXP = "deepseek-v4-flash-vision-exp"


# 官方价格表：USD per 1M tokens；peak 时段 01:00-04:00 与 06:00-10:00 UTC（周一至周五），其余 off-peak。
# price[model][kind] = (peak, off_peak)
PRICE_TABLE: dict[FeeModel, dict[str, tuple[Decimal, Decimal]]] = {
    FeeModel.FLASH: {
        "input_hit": (Decimal("0.014"), Decimal("0.007")),
        "input_miss": (Decimal("0.44"), Decimal("0.22")),
        "output": (Decimal("1.32"), Decimal("0.66")),
    },
    FeeModel.PRO: {
        "input_hit": (Decimal("0.044"), Decimal("0.022")),
        "input_miss": (Decimal("1.32"), Decimal("0.66")),
        "output": (Decimal("3.96"), Decimal("1.98")),
    },
    # 官方价格表 vision-exp 列与 flash 数值一致（独立列，非 starts_with 推断）。
    FeeModel.VISION_EXP: {
        "input_hit": (Decimal("0.014"), Decimal("0.007")),
        "input_miss": (Decimal("0.44"), Decimal("0.22")),
        "output": (Decimal("1.32"), Decimal("0.66")),
    },
}

_KNOWN_MODELS = set(PRICE_TABLE)

# 官方页面公示的 MODEL VERSION 值（见 pricing-source.md）。响应返回这些身份时属于
# "服务端合法版本别名"，但与请求 model 的对应关系未经验证：显式标记未验证并停止，
# 不得静默放行，也不得按别名猜测计费。
OFFICIAL_VERSION_ALIASES: dict[str, str] = {
    "DeepSeek-V4-Flash-0731": "deepseek-v4-flash",
    "DeepSeek-V4-Pro-0813": "deepseek-v4-pro",
    "DeepSeek-V4-Flash-Vision-Exp": "deepseek-v4-flash-vision-exp",
}


def is_peak(now_utc: dt.datetime) -> bool:
    """官方定义：01:00-04:00 与 06:00-10:00 UTC，周一至周五。"""
    weekday = now_utc.weekday()  # 0=Mon
    if weekday >= 5:
        return False
    t = now_utc.time().replace(tzinfo=None)
    return dt.time(1, 0) <= t < dt.time(4, 0) or dt.time(6, 0) <= t < dt.time(10, 0)


def rate(model: str, kind: str, now_utc: dt.datetime) -> Decimal:
    """结算用官方计价：按结算时间落在峰谷时段取价。未知模型/类目 → 停止，不猜价格。"""
    try:
        peak, off_peak = PRICE_TABLE[FeeModel(model)][kind]
    except KeyError as exc:
        raise StopSpike(f"未知价格类目 model={model!r} kind={kind!r}，停止并人工核查") from exc
    return peak if is_peak(now_utc) else off_peak


def max_rate(model: str, kind: str) -> Decimal:
    """预留用保守最坏价格：峰谷取最大，与调用时间无关（请求期间可能跨入峰段，
    且服务端计价时段可能与本地时钟有偏差；宁高不低）。"""
    try:
        peak, off_peak = PRICE_TABLE[FeeModel(model)][kind]
    except KeyError as exc:
        raise StopSpike(f"未知价格类目 model={model!r} kind={kind!r}，停止并人工核查") from exc
    return max(peak, off_peak)


def cheapest_rate(model: str, kind: str) -> Decimal:
    """防呆下界用最廉价格：峰谷取最小、跨类目取最小。未知模型/类目 → 停止。"""
    try:
        prices = PRICE_TABLE[FeeModel(model)]
        low = min(min(p) for p in prices.values())
        _ = prices[kind]  # 类目必须存在
    except KeyError as exc:
        raise StopSpike(f"未知价格类目 model={model!r} kind={kind!r}，停止并人工核查") from exc
    return low


def _per_token(rate_1m: Decimal) -> Decimal:
    return rate_1m / Decimal(1_000_000)


@dataclass
class Reservation:
    model: str
    usd: Decimal
    input_tokens_reserve: int = 0
    max_output: int = 0


@dataclass
class CallRecord:
    model: str
    reserved_usd: Decimal
    settled_usd: Decimal | None  # None = usage 缺失/取消/身份异常，预留全额保留
    usage_raw: dict | None = None
    # 防呆下界：按本次实际 token 规模 × 该模型最廉价 per-token 价（峰谷取最小）。
    # 结算价低于该下限即计费异常（少收费）。None = 无 usage，不适用。
    expected_min_usd: Decimal | None = None
    note: str | None = None


@dataclass
class FeeGuard:
    """唯一费用账本。所有真实调用必须先 reserve，调用后 settle；
    usage 缺失/取消保留预留不算 0；所有记账路径统一走同一阈值检查。"""

    reserved_usd: Decimal = Decimal("0")
    settled_usd: Decimal = Decimal("0")
    calls: list[CallRecord] = field(default_factory=list)
    stopped: str | None = None

    # ---- 预留 ----
    def worst_case_cost(self, model: str, *, input_tokens_reserve: int, max_output: int) -> Decimal:
        """保守最坏成本：全部按 max(peak, off_peak)、全部按 cache miss（hit 价 < miss 价）、输出按 max_output。
        与调用时间无关（见 max_rate）。"""
        _check_known(model)
        if not isinstance(input_tokens_reserve, int) or input_tokens_reserve < 0:
            raise StopSpike(f"输入预留 token 非法：{input_tokens_reserve!r}")
        _check_output_limit(max_output)
        return (
            _per_token(max_rate(model, "input_miss")) * input_tokens_reserve
            + _per_token(max_rate(model, "output")) * max_output
        )

    def reserve(
        self,
        model: str,
        *,
        input_tokens_reserve: int,
        max_output: int = MAX_TOKENS_LIMIT,
    ) -> Reservation:
        _check_known(model)
        _check_output_limit(max_output)
        if self.stopped:
            raise StopSpike(f"账本已停止：{self.stopped}")
        cost = self.worst_case_cost(model, input_tokens_reserve=input_tokens_reserve, max_output=max_output)
        if self.settled_usd + self.reserved_usd + cost > HARD_CAP_USD:
            raise StopSpike(
                f"硬顶 ${HARD_CAP_USD} 拒绝预留：settled={self.settled_usd} reserved={self.reserved_usd} need={cost}"
            )
        self.reserved_usd += cost
        return Reservation(model=model, usd=cost, input_tokens_reserve=input_tokens_reserve, max_output=max_output)

    # ---- 结算 ----
    def settle(
        self,
        res: Reservation,
        usage_raw: dict | None,
        *,
        now_utc: dt.datetime,
        model: str,
    ) -> Decimal:
        """按 usage 折算实际费用。model 必填 = 响应身份，由调用方在身份检查通过后传入；
        缺失/不匹配的身份不得进入 settle（调用方走 cancel + stop）。
        usage_raw=None 或缺 prompt/completion 总数 → 预留全额保留（不得记 0）。"""
        _check_known(model)
        actual: Decimal | None = None
        expected_min: Decimal | None = None
        try:
            if usage_raw is not None and not isinstance(usage_raw, dict):
                self.stopped = self.stopped or f"usage 非法（须为对象）：{usage_raw!r}，停止人工核查"
                raise StopSpike(self.stopped)
            parts = self._validated_usage(usage_raw) if usage_raw is not None else None
        except StopSpike as exc:
            # 结算校验失败（畸形/矛盾 usage）：不得在验证通过前解除预留后静默漏记。
            # 预留解除但仍全额计入累计（不算 0，与 usage 缺失同保守度），留可核查记录；
            # guard 停止并拒绝后续 reserve；预留只计一次，不重复计费。
            # 注意：_int_or_stop 等路径只抛异常不置停止位，此处兜底置位。
            self.stopped = self.stopped or f"usage 结算校验失败，停止人工核查：{exc}"
            self.reserved_usd -= res.usd
            self.calls.append(
                CallRecord(
                    model=model,
                    reserved_usd=res.usd,
                    settled_usd=None,
                    usage_raw=usage_raw,
                    note="settle_validation_failed",
                )
            )
            self.settled_usd += res.usd
            self._threshold_checks(raise_on_new_stop=False)
            raise
        self.reserved_usd -= res.usd
        if parts is not None:
            actual = self._cost(model, parts, now_utc)
            total_tokens = parts["hit"] + parts["miss"] + parts["completion"]
            expected_min = _per_token(cheapest_rate(model, "output")) * total_tokens
        if actual is None:
            # usage 缺失：预留全额保留，不算 0。
            self.calls.append(
                CallRecord(model=model, reserved_usd=res.usd, settled_usd=None, usage_raw=usage_raw, note="usage_missing")
            )
            self.settled_usd += res.usd
            self._threshold_checks(raise_on_new_stop=True)
            return res.usd
        if actual > res.usd:
            self.calls.append(
                CallRecord(
                    model=model,
                    reserved_usd=res.usd,
                    settled_usd=actual,
                    usage_raw=usage_raw,
                    expected_min_usd=expected_min,
                    note="actual_exceeds_reservation",
                )
            )
            self.settled_usd += actual
            self.stopped = self.stopped or (
                f"实际费用 ${actual} 超过预留 ${res.usd}：输入上界估算不足，停止并人工核查"
            )
            raise StopSpike(self.stopped)
        self.calls.append(
            CallRecord(model=model, reserved_usd=res.usd, settled_usd=actual, usage_raw=usage_raw, expected_min_usd=expected_min)
        )
        self.settled_usd += actual
        self._threshold_checks(raise_on_new_stop=True)
        return actual

    def _validated_usage(self, usage_raw: dict) -> dict | None:
        """usage 分项严格校验：非负有限整数；hit+miss==prompt（服务端分项同时提供时）；
        raw hit 与 details.cached_tokens 双来源必须一致。矛盾 → 停止人工核查。
        缺 prompt/completion → None（走预留保留路径）。"""
        prompt = usage_raw.get("prompt_tokens")
        completion = usage_raw.get("completion_tokens")
        if prompt is None or completion is None:
            return None
        prompt = self._int_or_stop("prompt_tokens", prompt)
        completion = self._int_or_stop("completion_tokens", completion)
        hit_raw = usage_raw.get("prompt_cache_hit_tokens")
        details = usage_raw.get("prompt_tokens_details")
        if details is not None and not isinstance(details, dict):
            # prompt_tokens_details 类型未先校验时，非 dict 会 AttributeError 而非保守停止。
            self.stopped = self.stopped or (
                f"usage 字段 prompt_tokens_details 非法（须为对象）：{details!r}，停止人工核查"
            )
            raise StopSpike(self.stopped)
        cached = details.get("cached_tokens") if details is not None else None
        if hit_raw is not None:
            hit_raw = self._int_or_stop("prompt_cache_hit_tokens", hit_raw)
        if cached is not None:
            cached = self._int_or_stop("prompt_tokens_details.cached_tokens", cached)
        miss_raw = usage_raw.get("prompt_cache_miss_tokens")
        if miss_raw is not None:
            miss_raw = self._int_or_stop("prompt_cache_miss_tokens", miss_raw)
        # 双来源一致性：raw hit 与嵌套 cached_tokens 同为命中分段，同时提供且不一致 → 停。
        if hit_raw is not None and cached is not None and hit_raw != cached:
            self.stopped = self.stopped or (
                f"usage 双来源不一致：prompt_cache_hit_tokens={hit_raw} != cached_tokens={cached}，停止人工核查"
            )
            raise StopSpike(self.stopped)
        if hit_raw is not None:
            hit = hit_raw
        elif cached is not None:
            hit = cached  # 明确记录的回退来源：details.cached_tokens
        else:
            hit = 0  # 无任何命中证据：保守按 0 命中（全部 miss 计费，多算不少算）
        if miss_raw is not None and hit + miss_raw != prompt:
            self.stopped = self.stopped or (
                f"usage 分项矛盾：hit({hit}) + miss({miss_raw}) != prompt({prompt})，停止人工核查"
            )
            raise StopSpike(self.stopped)
        miss = prompt - hit
        if miss < 0:
            self.stopped = self.stopped or f"usage 异常：hit({hit}) > prompt({prompt})，停止人工核查"
            raise StopSpike(self.stopped)
        return {"prompt": prompt, "completion": completion, "hit": hit, "miss": miss}

    @staticmethod
    def _int_or_stop(name: str, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or not math.isfinite(value):
            raise StopSpike(f"usage 字段 {name} 非法（须为非负整数）：{value!r}，停止人工核查")
        return value

    def _cost(self, model: str, parts: dict, now_utc: dt.datetime) -> Decimal:
        """同一 token 不重复计费：input = hit + miss（互斥分段），output 单列。"""
        return (
            _per_token(rate(model, "input_hit", now_utc)) * parts["hit"]
            + _per_token(rate(model, "input_miss", now_utc)) * parts["miss"]
            + _per_token(rate(model, "output", now_utc)) * parts["completion"]
        )

    def cancel(self, res: Reservation, reason: str) -> None:
        """调用取消/失败/身份异常：预留全额保留（不得算 0），计为未结算记录。
        不抛出（取消流程本身在收尾）；阈值检查统一生效，达到停止线后禁止下一次预留。"""
        self.reserved_usd -= res.usd
        self.calls.append(CallRecord(model=res.model, reserved_usd=res.usd, settled_usd=None, usage_raw=None, note=reason))
        self.settled_usd += res.usd
        self._threshold_checks(raise_on_new_stop=False)

    def stop(self, reason: str) -> None:
        """外部显式停止（如模型身份异常）：保留现场。"""
        if not self.stopped:
            self.stopped = reason

    # ---- 统一阈值检查（所有记账路径共用）----
    def _threshold_checks(self, *, raise_on_new_stop: bool) -> None:
        new_stop = self._new_stop_reason()
        if new_stop is not None:
            self.stopped = self.stopped or new_stop
            if raise_on_new_stop:
                raise StopSpike(self.stopped)

    def _new_stop_reason(self) -> str | None:
        if self.stopped:
            return None
        if self.settled_usd >= AUTO_STOP_USD:
            return f"累计结算 ${self.settled_usd} ≥ 自动停 ${AUTO_STOP_USD}"
        if self.settled_usd + self.reserved_usd > HARD_CAP_USD:
            return f"总占用 ${self.settled_usd + self.reserved_usd} 超硬顶 ${HARD_CAP_USD}"
        if len(self.calls) == ANOMALY_CHECK_AFTER_CALLS:
            return self._anomaly_reason()
        return None

    def _anomaly_reason(self) -> str | None:
        """第 10 次费用防呆：基于实际请求规模/模型的量级推导，不用武断固定金额窗口。
        - 累计为 0：异常（可能漏记账）。
        - 任何有 usage 的记录：结算价 < 期望下限（实际 token × 最廉价单价）→ 少收费异常。
        - 结算价 > 该次预留 → 已在 settle 即停（此处双保险）。"""
        if self.settled_usd == 0:
            return f"前 {len(self.calls)} 次调用累计费用为 0，停止人工核查价格表"
        for i, c in enumerate(self.calls, 1):
            if c.settled_usd is None:
                continue
            if c.expected_min_usd is not None and c.settled_usd < c.expected_min_usd:
                return (
                    f"第 {i} 次调用结算 ${c.settled_usd} 低于按实际 token 规模（最廉价单价）的期望下限 "
                    f"${c.expected_min_usd}，停止人工核查价格表"
                )
            if c.settled_usd > c.reserved_usd:
                return f"第 {i} 次调用结算 ${c.settled_usd} 超过预留 ${c.reserved_usd}，停止人工核查"
        return None

    def require_active(self) -> None:
        """供真实入口在每次调用前检查：账本已停止则拒绝。"""
        if self.stopped:
            raise StopSpike(f"账本已停止：{self.stopped}")


def _check_known(model: str) -> None:
    if model not in _KNOWN_MODELS:
        raise StopSpike(f"未知模型 {model!r}（含静默回落风险）：停止，不猜价格")


def _check_output_limit(max_output: int) -> None:
    if not isinstance(max_output, int) or isinstance(max_output, bool) or max_output <= 0 or max_output > MAX_TOKENS_LIMIT:
        raise StopSpike(f"max_output={max_output!r} 违反单次 ≤{MAX_TOKENS_LIMIT} 硬约束")
