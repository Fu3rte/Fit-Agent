"""S4-06b：活跃 prompt 估算、旧历史安全压缩与请求投影（stage4.md S4-06 验收；08「容量、估算与溢出」）。

覆盖（每项对应 stage4.md S4-06 验收的一条）：

1. 估算 = ``max(ceil(0.6 × 字符数), 最近真实 usage.input_tokens + ceil(0.6 × 新增字符))``；
   普通请求容量方程（有效输入上限 + 输出预留 + 安全余量）与摘要请求容量方程。
2. 达到派生触发点才压缩；未达到不动历史。
3. 只摘要最老的完整交互：工具调用与结果不拆、当前 Run 不压缩、未完成回答不进摘要输入，
   失败／取消请求与中断标注保留。
4. 保留尾段：最近完整交互到派生 10% 目标。
5. 连续摘要：覆盖延伸（前缀扩展）、来源并集、原消息仍可追溯。
6. 有界收缩、无安全切点、缩到无可摘要、同一上下文状态不重复尝试。
7. 可恢复生成失败保留旧上下文；存储失败与取消不降级继续。
8. 最终 ``context_budget_exceeded``：不实际发送、Run 终态结束。

全程离线确定性：直接构造的缩小阈值 ``EffectiveHarness``、``tmp_path`` 临时文件库、脚本桩模型，
无真实 Provider 请求、不读取凭据。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from config import EffectiveHarness, HarnessConfig, effective_harness_config
from runtime.agent_factory import build_run_work
from runtime.budget import BudgetedModel, RunBudget
from runtime.compression import (
    ContextCompressor,
    PromptEstimator,
    character_bound_tokens,
    load_active_summary,
    ordinary_request_fits,
    summary_request_fits,
)
from runtime.context import INTERRUPTION_MARKER, load_conversation_interactions
from runtime.error_codes import CONTEXT_BUDGET_EXCEEDED, MODEL_REQUEST_FAILED
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver, ExecutionFailure
from storage.errors import InvalidInput
from storage.run_repo import RunRepo
from storage.summary_repo import SummaryRepo
from tests.support import dump_framework_message, open_database
from tests.test_stage3_plan_confirm import _formal_profile

CONVERSATION_ID = "c1"
BUSINESS_DATE = date(2026, 9, 20)
CURRENT_RUN = "run-current"
SUMMARY_TEXT = "较早历史摘要XYZ"
PARTIAL_TEXT = "未完成的部分回答不应进摘要"
CURRENT_TEXT = "本轮用户请求"
OLD_TEXT = "老" * 900
OLD_2_TEXT = "二" * 900
OLD_3_TEXT = "三" * 900
FAILED_TEXT = "被取消的请求" * 8
#: 系统事实投影的实际开头（用于区分普通请求与摘要请求）。
FACTS_MARKER = "以下是系统在本次请求开始时"


def _reduced_harness(**overrides: Any) -> EffectiveHarness:
    """缩小阈值的确定性有效配置（直接构造即 S4-05a 已知测试旁路；边界校验归配置测试）。"""
    values: dict[str, Any] = {
        "effective_input_tokens": 4_000,
        "compression_trigger_tokens": 3_000,
        "retained_history_tokens": 200,
        "summary_output_tokens": 100,
        "max_output_tokens": 100,
        "context_window": 20_000,
    }
    values.update(overrides)
    return replace(effective_harness_config(HarnessConfig()), **values)


def _integration_harness(**overrides: Any) -> EffectiveHarness:
    """经 ``build_run_work`` 的整链路阈值：系统事实与工具定义本身已占数千字符。"""
    values: dict[str, Any] = {
        "effective_input_tokens": 12_000,
        "compression_trigger_tokens": 9_600,
        "retained_history_tokens": 1_200,
        "summary_output_tokens": 200,
        "max_output_tokens": 200,
        "context_window": 100_000,
    }
    values.update(overrides)
    return replace(effective_harness_config(HarnessConfig()), **values)


class _Clock:
    """可控单调钟：预算墙钟不依赖真实等待。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Sleeper:
    """退避等待替身。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _budget(
    harness: EffectiveHarness, *, cancelled: Any = None, clock: _Clock | None = None
) -> RunBudget:
    return RunBudget(
        harness,
        cancelled=cancelled if cancelled is not None else (lambda: False),
        clock=clock if clock is not None else _Clock(),
        sleep=_Sleeper(),
    )


def _facts_request(text: str = "当前事实") -> ModelRequest:
    return ModelRequest(parts=[SystemPromptPart(content=text)])


def _message_json(messages: Sequence[ModelMessage]) -> str:
    return ModelMessagesTypeAdapter.dump_json(list(messages)).decode("utf-8")


class _SummaryStub:
    """摘要模型桩：记录每次摘要请求；可先抛可恢复失败。"""

    def __init__(
        self, *, error: BaseException | None = None, text: str = SUMMARY_TEXT
    ) -> None:
        self.calls: list[list[ModelMessage]] = []
        self.settings: list[ModelSettings | None] = []
        self.error = error
        self.text = text

    async def request_summary(
        self, messages: list[ModelMessage], model_settings: ModelSettings | None = None
    ) -> ModelResponse:
        self.calls.append(list(messages))
        self.settings.append(model_settings)
        if self.error is not None:
            raise self.error
        return ModelResponse(parts=[TextPart(content=self.text)])


class _RecordingModel:
    """脚本桩模型：固定回答，记录每次请求消息（可用于区分摘要请求与普通请求）。"""

    def __init__(
        self,
        *,
        answer: str = "完成",
        delay: float = 0.0,
        started: asyncio.Event | None = None,
    ) -> None:
        self.answer = answer
        self.delay = delay
        self.started = started
        self.calls: list[str] = []

    def model(self) -> FunctionModel:
        async def respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> ModelResponse:
            self.calls.append(_message_json(messages))
            if self.started is not None:
                self.started.set()
            if self.delay:
                await asyncio.sleep(self.delay)
            return ModelResponse(parts=[TextPart(content=self._reply(messages))])

        return FunctionModel(respond)

    def _reply(self, messages: Sequence[ModelMessage]) -> str:
        """摘要请求（系统指令来自摘要器）回摘要文本，普通请求回固定回答。"""
        for message in messages:
            for part in getattr(message, "parts", ()):
                if isinstance(part, SystemPromptPart) and str(part.content).startswith(
                    "你是会话上下文整理器"
                ):
                    return SUMMARY_TEXT
        return self.answer


# ---------- 临时库与历史交互种子（真实存储链路，不旁路写表） ----------


async def _seed_run(
    db: Any, *, run_id: str, text: str, answer: str = "好的", status: str = "completed"
) -> None:
    repo = RunRepo(db)
    await repo.create_run_with_user_message(
        CONVERSATION_ID, run_id, f"req-{run_id}", text
    )
    await repo.start_run(run_id)
    if status == "completed":
        await repo.complete_run(
            run_id,
            [
                (
                    "user",
                    dump_framework_message(
                        ModelRequest(parts=[UserPromptPart(content=text)])
                    ),
                ),
                (
                    "assistant",
                    dump_framework_message(
                        ModelResponse(parts=[TextPart(content=answer)])
                    ),
                ),
            ],
        )
    elif status == "cancelled":
        await repo.cancel_run(run_id)
    else:
        await repo.fail_run(run_id, MODEL_REQUEST_FAILED)


async def _seed_tool_run(
    db: Any, *, run_id: str, text: str, answer: str = "看完记录"
) -> None:
    """含完整工具调用／结果对与一条部分回答的已完成 Run。"""
    repo = RunRepo(db)
    await repo.create_run_with_user_message(
        CONVERSATION_ID, run_id, f"req-{run_id}", text
    )
    await repo.start_run(run_id)
    call_id = f"call-{run_id}"
    await repo.save_partial_answer(run_id, PARTIAL_TEXT)
    await repo.complete_run(
        run_id,
        [
            (
                "user",
                dump_framework_message(
                    ModelRequest(parts=[UserPromptPart(content=text)])
                ),
            ),
            (
                "assistant",
                dump_framework_message(
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="read_week_completion",
                                args={"week_no": 1},
                                tool_call_id=call_id,
                            )
                        ]
                    )
                ),
            ),
            (
                "user",
                dump_framework_message(
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                tool_name="read_week_completion",
                                content={"completed": 1},
                                tool_call_id=call_id,
                            )
                        ]
                    )
                ),
            ),
            (
                "assistant",
                dump_framework_message(ModelResponse(parts=[TextPart(content=answer)])),
            ),
        ],
    )


async def _current_run(
    db: Any, *, run_id: str = CURRENT_RUN, text: str = CURRENT_TEXT
) -> None:
    repo = RunRepo(db)
    await repo.create_run_with_user_message(
        CONVERSATION_ID, run_id, f"req-{run_id}", text
    )
    await repo.start_run(run_id)


async def _interactions(db: Any, *, current_run_id: str = CURRENT_RUN) -> list[Any]:
    return await load_conversation_interactions(
        RunRepo(db), conversation_id=CONVERSATION_ID, current_run_id=current_run_id
    )


def _compressor(
    db: Any,
    *,
    harness: EffectiveHarness,
    model: Any,
    run_id: str = CURRENT_RUN,
) -> ContextCompressor:
    return ContextCompressor(
        summaries=SummaryRepo(db),
        harness=harness,
        budget=_budget(harness),
        model=model,
        estimator=PromptEstimator(),
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
    )


# ---------- 1. 估算与容量方程 ----------


def test_prompt_estimator_uses_character_bound_and_real_usage_anchor() -> None:
    estimator = PromptEstimator()
    assert character_bound_tokens(1_000) == 600  # ceil(0.6 × 字符数)
    assert estimator.estimate(1_000) == 600  # 无锚点：官方字符上界
    estimator.record(1_000, 700)
    assert estimator.estimate(1_000) == 700  # 与真实 usage 取较大值
    assert estimator.estimate(1_100) == max(660, 700 + 60)  # 新增字符按 0.6 增长
    estimator.record(1_100, None)  # 未知 usage 不替换锚点
    assert estimator.estimate(1_100) == 760
    estimator.clear()  # 上下文被压缩重写后锚点失效
    assert estimator.estimate(1_100) == 660


def test_capacity_equations_follow_input_limit_output_reserve_and_window() -> None:
    harness = _reduced_harness()
    assert ordinary_request_fits(4_000, harness)
    assert not ordinary_request_fits(4_001, harness)  # 超有效输入上限
    # 窗口方程：估算输入 + 输出预留 100 + 安全余量 max(4096, 10%) ≤ 模型窗口
    assert not ordinary_request_fits(4_000, _reduced_harness(context_window=8_195))
    assert ordinary_request_fits(4_000, _reduced_harness(context_window=8_196))
    # 摘要请求方程：估算输入 + 摘要输出上限 ≤ 有效输入上限
    assert summary_request_fits(3_900, harness)
    assert not summary_request_fits(3_901, harness)


async def test_budgeted_model_gate_ends_oversized_request_without_sending() -> None:
    sent: list[list[ModelMessage]] = []

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        sent.append(messages)
        return ModelResponse(parts=[TextPart(content="完成")])

    harness = _reduced_harness(effective_input_tokens=10, compression_trigger_tokens=8)
    budgeted = BudgetedModel(FunctionModel(respond), _budget(harness))
    with pytest.raises(ExecutionFailure) as excinfo:
        await budgeted.request(
            [ModelRequest(parts=[UserPromptPart(content="字" * 50)])],
            None,
            ModelRequestParameters(),
        )
    assert excinfo.value.error_code == CONTEXT_BUDGET_EXCEEDED
    assert sent == []  # 未实际发送（无 provider 溢出应急重试）


async def test_budgeted_model_records_real_usage_anchor_and_summary_shares_budget() -> (
    None
):
    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart(content="完成")],
            usage=RequestUsage(input_tokens=1_234, output_tokens=5),
        )

    harness = _reduced_harness(max_model_requests=2)
    budgeted = BudgetedModel(FunctionModel(respond), _budget(harness))
    message = ModelRequest(parts=[UserPromptPart(content="旧")])
    response = await budgeted.request([message], None, ModelRequestParameters())
    assert response.usage.input_tokens == 1_234
    assert budgeted.estimator.anchored
    assert budgeted.estimator.estimate(request_chars(message)) == max(
        character_bound_tokens(request_chars(message)), 1_234
    )
    # 摘要请求与普通请求共用同一 Run 请求预算
    await budgeted.request_summary([message], ModelSettings(max_tokens=100))
    assert budgeted.budget.requests_used == 2
    with pytest.raises(ExecutionFailure) as excinfo:
        await budgeted.request([message], None, ModelRequestParameters())
    assert excinfo.value.error_code == MODEL_REQUEST_FAILED


def request_chars(message: ModelMessage) -> int:
    return len(_message_json([message]))


# ---------- 2–6. 压缩编排（缩小阈值 + 真实存储链路） ----------


async def test_below_trigger_keeps_full_history_uncompressed(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text=OLD_TEXT)
        await _current_run(db)
        harness = _reduced_harness(compression_trigger_tokens=10_000)
        stub = _SummaryStub()
        compressor = _compressor(db, harness=harness, model=stub)
        projection = await compressor.project(
            facts_request=_facts_request(),
            interactions=await _interactions(db),
            summary=None,
            user_text=CURRENT_TEXT,
        )
        assert projection.compressed is False
        assert compressor.attempted is False  # 未到触发点：连尝试都没有
        assert stub.calls == []
        assert OLD_TEXT in _message_json(projection.message_history)
        assert await SummaryRepo(db).list_summaries(CONVERSATION_ID) == []


async def test_compression_summarizes_only_oldest_complete_interactions(
    tmp_path: Path,
) -> None:
    """触发点压缩：只摘要最老的完整交互；工具对不拆、部分回答不进、当前 Run 不压缩。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="failed-1", text=FAILED_TEXT, status="cancelled")
        await _seed_tool_run(db, run_id="old-tool", text=OLD_TEXT)
        await _seed_run(db, run_id="old-2", text=OLD_2_TEXT, answer=OLD_2_TEXT)
        await _seed_run(db, run_id="old-3", text=OLD_3_TEXT, answer=OLD_3_TEXT)
        await _current_run(db)
        harness = _reduced_harness(effective_input_tokens=3_200)
        stub = _SummaryStub()
        compressor = _compressor(db, harness=harness, model=stub)
        projection = await compressor.project(
            facts_request=_facts_request(),
            interactions=await _interactions(db),
            summary=None,
            user_text=CURRENT_TEXT,
        )
    assert projection.compressed is True
    assert len(stub.calls) == 1
    assert stub.settings[0] is not None
    assert (
        stub.settings[0].get("max_tokens") == harness.summary_output_tokens
    )  # 摘要输出上限
    summary_input = stub.calls[0]
    # 摘要输入：失败／取消请求与中断标注保留；工具调用与结果成对出现；部分回答与当前 Run 不在内
    assert FAILED_TEXT in _message_json(summary_input)
    assert INTERRUPTION_MARKER in _message_json(summary_input)
    parts = [
        part for message in summary_input for part in getattr(message, "parts", ())
    ]
    calls = [part for part in parts if isinstance(part, ToolCallPart)]
    returns = [part for part in parts if isinstance(part, ToolReturnPart)]
    assert [part.tool_call_id for part in calls] == [
        part.tool_call_id for part in returns
    ]
    assert PARTIAL_TEXT not in _message_json(summary_input)
    assert CURRENT_TEXT not in _message_json(summary_input)
    assert FACTS_MARKER not in _message_json(summary_input)  # 当前事实不进摘要输入
    # 只摘要最老交互：保留尾段（old-2/old-3）仍原样投影
    assert OLD_TEXT not in _message_json(projection.message_history)
    assert OLD_2_TEXT in _message_json(projection.message_history)
    assert OLD_3_TEXT in _message_json(projection.message_history)


async def test_summary_extras_commit_coverage_sources_and_retained_tail(
    tmp_path: Path,
) -> None:
    """提交成功才启用：覆盖范围、来源关联、保留尾段与摘要投影形状。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="failed-1", text=FAILED_TEXT, status="cancelled")
        await _seed_tool_run(db, run_id="old-tool", text=OLD_TEXT)
        await _seed_run(db, run_id="old-2", text=OLD_2_TEXT, answer=OLD_2_TEXT)
        await _seed_run(db, run_id="old-3", text=OLD_3_TEXT, answer=OLD_3_TEXT)
        await _current_run(db)
        harness = _reduced_harness(effective_input_tokens=3_200)
        stub = _SummaryStub()
        compressor = _compressor(db, harness=harness, model=stub)
        facts_request = _facts_request()
        before_rows = await RunRepo(db).list_messages(CONVERSATION_ID)
        projection = await compressor.project(
            facts_request=facts_request,
            interactions=await _interactions(db),
            summary=None,
            user_text=CURRENT_TEXT,
        )
        rows = await RunRepo(db).list_messages(CONVERSATION_ID)
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        sources = await SummaryRepo(db).list_source_message_ids(summaries[0]["id"])
        active = await load_active_summary(SummaryRepo(db), CONVERSATION_ID)
    assert len(summaries) == 1
    assert summaries[0]["content"] == SUMMARY_TEXT
    assert summaries[0]["covered_from_seq"] == rows[0]["seq"]  # 覆盖从会话最早消息起
    tool_rows = [row for row in rows if row["run_id"] == "old-tool"]
    assert summaries[0]["covered_to_seq"] == max(row["seq"] for row in tool_rows)
    # 来源并集 = 覆盖区间内真正用到的行（部分回答除外），原消息一行未删
    assert sorted(sources) == sorted(
        int(row["id"])
        for row in rows
        if row["run_id"] in ("failed-1", "old-tool")
        and not (row["run_id"] == "old-tool" and row["kind"] == "partial")
    )
    assert (
        active is not None and active.covered_to_seq == summaries[0]["covered_to_seq"]
    )
    assert [dict(row) for row in rows] == [
        dict(row) for row in before_rows
    ]  # 原消息不删
    history = projection.message_history
    assert history[0] == facts_request
    assert SUMMARY_TEXT in _message_json([history[1]])  # 摘要投影：辅助历史
    assert OLD_2_TEXT in _message_json(history[2:])  # 保留尾段
    assert OLD_3_TEXT in _message_json(history[2:])
    assert OLD_TEXT not in _message_json(history)  # 被摘要的老交互不再原样投影
    assert CURRENT_TEXT not in _message_json(history)  # 当前 Run 不压缩、也不在历史里
    # 保留尾段从完整交互边界开始：第一条是 old-2 的用户请求（不是悬空工具结果）
    first_retained = history[2]
    assert isinstance(first_retained, ModelRequest)
    assert any(
        isinstance(part, UserPromptPart) and OLD_2_TEXT in str(part.content)
        for part in first_retained.parts
    )


async def test_consecutive_summaries_extend_coverage_and_union_sources(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text=OLD_TEXT, answer=OLD_TEXT)
        await _seed_run(db, run_id="old-2", text=OLD_2_TEXT, answer=OLD_2_TEXT)
        await _current_run(db, run_id="run-1")
        harness = _reduced_harness()
        first = _SummaryStub(text="第一版摘要")
        compressor = _compressor(db, harness=harness, model=first, run_id="run-1")
        first_projection = await compressor.project(
            facts_request=_facts_request(),
            interactions=await _interactions(db, current_run_id="run-1"),
            summary=None,
            user_text=CURRENT_TEXT,
        )
        assert first_projection.compressed is True
        first_summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        first_sources = set(
            await SummaryRepo(db).list_source_message_ids(first_summaries[0]["id"])
        )
        # 第一次压缩后 run-1 正常完成，随后新增一条已完成交互与新的当前 Run
        await RunRepo(db).complete_run(
            "run-1",
            [
                (
                    "user",
                    dump_framework_message(
                        ModelRequest(parts=[UserPromptPart(content=CURRENT_TEXT)])
                    ),
                ),
                (
                    "assistant",
                    dump_framework_message(
                        ModelResponse(parts=[TextPart(content="好的")])
                    ),
                ),
            ],
        )
        await _seed_run(db, run_id="old-3", text=OLD_3_TEXT, answer=OLD_3_TEXT)
        await _current_run(db, run_id="run-2", text="第二轮请求")
        active = await load_active_summary(SummaryRepo(db), CONVERSATION_ID)
        assert active is not None
        interactions = await load_conversation_interactions(
            RunRepo(db),
            conversation_id=CONVERSATION_ID,
            current_run_id="run-2",
            after_seq=active.covered_to_seq,
        )
        second = _SummaryStub(text="第二版摘要")
        second_compressor = _compressor(
            db, harness=harness, model=second, run_id="run-2"
        )
        before_second = await RunRepo(db).list_messages(CONVERSATION_ID)
        second_projection = await second_compressor.project(
            facts_request=_facts_request(),
            interactions=interactions,
            summary=active,
            user_text="第二轮请求",
        )
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        second_sources = set(
            await SummaryRepo(db).list_source_message_ids(summaries[-1]["id"])
        )
        remaining_rows = await RunRepo(db).list_messages(CONVERSATION_ID)
    assert second_projection.compressed is True
    assert len(summaries) == 2
    assert summaries[-1]["covered_from_seq"] == summaries[0]["covered_from_seq"]
    assert summaries[-1]["covered_to_seq"] > summaries[0]["covered_to_seq"]
    assert first_sources < second_sources  # 来源并集：仍可追溯到最初原消息
    assert second_sources <= {int(row["id"]) for row in remaining_rows}
    # 已摘要原消息不再重复投影：第二次投影只有新摘要 + 尾段交互
    assert OLD_TEXT not in _message_json(second_projection.message_history)
    assert OLD_2_TEXT not in _message_json(second_projection.message_history)
    assert OLD_3_TEXT in _message_json(second_projection.message_history)
    assert [dict(row) for row in remaining_rows] == [
        dict(row) for row in before_second
    ]  # 第二次摘要同样只追加摘要行，原消息逐条不变


async def test_bounded_shrinking_summarizes_only_oldest_fitting_interaction(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text="甲" * 1_500, answer="甲" * 1_500)
        await _seed_run(db, run_id="old-2", text="乙" * 1_500, answer="乙" * 1_500)
        await _seed_run(db, run_id="old-3", text="丙" * 1_500, answer="丙" * 1_500)
        await _current_run(db)
        harness = _reduced_harness()
        stub = _SummaryStub()
        compressor = _compressor(db, harness=harness, model=stub)
        projection = await compressor.project(
            facts_request=_facts_request(),
            interactions=await _interactions(db),
            summary=None,
            user_text=CURRENT_TEXT,
        )
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        rows = await RunRepo(db).list_messages(CONVERSATION_ID)
    assert projection.compressed is True
    assert len(stub.calls) == 1
    # 两条最老交互的摘要请求放不下 → 收缩到最老一条；其余原样保留
    assert "甲" * 1_500 in _message_json(stub.calls[0])
    assert "乙" * 1_500 not in _message_json(stub.calls[0])
    assert summaries[0]["covered_to_seq"] == max(
        row["seq"] for row in rows if row["run_id"] == "old-1"
    )
    assert "乙" * 1_500 in _message_json(projection.message_history)
    assert "丙" * 1_500 in _message_json(projection.message_history)


async def test_no_safe_cut_abandons_once_without_calling_summary_model(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text=OLD_TEXT, answer=OLD_TEXT)
        await _seed_run(db, run_id="old-2", text=OLD_2_TEXT, answer=OLD_2_TEXT)
        await _seed_run(db, run_id="old-3", text=OLD_3_TEXT, answer=OLD_3_TEXT)
        await _current_run(db)
        # 保留目标大到容纳全部交互：没有可摘要的更老交互（没有安全切点）
        harness = _reduced_harness(retained_history_tokens=100_000)
        stub = _SummaryStub()
        compressor = _compressor(db, harness=harness, model=stub)
        interactions = await _interactions(db)
        facts_request = _facts_request()
        first = await compressor.project(
            facts_request=facts_request,
            interactions=interactions,
            summary=None,
            user_text=CURRENT_TEXT,
        )
        second = await compressor.project(
            facts_request=facts_request,
            interactions=interactions,
            summary=None,
            user_text=CURRENT_TEXT,
        )
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
    assert first.compressed is False and second.compressed is False
    assert stub.calls == []  # 无安全切点：不发起摘要请求
    assert compressor.attempted is True
    assert summaries == []
    assert OLD_TEXT in _message_json(first.message_history)  # 旧上下文原样保留
    assert OLD_2_TEXT in _message_json(first.message_history)
    assert OLD_3_TEXT in _message_json(first.message_history)


async def test_shrink_to_empty_abandons_and_keeps_old_context(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text="甲" * 3_000, answer="甲" * 3_000)
        await _seed_run(db, run_id="old-2", text="乙" * 3_000, answer="乙" * 3_000)
        await _current_run(db)
        harness = _reduced_harness()
        stub = _SummaryStub()
        compressor = _compressor(db, harness=harness, model=stub)
        interactions = await _interactions(db)
        projection = await compressor.project(
            facts_request=_facts_request(),
            interactions=interactions,
            summary=None,
            user_text=CURRENT_TEXT,
        )
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
    assert projection.compressed is False
    assert stub.calls == []  # 缩到最老一条仍放不下：放弃，不发起请求
    assert summaries == []
    assert "甲" * 3_000 in _message_json(projection.message_history)
    assert "乙" * 3_000 in _message_json(projection.message_history)


async def test_recoverable_generation_failure_keeps_old_context(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text=OLD_TEXT, answer=OLD_TEXT)
        await _seed_run(db, run_id="old-2", text=OLD_2_TEXT, answer=OLD_2_TEXT)
        await _current_run(db)
        harness = _reduced_harness()
        stub = _SummaryStub(error=ExecutionFailure(MODEL_REQUEST_FAILED))
        compressor = _compressor(db, harness=harness, model=stub)
        interactions = await _interactions(db)
        first = await compressor.project(
            facts_request=_facts_request(),
            interactions=interactions,
            summary=None,
            user_text=CURRENT_TEXT,
        )
        # 同一上下文状态不再重复尝试（等上下文变化再试）
        second = await compressor.project(
            facts_request=_facts_request(),
            interactions=interactions,
            summary=None,
            user_text=CURRENT_TEXT,
        )
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
    assert first.compressed is False and second.compressed is False
    assert len(stub.calls) == 1  # 可恢复生成失败：保留旧上下文继续，不重复消耗
    assert summaries == []
    assert OLD_TEXT in _message_json(first.message_history)
    assert OLD_2_TEXT in _message_json(first.message_history)


async def test_commit_failure_propagates_and_does_not_degrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text=OLD_TEXT, answer=OLD_TEXT)
        await _seed_run(db, run_id="old-2", text=OLD_2_TEXT, answer=OLD_2_TEXT)
        await _current_run(db)
        harness = _reduced_harness()
        stub = _SummaryStub()

        async def fail_commit(self: SummaryRepo, **kwargs: Any) -> dict[str, Any]:
            raise InvalidInput("存储失败注入")

        monkeypatch.setattr(SummaryRepo, "commit_summary", fail_commit)
        compressor = _compressor(db, harness=harness, model=stub)
        with pytest.raises(InvalidInput):
            await compressor.project(
                facts_request=_facts_request(),
                interactions=await _interactions(db),
                summary=None,
                user_text=CURRENT_TEXT,
            )
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
    assert len(stub.calls) == 1  # 摘要已生成，但存储失败必须上抛、不降级继续
    assert summaries == []


# ---------- 7–8. 整链路：投影生效、预算共享、取消与超限终态 ----------


async def _drive_run(
    db: Any,
    *,
    run_id: str,
    harness: EffectiveHarness,
    model: FunctionModel,
    user_text: str = CURRENT_TEXT,
) -> tuple[ExecutionDriver, "asyncio.Task[None]"]:
    repo = RunRepo(db)
    await RunService(repo).submit_request(
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        client_request_id=f"req-{run_id}",
        text=user_text,
    )
    driver = ExecutionDriver(repo)
    work = build_run_work(
        db=db,
        repo=repo,
        model=model,
        harness=harness,
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        business_date=BUSINESS_DATE,
    )
    return driver, driver.start(run_id, work)


async def _run_record(db: Any, run_id: str) -> dict[str, Any]:
    run = await RunRepo(db).get_run(run_id)
    assert run is not None
    return dict(run)


async def test_run_completes_with_compressed_projection(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _seed_run(db, run_id="old-1", text="甲" * 4_000, answer="甲" * 4_000)
        await _seed_run(db, run_id="old-2", text="乙" * 4_000, answer="乙" * 4_000)
        model = _RecordingModel()
        harness = _integration_harness()
        _driver, task = await _drive_run(
            db, run_id="run-compress", harness=harness, model=model.model()
        )
        await task
        run = await _run_record(db, "run-compress")
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        first_summary_row = summaries[0] if summaries else None
    assert run["status"] == "completed"
    assert first_summary_row is not None
    ordinary = [call for call in model.calls if FACTS_MARKER in call]
    assert len(ordinary) == 1
    assert SUMMARY_TEXT in ordinary[0]  # 新摘要进入普通请求投影
    assert "乙" * 4_000 in ordinary[0]  # 保留最近完整交互
    assert "甲" * 4_000 not in ordinary[0]  # 最老交互已被摘要替换
    assert ordinary[0].count(FACTS_MARKER) == 1  # 当前事实仍只注入一次


async def test_next_run_projects_summary_plus_tail_without_duplicating_history(
    tmp_path: Path,
) -> None:
    """第二次 Run 的投影 = 有效摘要 + 覆盖终点之后的尾段；已摘要原消息不重复投影。"""
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _seed_run(db, run_id="old-1", text="甲" * 4_000, answer="甲" * 4_000)
        await _seed_run(db, run_id="old-2", text="乙" * 4_000, answer="乙" * 4_000)
        first = _RecordingModel()
        _driver, task = await _drive_run(
            db, run_id="run-1", harness=_integration_harness(), model=first.model()
        )
        await task
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        assert len(summaries) == 1  # 第一次 Run 已提交摘要

        # 第二次 Run 提高触发点：本 Run 不压缩，只验证摘要 + 尾段投影
        second = _RecordingModel()
        _driver2, task2 = await _drive_run(
            db,
            run_id="run-2",
            harness=_integration_harness(compression_trigger_tokens=20_000),
            model=second.model(),
        )
        await task2
        run2 = await _run_record(db, "run-2")
        summaries_after = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
    assert run2["status"] == "completed"
    assert len(summaries_after) == 1  # 未触发：不新增摘要
    ordinary = [call for call in second.calls if FACTS_MARKER in call]
    assert len(ordinary) == 1
    assert SUMMARY_TEXT in ordinary[0]  # 有效摘要进入投影
    assert "乙" * 4_000 in ordinary[0]  # 覆盖终点之后的尾段仍原样投影
    assert "甲" * 4_000 not in ordinary[0]  # 已摘要原消息不再重复投影


async def test_summary_request_consumes_the_shared_run_request_budget(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _seed_run(db, run_id="old-1", text="甲" * 4_000, answer="甲" * 4_000)
        await _seed_run(db, run_id="old-2", text="乙" * 4_000, answer="乙" * 4_000)
        model = _RecordingModel()
        harness = _integration_harness(max_model_requests=1)
        _driver, task = await _drive_run(
            db, run_id="run-budget", harness=harness, model=model.model()
        )
        await task
        run = await _run_record(db, "run-budget")
    assert run["status"] == "failed"
    assert run["error_code"] == MODEL_REQUEST_FAILED  # 请求池被摘要请求用尽
    assert len(model.calls) == 1  # 只有摘要请求实际发送
    assert SUMMARY_TEXT not in model.calls[0]  # 第一次是摘要请求本身


async def test_cancel_during_summary_generation_commits_nothing(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        await _seed_run(db, run_id="old-1", text="甲" * 4_000, answer="甲" * 4_000)
        await _seed_run(db, run_id="old-2", text="乙" * 4_000, answer="乙" * 4_000)
        started = asyncio.Event()
        model = _RecordingModel(delay=5.0, started=started)
        harness = _integration_harness()
        driver, task = await _drive_run(
            db, run_id="run-cancel", harness=harness, model=model.model()
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        await driver.cancel("run-cancel")
        await task
        run = await _run_record(db, "run-cancel")
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
        messages = await RunRepo(db).list_run_messages("run-cancel")
    assert run["status"] == "cancelled"
    assert summaries == []  # 取消先发生：摘要不启用、不补写
    assert len(model.calls) == 1  # 取消后不启动普通请求
    assert [row["kind"] for row in messages] == ["user_request"]


async def test_context_budget_exceeded_ends_run_without_sending(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        await _formal_profile(db)
        model = _RecordingModel()
        harness = _integration_harness(
            effective_input_tokens=500, compression_trigger_tokens=400
        )
        _driver, task = await _drive_run(
            db, run_id="run-over", harness=harness, model=model.model()
        )
        await task
        run = await _run_record(db, "run-over")
        messages = await RunRepo(db).list_run_messages("run-over")
        summaries = await SummaryRepo(db).list_summaries(CONVERSATION_ID)
    assert run["status"] == "failed"
    assert run["error_code"] == CONTEXT_BUDGET_EXCEEDED
    assert model.calls == []  # 容量不成立：不发送（无 provider 溢出应急重试）
    assert summaries == []
    assert [row["kind"] for row in messages] == ["user_request"]  # 不写迟到回答
