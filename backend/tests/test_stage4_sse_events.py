"""S4-07：SSE 产品事件、脱敏、heartbeat 与部分回答（stage4.md S4-07 验收；08 8.7/8.8）。

覆盖（逐项对应 S4-07 验收）：

1. 正常流：状态与回答块按发生顺序给出，终态事件之后流结束。
2. 事件白名单与脱敏：隐藏推理与工具轨迹不进事件；帧里没有 ``id:``（不做恢复游标）。
3. heartbeat：无业务事件时只在传输层产生，不入库、不分配 ID（用缩小间隔驱动同一实现）。
4. 流失败：按已拍边界 fail-closed 结束，**首个业务事件之前也不重放**（2026-09-12 拍板）。
5. 部分回答：分批落盘（不逐 Token 写库）、失败后是可恢复的未完成文本、查询恢复不重复拼接。
6. 断开不取消、不重跑：消费方关闭事件流后 Run 仍推进到终态。
7. 草稿就绪通知晚于落盘；压缩状态事件与真实压缩一致。

全程离线：``FunctionModel`` 脚本桩（流式 + 非流式两路），无真实 Provider 请求；每例只操作
``tmp_path`` 下的临时文件库，业务事实与投影走既有真实应用层链路。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingPart,
    DeltaToolCall,
    FunctionModel,
)

from app.draft_repo import DraftRepo
from config import HarnessConfig, effective_harness_config
from domain.profile.schema import profile_to_json
from runtime.agent_factory import PARTIAL_BATCH_CHARACTERS, build_streaming_run_work
from runtime.error_codes import MODEL_REQUEST_FAILED
from runtime.events import (
    EVENT_KINDS,
    HEARTBEAT_SECONDS,
    RunEventStream,
    run_product_events,
    sse_frame,
)
from runtime.run_service import RunService
from runtime.run_task import ExecutionDriver
from storage.run_repo import RunRepo
from storage.summary_repo import SummaryRepo
from tests.support import open_database
from tests.test_stage3_plan_drafts import _profile
from tests.test_stage4_compression import _integration_harness, _seed_run

CONVERSATION_ID = "c1"
BUSINESS_DATE = date(2026, 9, 20)
HARNESS = effective_harness_config(HarnessConfig())

#: 隐藏推理标记：绝不能出现在任何产品事件里。
THINKING_TEXT = "隐藏推理标记：先想一下再回答"


class _StreamingStub:
    """脚本桩流式模型：一个模型请求 = 一段脚本（文本块／思考块／工具调用），可注入失败。

    非流式 ``function`` 只服务摘要请求（压缩走 ``Model.request``）；可见回答走 ``stream_function``，
    两路共用同一个步数计数。
    """

    SUMMARY_TEXT = "（摘要）较早会话已整理"

    def __init__(
        self,
        steps: Sequence[Sequence[Any]] | None = None,
        *,
        on_step: Callable[[int], Awaitable[None]] | None = None,
        on_item: Callable[[int, int], Awaitable[None]] | None = None,
        fail_after: Sequence[int] = (),
    ) -> None:
        self.steps = list(steps or [])
        self.on_step = on_step
        self.on_item = on_item
        self.fail_after = tuple(fail_after)
        self.seen: list[list[ModelMessage]] = []

    def model(self) -> FunctionModel:
        async def respond(messages, info) -> ModelResponse:
            self.seen.append(list(messages))
            return ModelResponse(parts=[TextPart(content=self.SUMMARY_TEXT)])

        async def stream(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[Any]:
            step = len(self.seen)
            self.seen.append(list(messages))
            if self.on_step is not None:
                await self.on_step(step)
            items = self.steps[step] if step < len(self.steps) else ["完成"]
            for index, item in enumerate(items):
                yield item
                if self.on_item is not None:
                    await self.on_item(step, index)
            if step in self.fail_after:
                raise httpx2.ConnectError("refused")

        return FunctionModel(respond, stream_function=stream)

    @property
    def attempts(self) -> int:
        return len(self.seen)


def _thinking(text: str = THINKING_TEXT) -> dict[int, DeltaThinkingPart]:
    return {0: DeltaThinkingPart(content=text)}


def _tool_call(
    name: str, args: dict[str, Any], call_id: str = "call-1"
) -> dict[int, DeltaToolCall]:
    return {
        0: DeltaToolCall(
            name=name,
            json_args=json.dumps(args, ensure_ascii=False),
            tool_call_id=call_id,
        )
    }


async def _wait_until(condition: Callable[[], bool], *, what: str) -> None:
    """在有限期限内等一个纯内存条件成立（无长等待、无真实网络）。"""
    deadline = time.monotonic() + 5.0
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"条件未在期限内满足：{what}")
        await asyncio.sleep(0.01)


async def _collect(
    repo: RunRepo,
    events: RunEventStream,
    run_id: str,
    *,
    seen: list[tuple[str, dict[str, Any]]] | None = None,
    probe: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    heartbeat_seconds: float = 1.0,
) -> list[tuple[str, dict[str, Any]]]:
    """消费一个 Run 的产品事件流，可选逐条探测（边收边查库）。"""
    collected: list[tuple[str, dict[str, Any]]] = []
    async for name, payload in run_product_events(
        repo=repo, events=events, run_id=run_id, heartbeat_seconds=heartbeat_seconds
    ):
        collected.append((name, payload))
        if seen is not None:
            seen.append((name, payload))
        if probe is not None:
            await probe(name, payload)
    return collected


def _drive(
    db: Any,
    stub: _StreamingStub,
    *,
    events: RunEventStream,
    run_id: str,
    user_text: str = "帮我看看训练安排",
    harness: Any = HARNESS,
) -> tuple[ExecutionDriver, asyncio.Task[None]]:
    """提交并启动一次生产流式 Run（真实驱动 + 真实存储，模型是离线桩）。"""
    repo = RunRepo(db)
    driver = ExecutionDriver(repo, on_status=events.publish_status)
    work = build_streaming_run_work(
        db=db,
        repo=repo,
        model=stub.model(),
        harness=harness,
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        business_date=BUSINESS_DATE,
        events=events,
    )
    return driver, driver.start(run_id, work)


async def _submit(repo: RunRepo, run_id: str, text: str = "帮我看看训练安排") -> None:
    if await repo.get_conversation(CONVERSATION_ID) is None:
        await repo.create_conversation(CONVERSATION_ID)
    await RunService(repo).submit_request(
        conversation_id=CONVERSATION_ID,
        run_id=run_id,
        client_request_id=f"req-{run_id}",
        text=text,
    )


async def _run(db: Any, run_id: str) -> dict[str, Any]:
    record = await RunRepo(db).get_run(run_id)
    assert record is not None, f"Run 不存在：{run_id}"
    return dict(record)


def _names(events: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _payload in events]


def _answers(events: Sequence[tuple[str, dict[str, Any]]]) -> str:
    return "".join(str(payload["text"]) for name, payload in events if name == "answer")


# ---------- 1. 正常流 ----------


async def test_stream_emits_status_and_answer_chunks_then_ends_on_terminal(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        released = asyncio.Event()
        first_chunk_sent = asyncio.Event()

        async def on_item(step: int, index: int) -> None:
            if index == 0:
                first_chunk_sent.set()
                await released.wait()

        stub = _StreamingStub([["第一段", "第二段"]], on_item=on_item)
        driver, task = _drive(db, stub, events=events, run_id="r1")
        seen: list[tuple[str, dict[str, Any]]] = []
        consumer = asyncio.create_task(_collect(repo, events, "r1", seen=seen))

        # 阻塞在第二段之前：此时第一段已经作为回答块发出，Run 仍是 running
        await asyncio.wait_for(first_chunk_sent.wait(), timeout=5.0)
        await _wait_until(lambda: _answers(seen) == "第一段", what="第一段回答块")
        assert (
            "status",
            {"run_id": "r1", "status": "running", "error_code": None},
        ) in seen
        assert (await _run(db, "r1"))["status"] == "running"

        released.set()
        await asyncio.wait_for(task, timeout=5.0)
        collected = await asyncio.wait_for(consumer, timeout=5.0)

        # 状态与回答块按发生顺序；终态事件是最后一个事件
        assert _names(collected)[0] == "status"
        assert _names(collected)[-1] == "status"
        assert collected[-1][1] == {
            "run_id": "r1",
            "status": "completed",
            "error_code": None,
        }
        assert _answers(collected) == "第一段第二段"
        assert all(
            set(payload) == {"run_id", "text"}
            for name, payload in collected
            if name == "answer"
        )
        assert (await _run(db, "r1"))["status"] == "completed"
        # 完整回答与 completed 同事务落库（框架消息）；运行中先落的分批回答行仍可追溯
        rows = await repo.list_run_messages("r1")
        assert [row["kind"] for row in rows] == [
            "user_request",
            "partial",
            "framework",
            "framework",
        ]
        assert (await repo.list_run_events("r1")) == []  # 产品事件不入库


# ---------- 2. 白名单与脱敏 ----------


async def test_event_allowlist_is_closed_and_hidden_reasoning_never_leaks(
    tmp_path: Path,
) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        # 隐藏推理与可见回答在同一段响应里：思考块不属产品事件，但该响应被真实处理
        stub = _StreamingStub([[_thinking(), "可见回答"]])
        _driver, task = _drive(db, stub, events=events, run_id="r1")
        collected = await asyncio.wait_for(
            _collect(repo, events, "r1", seen=None), timeout=10.0
        )
        await asyncio.wait_for(task, timeout=5.0)

        assert set(_names(collected)) <= set(EVENT_KINDS)
        assert set(EVENT_KINDS) == {
            "status",
            "answer",
            "rationale",
            "draft",
            "compression",
            "heartbeat",
        }
        # 隐藏推理不进任何产品事件；回答块只有可见文本
        assert all(
            THINKING_TEXT not in json.dumps(payload, ensure_ascii=False)
            for _name, payload in collected
        )
        assert _answers(collected) == "可见回答"
        assert (await _run(db, "r1"))["status"] == "completed"  # 思考块被真正处理过
        # Answer 块不含工具调用与草稿细节（工具轨迹只保留在后台）
        frame_dump = "".join(sse_frame(name, payload) for name, payload in collected)
        for leaked in (
            "propose_profile_draft",
            "tool_call",
            "draft_id",
            "usage",
            "cache",
            "Token",
        ):
            assert leaked not in frame_dump, leaked
        # 帧里没有 id: 行（run_events.id 不是恢复游标，也不补读）
        assert "id:" not in frame_dump


async def test_draft_notification_follows_persistence(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        draft_args = {"proposed": json.loads(profile_to_json(_profile()))}
        persisted_at_notification: list[list[str]] = []

        async def probe(name: str, payload: dict[str, Any]) -> None:
            if name != "draft":
                return
            drafts = await DraftRepo(db).list_for_conversation(CONVERSATION_ID)
            persisted_at_notification.append([draft.id for draft in drafts])

        stub = _StreamingStub(
            [
                [_tool_call("propose_profile_draft", draft_args)],
                ["草稿已保存，请确认"],
            ]
        )
        _driver, task = _drive(db, stub, events=events, run_id="r1")
        collected = await asyncio.wait_for(
            _collect(repo, events, "r1", probe=probe), timeout=10.0
        )
        await asyncio.wait_for(task, timeout=5.0)

        draft_events = [payload for name, payload in collected if name == "draft"]
        assert len(draft_events) == 1
        payload = draft_events[0]
        assert payload["kind"] == "profile_update"
        assert payload["status"] == "pending"
        assert payload["revision"] == 1
        # 通知到达时草稿已经在库里（通知晚于落盘，且不是当前状态的事实源）
        assert persisted_at_notification == [[payload["draft_id"]]]


# ---------- 3. heartbeat ----------


async def test_heartbeat_is_transport_only_and_not_persisted(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        release = asyncio.Event()
        blocked = asyncio.Event()

        async def on_step(step: int) -> None:
            blocked.set()
            await release.wait()

        stub = _StreamingStub([["迟到回答"]], on_step=on_step)
        _driver, task = _drive(db, stub, events=events, run_id="r1")
        seen: list[tuple[str, dict[str, Any]]] = []
        consumer = asyncio.create_task(
            _collect(repo, events, "r1", seen=seen, heartbeat_seconds=0.01)
        )
        await _wait_until(lambda: blocked.is_set(), what="模型请求已开始")
        await _wait_until(
            lambda: _names(seen).count("heartbeat") >= 2,
            what="无业务事件时的 heartbeat",
        )

        # 已拍间隔是 15 秒（测试用缩小间隔驱动同一实现，不另建心跳逻辑）
        assert HEARTBEAT_SECONDS == 15.0
        heartbeat = [payload for name, payload in seen if name == "heartbeat"][0]
        assert heartbeat == {}
        assert sse_frame("heartbeat", {}) == "event: heartbeat\ndata: {}\n\n"
        assert "id:" not in sse_frame("heartbeat", {})
        # heartbeat 不入库、不写轨迹事件
        assert await repo.list_run_events("r1") == []
        assert (await repo.list_run_messages("r1")) == [
            {
                "id": 1,
                "seq": 1,
                "role": "user",
                "kind": "user_request",
                "run_id": "r1",
                "payload_json": '{"text": "帮我看看训练安排"}',
            }
        ] or [row["kind"] for row in await repo.list_run_messages("r1")] == [
            "user_request"
        ]

        release.set()
        collected = await asyncio.wait_for(consumer, timeout=5.0)
        await asyncio.wait_for(task, timeout=5.0)
        assert collected[-1][1]["status"] == "completed"
        assert await repo.list_run_events("r1") == []


# ---------- 4. 流失败：fail-closed，首个业务事件前也不重放 ----------


async def test_stream_failure_before_any_visible_text_does_not_replay(
    tmp_path: Path,
) -> None:
    """已拍边界（2026-09-12）：流一旦交给消费方，即使尚无可见文本块也不重放。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        # 第一段只产出隐藏推理（不是产品事件）后失败：本次尝试没有向产品产出任何内容
        stub = _StreamingStub([[_thinking()]], fail_after=(0,))
        _driver, task = _drive(db, stub, events=events, run_id="r1")
        collected = await asyncio.wait_for(_collect(repo, events, "r1"), timeout=10.0)
        await asyncio.wait_for(task, timeout=5.0)

        record = await _run(db, "r1")
        assert record["status"] == "failed"
        assert record["error_code"] == MODEL_REQUEST_FAILED
        assert stub.attempts == 1  # 不重放
        assert [name for name in _names(collected) if name == "answer"] == []
        assert [row["kind"] for row in await repo.list_run_messages("r1")] == [
            "user_request"
        ]  # 没有可见文本就没有部分回答
        assert [event["event_type"] for event in await repo.list_run_events("r1")] == [
            "failed"
        ]


async def test_stream_failure_keeps_partial_text_as_incomplete(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        stub = _StreamingStub([["已经说了一半的话"]], fail_after=(0,))
        _driver, task = _drive(db, stub, events=events, run_id="r1")
        collected = await asyncio.wait_for(_collect(repo, events, "r1"), timeout=10.0)
        await asyncio.wait_for(task, timeout=5.0)

        record = await _run(db, "r1")
        assert (record["status"], record["error_code"]) == (
            "failed",
            MODEL_REQUEST_FAILED,
        )
        # 已产出的可见文本按未完成保留：可恢复，但不是成功回答
        partials = await repo.list_partial_answers("r1")
        assert [item["text"] for item in partials] == ["已经说了一半的话"]
        assert [row["kind"] for row in await repo.list_run_messages("r1")] == [
            "user_request",
            "partial",
        ]
        assert _answers(collected) == "已经说了一半的话"


# ---------- 5. 部分回答分批与查询恢复 ----------


async def test_partial_answers_are_persisted_in_bounded_batches(tmp_path: Path) -> None:
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        chunk = "字" * 60
        chunks = [chunk] * 12  # 共 720 字符、12 个可见块
        stub = _StreamingStub([chunks])
        _driver, task = _drive(db, stub, events=events, run_id="r1")
        collected = await asyncio.wait_for(_collect(repo, events, "r1"), timeout=10.0)
        await asyncio.wait_for(task, timeout=5.0)

        assert _answers(collected) == chunk * 12
        partials = await repo.list_partial_answers("r1")
        assert 0 < len(partials) < len(chunks)  # 分批落盘，不逐块写库
        assert (
            "".join(item["text"] for item in partials) == chunk * 12
        )  # 内容不丢、不重复
        # 批次有界：每行不超过「批次上限 + 一个传输块」
        for item in partials:
            assert len(item["text"]) <= PARTIAL_BATCH_CHARACTERS + len(chunk)


async def test_disconnect_and_refresh_recover_without_duplicating_answer(
    tmp_path: Path,
) -> None:
    """断开只关掉阅读者：不取消、不重跑；查询恢复不重复拼接（08 8.7）。"""
    async with open_database(tmp_path / "app.db") as db:
        repo = RunRepo(db)
        await _submit(repo, "r1")
        events = RunEventStream()
        released = asyncio.Event()
        blocked = asyncio.Event()

        async def on_step(step: int) -> None:
            blocked.set()
            await released.wait()

        stub = _StreamingStub([["第一段", "第二段"]], on_step=on_step)
        driver, task = _drive(db, stub, events=events, run_id="r1")
        seen: list[tuple[str, dict[str, Any]]] = []
        consumer = asyncio.create_task(_collect(repo, events, "r1", seen=seen))
        await _wait_until(lambda: blocked.is_set(), what="模型请求已开始")
        assert driver.active_run_id == "r1"

        consumer.cancel()  # 等价于客户端断开／关闭 EventSource
        with pytest.raises(asyncio.CancelledError):
            await consumer
        assert (await _run(db, "r1"))["status"] == "running"  # 断开不取消
        assert stub.attempts == 1  # 也不重跑

        released.set()
        await asyncio.wait_for(task, timeout=5.0)
        record = await _run(db, "r1")
        assert record["status"] == "completed"
        assert driver.active_run_id is None

        # 查询恢复：用户请求 + 完整回答各一次；已完成 Run 的 partial 不重复拼接
        rows = await repo.list_run_messages("r1")
        transcript = await _transcript(db)
        assert [
            (item["role"], item["kind"], item["complete"]) for item in transcript
        ] == [
            ("user", "user_request", True),
            ("assistant", "answer", True),
        ]
        assert transcript[1]["text"] == "第一段第二段"
        assert [row["kind"] for row in rows] == [
            "user_request",
            "partial",
            "framework",
            "framework",
        ]
        assert any(row["kind"] == "partial" for row in rows)  # 中途确实落过盘


async def _transcript(db: Any) -> list[dict[str, Any]]:
    """会话查询的消息投影（与 ``GET /api/sessions/{id}`` 同一实现）。"""
    from api.dto import session_messages_dto

    return session_messages_dto(await RunRepo(db).list_messages(CONVERSATION_ID))


# ---------- 6. 压缩状态事件 ----------


async def test_compression_status_events_are_mapped(tmp_path: Path) -> None:
    """真实压缩（缩小阈值的整链路 Harness）产生 started／finished 两个状态事件。"""
    async with open_database(tmp_path / "app.db") as db:
        await RunRepo(db).create_conversation(CONVERSATION_ID)
        await _seed_run(db, run_id="old-1", text="甲" * 4_000, answer="甲" * 4_000)
        await _seed_run(db, run_id="old-2", text="乙" * 4_000, answer="乙" * 4_000)
        await _submit(RunRepo(db), "r1")
        events = RunEventStream()
        stub = _StreamingStub([["整理完之后继续回答"]])
        harness = _integration_harness()
        _driver, task = _drive(db, stub, events=events, run_id="r1", harness=harness)
        collected = await asyncio.wait_for(
            _collect(repo=RunRepo(db), events=events, run_id="r1"), timeout=10.0
        )
        await asyncio.wait_for(task, timeout=5.0)

        states = [
            payload["state"] for name, payload in collected if name == "compression"
        ]
        assert states == ["started", "finished"]
        # 压缩确实发生（提交了摘要），状态事件不是空转提示
        assert len(await SummaryRepo(db).list_summaries(CONVERSATION_ID)) == 1
        assert (await _run(db, "r1"))["status"] == "completed"
