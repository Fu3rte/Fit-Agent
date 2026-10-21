import asyncio
import json
import os
import time
from concurrent.futures import CancelledError as FuturesCancelled
from pathlib import Path
from threading import Event

from dotenv import load_dotenv

from app.application.business import retrieval_frozen
from app.application.business import retrieval_text as rt
from app.application.business.retrieval_conditions import evaluate_search_conditions
from app.application.business.retrieval_config import MissingRerankConfig
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.service import BusinessService
from app.domain.business.models import CatalogSearchArguments, ProfileContent
from test.check_profile_confirmation import Fixture
from test.regression_support import temporary_root

BACKEND = Path(__file__).resolve().parents[1]
EVIDENCE = temporary_root("action-retrieval-rerank-production")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def search_case(
    business: BusinessService,
    observations: list,
    catalog,
    mappings: dict,
    arguments: CatalogSearchArguments,
    label: str,
    *,
    expected_empty: bool = False,
) -> dict:
    before = len(observations)
    response = await business.search_exercises(arguments, tool_call_id=f"live-{label}")
    require(len(observations) == before + 1, "-observation-未记录")
    observation = observations[-1]
    stage = observation.stage
    require(stage.rerank is not None, "重排结果缺失")
    require(stage.rerank_identity is not None, "重排配置身份缺失")
    require(stage.rerank_identity.model, "重排模型标识为空")
    require(stage.rerank_identity.endpoint, "重排地址为空")
    require(
        stage.rerank_identity.text_version == rt.ENCODING_RULE_VERSION,
        "重排文本版本不符",
    )
    require(bool(observation.collection) and bool(observation.manifest_json), "索引身份缺失")
    require(set(response.model_dump()) == {"items", "has_more"}, "公开字段越界")
    require(
        [item.id for item in stage.final_input]
        == [item.id for item in response.items],
        "最终输入与公开条目不一致",
    )
    profile = (await business.profile.get_profile()).content
    evaluated = evaluate_search_conditions(arguments, profile, catalog, mappings)
    require(
        all(
            evaluated.conditions.matches(catalog.get(item.id))
            for item in stage.candidate_pool
        ),
        "候选违反硬约束",
    )
    pool_ids = [item.id for item in stage.candidate_pool]
    require(len(pool_ids) <= 20, "候选池超过 20 条")
    require(
        all(
            left.score >= right.score
            for left, right in zip(stage.candidate_pool, stage.candidate_pool[1:])
        ),
        "候选池未按 RRF 顺序排列",
    )
    if expected_empty:
        require(pool_ids == [], "预期空池但存在候选")
        require(stage.rerank.scored == (), "空池返回了重排结果")
        require(stage.rerank.usage.requests == 0, "空池发起了重排请求")
        require(stage.rerank.request_id is None, "空池返回了请求身份")
        require(response.items == [] and response.has_more is False, "空池响应非法")
    else:
        require(bool(pool_ids), "非空预期但候选池为空")
        rows = stage.rerank.scored
        require({row.id for row in rows} == set(pool_ids), "重排结果未覆盖候选池")
        require(
            [row.id for row in rows]
            == [pool_ids[row.rrf_rank] for row in rows],
            "重排原排名与原候选不符",
        )
        scores = [row.rerank_score for row in rows]
        require(scores == sorted(scores, reverse=True), "重排分数未降序")
        require(stage.rerank.usage.requests == 1, "重排用量未按单次调用记录")
        require(stage.rerank.usage.prompt_tokens > 0, "重排 prompt_tokens 非正")
        require(stage.rerank.usage.total_tokens > 0, "重排 total_tokens 非正")
        require(bool(stage.rerank.request_id), "重排 request_id 缺失")
        require(
            len(response.items) == min(retrieval_frozen.RECALL_LIMIT, len(pool_ids), 5),
            "公开条目数量不符",
        )
    return {
        "arguments": arguments.model_dump(mode="json"),
        "pool": pool_ids,
        "rerank_order": [row.id for row in stage.rerank.scored],
        "rerank_scores": [row.rerank_score for row in stage.rerank.scored],
        "final_input": [item.id for item in stage.final_input],
        "items": [item.id for item in response.items],
        "has_more": response.has_more,
        "usage": {
            "requests": stage.rerank.usage.requests,
            "prompt_tokens": stage.rerank.usage.prompt_tokens,
            "total_tokens": stage.rerank.usage.total_tokens,
        },
        "request_id": stage.rerank.request_id,
        "identity": {
            "model": stage.rerank_identity.model,
            "endpoint": stage.rerank_identity.endpoint,
            "text_version": stage.rerank_identity.text_version,
        },
        "collection": observation.collection,
    }


async def concurrent_case(runtime: ActionRetrievalRuntime, catalog) -> dict:
    first, second = catalog.all()[0], catalog.all()[1]
    executions = await asyncio.gather(
        runtime.search(first.name, limit=retrieval_frozen.RECALL_LIMIT),
        runtime.search(second.name, limit=retrieval_frozen.RECALL_LIMIT),
    )
    request_ids = []
    for execution in executions:
        pool_ids = [item.id for item in execution.candidates]
        require({row.id for row in execution.rerank.scored} == set(pool_ids), "并发结果串用")
        require(
            [row.id for row in execution.rerank.scored]
            == [pool_ids[row.rrf_rank] for row in execution.rerank.scored],
            "并发原排名串用",
        )
        require(execution.rerank.usage.requests == 1, "并发用量缺失")
        request_ids.append(execution.rerank.request_id)
    require(all(request_ids), "并发请求身份缺失")
    require(len(set(request_ids)) == len(request_ids), "并发请求身份重复")
    return {"pools": [[item.id for item in item.candidates] for item in executions],
            "request_ids": request_ids}


async def inflight_cancel_case(runtime: ActionRetrievalRuntime, catalog) -> dict:
    name = catalog.all()[0].name
    signal = Event()
    task = asyncio.ensure_future(
        runtime.search(name, limit=retrieval_frozen.RECALL_LIMIT, signal=signal)
    )
    await asyncio.sleep(0.01)
    require(not task.done(), "取消前搜索已完成")
    signal.set()
    error = None
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - 取消按既有语义传播
        error = exc
    require(isinstance(error, FuturesCancelled), "在途 signal 取消未传播取消错误")
    return {"started_before_cancel": True, "outcome": type(error).__name__}


async def outer_cancel_case(runtime: ActionRetrievalRuntime, catalog) -> dict:
    name = catalog.all()[1].name
    task = asyncio.ensure_future(
        runtime.search(name, limit=retrieval_frozen.RECALL_LIMIT)
    )
    await asyncio.sleep(0.01)
    require(not task.done(), "取消前搜索已完成")
    task.cancel()
    error = None
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - 外层取消按既有语义传播
        error = exc
    require(isinstance(error, asyncio.CancelledError), "外层取消未传播 CancelledError")
    return {"started_before_cancel": True, "outcome": type(error).__name__}


async def init_failure_case(catalog) -> dict:
    names = (
        "RERANKER_MODEL",
        "RERANKER_BASE_URL",
        "RERANKER_API_KEY",
        "RERANKER_TIMEOUT",
    )
    saved = {name: os.environ.get(name) for name in names}
    runtime = ActionRetrievalRuntime()
    try:
        os.environ["RERANKER_API_KEY"] = ""
        try:
            await runtime.search(catalog.all()[0].name, limit=retrieval_frozen.RECALL_LIMIT)
        except MissingRerankConfig:
            pass
        else:
            raise AssertionError("缺少重排配置未在初始化阶段失败")
        require(runtime._stack is None, "初始化失败后资源栈未释放")
        require(runtime._retriever is None, "初始化失败后记录了召回实例")
        require(runtime._rerank is None and runtime._rerank_identity is None, "初始化失败后记录了重排资源")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    execution = await runtime.search(
        catalog.all()[0].name, limit=retrieval_frozen.RECALL_LIMIT
    )
    require(execution.rerank.usage.requests == 1, "失败后复用未完成重排")
    await runtime.close()
    require(runtime._stack is None and runtime._rerank is None, "重排资源未关闭")
    return {"failed_without_resource_leak": True, "reused_after_failure": True}


async def run() -> dict:
    load_dotenv(BACKEND / ".env")
    db_path = EVIDENCE / "db" / "production.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    fixture = await Fixture(db_path).seeded()
    observations: list = []
    runtime = ActionRetrievalRuntime()
    business = BusinessService(
        fixture.repository,
        fixture.catalog,
        fixture.service.queries,
        fixture.replacements,
        runtime,
        search_observer=observations.append,
    )
    mappings = rt.load_mappings()
    try:
        evidence = {
            "normal": await search_case(
                business,
                observations,
                fixture.catalog,
                mappings,
                CatalogSearchArguments(purpose="lookup", name="卧推"),
                "normal",
            )
        }
        equipment = (
            "barbell"
            if "barbell" in fixture.catalog.equipment
            else sorted(fixture.catalog.equipment)[0]
        )
        excluded = fixture.catalog.all()[0].id
        filtered = await search_case(
            business,
            observations,
            fixture.catalog,
            mappings,
            CatalogSearchArguments(
                purpose="generation",
                name="卧推",
                unavailable_equipment=[equipment],
                excluded_ids=[excluded],
            ),
            "filtered",
        )
        require(
            all(
                fixture.catalog.get(item).equipment != equipment
                for item in filtered["pool"]
            ),
            "硬过滤候选仍含不可用器械",
        )
        require(excluded not in filtered["pool"], "排除动作仍出现在候选池")
        evidence["filtered"] = filtered

        evidence["concurrent"] = await concurrent_case(runtime, fixture.catalog)
        evidence["inflight_cancel"] = await inflight_cancel_case(runtime, fixture.catalog)
        evidence["outer_cancel"] = await outer_cancel_case(runtime, fixture.catalog)
        evidence["reuse_after_cancel"] = await search_case(
            business,
            observations,
            fixture.catalog,
            mappings,
            CatalogSearchArguments(
                purpose="lookup", name=fixture.catalog.all()[2].name
            ),
            "reuse",
        )

        async with fixture.repository.transaction():
            await fixture.repository.save_profile(
                ProfileContent(
                    goal=None,
                    experience=None,
                    environment=None,
                    availability=None,
                    health_notes=None,
                    movement_restrictions=None,
                    unavailable_equipment=sorted(fixture.catalog.equipment),
                    forbidden_exercise_ids=None,
                ),
                1,
                now_ms(),
            )
        evidence["empty"] = await search_case(
            business,
            observations,
            fixture.catalog,
            mappings,
            CatalogSearchArguments(purpose="generation", name="卧推"),
            "empty",
            expected_empty=True,
        )
    finally:
        await runtime.close()
        await fixture.close()
    evidence["init_failure"] = await init_failure_case(fixture.catalog)
    return evidence


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "db").mkdir(parents=True, exist_ok=True)
    evidence = asyncio.run(run())
    (EVIDENCE / "production.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 生产检索重排链路、硬过滤、空池、并发身份与取消均通过真实服务核验")
    for name in ("normal", "filtered", "reuse_after_cancel", "empty"):
        case = evidence[name]
        print(
            f"{name}: pool={len(case['pool'])} final={len(case['final_input'])} "
            f"usage={case['usage']} request_id={case['request_id']}"
        )


if __name__ == "__main__":
    check()
