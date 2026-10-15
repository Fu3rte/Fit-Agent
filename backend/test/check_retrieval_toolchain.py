import asyncio
import json
import time
from pathlib import Path

from app.agent.tool import run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.ai.messages import TextContent, ToolCall
from app.application.business.retrieval_runtime import ActionRetrievalRuntime
from app.application.business.retrieval_search import search_vectors
from app.application.business.service import BusinessService
from app.domain.business.models import ProfileContent
from test.check_profile_confirmation import Fixture
from test.regression_support import temporary_root

EVIDENCE = temporary_root("action-retrieval-t6") / "toolchain"
DECLARED = {item.name: item for item in business_tool_declarations()}
PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周三次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": ["barbell"],
    "forbidden_exercise_ids": [],
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def body(message) -> dict:
    assert len(message.content) == 1 and isinstance(message.content[0], TextContent)
    return json.loads(message.content[0].text)


async def tool_search(business, call_fn, context, arguments: dict):
    tools = bind_business_tools(business, context, call_fn, {}, {})
    return await run_tool_call(
        ToolCall(type="toolCall", id="call-" + str(now_ms()), name="search_exercises", arguments=arguments),
        tools={"search_exercises": tools["search_exercises"]},
        declared={"search_exercises": DECLARED["search_exercises"]},
    )


async def stored_vector_cases(runtime: ActionRetrievalRuntime) -> dict:
    # 零费用：复用已存向量，只核实发布清单、轻量校验与过滤，不编码查询。
    retriever = await runtime._ensure()
    db, corpus = retriever.db, retriever.corpus
    collection = retriever._validated[0]
    record = next(item for item in corpus.records if item["id"] == "0001")
    stored = await db.call(
        "get", collection, ids=["0001"], output_fields=["dense"], consistency_level="Strong"
    )
    vector = stored[0]["dense"]
    modes = {}
    for mode in ("bm25", "dense", "hybrid"):
        hits = await search_vectors(
            db, collection, corpus.catalog, record["text"], vector, mode=mode, limit=5
        )
        assert hits, mode
        modes[mode] = [hit.id for hit in hits]
    assert retriever.embedding.usage.requests == 0, "已存向量检查不应发起 Embedding 请求"
    return {"collection": collection, "modes": modes, "embedding_requests": 0}


async def toolchain_cases(root: Path) -> dict:
    fixture = await Fixture(root / "toolchain.db").seeded()
    try:
        session_id = await fixture.session("检索工具链会话")
        context = fixture.context(session_id, session_id, session_id)
        runtime = ActionRetrievalRuntime()
        business = BusinessService(
            fixture.repository, fixture.catalog, fixture.service, fixture.replacements, runtime
        )
        # 首次真实语义查询（generation）：走主循环检索资源。
        first = await tool_search(
            business, fixture.call, context,
            {"purpose": "generation", "name": "练背", "body_part": "背"},
        )
        assert first.is_error is False, body(first)
        first_body = body(first)
        assert set(first_body) == {"items", "has_more", "remaining_ids"}
        assert first_body["items"]
        assert all(set(item) == {"id", "name", "equipment", "target", "matches"} for item in first_body["items"])
        assert len(json.dumps(first_body, ensure_ascii=False, allow_nan=False)) <= 4000
        retriever = runtime._retriever
        assert retriever is not None

        # 连续查询复用同一 retriever 实例，不重建、不重新加载索引。
        second = await tool_search(
            business, fixture.call, context,
            {"purpose": "lookup", "name": "卧推"},
        )
        assert second.is_error is False, body(second)
        assert runtime._retriever is retriever, "连续查询未复用同一 retriever 实例"

        # 保存画像限制后 generation 排除受限器械：真实 SQLite 跨查询生效。
        async with fixture.repository.transaction():
            await fixture.repository.save_profile(
                ProfileContent.model_validate(PROFILE), 1, now_ms()
            )
        filtered = await tool_search(
            business, fixture.call, context,
            {"purpose": "generation", "name": "卧推"},
        )
        assert filtered.is_error is False, body(filtered)
        filtered_body = body(filtered)
        assert all(item["equipment"] != "barbell" for item in filtered_body["items"])
        usage = retriever.embedding.usage
        return {
            "first_items": len(first_body["items"]),
            "first_has_more": first_body["has_more"],
            "lookup_items": len(body(second)["items"]),
            "filtered_items": len(filtered_body["items"]),
            "reused_retriever": True,
            "embedding_requests": usage.requests,
            "embedding_total_tokens": usage.total_tokens,
        }
    finally:
        await fixture.close()


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    runtime = ActionRetrievalRuntime()

    async def run() -> dict:
        try:
            evidence = {"stored_vector": await stored_vector_cases(runtime)}
            evidence["toolchain"] = await toolchain_cases(EVIDENCE.parent / "db")
            return evidence
        finally:
            await runtime.close()

    evidence = asyncio.run(run())
    (EVIDENCE / "toolchain.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("PASS: 已存向量检索核验与真实语义工具链（含 retriever 复用与画像过滤）")


if __name__ == "__main__":
    check()
