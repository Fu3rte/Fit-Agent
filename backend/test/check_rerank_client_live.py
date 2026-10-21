import asyncio
import json
from concurrent.futures import CancelledError as FuturesCancelled
from dataclasses import asdict
from pathlib import Path
from threading import Event

from app.application.business.retrieval_index import RetrievalCorpus
from app.application.business.retrieval_rerank import RerankClient, build_request
from app.application.business.retrieval_search import RetrievalCandidate

# 证据写入仓库已忽略的 tmp 目录；不保存查询、文档正文、授权头或原始响应。
EVIDENCE = Path(__file__).resolve().parents[2] / "tmp" / "action-retrieval-rerank"
QUERY = "卧推"
COUNTS = (1, 20)


def candidates(corpus: RetrievalCorpus, count: int) -> list[RetrievalCandidate]:
    return [
        RetrievalCandidate(
            id=record["id"],
            score=float(count - index),
            name=corpus.catalog.get(record["id"]).name,
            equipment=record["equipment"],
            body_part=record["body_part"],
            target=record["target"],
            muscle_group=record["muscle_group"],
        )
        for index, record in enumerate(corpus.records[:count])
    ]


async def score_case(
    client: RerankClient, corpus: RetrievalCorpus, count: int
) -> dict:
    request = build_request(QUERY, candidates(corpus, count), corpus.records)
    result = await client.rerank(request)
    input_ids = [document.id for document in request.documents]
    scored_ids = [row.id for row in result.scored]
    scores = [row.rerank_score for row in result.scored]
    assert len(scored_ids) == count
    assert set(scored_ids) == set(input_ids)
    assert len(set(scored_ids)) == count
    assert scores == sorted(scores, reverse=True)
    assert all(0.0 <= score <= 1.0 for score in scores)
    assert all(
        request.documents[row.rrf_rank].id == row.id for row in result.scored
    )
    assert result.usage.requests == 1
    assert result.usage.prompt_tokens > 0
    assert result.usage.total_tokens > 0
    assert isinstance(result.request_id, str) and result.request_id.strip()
    return {
        "count": count,
        "distinct_ids": len(set(scored_ids)),
        "scores": scores,
        "usage": asdict(result.usage),
        "request_id": result.request_id,
        "ids_match_input": True,
        "index_mapping_valid": True,
        "descending": True,
    }


async def signal_cancel_case(
    client: RerankClient, corpus: RetrievalCorpus
) -> dict:
    request = build_request(QUERY, candidates(corpus, 20), corpus.records)
    signal = Event()
    task = asyncio.ensure_future(client.rerank(request, signal=signal))
    await asyncio.sleep(0.01)
    assert not task.done(), "请求在取消前已完成，未覆盖在途取消"
    signal.set()
    error = None
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - 取消按既有语义传播
        error = exc
    assert isinstance(error, FuturesCancelled), "在途 signal 取消应传播取消错误"
    empty = build_request(QUERY, [], corpus.records)
    assert (await client.rerank(empty)).usage.requests == 0
    return {
        "started_before_cancel": True,
        "outcome": type(error).__name__,
        "client_reusable": True,
    }


async def outer_cancel_case(
    client: RerankClient, corpus: RetrievalCorpus
) -> dict:
    request = build_request(QUERY, candidates(corpus, 20), corpus.records)
    task = asyncio.ensure_future(client.rerank(request))
    await asyncio.sleep(0.01)
    assert not task.done(), "请求在取消前已完成，未覆盖外层取消"
    task.cancel()
    error = None
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - 外层取消按既有语义传播
        error = exc
    assert isinstance(error, asyncio.CancelledError), "外层取消应传播 CancelledError"
    empty = build_request(QUERY, [], corpus.records)
    assert (await client.rerank(empty)).usage.requests == 0
    return {
        "started_before_cancel": True,
        "outcome": type(error).__name__,
        "client_reusable": True,
    }


async def run() -> dict:
    corpus = RetrievalCorpus.load()
    evidence: dict = {"model": None, "endpoint": None, "cases": {}}
    async with RerankClient.from_env() as client:
        evidence["model"] = client.config.model
        evidence["endpoint"] = client.config.endpoint
        for count in COUNTS:
            evidence["cases"][f"count_{count}"] = await score_case(
                client, corpus, count
            )
        evidence["signal_cancel"] = await signal_cancel_case(client, corpus)
        evidence["outer_cancel"] = await outer_cancel_case(client, corpus)
    evidence["post_close_tasks"] = len(
        [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    )
    assert evidence["post_close_tasks"] == 0, "关闭后仍遗留任务"
    return evidence


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence = asyncio.run(run())
    (EVIDENCE / "rerank-client-live.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: 真实重排完成 1 条与 20 条评分；在途 signal 取消与外层取消均传播；关闭后无遗留任务")
    print(f"model={evidence['model']} post_close_tasks={evidence['post_close_tasks']}")
    for name, case in evidence["cases"].items():
        print(
            f"{name}: usage={case['usage']} request_id={case['request_id']}"
        )


if __name__ == "__main__":
    check()
