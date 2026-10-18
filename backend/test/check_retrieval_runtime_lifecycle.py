import asyncio
import json
from concurrent.futures import CancelledError as FuturesCancelled
from pathlib import Path
from threading import Event, Thread

from app.agent.tool import run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import ToolCall
from app.application.business.retrieval_index import IndexCompatibilityError
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.retrieval_search import _encode_cancellable
from app.application.business.service import BusinessService
from test.check_profile_confirmation import Fixture
from test.regression_support import temporary_root

EVIDENCE = temporary_root("action-retrieval-t6") / "fix-runtime"
DECLARED = {item.name: item for item in business_tool_declarations()}


class ArmedSignal:
    # 由真实 RPC 阶段驱动的取消源：第 arm_after+1 次 is_set 起返回 True。
    # 只在主事件循环内被查询，用于确定性地在指定阶段触发取消，不替换任何 SDK 行为。
    def __init__(self, arm_after: int) -> None:
        self._arm_after = arm_after
        self._count = 0

    def is_set(self) -> bool:
        self._count += 1
        return self._count > self._arm_after

    def set(self) -> None:
        self._count = self._arm_after + 1


class BlockingEncoder:
    # 在途取消竞速的单元检查替身：真实 SDK 在途中止受 0 费用约束，未对线上 API 演练。
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def encode_query(self, text: str) -> list[float]:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [0.0]


async def inflight_cancel_case() -> dict:
    # 编码在途取消：以真实任务取消中止请求，且不返回结果。
    encoder = BlockingEncoder()
    signal = Event()
    task = asyncio.ensure_future(_encode_cancellable(encoder, "卧推", signal))
    await encoder.started.wait()
    assert not task.done()
    signal.set()
    error = None
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - 取消按既有语义传播
        error = exc
    assert encoder.cancelled is True, "在途编码任务应被真实取消"
    assert error is not None, "取消不得返回结果"
    return {"inflight_encode_cancelled": True}


async def cancel_cases(root: Path, evidence: dict) -> None:
    # 1) 已取消的搜索不初始化，也不建立任何连接。
    runtime = ActionRetrievalRuntime()
    signal = Event()
    signal.set()
    raised = False
    try:
        await runtime.search("卧推", signal=signal)
    except FuturesCancelled:
        raised = True
    assert raised and runtime._stack is None and runtime._retriever is None
    evidence["precancelled_no_init"] = True

    # 2) 初始化期间取消：等待当前 RPC 收尾后释放临时资源，不记录伪就绪。
    runtime = ActionRetrievalRuntime()
    raised = False
    try:
        await runtime.search("卧推", signal=ArmedSignal(2))
    except FuturesCancelled:
        raised = True
    assert raised and runtime._stack is None and runtime._retriever is None
    evidence["init_cancel_released"] = True

    # 3) 校验返回后取消：不进入编码，真实 Embedding usage 保持 0 请求。
    runtime = ActionRetrievalRuntime()
    retriever = await runtime._ensure()
    assert retriever.embedding.usage.requests == 0
    raised = False
    try:
        await runtime.search("卧推", signal=ArmedSignal(1))
    except FuturesCancelled:
        raised = True
    assert raised
    assert retriever.embedding.usage.requests == 0, "校验后取消不得进入编码"
    core_requests = retriever.embedding.usage.requests
    await runtime.close()
    evidence["post_validation_cancel_requests"] = core_requests

    # 4) 工具级取消桥接：signal 经工具与业务传入运行时；未初始化、无网络。
    #   框架按既有语义向外传播取消（不转换成成功或普通空结果）。
    fixture = await Fixture(root / "cancel.db").seeded()
    try:
        runtime = ActionRetrievalRuntime()
        business = BusinessService(
            fixture.repository, fixture.catalog, fixture.service.queries, fixture.replacements, runtime
        )
        session_id = await fixture.session("取消桥接会话")
        context = fixture.context(session_id, session_id, session_id)
        tools = bind_business_tools(business, context, fixture.call, {}, {})
        probe = ArmedSignal(1)
        message = None
        error = None
        try:
            message = await run_tool_call(
                ToolCall(
                    type="toolCall",
                    id="call-cancel",
                    name="search_exercises",
                    arguments={"purpose": "lookup", "name": "卧推"},
                ),
                tools={"search_exercises": tools["search_exercises"]},
                declared={"search_exercises": DECLARED["search_exercises"]},
                signal=probe,
            )
        except BaseException as exc:  # noqa: BLE001 - 框架按既有语义传播取消
            error = exc
        assert probe._count >= 2, "取消信号未到达业务层"
        assert error is not None or (message is not None and message.is_error is True), "取消不得产生成功结果"
        assert runtime._stack is None and runtime._retriever is None
        evidence["tool_cancel_outcome"] = "raised" if error is not None else "error_result"
    finally:
        await fixture.close()


async def lifecycle_cases() -> dict:
    runtime = ActionRetrievalRuntime()
    retriever = await runtime._ensure()
    assert runtime._retriever is not None
    db, embedding = retriever.db, retriever.embedding
    assert await runtime._ensure() is retriever, "连续调用未复用同一 retriever 实例"

    # 异循环关闭：明确失败，且原有客户端与关闭入口保持。
    outcome: dict[str, str] = {}

    def foreign() -> None:
        async def run() -> str:
            try:
                await runtime.close()
                return "closed"
            except IndexCompatibilityError:
                return "incompatible"

        outcome["value"] = asyncio.run(run())

    thread = Thread(target=foreign)
    thread.start()
    thread.join()
    assert outcome["value"] == "incompatible"
    assert runtime._stack is not None and runtime._retriever is retriever, "异循环关闭不得清空状态"
    aliases = await db.call("list_aliases")
    assert "fit_agent_actions_current" in aliases["aliases"], "异循环关闭后原资源仍应可用"

    # 所属循环关闭：两个客户端真实关闭，成功后才清空状态；重复关闭安全。
    await runtime.close()
    assert runtime._stack is None and runtime._retriever is None
    assert embedding._client is None, "Embedding 客户端应真实关闭"
    await runtime.close()
    closed = False
    try:
        await db.call("list_aliases")
    except IndexCompatibilityError:
        closed = True
    assert closed, "Milvus 客户端应真实关闭"

    # 并发关闭安全。
    concurrent = ActionRetrievalRuntime()
    await concurrent._ensure()
    await asyncio.gather(concurrent.close(), concurrent.close())
    assert concurrent._stack is None and concurrent._retriever is None
    return {"foreign_close": outcome["value"], "reused_retriever": True, "repeat_close_safe": True}


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}

    async def run() -> None:
        await cancel_cases(EVIDENCE.parent / "db", evidence)
        evidence["inflight"] = await inflight_cancel_case()
        evidence["lifecycle"] = await lifecycle_cases()

    asyncio.run(run())
    (EVIDENCE / "runtime-lifecycle.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 取消不初始化/不收窄编码/在途编码真实取消；异循环关闭保留资源；所属循环关闭与重复/并发关闭正确")


if __name__ == "__main__":
    check()
