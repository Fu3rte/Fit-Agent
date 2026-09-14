"""Stage 6 真实联调适配的离线验证（目录／profile／端点／费用护栏；无真实 Provider 请求）。

覆盖（每条都是可失败断言，不依赖真实网络）：

1. ``qwen3.7-flash`` 与 ``qwen3.6-flash`` 进入目录（owner 指定 3.6 为默认因 3.7 quota
   不可用），端点与窗口／输出上限取官方只读核对值／同族保守沿用；目录外 id 仍拒绝启动；
2. 两个模型的 framework profile 如实声明混合思考（默认开启）且窗口取目录值；
3. 生产模型构造走目录端点，并使用通用 OpenAI 兼容 provider（不是 DeepSeek 专属 provider）；
4. 冻结 Harness 容量参数在新模型下仍成立（有效输入 250,000 + 输出预留 + 安全余量 ≤ 窗口）；
5. 费用预留算术取官方最高档 + 缓存未命中 + 固定保守汇率；
6. 持久账本：预留／真实结算／未知 usage 保守扣账／崩溃遗留孤儿结算／跨重启累计／
   stage 之间不混用；
7. 生产执行路径真的接了护栏：Stub 模型跑完一次 Run 后账本有已花费、无在途预留；
   余额不足以支付一次预留时**不发送**任何请求（沿用 ``model_request_failed``）；
8. 结算边界：usage 为 0/0 按未知处理按预留额扣账、非流式 ``finish_reason`` 拒绝先结算
   已知 usage、流完整交付并结算后内层收尾抛错不改判结果也不二次结算。
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from config import HarnessConfig, effective_harness_config
from runtime.agent_factory import build_review_run_work, build_run_work
from runtime.budget import BudgetedModel, RunBudget
from runtime.error_codes import MODEL_REQUEST_FAILED
from runtime.fees import (
    FIXED_CNY_PER_USD,
    STAGE6_LIMIT_USD,
    FeeLedger,
    MissingPrice,
    price_for,
    reservation_usd,
    usage_usd,
)
from runtime.models import (
    ALIYUN_BAILIAN_BASE_URL,
    QWEN36_FLASH,
    QWEN37_FLASH,
    UnknownModelId,
    require_supported_model_id,
    resolve_model_profile,
)
from runtime.provider import build_model
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver, ExecutionFailure
from storage.fee_repo import FeeRepo
from storage.run_repo import RunRepo
from tests.support import open_database

MODEL_ID = "qwen3.6-flash"  # owner 指定的默认生产模型（3.7 403 quota 不可用时的可用同族）
CONVERSATION_ID = "c1"
#: 预留算术的手算期望（官方最高档 1.2/4.8 元每百万 token ÷ 固定 6.5；输入 250,000、
#: 输出上界 262,144（最大思维链）+ 131,072（最大输出））。
EXPECTED_RESERVATION_USD = (
    250_000 * 1.2 / FIXED_CNY_PER_USD + (262_144 + 131_072) * 4.8 / FIXED_CNY_PER_USD
) / 1_000_000

pytestmark = pytest.mark.anyio


def _text_model(text: str, counter: list[int] | None = None) -> FunctionModel:
    """离线桩模型：只回答一段可见文本，可选统计实际被调用的次数。"""

    def respond(messages: Sequence[object], info: AgentInfo) -> ModelResponse:
        if counter is not None:
            counter.append(1)
        return ModelResponse(parts=[TextPart(content=text)])

    return FunctionModel(respond)


# ---------- 目录 / profile / 端点 ----------


async def test_qwen_catalog_entry_uses_official_facts_and_owner_endpoint() -> None:
    spec = require_supported_model_id(MODEL_ID)
    assert spec is QWEN36_FLASH
    assert spec.base_url == ALIYUN_BAILIAN_BASE_URL
    assert spec.context_window == 1_000_000
    assert spec.max_output_tokens == 131_072
    # 3.7 仍在目录里可选（quota 恢复后可切回），事实与端点不变
    spec37 = require_supported_model_id("qwen3.7-flash")
    assert spec37 is QWEN37_FLASH
    assert spec37.base_url == ALIYUN_BAILIAN_BASE_URL
    assert spec37.context_window == 1_000_000
    assert spec37.max_output_tokens == 131_072


async def test_unknown_model_id_is_still_rejected() -> None:
    with pytest.raises(UnknownModelId):
        require_supported_model_id("qwen3.6-flash-2026-04-16")
    with pytest.raises(UnknownModelId):
        require_supported_model_id("qwen3.7-flash-2026-07-15")


async def test_qwen_profile_declares_hybrid_thinking_on_by_default() -> None:
    profile = resolve_model_profile(MODEL_ID)
    assert profile.get("context_window") == 1_000_000
    assert profile.get("supports_thinking") is True
    assert profile.get("openai_reasoning_enabled_by_default") is True
    # 混合模式：Provider 也支持关闭，目录不得写成「无法关闭思考」
    assert profile.get("thinking_always_enabled") is False


async def test_frozen_harness_capacity_parameters_still_hold_for_qwen() -> None:
    harness = effective_harness_config(HarnessConfig(model_id=MODEL_ID))
    assert harness.context_window == 1_000_000
    assert harness.effective_input_tokens == 250_000
    assert harness.max_output_tokens == 8_192
    assert (
        harness.effective_input_tokens
        + harness.output_reserve_tokens
        + harness.safety_margin_tokens(harness.effective_input_tokens)
        <= harness.context_window
    )


async def test_build_model_targets_catalog_endpoint_with_generic_openai_provider() -> (
    None
):
    model = build_model("fake-key-not-used", model_id=MODEL_ID)
    assert str(model.client.base_url).rstrip("/") == ALIYUN_BAILIAN_BASE_URL
    # 通用 OpenAI 兼容 provider（不是 DeepSeek 专属 provider，也不推断别名）
    provider = model.provider
    assert provider is not None
    assert provider.name != "deepseek"


# ---------- 费用算术 ----------


async def test_reservation_matches_top_tier_cache_miss_with_fixed_fx() -> None:
    price = price_for(MODEL_ID)
    assert price.usd_per_million_input == pytest.approx(1.2 / FIXED_CNY_PER_USD)
    assert price.usd_per_million_output == pytest.approx(4.8 / FIXED_CNY_PER_USD)
    assert price.billed_output_bound_tokens == 262_144 + 131_072
    assert reservation_usd(MODEL_ID, 250_000) == pytest.approx(EXPECTED_RESERVATION_USD)
    assert EXPECTED_RESERVATION_USD == pytest.approx(0.3365287, abs=1e-6)
    # 一次预留远小于额度：USD 50 至少可支持 100 次最坏预留
    assert STAGE6_LIMIT_USD / reservation_usd(MODEL_ID, 250_000) > 100


async def test_usage_settlement_counts_reasoning_tokens_as_output() -> None:
    # 官方：思考内容按输出 Token 计费；框架的 output_tokens 已含 reasoning tokens
    assert usage_usd(
        MODEL_ID, input_tokens=1_000, output_tokens=2_000
    ) == pytest.approx(
        1_000 * 1.2 / FIXED_CNY_PER_USD / 1_000_000
        + 2_000 * 4.8 / FIXED_CNY_PER_USD / 1_000_000
    )


async def test_missing_price_refuses_to_estimate() -> None:
    with pytest.raises(MissingPrice):
        reservation_usd("no-such-model", 250_000)


# ---------- 持久账本 ----------


async def test_ledger_reserves_settles_and_survives_reopen(tmp_path) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        ledger = FeeLedger(FeeRepo(db))
        fresh = await ledger.snapshot()
        assert fresh == {
            "limit_usd": STAGE6_LIMIT_USD,
            "spent_usd": 0.0,
            "reserved_usd": 0.0,
            "available_usd": STAGE6_LIMIT_USD,
        }
        reservation = await ledger.reserve(1.0)
        assert reservation is not None
        in_flight = await ledger.snapshot()
        assert in_flight["reserved_usd"] == pytest.approx(1.0)
        assert in_flight["available_usd"] == pytest.approx(49.0)
        settled = await ledger.settle(reservation, actual_usd=0.25)
        assert settled["spent_usd"] == pytest.approx(0.25)
        assert settled["reserved_usd"] == 0.0
    # 跨重启累计：重新打开同一库，已花费与余额不重置（08）
    async with open_database(path) as db:
        reopened = await FeeLedger(FeeRepo(db)).snapshot()
        assert reopened["spent_usd"] == pytest.approx(0.25)
        assert reopened["available_usd"] == pytest.approx(49.75)


async def test_ledger_refuses_when_balance_is_insufficient(tmp_path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        ledger = FeeLedger(FeeRepo(db), limit_usd=1.0)
        first = await ledger.reserve(0.75)
        assert first is not None
        assert await ledger.reserve(0.5) is None  # 余额 0.25，拒绝
        after = await ledger.snapshot()
        assert after["reserved_usd"] == pytest.approx(0.75)  # 拒绝不写账本
        assert after["spent_usd"] == 0.0


async def test_unknown_usage_charges_the_reservation_not_zero(tmp_path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        ledger = FeeLedger(FeeRepo(db))
        reservation = await ledger.reserve(0.4)
        assert reservation is not None
        settled = await ledger.settle(reservation, actual_usd=None)
        assert settled["spent_usd"] == pytest.approx(0.4)  # 未知 usage 按预留额保守扣账
        assert settled["reserved_usd"] == 0.0


async def test_crashed_process_reservation_is_charged_as_unknown_on_next_run(
    tmp_path,
) -> None:
    path = tmp_path / "app.db"
    async with open_database(path) as db:
        ledger = FeeLedger(FeeRepo(db))
        assert await ledger.reserve(0.9) is not None  # 模拟发送后进程消失，未结算
    async with open_database(path) as db:
        ledger = FeeLedger(FeeRepo(db))
        prepared = await ledger.prepare_run()  # 下一次 Run 开始
        assert prepared["spent_usd"] == pytest.approx(0.9)
        assert prepared["reserved_usd"] == 0.0
        assert prepared["available_usd"] == pytest.approx(49.1)


async def test_stage_ledgers_do_not_mix(tmp_path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = FeeRepo(db)
        assert await repo.reserve("stage4", amount_usd=5.0, limit_usd=10.0)
        stage6 = await FeeLedger(repo).snapshot()  # Stage 6 账本不受 Stage 4 行影响
        assert stage6["spent_usd"] == 0.0
        assert stage6["reserved_usd"] == 0.0
        assert stage6["available_usd"] == pytest.approx(STAGE6_LIMIT_USD)


# ---------- 生产执行路径接线 ----------


async def test_insufficient_balance_blocks_the_request_before_sending(tmp_path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        calls: list[int] = []
        ledger = FeeLedger(FeeRepo(db), limit_usd=0.01)  # 连一次预留都不够
        budget = RunBudget(
            effective_harness_config(HarnessConfig(model_id=MODEL_ID)),
            cancelled=lambda: False,
        )
        model = BudgetedModel(_text_model("不会发生", calls), budget, ledger=ledger)
        with pytest.raises(ExecutionFailure) as excinfo:
            await model.request([], None, ModelRequestParameters())
        assert excinfo.value.error_code == MODEL_REQUEST_FAILED
        assert calls == []  # 余额不足不发送（08）
        assert (await ledger.snapshot())["spent_usd"] == 0.0


async def test_production_run_wires_ledger_and_settles_real_usage(tmp_path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await repo.create_conversation(CONVERSATION_ID)
        await RunService(repo).submit_request(
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            client_request_id="req-r1",
            text="你好",
        )
        harness = effective_harness_config(HarnessConfig(model_id=MODEL_ID))
        work = build_run_work(
            db=db,
            repo=repo,
            model=_text_model("好的"),
            harness=harness,
            conversation_id=CONVERSATION_ID,
            run_id="r1",
            business_date=date(2026, 9, 20),
        )
        await ExecutionDriver(repo).start("r1", work)
        snapshot = await FeeLedger(FeeRepo(db)).snapshot()
        assert snapshot["reserved_usd"] == 0.0  # 已结算，无在途预留
        assert snapshot["spent_usd"] > 0.0  # 真实 usage 已入账
        assert snapshot["spent_usd"] < snapshot["limit_usd"]


async def test_review_run_also_wires_the_ledger(tmp_path) -> None:
    """复盘 Run 是另一条真实模型入口：同样预留／结算，不留费用旁路。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await RunService(repo).request_review(
            run_id="r-rev", client_request_id="req-r-rev"
        )
        harness = effective_harness_config(HarnessConfig(model_id=MODEL_ID))
        work = build_review_run_work(
            db=db,
            model=_text_model("当前没有可解释的完成率或 PR 数据。"),
            harness=harness,
            business_date=date(2026, 9, 20),
        )
        await ExecutionDriver(repo).start("r-rev", work)
        snapshot = await FeeLedger(FeeRepo(db)).snapshot()
        assert snapshot["reserved_usd"] == 0.0  # 已结算，无在途预留
        assert snapshot["spent_usd"] > 0.0  # 该入口的请求同样计入 Stage 6 额度


# ---------- 结算边界（owner 1A） ----------


def _budget() -> RunBudget:
    return RunBudget(
        effective_harness_config(HarnessConfig(model_id=MODEL_ID)),
        cancelled=lambda: False,
    )


class _FixedResponseModel(WrapperModel):
    """桩：``request`` 原样返回给定响应（绕过 FunctionModel 的 usage 估算）。"""

    def __init__(self, response: ModelResponse) -> None:
        super().__init__(FunctionModel(lambda messages, info: response))
        self._response = response

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        return self._response


class _TeardownRaisesModel(WrapperModel):
    """桩：内层流完整交付后，收尾（``__aexit__``）抛错。"""

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with self.wrapped.request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as response_stream:
            yield response_stream
            raise RuntimeError("内层流收尾失败（桩）")


async def test_zero_usage_is_unknown_and_charges_the_reservation(tmp_path) -> None:
    """输入与输出都为 0 的 usage 不是「已知零花费」：按未知 usage 扣预留额。"""
    async with open_database(tmp_path / "app.db") as db:
        ledger = FeeLedger(FeeRepo(db))
        model = BudgetedModel(
            _FixedResponseModel(ModelResponse(parts=[TextPart(content="用量未回报")])),
            _budget(),
            ledger=ledger,
        )
        response = await model.request([], None, ModelRequestParameters())
        assert response.usage.input_tokens == 0
        assert response.usage.output_tokens == 0
        snapshot = await ledger.snapshot()
        assert snapshot["spent_usd"] == pytest.approx(EXPECTED_RESERVATION_USD)
        assert snapshot["reserved_usd"] == 0.0


async def test_non_stream_finish_reason_rejection_settles_known_usage(tmp_path) -> None:
    """已知非流式 usage 先结算再判 finish_reason：拒绝不留占用全额预留的孤儿预留。"""
    async with open_database(tmp_path / "app.db") as db:
        ledger = FeeLedger(FeeRepo(db))
        rejected = ModelResponse(
            parts=[TextPart(content="被截断的回答")],
            usage=RequestUsage(input_tokens=1_000, output_tokens=200),
            finish_reason="length",
        )
        model = BudgetedModel(_FixedResponseModel(rejected), _budget(), ledger=ledger)
        with pytest.raises(ExecutionFailure) as excinfo:
            await model.request([], None, ModelRequestParameters())
        assert excinfo.value.error_code == MODEL_REQUEST_FAILED
        snapshot = await ledger.snapshot()
        assert snapshot["spent_usd"] == pytest.approx(
            usage_usd(MODEL_ID, input_tokens=1_000, output_tokens=200)
        )
        assert snapshot["reserved_usd"] == 0.0


async def test_stream_teardown_after_delivery_settles_exactly_once(tmp_path) -> None:
    """流已完整交付并结算后内层收尾抛错：结果不改判、账本不二次结算。"""

    async def stream_fn(
        messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncGenerator[str]:
        yield "流式回答"

    async with open_database(tmp_path / "app.db") as db:
        ledger = FeeLedger(FeeRepo(db))
        model = BudgetedModel(
            _TeardownRaisesModel(FunctionModel(stream_function=stream_fn)),
            _budget(),
            ledger=ledger,
        )
        async with model.request_stream([], None, ModelRequestParameters()) as stream:
            events = [event async for event in stream]
            usage = stream.usage
        assert events  # 流确实被完整消费
        assert usage.has_values()  # 桩流给出已知（非 0/0）usage
        snapshot = await ledger.snapshot()
        assert snapshot["reserved_usd"] == 0.0
        assert snapshot["spent_usd"] == pytest.approx(
            usage_usd(
                MODEL_ID,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        )
