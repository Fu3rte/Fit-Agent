import argparse
import asyncio
import json
from concurrent.futures import CancelledError as FuturesCancelled
from dataclasses import replace
from threading import Event
from uuid import uuid4

from app.agent.tool import (
    AfterToolCallResult,
    AgentTool,
    BeforeToolCallResult,
    CredentialDetectedError,
    FinalizedToolCall,
    _ResultProtocolError,
    prepare_tool_call,
    run_tool_call,
)
from app.agent.tools.common import envelope, unbound
from app.agent.tools.exercises import _details_tool, _retrieval_envelope, _search_tool
from app.ai.messages import TextContent, ToolCall
from app.ai.types import check_cancelled
from app.application.business import retrieval_frozen
from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import evaluate_search_conditions
from app.application.business.retrieval_embedding import EmbeddingClient
from app.application.business.retrieval_index import (
    IndexCompatibilityError,
    MilvusConnection,
    RetrievalCorpus,
)
from app.application.business.retrieval_response import (
    DETAILS_BUDGET,
    SEARCH_BUDGET,
    build_search_result,
)
from app.application.business.retrieval_search import HybridRetriever
from app.domain.business.models import CatalogSearchArguments
from test.check_profile_confirmation import (
    GET_EXERCISE_DETAILS,
    SEARCH_EXERCISES,
    Fixture,
    message_text,
)
from test.check_profile_confirmation import (
    envelope as message_payload,
)
from test.check_retrieval_conditions import condition_cases
from test.check_retrieval_response import (
    candidate_rows,
    details_cases,
    evaluated,
    search_cases,
)
from test.regression_support import temporary_root


def assert_json(message, budget: int, code: str | None = None) -> dict:
    text = message_text(message)
    assert len(text) <= budget, len(text)
    payload = json.loads(text)
    assert isinstance(payload, dict)
    if code is not None:
        assert message.is_error is True
        assert payload["code"] == code, payload
    return payload


def tool_call(tool: AgentTool, arguments: dict) -> ToolCall:
    return ToolCall(type="toolCall", id="retrieval-fix-check", name=tool.name, arguments=arguments)


async def invoke(tool: AgentTool, arguments: dict, **kwargs):
    return await run_tool_call(
        tool_call(tool, arguments),
        tools={tool.name: tool},
        declared={tool.name: tool.definition()},
        **kwargs,
    )


async def framework_cases() -> None:
    # 使用声明实例的真实 unbound 拒绝路径，执行不会触达网络。
    for tool, arguments, budget in (
        (_search_tool(unbound), {"purpose": "lookup", "name": "卧推"}, SEARCH_BUDGET),
        (_details_tool(unbound), {"ids": ["0001"]}, DETAILS_BUDGET),
    ):
        message = await invoke(tool, arguments)
        assert_json(message, budget, "tool_execution_failed")
        for invalid in (
            {},
            {**arguments, "unexpected" * 1000: "x" * 20000},
            {**arguments, "ids" if tool.name == GET_EXERCISE_DETAILS else "purpose": "x" * 20000},
        ):
            rejected = await invoke(tool, invalid)
            payload = assert_json(rejected, budget, "invalid_tool_arguments")
            assert payload["parameter"] in {*tool.arguments.model_fields, "arguments"}
            assert "x" * 100 not in message_text(rejected)

        async def blocked(context, signal):
            return BeforeToolCallResult(block=True, reason="x" * 20000)

        rejected = await invoke(tool, arguments, before_tool_call=blocked)
        assert_json(rejected, budget, "tool_blocked")

        async def permission_failure(context, signal):
            raise ValueError("x" * 20000)

        rejected = await invoke(tool, arguments, before_tool_call=permission_failure)
        assert_json(rejected, budget, "permission_check_failed")

        def preparation_failure(arguments):
            raise ValueError("x" * 20000)

        rejected = await invoke(replace(tool, prepare_arguments=preparation_failure), arguments)
        assert_json(rejected, budget, "argument_preparation_failed")

        async def postprocess_failure(context, signal):
            raise ValueError("x" * 20000)

        rejected = await invoke(tool, arguments, after_tool_call=postprocess_failure)
        assert_json(rejected, budget, "tool_postprocess_failed")

        unauthorized = await run_tool_call(
            tool_call(tool, arguments), tools={tool.name: tool}, declared={}
        )
        assert_json(unauthorized, budget, "tool_not_authorized")

        signal = Event()
        signal.set()
        prepared = await prepare_tool_call(
            0, tool_call(tool, arguments), tools={tool.name: tool},
            declared={tool.name: tool.definition()}, signal=signal,
        )
        assert isinstance(prepared, FinalizedToolCall) and prepared.cancelled
        assert message_text(prepared.message) == "Operation aborted"

        # 原始异常和原始拒绝参数仍经过凭据检查。
        for params, options, marker in (
            ({**arguments, "credential": "sensitive-marker"}, {}, "sensitive-marker"),
            (arguments, {"before_tool_call": permission_failure}, "x" * 100),
            (arguments, {"after_tool_call": postprocess_failure}, "x" * 100),
            (arguments, {}, "不可执行"),
        ):
            def credential_check(text):
                return marker in text

            try:
                await invoke(tool, params, contains_credentials=credential_check, **options)
            except CredentialDetectedError:
                pass
            else:
                raise AssertionError("凭据拒绝被错误格式器绕过")

        async def credential_failure(context, signal):
            raise CredentialDetectedError()

        try:
            await invoke(tool, arguments, before_tool_call=credential_failure)
        except CredentialDetectedError:
            pass
        else:
            raise AssertionError("CredentialDetectedError 未传播")

        invalid_formatter = replace(tool, format_error=lambda code: envelope({"code": code}))
        try:
            await invoke(invalid_formatter, {})
        except _ResultProtocolError:
            pass
        else:
            raise AssertionError("错误格式器协议未校验")

    # 未启用格式器的工具保持既有文本错误与参数诊断。
    plain = AgentTool("plain_check", "检查", CatalogSearchArguments, unbound)
    rejected = await invoke(plain, {"name": "卧推"})
    assert "收到的参数" in message_text(rejected)
    failed = await invoke(plain, {"purpose": "lookup", "name": "卧推"})
    assert "工具执行失败" in message_text(failed) and "不可执行" in message_text(failed)


async def output_validation_cases() -> None:
    for tool, arguments, budget in (
        (_search_tool(unbound), {"purpose": "lookup", "name": "卧推"}, SEARCH_BUDGET),
        (_details_tool(unbound), {"ids": ["0001"]}, DETAILS_BUDGET),
    ):
        valid_payload = (
            {"items": [], "has_more": False}
            if tool.name == SEARCH_EXERCISES
            else {"items": [], "has_more": False, "remaining_ids": []}
        )
        for payload, is_error in (
            (valid_payload, False),
            ({"code": "exercise_not_found", "message": "ids: 动作 ID 不存在于动作目录。"}, True),
        ):
            expected = envelope(payload, is_error)

            async def valid_override(context, signal):
                return AfterToolCallResult(content=expected.content, is_error=is_error)

            message = await invoke(tool, arguments, after_tool_call=valid_override)
            assert message.content == expected.content and message.is_error is is_error
            assert assert_json(message, budget) == payload

        for text, error_type, error_message in (
            (json.dumps({"message": "x" * budget}), ValueError, "检索工具响应超出完整 JSON 预算"),
            ("x" * (budget + 1), ValueError, "检索工具响应超出完整 JSON 预算"),
            ("plain text", json.JSONDecodeError, None),
            ("[]", ValueError, "检索工具响应必须为 JSON 对象"),
            ('{"score": NaN}', ValueError, "检索工具响应包含非法 JSON 常量"),
        ):
            async def invalid_override(context, signal):
                return AfterToolCallResult(content=[TextContent(type="text", text=text)])

            try:
                await invoke(tool, arguments, after_tool_call=invalid_override)
            except ValueError as error:
                assert type(error) is error_type
                if error_message is not None:
                    assert str(error) == error_message
            else:
                raise AssertionError("非法最终覆盖未直接失败")

        async def credential_override(context, signal):
            return AfterToolCallResult(content=[TextContent(type="text", text="sensitive-marker")])

        try:
            await invoke(
                tool, arguments, after_tool_call=credential_override,
                contains_credentials=lambda text: "sensitive-marker" in text,
            )
        except CredentialDetectedError:
            pass
        else:
            raise AssertionError("最终校验掩盖凭据拒绝")

        async def invalid_protocol(context, signal):
            return AfterToolCallResult(content=["invalid"])

        try:
            await invoke(tool, arguments, after_tool_call=invalid_protocol)
        except _ResultProtocolError:
            pass
        else:
            raise AssertionError("最终校验掩盖结果协议拒绝")

        def broken_validator(result):
            raise ArithmeticError("output validation failure")

        for params in (arguments, {}):
            try:
                await invoke(replace(tool, validate_output=broken_validator), params)
            except ArithmeticError as error:
                assert str(error) == "output validation failure"
            else:
                raise AssertionError("准备或执行最终校验异常未直接传播")

        invalid_formatter = replace(
            tool, format_error=lambda code: envelope("invalid", True)
        )
        try:
            await invoke(invalid_formatter, {})
        except ValueError as error:
            assert str(error) == "检索工具响应必须为 JSON 对象"
        else:
            raise AssertionError("准备定稿未校验最终 JSON 对象")

        # 真实取消通道保留其收尾结果，跳过输出格式要求。
        signal = Event()
        signal.set()
        cancelled = await invoke(replace(tool, validate_output=broken_validator), arguments, signal=signal)
        assert message_text(cancelled) == "Operation aborted"

        async def cancel_execution(tool_call_id, params, signal, on_update):
            signal.set()
            check_cancelled(signal)

        cancelled = await invoke(
            replace(tool, execute=cancel_execution, validate_output=broken_validator),
            arguments, signal=Event(),
        )
        assert cancelled.is_error and message_text(cancelled) == "工具执行失败：执行已取消"

        async def cancel_postprocess(context, signal):
            raise FuturesCancelled("执行已取消")

        cancelled = await invoke(
            replace(tool, validate_output=broken_validator), arguments,
            after_tool_call=cancel_postprocess,
        )
        assert cancelled.is_error and "执行已取消" in message_text(cancelled)

        started, release = asyncio.Event(), asyncio.Event()

        async def pending_execution(tool_call_id, params, signal, on_update):
            started.set()
            await release.wait()
            return envelope({"items": []})

        task = asyncio.create_task(invoke(
            replace(tool, execute=pending_execution, validate_output=broken_validator),
            arguments, signal=Event(),
        ))
        await started.wait()
        task.cancel()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("结构化任务取消未传播")

    plain = AgentTool("plain_output_check", "检查", CatalogSearchArguments, unbound)
    assert plain.validate_output is None
    text = "x" * (DETAILS_BUDGET + 1)

    async def plain_override(context, signal):
        return AfterToolCallResult(content=[TextContent(type="text", text=text)], is_error=False)

    message = await invoke(plain, {"purpose": "lookup", "name": "卧推"}, after_tool_call=plain_override)
    assert message_text(message) == text and message.is_error is False


def response_boundary_cases() -> None:
    catalog = Catalog.load()
    rows = candidate_rows(catalog, 11)
    for conditions in (evaluated(), evaluated(notes=("环境=" + "家庭" * 10000,))):
        response = build_search_result(rows, conditions)
        result = _retrieval_envelope(response.model_dump(), budget=SEARCH_BUDGET)
        payload = assert_json(result, SEARCH_BUDGET)
        assert result.is_error is False
        assert set(payload) == {"items", "has_more"}
    for budget in (SEARCH_BUDGET, DETAILS_BUDGET):
        try:
            _retrieval_envelope({"message": "x" * budget}, True, budget=budget)
        except ValueError:
            pass
        else:
            raise AssertionError("最终序列化预算未执行")


async def business_cases() -> None:
    root = temporary_root("action-retrieval-production-fixes")
    fixture = await Fixture(root / f"{uuid4().hex}.db").seeded()
    try:
        session = await fixture.session("动作检索修复检查")
        context = fixture.context(session, session, session)
        for field in ("equipment", "body_part", "target", "muscle_group", "available_equipment", "unavailable_equipment", "excluded_ids"):
            value = ["x" * 5000] if field in ("available_equipment", "unavailable_equipment", "excluded_ids") else "x" * 5000
            arguments = {"purpose": "generation", "name": "练背", field: value}
            failed = await fixture.invoke(SEARCH_EXERCISES, arguments, context)
            payload = assert_json(failed, SEARCH_BUDGET, "invalid_search_condition")
            assert field in payload["message"]
            assert "x" * 100 not in message_text(failed)
        failed = await fixture.invoke(GET_EXERCISE_DETAILS, {"ids": ["x" * 8000]}, context)
        assert_json(failed, DETAILS_BUDGET, "exercise_not_found")
        assert "x" * 100 not in message_text(failed)
        for arguments in ({"ids": [" "]}, {"ids": ["0001"] * 10000}, {"ids": ["0001", "0001"]}):
            failed = await fixture.invoke(GET_EXERCISE_DETAILS, arguments, context)
            assert_json(failed, DETAILS_BUDGET, "invalid_tool_arguments")
        success = await fixture.invoke(GET_EXERCISE_DETAILS, {"ids": ["0001"]}, context)
        payload = assert_json(success, DETAILS_BUDGET)
        assert success.is_error is False
        assert payload["items"][0] == fixture.catalog.get("0001").model_dump()
        assert set(payload) == {"items", "has_more", "remaining_ids"}
        assert message_payload(success) == payload
        assert (await fixture.business.profile.get_profile()).version is None
    finally:
        await fixture.close()


async def index_readonly_cases() -> None:
    # 真实发布校验与缓存；SDK 不进入上下文，任何编码均在本地生命周期校验处停止。
    # 该入口只读取 Milvus；不构建、发布、删除或写入索引，不发 Embedding 请求。
    corpus = RetrievalCorpus.load()
    embedding = EmbeddingClient.from_env()
    evaluated = evaluate_search_conditions(
        CatalogSearchArguments(purpose="generation", name="练背", task_environment="在家训练", equipment="dumbbell"),
        None, corpus.catalog, rt.load_mappings(),
    )
    assert not any(evaluated.conditions.matches(item) for item in corpus.catalog.all())
    async with MilvusConnection.from_env() as db:
        calls: dict[str, int] = {}
        original_call = db.call

        async def counted_call(method, *args, **kwargs):
            calls[method] = calls.get(method, 0) + 1
            return await original_call(method, *args, **kwargs)

        db.call = counted_call
        retriever = await HybridRetriever.open(db, corpus, embedding)
        identity = retriever.validation_identity()
        calls.clear()
        empty_execution = await retriever.search("练背", conditions=evaluated.conditions)
        assert empty_execution.candidates == []
        assert (empty_execution.collection, empty_execution.manifest_json) == identity
        assert retriever.validation_identity() == identity
        assert not any(calls.get(method, 0) for method in ("get", "query", "load_collection", "search", "hybrid_search")), calls
        assert sum(calls.values()) == 4, calls
        assert embedding.usage.requests == 0 and embedding._client is None

        # 可行条件必须抵达真实编码入口；未打开 SDK 的生命周期错误须直接传播。
        try:
            await retriever.search("练背")
        except RuntimeError as error:
            assert type(error) is RuntimeError
            assert str(error) == "EmbeddingClient 必须在 async with 上下文中使用"
        else:
            raise AssertionError("可行查询提前返回")
        assert not calls.get("search", 0) and not calls.get("hybrid_search", 0)

        incompatible = EmbeddingClient(embedding.config.model_copy(update={"model": "local-incompatible-check"}))
        invalid = HybridRetriever(db, corpus, incompatible)
        try:
            await invalid.search("练背", conditions=evaluated.conditions)
        except IndexCompatibilityError:
            pass
        else:
            raise AssertionError("互斥条件吞掉索引不兼容")
        assert invalid._validated is None

        signal = Event()
        original_validate = retriever._validate_published

        async def cancel_after_validation():
            validated = await original_validate()
            signal.set()
            return validated

        retriever._validate_published = cancel_after_validation
        try:
            await retriever.search("练背", conditions=evaluated.conditions, signal=signal)
        except FuturesCancelled:
            pass
        else:
            raise AssertionError("校验后取消被空结果吞掉")
        assert embedding.usage.requests == 0 and embedding._client is None
        assert retriever.validation_identity() == identity
    print("PASS: 真实索引互斥零编码、4 RPC 缓存、可行查询编码入口、不兼容与取消传播；0 Embedding 请求")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("pure", "index-readonly"))
    mode = parser.parse_args().mode
    assert retrieval_frozen.frozen_values() == {
        "fusion": "rrf", "rrf_k": 60, "bm25_depth": 100, "dense_depth": 100,
        "threshold": 0.0, "candidate_pool_limit": 20, "final_input_limit": 5,
        "recall_limit": 20,
    }
    if mode == "index-readonly":
        asyncio.run(index_readonly_cases())
    else:
        condition_cases()
        search_cases()
        details_cases()
        response_boundary_cases()
        asyncio.run(framework_cases())
        asyncio.run(output_validation_cases())
        asyncio.run(business_cases())
        print("PASS: 居家同义词、搜索/详情成功预算、长输入业务/框架JSON、权限/预处理/后处理、凭据/取消及未启用兼容")


if __name__ == "__main__":
    main()
