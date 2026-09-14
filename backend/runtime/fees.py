"""Stage 6 真实联调费用护栏：价目事实、预留／结算算术与持久账本（08「Stage 6 联调费用护栏」）。

owner 2026-09-13 拍板：Stage 6 真实模型联调累计 **USD 50**，与历史 Stage 4 的 USD 10
**分开记账、不累加、不混用**；预留／结算／未知 usage 保守扣账／跨重启累计的规则与
Stage 4 护栏同构。本模块是这条护栏的唯一实现点。

价目事实（只读核对，检索日期与来源 URL 登记在 ``pre-prj/stage/evidence/S4-evidence.md``）：

- 端点：阿里云百炼 OpenAI 兼容端点（业务空间专属域名，见 :mod:`runtime.models`），
  按 Token 计费（owner 2026-09-13 确认非 PTU／模型单元时长计费）。
- ``qwen3.7-flash``（华北2 北京，官方「模型信息」页）：单次请求输入长度分档计价，
  输入 ``≤32k`` 0.2 元／``32k–256k`` 0.6 元／``256k–1m`` 1.2 元，输出 0.8／2.4／4.8 元
  （每百万 token）。预留取**最高档 + 缓存未命中**（官方隐式缓存命中约 20%，缺失或不命中
  即按全价），因此预检与结算都不打折。
- 思考内容按输出 Token 计费（官方「深度思考模型的用法」），可见 ``max_tokens`` 不是总输出
  上界：输出预留上界取官方「最大思维链长度 262,144 + 最大输出长度 131,072」的保守合计。
- ``qwen3.6-flash``：未单独核到 3.6 价目，按 3.7 最高档（1.2／4.8 元每百万 token）
  保守预留；输出上界同 3.7 思维链口径。

币种：官方价目只有人民币，账本按已拍口径以 **USD** 计价，只在这里做一次换算，
汇率取固定保守值 ``1 USD = 6.5 CNY``（owner 2026-09-13 拍板）：实际汇率不低于该值时，
USD 上限对应的真实花费不超过 USD 50。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from runtime.models import DEEPSEEK_FLASH, QWEN36_FLASH, QWEN37_FLASH
from storage.fee_repo import FeeRepo

#: 本护栏的账本身份：Stage 6 与历史 Stage 4（USD 10，脚本级）不共用行、不互相解冻。
STAGE6_STAGE = "stage6"

#: Stage 6 累计额度（USD，2026-09-13 owner 拍板；不含 09 章正式测评）。
STAGE6_LIMIT_USD = 50.0

#: 人民币→美元固定保守汇率（owner 2026-09-13 拍板）。
FIXED_CNY_PER_USD = 6.5


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的费用事实（USD／百万 token）与输出预留上界（token）。"""

    usd_per_million_input: float
    usd_per_million_output: float
    billed_output_bound_tokens: int


def _cny_top_tier(cny_input: float, cny_output: float, bound_tokens: int) -> ModelPrice:
    """人民币最高档价目 → USD 价目（唯一换算点，汇率见模块 docstring）。"""
    return ModelPrice(
        usd_per_million_input=cny_input / FIXED_CNY_PER_USD,
        usd_per_million_output=cny_output / FIXED_CNY_PER_USD,
        billed_output_bound_tokens=bound_tokens,
    )


#: 价目表：键为模型目录里的 id（``runtime.models.SUPPORTED_MODELS``），缺价即拒绝发送。
PRICES: Mapping[str, ModelPrice] = {
    DEEPSEEK_FLASH.model_id: ModelPrice(
        # 官方 Models & Pricing（2026-09-12 只读核对）峰价上界，USD／百万 token。
        usd_per_million_input=0.30,
        usd_per_million_output=1.20,
        # 目录事实：模型总输出上限（DeepSeek 无单独思维链上限）。
        billed_output_bound_tokens=DEEPSEEK_FLASH.max_output_tokens,
    ),
    QWEN37_FLASH.model_id: _cny_top_tier(
        1.2, 4.8, 262_144 + QWEN37_FLASH.max_output_tokens
    ),
    # 未单独核到 3.6 价目，按 3.7 最高档保守预留；输出上界同 3.7 思维链口径。
    QWEN36_FLASH.model_id: _cny_top_tier(
        1.2, 4.8, 262_144 + QWEN36_FLASH.max_output_tokens
    ),
}


class MissingPrice(RuntimeError):
    """目录里的模型没有已核实价目：拒绝发送（08：价格或计费口径无法核实时不发送）。"""


def price_for(model_id: str) -> ModelPrice:
    """模型价目；目录外或缺价一律抛 :class:`MissingPrice`，不取默认值。"""
    price = PRICES.get(model_id)
    if price is None:
        raise MissingPrice(
            f"模型 {model_id!r} 没有已核实的价目：拒绝发送任何请求（不按默认价预检）"
        )
    return price


def reservation_usd(model_id: str, input_bound_tokens: int) -> float:
    """单次请求的费用预留上界（发送前预留；余额不足不发送）。

    输入按调用方给出的输入上界（Run 冻结的有效输入上限），输出按模型的输出预留上界，
    两顶都按缓存未命中的全价计——预留是上界，不以下限或缓存命中估算。
    """
    price = price_for(model_id)
    amount = (
        input_bound_tokens * price.usd_per_million_input
        + price.billed_output_bound_tokens * price.usd_per_million_output
    ) / 1_000_000
    if not math.isfinite(amount) or amount <= 0:
        raise MissingPrice(f"模型 {model_id!r} 的预留额非正或非有限：拒绝发送")
    return amount


def usage_usd(model_id: str, *, input_tokens: int, output_tokens: int) -> float:
    """按真实 usage 结算的费用（思考 token 已含在 ``output_tokens`` 里，官方口径）。

    缓存命中的输入同样按全价计（保守侧；官方隐式缓存折扣即“未打折”，不低估费用）。
    """
    price = price_for(model_id)
    if input_tokens < 0 or output_tokens < 0:
        raise MissingPrice("usage 为负：拒绝结算，不写账本")
    return (
        input_tokens * price.usd_per_million_input
        + output_tokens * price.usd_per_million_output
    ) / 1_000_000


@dataclass(frozen=True)
class Reservation:
    """一次已入账的预留：结算时按身份归还预留额并计入真实花费（或未知 usage 的预留额）。"""

    amount_usd: float


class FeeLedger:
    """跨重启持久账本（08：重启、换会话、新建 Run 都不重置额度）。

    - ``reserve`` 是唯一放行点：余额（额度 − 已花费 − 在途预留）不足返回 ``None``，
      调用方**不发送**请求（不自动提额、不降级为部分发送）。
    - ``settle`` 把在途预留换成真实花费；usage 未知（断流、未分类失败、进程崩溃）时按
      预留额保守扣账，不把未知费用记为零。
    - ``prepare_run`` 把上一次进程留下的在途预留按未知 usage 扣账（全局单 Run：本进程
      同一时刻最多一笔在途预留，因此 Run 开始时仍在途的预留一定来自崩溃）。
    """

    def __init__(
        self,
        repo: FeeRepo,
        *,
        stage: str = STAGE6_STAGE,
        limit_usd: float = STAGE6_LIMIT_USD,
    ) -> None:
        if not math.isfinite(limit_usd) or limit_usd <= 0:
            raise ValueError("费用额度必须是正有限数值（不降级为无上限）")
        self._repo = repo
        self._stage = stage
        self._limit_usd = limit_usd

    async def prepare_run(self) -> dict[str, float]:
        """Run 开始前处理崩溃遗留的在途预留（按未知 usage 扣账），返回账本快照。"""
        return await self._repo.settle_orphan_reservations(
            self._stage, limit_usd=self._limit_usd
        )

    async def reserve(self, amount_usd: float) -> Reservation | None:
        """预留费用上界；余额不足返回 ``None``（调用方不得发送请求）。"""
        if not math.isfinite(amount_usd) or amount_usd <= 0:
            return None
        granted = await self._repo.reserve(
            self._stage, amount_usd=amount_usd, limit_usd=self._limit_usd
        )
        return None if not granted else Reservation(amount_usd=amount_usd)

    async def settle(
        self, reservation: Reservation, *, actual_usd: float | None
    ) -> dict[str, float]:
        """结算一笔预留：``actual_usd`` 为 ``None`` 时按预留额保守扣账（未知 usage）。"""
        charged = reservation.amount_usd if actual_usd is None else max(0.0, actual_usd)
        return await self._repo.settle(
            self._stage,
            reserved_usd=reservation.amount_usd,
            charged_usd=charged,
            limit_usd=self._limit_usd,
        )

    async def snapshot(self) -> dict[str, float]:
        """账本快照（额度、已花费、在途预留、余额）；供查询与证据，不改账本。"""
        return await self._repo.snapshot(self._stage, limit_usd=self._limit_usd)
