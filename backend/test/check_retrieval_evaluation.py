import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pymilvus
from pymilvus import MilvusClient

from app.application.business import retrieval_text as rt
from app.application.business.retrieval_embedding import (
    EmbeddingClient,
    validate_embedding_data,
)
from app.application.business.retrieval_index import (
    ANALYZER,
    MilvusConnection,
    RetrievalCorpus,
)
from app.application.business.retrieval_response import build_details_result
from app.application.business.retrieval_search import RetrievalCandidate
from app.domain.business.errors import ExerciseDetailsTooLarge, ExerciseNotFound
from app.domain.business.models import CatalogSearchArguments, ExerciseDetailsArguments
from test import retrieval_evaluation_support as support
from test.check_retrieval_baseline import aggregate, evaluate, percentile, run_baseline
from test.check_retrieval_response import candidate_rows

ENV_NAMES = [
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_WORKSPACE_ID",
    "FIT_AGENT_EMBEDDING_MODEL",
    "FIT_AGENT_MILVUS_URI",
    "FIT_AGENT_MILVUS_TOKEN",
    "FIT_AGENT_MILVUS_TIMEOUT",
]
GROUPS = ("name", "bm25", "dense", "hybrid")
SCORED_GROUPS = ("hybrid",)
ANALYZER_CANDIDATES = (
    {"label": "chinese", "params": {"type": "chinese"}},
    {"label": "standard", "params": {"type": "standard"}},
    {"label": "jieba", "params": {"type": "jieba"}},
    {"label": "icu", "params": {"type": "icu"}},
)
ANALYZER_SAMPLES = (
    "杠铃卧推",
    "坐姿哑铃弯举，锻炼肱二头肌",
    "barbell bench press",
    "Dumbbell 哑铃卧推 3/4",
    "家里有哑铃，想练背",
)


def load_queries(split: str) -> list[dict]:
    queries = support.load_document(support.QUERIES_PATH)["queries"]
    selected = queries if split == "all" else [item for item in queries if item["split"] == split]
    if not selected:
        raise support.EvaluationBlocked(f"拆分 {split} 没有查询")
    return selected


def embedding_config():
    # 只读取配置身份用于缓存校验；不进入上下文即不产生任何网络请求。
    return EmbeddingClient.from_env().config


def score_scale(scores: list[float]) -> dict:
    if not scores:
        return {"samples": 0}
    ordered = sorted(scores)
    return {
        "samples": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "p50": percentile(ordered, 0.5),
        "p95": percentile(ordered, 0.95),
    }


def resolve_conditions(query: dict, catalog, mappings, mode: str, purposes: dict, adaptation: dict):
    if mode == "explicit-filters":
        return support.explicit_filter_conditions(query), None
    purpose = purposes[query["id"]]
    temporary = support.temporary_mapping(adaptation, query["id"])
    evaluated = support.effective_conditions(query, catalog, mappings, purpose, temporary)
    return evaluated, {"purpose": purpose, "temporary_mapping": temporary}


def projection_rows(catalog, projections: dict[str, dict]) -> dict:
    return {
        "hard_constraint_violation_total": sum(
            len(row["hard_constraint_violations"]) for row in projections.values()
        ),
        "budget_noncompliant": [
            query_id for query_id, row in projections.items() if not row["budget_compliant"]
        ],
        "dropped_by_threshold_total": sum(
            len(row["dropped_by_threshold"]) for row in projections.values()
        ),
        "dropped_by_budget_total": sum(
            len(row["dropped_by_budget"]) for row in projections.values()
        ),
    }


async def collect_group(
    group: str,
    queries: list[dict],
    catalog,
    mappings,
    conditions_mode: str,
    purposes: dict,
    adaptation: dict,
    *,
    db: MilvusConnection | None,
    collection: str | None,
    vectors: dict,
    alpha: float,
    depth: int,
    threshold: float | None,
) -> dict:
    projections = {}
    adaptation_context = {}
    scores = []
    for query in queries:
        evaluated, adapted = resolve_conditions(
            query, catalog, mappings, conditions_mode, purposes, adaptation
        )
        if adapted is not None:
            adaptation_context[query["id"]] = adapted
        if group == "name":
            recalled = support.name_candidates(catalog, query, evaluated)[: support.RECALL_LIMIT]
            used_threshold = None
        else:
            vector = vectors[query["id"]] if group in ("dense", "hybrid") else []
            recalled = await support.vector_candidates(
                db, collection, catalog, query, vector, evaluated,
                mode=group, alpha=alpha, bm25_depth=depth, dense_depth=depth,
            )
            used_threshold = threshold if group in SCORED_GROUPS else None
            scores.extend(candidate.score for candidate in recalled)
        projections[query["id"]] = support.project_response(
            catalog, recalled, evaluated, threshold=used_threshold, scored=group in SCORED_GROUPS
        )
    metrics = support.metrics(queries, projections)
    scored = group in SCORED_GROUPS
    metrics["per_query_projection"] = projections
    metrics["score_scale"] = score_scale(scores) if scored or group in ("bm25", "dense") else {"samples": 0, "note": "名称基线无相关度分数"}
    metrics["projection_totals"] = projection_rows(catalog, projections)
    metrics["conditions"] = {
        "mode": conditions_mode,
        "expressions": {
            query["id"]: support.resolve_expression(
                query, catalog, mappings, conditions_mode, purposes, adaptation
            )
            for query in queries
        },
    }
    metrics["adaptation"] = adaptation_context
    metrics["parameters"] = {
        "alpha": alpha if group == "hybrid" else None,
        "bm25_depth": depth if group in ("bm25", "hybrid") else None,
        "dense_depth": depth if group in ("dense", "hybrid") else None,
        "threshold": threshold if group in SCORED_GROUPS else None,
        "threshold_scope": "门槛只作用于 norm_score=True 的融合分数",
        "recall_limit": support.RECALL_LIMIT,
        "fields_consumed": {
            "name": "原始目录 name 字段的中文子串匹配",
            "bm25": "统一检索文本（name, aliases, english_name, equipment, body_part, target, muscle_group, steps_zh, steps_en）经 chinese analyzer 生成的 sparse 向量",
            "dense": "同一统一检索文本的 1024 维 COSINE 向量",
            "hybrid": "以上两路 + WeightedRanker(alpha, 1-alpha, norm_score=True)",
        },
    }
    return metrics


async def _compare_run(
    run,
    args,
    *,
    frozen_gate,
    ledger: dict,
    observe=None,
    finalize=None,
) -> int:
    # 正式留出主体：普通留出与获准复验共用同一执行、投影与门禁口径；门禁、账本、观察与附加证据由调用方注入。
    selected = load_queries(args.split)
    corpus = RetrievalCorpus.load()
    catalog, mappings = corpus.catalog, rt.load_mappings()
    identity = support.query_vector_identity(embedding_config(), corpus)
    frozen = None
    ledger_key = None
    ledger_status = ledger["abort_status"]
    if args.split == "holdout":
        # 正式留出：参数、门槛与条件固定取自冻结账本，不接受命令行覆盖。
        frozen = support.load_frozen()
        alpha, depth, threshold = frozen["alpha"], frozen["bm25_depth"], frozen["threshold"]
        conditions_mode = "adapted"
    else:
        alpha, depth, threshold = args.alpha, args.depth, args.threshold
        conditions_mode = args.conditions
    purposes: dict[str, str] = {}
    adaptation: dict = {}
    deferred: tuple[str, ...] = ()
    if conditions_mode == "adapted":
        adaptation, purposes = support.load_adaptation()
        deferred = support.deferred_query_ids(adaptation)
        selected = [item for item in selected if item["id"] not in deferred]
    needs_vectors = any(group in ("dense", "hybrid") for group in args.groups)
    needs_db = any(group != "name" for group in args.groups) or frozen is not None
    vectors = support.read_query_cache(identity, selected)[0] if needs_vectors else {}
    db: MilvusConnection | None = None
    collection = None
    index_identity = None
    context = {
        "selected": selected,
        "catalog": catalog,
        "query_ids": sorted(item["id"] for item in selected),
        "vector_cache_identity": identity,
        "vectors": vectors,
    }
    evidence = {
        "split": args.split,
        "queries": len(selected),
        "conditions_mode": conditions_mode,
        "deferred_query_ids": list(deferred),
        "collection": None,
        "groups": {},
        "embedding_requests": 0,
        "run_identity": run.fingerprint(),
    }
    try:
        if needs_db:
            db = MilvusConnection.from_env()
            await db.__aenter__()
            if observe is not None:
                observe(db)
            collection, index_identity = await support.open_published(db, corpus)
            evidence["collection"] = collection
            evidence["index_identity"] = index_identity
        if frozen is not None:
            context["frozen_gate_result"] = frozen_gate(
                frozen, run.fingerprint(), index_identity,
                {"alpha": args.alpha, "depth": args.depth, "threshold": args.threshold, "conditions": args.conditions},
                context,
            )
            ledger_key = ledger["key"](frozen)
            ledger["begin"](ledger_key, run.run_id)
            evidence["frozen_parameters"] = {
                key: frozen.get(key) for key in ("alpha", "bm25_depth", "dense_depth", "threshold")
            }
            evidence["frozen_config_identity"] = frozen.get("frozen_config_identity")
            evidence["holdout_observation_note"] = ledger["note"]
        reference_results = run_baseline(catalog, selected)
        by_intent, rows = evaluate(selected, reference_results)
        evidence["name_baseline_raw"] = {
            "口径": "金标 baseline_rule 主对照口径：原始 query 名称子串 + filters 四项精确筛选，无门槛与预算投影",
            "returned_total": sum(len(hits) for hits in reference_results.values()),
            "empty_queries": sum(1 for hits in reference_results.values() if not hits),
            "over_limit_queries": sum(1 for hits in reference_results.values() if len(hits) > 10),
            "by_intent": by_intent,
            "overall": aggregate(rows),
        }
        for group in args.groups:
            evidence["groups"][group] = await collect_group(
                group, selected, catalog, mappings, conditions_mode, purposes, adaptation,
                db=db, collection=collection, vectors=vectors,
                alpha=alpha, depth=depth, threshold=threshold,
            )
        if "name" in evidence["groups"]:
            evidence["diff_vs_name"] = {
                group: support.compare_groups(evidence["groups"]["name"], evidence["groups"][group], selected)
                for group in args.groups if group != "name"
            }
        gates = {
            group: {
                "hard_constraint_violations": data["projection_totals"]["hard_constraint_violation_total"],
                "budget_noncompliant": data["projection_totals"]["budget_noncompliant"],
            }
            for group, data in evidence["groups"].items()
        }
        assert all(item["hard_constraint_violations"] == 0 for item in gates.values()), gates
        assert all(not item["budget_noncompliant"] for item in gates.values()), gates
        evidence["gates"] = gates
        if finalize is not None:
            finalize(evidence, context)
        ledger_status = "completed"
        run.stage(run.mode, evidence)
        print(json.dumps({
            "run_id": run.run_id,
            "split": args.split,
            "conditions": conditions_mode,
            "groups": {
                group: {
                    "recall@10_returned": data["stage_metrics"]["returned"]["overall"]["recall@10"],
                    "ndcg@10_returned": data["stage_metrics"]["returned"]["overall"]["ndcg@10"],
                    "false_return_rate": data["stage_metrics"]["returned"]["overall"]["false_return_rate"],
                    "empty_queries": data["projection"]["empty_queries"],
                    "chars_p95": data["projection"]["returned_chars_p95"],
                }
                for group, data in evidence["groups"].items()
            },
        }, ensure_ascii=False))
        return 0
    finally:
        if ledger_key is not None:
            ledger["complete"](ledger_key, ledger_status)
        if db is not None:
            await db.__aexit__(None, None, None)
        run.finish()


async def compare(args) -> int:
    run = support.RunContext.start("compare", sys.argv, ENV_NAMES)
    return await _compare_run(
        run,
        args,
        frozen_gate=lambda frozen, fingerprint, index_identity, params, context: support.require_frozen_holdout(
            frozen, fingerprint, index_identity, params
        ),
        ledger={
            "key": support.holdout_ledger_key,
            "begin": support.begin_holdout_execution,
            "complete": support.complete_holdout_execution,
            "abort_status": "aborted",
            "note": "批准拆分未改动：此前 all 拆分已包含留出查询，本执行按冻结参数评估，不宣称为严格未见留出",
        },
    )


def require_approved_inputs(verification: dict, action: str) -> None:
    # 批准输入核验未通过即停止，复验路径不允许带着漂移的目录/金标/账本继续。
    if not verification["checks"]["passed"]:
        raise support.EvaluationBlocked(
            f"批准输入核验未通过，拒绝{action}",
            tuple(sorted(key for key, passed in verification["checks"].items() if not passed)),
        )


def load_evidence_document(path: Path, label: str) -> dict:
    # 只读既有证据文件：缺失即停止，不使用默认值或占位记录。
    if not path.is_file():
        raise support.EvaluationBlocked(f"缺少{label}证据文件", (str(path),))
    return support.load_document(path)


def historical_stage_metrics(history: dict) -> dict:
    keys = ("recall@10", "mrr@10", "ndcg@10", "false_return_rate")
    return {
        group: {
            **{key: history["groups"][group]["stage_metrics"]["returned"]["overall"][key] for key in keys},
            "empty_queries": history["groups"][group]["projection"]["empty_queries"],
        }
        for group in GROUPS
    }


def observe_rpc(db: MilvusConnection, stats: dict) -> None:
    # 透传观察器：仅统计方法与真实往返耗时，真实调用照常执行并原样返回或抛出，不替换 SDK。
    original = db.call

    async def traced(method: str, *call_args, **call_kwargs):
        started = support.now_ms()
        try:
            return await original(method, *call_args, **call_kwargs)
        finally:
            stats["total"] += 1
            stats["by_method"][method] = stats["by_method"].get(method, 0) + 1
            stats["elapsed_ms"].append(support.now_ms() - started)

    db.call = traced


def requal_observation(rpc_stats: dict, embedding_stats: dict, cache_hit: int, cache_miss: int) -> dict:
    elapsed = rpc_stats["elapsed_ms"]
    return {
        "rpc": {
            "total": rpc_stats["total"],
            "by_method": dict(sorted(rpc_stats["by_method"].items())),
            "elapsed_ms_p50": percentile(elapsed, 0.5),
            "elapsed_ms_p95": percentile(elapsed, 0.95),
            "elapsed_ms_max": max(elapsed) if elapsed else 0,
        },
        "embedding": {
            "requests": embedding_stats["requests"],
            "encoded_texts": embedding_stats["texts"],
            "authorized_cap": 0,
        },
        "vector_cache": {"hit": cache_hit, "miss": cache_miss},
        "latency_scope": "elapsed_ms 为 Milvus 只读发布校验与检索往返的真实耗时；查询向量全部取自缓存，本次不含在线编码延迟",
    }


REQUAL_FIXED_PARAMETERS = {"alpha": 0.5, "bm25_depth": 100, "dense_depth": 100, "threshold": 0.0}
VECTOR_SOURCE_RUN_ID = "20261015T053105Z-c803ff72"


async def requalify_prepare(args) -> int:
    # 零质量查询、零模型请求的准备阶段：逐项核验兼容并原子写入复验授权；不写任何留出账本、不发起 search。
    run = support.RunContext.start("requalify-prepare", sys.argv, ENV_NAMES)
    if support.REQUAL_AUTH_PATH.is_file():
        raise support.EvaluationBlocked("复验授权文件已存在，拒绝覆盖既有授权", (str(support.REQUAL_AUTH_PATH),))
    frozen = support.load_frozen()
    corpus = RetrievalCorpus.load()
    catalog = corpus.catalog
    identity = support.query_vector_identity(embedding_config(), corpus)
    adaptation, _purposes = support.load_adaptation()
    deferred = support.deferred_query_ids(adaptation)
    selected = [item for item in load_queries("holdout") if item["id"] not in deferred]
    query_ids = sorted(item["id"] for item in selected)
    vectors, _entries = support.read_query_cache(identity, selected)
    cached = support.read_cached_entries(identity)
    cache_file = support.cache_path(identity)
    verification = support.verify_inputs(catalog)
    require_approved_inputs(verification, "建立复验授权")
    historical_ids = support.historical_holdout_query_ids()
    if query_ids != historical_ids:
        raise support.EvaluationBlocked("本次留出查询清单与历史留出逐 ID 不一致")
    async with MilvusConnection.from_env() as db:
        collection, index_identity = await support.open_published(db, corpus)
    history = load_evidence_document(
        support.PARENT_HOLDOUT_DIR / "compare.json", "历史正式留出"
    )
    source = load_evidence_document(
        support.EVAL_ROOT / VECTOR_SOURCE_RUN_ID / "vectors.json", "查询向量来源"
    )
    if source["identity"] != identity:
        raise support.EvaluationBlocked("向量缓存来源运行的身份与当前配置不一致", (VECTOR_SOURCE_RUN_ID,))
    parent = support.requalification_parent_digests(cache_file)
    params = {
        "alpha": frozen["alpha"], "depth": frozen["bm25_depth"],
        "threshold": frozen["threshold"], "conditions": "adapted",
    }
    snapshot = support.run_identity()
    wiring = support.frozen_config_identity()
    auth = {
        "scope": support.REQUAL_SCOPE,
        "authorized_by": "operator instruction 2026-10-15",
        "requalification_note": (
            "本次为获准的重复留出观察：同一 66 条留出查询已在历史正式留出运行中评估过一次，"
            "本次只核验当前生产源码在同一冻结参数下的行为，不宣称严格未见留出，"
            "不得据本次结果挑选参数或另起一轮挑结果"
        ),
        "request_caps": dict(support.REQUAL_REQUEST_CAPS),
        "run_budget": {
            "formal_holdout_requalifications": 1,
            "dev_grid_runs": 0,
            "threshold_search_runs": 0,
            "analyzer_ablations": 0,
            "embedding_requests": 0,
            "chat_requests": 0,
            "summary_requests": 0,
        },
        "parent": {
            **parent,
            "parent_holdout_run_id": support.PARENT_HOLDOUT_RUN_ID,
            "parent_grid_run_directory": frozen["grid_run_directory"],
            "frozen_at": frozen["frozen_at"],
        },
        "frozen_parameters": dict(REQUAL_FIXED_PARAMETERS),
        "frozen_config_identity": wiring,
        "approved_inputs": {
            **verification,
            "queries_sha256": support.sha256(support.QUERIES_PATH),
            "adaptation_sha256": support.sha256(support.ADAPTATION_PATH),
        },
        "split": "holdout",
        "conditions_mode": "adapted",
        "deferred_query_ids": list(deferred),
        "query_ids": query_ids,
        "excluded": {
            "deferred_query_ids": list(deferred),
            "deferred_reason": "主会话在批准用途适配中决定暂缓，本次沿用同一暂缓清单，不擅自解决其业务语义",
            "single_path_diagnostics": "bm25/dense 单路分数只作为同条件对照记录，不参与门槛与融合质量结论",
            "expect_empty_queries": {
                "kept": sum(1 for item in selected if item["expect_empty"]),
                "note": "无答案查询保留在 false_return_rate 指标内；threshold=0.0 不对其做任何过滤，该局限保持可见",
            },
        },
        "metric_definitions": {
            "implementation": "test/retrieval_evaluation_support.py:metrics 复用 test/check_retrieval_baseline.py:per_query_record/aggregate",
            "stages": list(support.SCORE_STAGES),
            "recall@10": "stage_metrics.<stage>.overall.recall@10 = 有标注查询 per_query_record.recall@10 的宏平均，分母为该查询 grade>0 的相关集合，只取前 10 候选",
            "mrr@10": "stage_metrics.<stage>.overall.mrr@10 = per_query_record.mrr@10 的宏平均，取首个相关候选名次的倒数",
            "ndcg@10": "stage_metrics.<stage>.overall.ndcg@10 = per_query_record.ndcg@10 的宏平均，以 grade 为增益并按理想排序 DCG 归一",
            "false_return_rate": "stage_metrics.<stage>.overall.false_return_rate = expect_empty 查询中返回任意候选的比例",
            "empty_queries": "projection.empty_queries = 预算裁剪后 returned 为空的查询数",
            "hard_constraint_violations": "gates.<group>.hard_constraint_violations = project_response.hard_constraint_violations 汇总",
            "budget_noncompliant": "gates.<group>.budget_noncompliant = project_response.budget_compliant 为 False 的查询 ID 列表",
        },
        "vector_cache": {
            "path": str(cache_file),
            "sha256": support.sha256(cache_file),
            "identity": identity,
            "entries": len(cached),
            "holdout_covered": len(query_ids),
            "cached_query_ids": len(vectors),
            "source_run_id": VECTOR_SOURCE_RUN_ID,
            "source_requests": {
                "attempted": source["attempted_requests"],
                "successful": source["successful_responses"],
                "total_tokens": source["total_tokens"],
                "unique_normalized_texts": source["unique_normalized_texts"],
            },
        },
        "code_identity": run.fingerprint(),
        "stable_snapshot": {
            "head": snapshot["head"],
            "branch": snapshot["branch"],
            "files": snapshot["files"],
            "versions": snapshot["versions"],
            "identity_note": "captured_at 与工作区脏清单属运行环境记录，不作为算法身份参与复验门禁",
        },
        "index_identity": index_identity,
        "historical_holdout_metrics": historical_stage_metrics(history),
        "historical_holdout_metrics_note": "历史数字仅作登记供差异说明，不参与本次判定，也不得据此挑选参数",
    }
    gate = support.require_requalification(
        auth, frozen, run.fingerprint(), index_identity, params, query_ids, identity
    )
    auth["identity_vs_history"] = gate["identity_diff"]
    auth["compatibility"] = {
        "path": "compatible",
        "adaptation": "compatible",
        "vector_space": "compatible",
        "index": "compatible",
        "params": "compatible",
        "query_ids": "compatible",
    }
    auth["authorized_at"] = support.iso_utc()
    # 写入前用同一批纯断言复核最终授权文档本身；任一门禁不通过就地失败且不留下文件。
    support.require_requalification(
        auth, frozen, run.fingerprint(), index_identity, params, query_ids, identity
    )
    support.REQUAL_ROOT.mkdir(parents=True, exist_ok=True)
    temp = support.REQUAL_AUTH_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, support.REQUAL_AUTH_PATH)
    run.stage("requalify-prepare", auth)
    print(json.dumps({
        "run_id": run.run_id,
        "collection": collection,
        "authorization": str(support.REQUAL_AUTH_PATH),
        "authorization_sha256": support.sha256(support.REQUAL_AUTH_PATH),
        "query_ids": len(query_ids),
        "vector_cache_entries": len(cached),
        "gates": gate["checks"],
        "identity_changed_files": len(gate["identity_diff"]["changed"]),
        "identity_added_files": gate["identity_diff"]["added"],
        "identity_removed_files": gate["identity_diff"]["removed"],
    }, ensure_ascii=False))
    run.finish()
    return 0


async def requalify_holdout(args) -> int:
    # 唯一一次当前源码固定参数正式复验：授权门禁通过并在独立账本登记后才发起质量查询。
    run = support.RunContext.start("requalify-holdout", sys.argv, ENV_NAMES)
    auth = support.load_requalification_authorization()
    frozen = support.load_frozen()
    if args.split != "holdout":
        raise support.EvaluationBlocked("正式复验只接受 holdout 拆分", ("--split holdout",))
    if tuple(args.groups) != GROUPS:
        raise support.EvaluationBlocked("正式复验要求四组同轮执行", (",".join(GROUPS),))
    if args.conditions != "adapted":
        raise support.EvaluationBlocked("正式复验必须使用已批准用途适配", ("--conditions adapted",))
    if (
        args.alpha != frozen["alpha"]
        or args.depth != frozen["bm25_depth"]
        or args.threshold != frozen["threshold"]
    ):
        raise support.EvaluationBlocked(
            "正式复验参数必须等于冻结取值",
            (f"--alpha {frozen['alpha']} --depth {frozen['bm25_depth']} --threshold {frozen['threshold']}",),
        )
    support.REQUAL_ROOT.mkdir(parents=True, exist_ok=True)
    authorization_sha256 = support.sha256(support.REQUAL_AUTH_PATH)
    history = load_evidence_document(support.PARENT_HOLDOUT_DIR / "compare.json", "历史正式留出")
    rpc_stats = {"total": 0, "by_method": {}, "elapsed_ms": []}
    embedding_stats = {"requests": 0, "texts": 0}
    state: dict = {"cache_hit": 0, "cache_miss": 0}
    original_embed = EmbeddingClient._embed

    async def traced_embed(self, texts: list[str]):
        embedding_stats["requests"] += 1
        embedding_stats["texts"] += len(texts)
        return await original_embed(self, texts)

    def gate(frozen_doc, fingerprint, index_identity, params, context):
        require_approved_inputs(support.verify_inputs(context["catalog"]), "正式留出复验")
        state["cache_hit"] = len(context["vectors"])
        state["cache_miss"] = len(context["selected"]) - len(context["vectors"])
        return support.require_requalification(
            auth, frozen_doc, fingerprint, index_identity, params,
            context["query_ids"], context["vector_cache_identity"],
        )

    def finalize(evidence: dict, context: dict) -> None:
        if embedding_stats["requests"] != 0:
            raise support.EvaluationBlocked(
                "正式复验出现新增编码请求，违反零模型请求授权上限", (str(embedding_stats["requests"]),)
            )
        evidence["embedding_requests"] = embedding_stats["requests"]
        evidence["authorization_sha256"] = authorization_sha256
        evidence["parent"] = auth["parent"]
        evidence["identity_vs_history"] = context["frozen_gate_result"]["identity_diff"]
        evidence["observation"] = requal_observation(
            rpc_stats, embedding_stats, state["cache_hit"], state["cache_miss"]
        )
        evidence["requalification_vs_history"] = {
            group: support.compare_groups(history["groups"][group], evidence["groups"][group], context["selected"])
            for group in GROUPS
        }

    EmbeddingClient._embed = traced_embed
    try:
        return await _compare_run(
            run,
            args,
            frozen_gate=gate,
            ledger={
                "key": lambda frozen_doc: support.requalification_ledger_key(auth),
                "begin": support.begin_requalification_execution,
                "complete": support.complete_requalification_execution,
                "abort_status": "failed",
                "note": "operator-authorized-requalification：获准的重复留出观察，不宣称严格未见留出，结果不用于选参",
            },
            observe=lambda db: observe_rpc(db, rpc_stats),
            finalize=finalize,
        )
    except BaseException as error:
        # 失败保留部分证据并把登记状态记为 failed，不自动补跑也不挑选另一轮结果。
        run.write("requalify-holdout-failed", {
            "run_id": run.run_id,
            "authorization_sha256": authorization_sha256,
            "error_type": type(error).__name__,
            "error": str(error)[:600],
            "stages_completed": list(run.finished_stages),
            "observation": requal_observation(rpc_stats, embedding_stats, state["cache_hit"], state["cache_miss"]),
        })
        raise
    finally:
        EmbeddingClient._embed = original_embed


async def vectors(args) -> int:
    run = support.RunContext.start("vectors", sys.argv, ENV_NAMES)
    selected = load_queries("all")
    corpus = RetrievalCorpus.load()
    identity = support.query_vector_identity(embedding_config(), corpus)
    cached_entries = support.read_cached_entries(identity, support.query_texts())
    entries = list(cached_entries.values())
    by_id = {item["id"]: item for item in selected}
    # 独立查询文本按规范化后去重；同一兼容向量在离线网格内复用，缓存命中不计新增请求。
    pending: dict[str, list[str]] = {}
    for query in selected:
        if query["id"] in cached_entries:
            continue
        pending.setdefault(support.normalized_query(query["query"]), []).append(query["id"])
    texts = sorted(pending)
    batches = [texts[start:start + 20] for start in range(0, len(texts), 20)]
    plan = {
        "queries": len(selected),
        "cache_hit": len(cached_entries),
        "uncached_queries": sum(len(ids) for ids in pending.values()),
        "unique_normalized_texts": len(texts),
        "batches": len(batches),
        "batch_limit": 20,
        "approved_request_cap": args.approve_request_cap,
    }
    if len(batches) > args.approve_request_cap:
        run.stage("vectors-blocked", plan)
        print(f"BLOCKED 需要 {len(batches)} 次编码请求，授权上限 {args.approve_request_cap}")
        run.finish()
        return 2
    # 用量台账：尝试按发请求前计数，成功与真实 usage 按响应后分别记录；usage 缺失记 unknown，不以 0 代替。
    usage = {
        "attempted_requests": 0,
        "successful_responses": 0,
        "prompt_tokens": 0,
        "total_tokens": 0,
        "usage_missing_responses": 0,
        "usage_status": "unknown_until_response",
    }
    attempts: list[dict] = []
    async with EmbeddingClient.from_env() as client:
        for index, batch in enumerate(batches, start=1):
            usage["attempted_requests"] += 1
            attempts.append({"batch": index, "size": len(batch), "state": "attempted"})
            run.stage("vectors-progress", {**plan, **usage, "attempting_batch": len(batch)})
            # 走同一 SDK 客户端入口以记录真实返回模型信息；数量、index、维度、有限值与非零范数由 T4 校验器把关。
            response = await client.client.embeddings.create(
                model=client.config.model,
                input=batch,
                dimensions=client.config.dimensions,
                encoding_format="float",
            )
            batch_usage = getattr(response, "usage", None)
            usage["successful_responses"] += 1
            if batch_usage is None:
                usage["usage_missing_responses"] += 1
            else:
                usage["prompt_tokens"] += int(getattr(batch_usage, "prompt_tokens", 0) or 0)
                usage["total_tokens"] += int(getattr(batch_usage, "total_tokens", 0) or 0)
            embeddings = validate_embedding_data(
                response.data, len(batch), client.config.dimensions
            )
            for text, vector in zip(batch, embeddings, strict=True):
                for query_id in pending[text]:
                    entries.append({
                        "id": query_id,
                        "query": by_id[query_id]["query"],
                        "normalized_sha256": support.sha256_text(text),
                        "vector": vector,
                        "model": client.config.model,
                        "requested_model": client.config.model,
                        "provider_response_model": getattr(response, "model", None),
                        "dimensions": client.config.dimensions,
                        "encoding_rule_version": identity["encoding_rule_version"],
                        "encoded_at": support.iso_utc(),
                    })
            attempts[-1]["state"] = "succeeded"
            # 每批校验成功后立即原子保存已完成条目：后续批次失败时已有成功向量仍可读，下次仅编码缺失文本。
            support.write_query_cache(identity, entries)
    path = support.write_query_cache(identity, entries)
    usage["usage_status"] = "reported" if usage["usage_missing_responses"] == 0 else "partially_unknown"
    evidence = {
        **plan,
        **usage,
        "attempts": attempts,
        "cache_path": str(path),
        "cache_entries": len(entries),
        "provider_response_models": sorted({str(item["provider_response_model"]) for item in entries}),
        "identity": identity,
    }
    run.stage("vectors", evidence)
    print(json.dumps({"cache": str(path), "usage": usage}, ensure_ascii=False))
    run.finish()
    return 0


async def grid(args) -> int:
    run = support.RunContext.start("grid", sys.argv, ENV_NAMES)
    if args.conditions != "adapted":
        # 质量网格必须绑定已批准用途适配；explicit-filters 仅作诊断，不进入正式冻结来源。
        raise support.EvaluationBlocked("网格执行必须使用已批准用途适配", ("--conditions adapted",))
    dev = load_queries("dev")
    corpus = RetrievalCorpus.load()
    catalog, mappings = corpus.catalog, rt.load_mappings()
    adaptation, purposes = support.load_adaptation()
    deferred = support.deferred_query_ids(adaptation)
    dev = [item for item in dev if item["id"] not in deferred]
    identity = support.query_vector_identity(embedding_config(), corpus)
    vectors, _cached_entries = support.read_query_cache(identity, dev)
    cells = []
    gate_hard_total = 0
    gate_budget_noncompliant: list[str] = []
    index_identity = None
    collection = None
    async with MilvusConnection.from_env() as db:
        collection, index_identity = await support.open_published(db, corpus)
        rpc_calls = 0
        for alpha, depth in support.grid_cells():
            recalled_by_query = {}
            for query in dev:
                evaluated, _ = resolve_conditions(
                    query, catalog, mappings, args.conditions, purposes, adaptation
                )
                recalled_by_query[query["id"]] = (
                    await support.vector_candidates(
                        db, collection, catalog, query, vectors[query["id"]], evaluated,
                        mode="hybrid", alpha=alpha, bm25_depth=depth, dense_depth=depth,
                    ),
                    evaluated,
                )
                rpc_calls += 1
            thresholds = []
            for threshold in support.THRESHOLD_CANDIDATES:
                returned = {}
                dropped_relevant = 0
                for query in dev:
                    recalled, evaluated = recalled_by_query[query["id"]]
                    projected = support.project_response(
                        catalog, recalled, evaluated, threshold=threshold, scored=True
                    )
                    returned[query["id"]] = projected["returned"]
                    # 门禁在无门槛基线（threshold=0.0，等价全保留）处统计硬约束与预算合规。
                    if threshold == support.THRESHOLD_CANDIDATES[0]:
                        gate_hard_total += len(projected["hard_constraint_violations"])
                        if not projected["budget_compliant"]:
                            gate_budget_noncompliant.append(f"{alpha}/{depth}/{query['id']}")
                    labelled = {label["id"] for label in query["labels"]}
                    dropped_relevant += sum(
                        1 for exercise_id in projected["dropped_by_threshold"] if exercise_id in labelled
                    )
                summary = aggregate([support.per_query_record(query, returned[query["id"]]) for query in dev])
                thresholds.append({
                    "threshold": threshold,
                    "recall@10": summary["recall@10"],
                    "mrr@10": summary["mrr@10"],
                    "ndcg@10": summary["ndcg@10"],
                    "false_return_rate": summary["false_return_rate"],
                    "empty_queries": sum(1 for query in dev if not returned[query["id"]]),
                    "relevant_candidates_dropped": dropped_relevant,
                })
            cells.append({
                "alpha": alpha,
                "depth": depth,
                "bm25_depth": depth,
                "dense_depth": depth,
                "baseline_metrics": next(row for row in thresholds if row["threshold"] == support.THRESHOLD_CANDIDATES[0]),
                "thresholds": thresholds,
            })
    evidence = {
        "split": "dev",
        "queries": len(dev),
        "conditions_mode": args.conditions,
        "adaptation_status": "approved",
        "adaptation_sha256": support.sha256(support.ADAPTATION_PATH),
        "deferred_query_ids": list(deferred),
        "collection": collection,
        "index_identity": index_identity,
        "hybrid_rpc_calls": rpc_calls,
        "embedding_requests": 0,
        "cache_replay_only": True,
        "vector_cache_identity": identity,
        "gates": {
            "hard_constraint_violation_total": gate_hard_total,
            "budget_noncompliant": gate_budget_noncompliant,
        },
        "grid": {
            "alpha": list(support.ALPHA_CANDIDATES),
            "depth": list(support.DEPTH_CANDIDATES),
            "threshold": list(support.THRESHOLD_CANDIDATES),
        },
        "code_identity": run.fingerprint(),
        "run_identity": run.fingerprint(),
        "cells": cells,
    }
    assert gate_hard_total == 0 and not gate_budget_noncompliant, evidence["gates"]
    run.stage("grid", evidence)
    print(json.dumps({"cells": len(cells), "hybrid_rpc_calls": rpc_calls}, ensure_ascii=False))
    run.finish()
    return 0


async def freeze(args) -> int:
    run = support.RunContext.start("freeze", sys.argv, ENV_NAMES)
    if args.grid:
        grid_path = Path(args.grid)
    else:
        candidates = sorted(support.EVAL_ROOT.glob("*/grid.json"), reverse=True)
        if len(candidates) != 1:
            # 不自动挑选旧目录或“可用”网格：存在歧义时要求显式指定来源。
            raise support.EvaluationBlocked(
                "存在多个网格结果，必须显式 --grid 指定来源", (f"{len(candidates)} 个候选",)
            )
        grid_path = candidates[0] if candidates else Path("grid.json")
    document = support.validate_grid_source(grid_path, run.fingerprint())
    corpus = RetrievalCorpus.load()
    async with MilvusConnection.from_env() as db:
        collection, index_identity = await support.open_published(db, corpus)
    if document.get("index_identity") != index_identity:
        run.stage("freeze-blocked", {"grid": str(grid_path), "reason": "当前发布集合或清单指纹与网格来源不一致"})
        print("BLOCKED 当前发布集合或清单指纹与网格来源不一致")
        run.finish()
        return 2
    selection = support.select_frozen(document["cells"])
    if "blocked" in selection:
        run.stage("freeze-blocked", {"grid": str(grid_path), **selection})
        print(f"BLOCKED {selection['blocked']}")
        run.finish()
        return 2
    wiring = support.frozen_config_identity()
    expected = {
        "alpha": selection["alpha"],
        "bm25_depth": selection["bm25_depth"],
        "dense_depth": selection["dense_depth"],
        "threshold": selection["threshold"],
    }
    if any(wiring["values"][key] != value for key, value in expected.items()):
        run.stage("freeze-blocked", {
            "grid": str(grid_path), "reason": "线上冻结参数接线与网格选定值不一致",
            "selection": expected, "wiring": wiring["values"],
        })
        print("BLOCKED 线上冻结参数接线与网格选定值不一致，先完成接线")
        run.finish()
        return 2
    frozen = {
        **selection,
        "split": "dev",
        "conditions_mode": "adapted",
        "grid_evidence": str(grid_path),
        "grid_run_directory": str(grid_path.parent),
        "grid_run_identity": document.get("run_identity"),
        "adaptation_sha256": support.sha256(support.ADAPTATION_PATH),
        "frozen_at": support.iso_utc(),
        "queries_sha256": support.sha256(support.QUERIES_PATH),
        "approved_inputs": support.verify_inputs(support.Catalog.load()),
        "code_identity": run.fingerprint(),
        "frozen_config_identity": wiring,
        "index_identity": index_identity,
        "online_wiring_status": "wired",
        "threshold_scope": "只作用于 WeightedRanker 归一化后的融合分数；单路诊断模式不附加该门槛",
    }
    support.FROZEN_PATH.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    run.stage("freeze", frozen)
    print(json.dumps({key: frozen[key] for key in ("alpha", "bm25_depth", "dense_depth", "threshold")}, ensure_ascii=False))
    run.finish()
    return 0


def analyzer(args) -> int:
    run = support.RunContext.start("analyzer", sys.argv, ENV_NAMES)
    client = MilvusClient(uri=args.uri, token=None, timeout=args.timeout)
    try:
        records = []
        for candidate in ANALYZER_CANDIDATES:
            row = {"label": candidate["label"], "analyzer_params": candidate["params"], "samples": []}
            try:
                for sample in ANALYZER_SAMPLES:
                    # pymilvus 3.0.2 的 run_analyzer 返回 AnalyzeResult，token 列表在 .tokens 上。
                    tokens = list(client.run_analyzer(sample, analyzer_params=candidate["params"]).tokens)
                    row["samples"].append({"sample": sample, "tokens": tokens, "count": len(tokens)})
                row["status"] = "ok"
            except Exception as error:  # 观测真实服务能力边界：逐条记录服务端原始错误类型与文本
                row["status"] = "rejected"
                row["error_type"] = type(error).__name__
                row["error"] = str(error)[:400]
            records.append(row)
        evidence = {
            "server_version": client.get_server_version(),
            "sdk_version": pymilvus.__version__,
            "published_analyzer": ANALYZER,
            "published_analyzer_samples": records[0],
            "candidates": records,
            "note": "run_analyzer 为只读分词观测；正式索引不可变，analyzer 索引对照需归属 T8 的隔离探针集合另行执行",
        }
        run.stage("analyzer", evidence)
        print(json.dumps({
            "server_version": evidence["server_version"],
            "statuses": {row["label"]: row["status"] for row in records},
        }, ensure_ascii=False))
        return 0
    finally:
        client.close()
        run.finish()


def smoke(args) -> int:
    # 管线自检：使用已校验集合内一条目录记录的真实向量与其文本，只证明代码路径跑通，不作为质量证据。
    run = support.RunContext.start("smoke", sys.argv, ENV_NAMES)
    evidence = {"quality_evidence": False, "vector_source": "已发布集合内目录记录的真实 dense 向量"}

    async def execute() -> None:
        corpus = RetrievalCorpus.load()
        catalog = corpus.catalog
        async with MilvusConnection.from_env() as db:
            collection, index_identity = await support.open_published(db, corpus)
            evidence["index_identity"] = index_identity
            record = corpus.records[0]
            rows = await db.call(
                "get", collection, ids=[record["id"]], output_fields=["dense"], consistency_level="Strong"
            )
            vector = rows[0]["dense"]
            exercise = catalog.get(record["id"])
            query = {
                "id": record["id"],
                "query": exercise.name,
                "labels": [],
                "filters": dict.fromkeys(rt.MAPPING_FIELDS),
            }
            evaluated = support.explicit_filter_conditions(query)
            groups = {}
            for mode in ("bm25", "dense", "hybrid"):
                recalled = await support.vector_candidates(
                    db, collection, catalog, query, vector, evaluated,
                    mode=mode, alpha=0.5, bm25_depth=20, dense_depth=20,
                )
                projected = support.project_response(
                    catalog, recalled, evaluated,
                    threshold=0.5 if mode == "hybrid" else None, scored=mode == "hybrid",
                )
                groups[mode] = {
                    "recalled": len(recalled),
                    "after_threshold": len(projected["after_threshold"]),
                    "returned": len(projected["returned"]),
                    "chars": projected["serialized_chars"],
                    "hard_constraint_violations": projected["hard_constraint_violations"],
                    "scores": score_scale([candidate.score for candidate in recalled]),
                }
            evidence["groups"] = groups
            evidence["collection"] = collection
            evidence["embedding_requests"] = 0

    asyncio.run(execute())
    run.stage("smoke", evidence)
    print(json.dumps({key: value["returned"] for key, value in evidence["groups"].items()}, ensure_ascii=False))
    run.finish()
    return 0


def inputs(args) -> int:
    run = support.RunContext.start("inputs", sys.argv, ENV_NAMES)
    verification = support.verify_inputs(support.Catalog.load())
    adaptation_status = "missing"
    pending: list = []
    if support.ADAPTATION_PATH.is_file():
        document = support.load_document(support.ADAPTATION_PATH)
        adaptation_status = document["status"]
        pending = document.get("open_questions", [])
    else:
        pending = ["缺少 backend/dataset/action-retrieval-purpose-adaptation.json"]
    frozen = support.load_document(support.FROZEN_PATH) if support.FROZEN_PATH.is_file() else None
    evidence = {
        "approved_inputs": verification,
        "adaptation_status": adaptation_status,
        "adaptation_path": str(support.ADAPTATION_PATH),
        "open_questions": pending,
        "frozen_parameters": (
            {key: frozen.get(key) for key in ("alpha", "bm25_depth", "dense_depth", "threshold", "online_wiring_status")}
            if frozen else None
        ),
        "grid": {
            "alpha": list(support.ALPHA_CANDIDATES),
            "depth": list(support.DEPTH_CANDIDATES),
            "threshold": list(support.THRESHOLD_CANDIDATES),
        },
        "run_identity": run.identity,
    }
    assert verification["checks"]["passed"], verification["checks"]
    run.stage("inputs", evidence)
    print(json.dumps({
        "checks_passed": verification["checks"]["passed"],
        "counts": verification["counts"],
        "adaptation_status": adaptation_status,
        "frozen": bool(frozen),
    }, ensure_ascii=False))
    return 0


def selfcheck(args) -> int:
    run = support.RunContext.start("selfcheck", sys.argv, ENV_NAMES)
    checks: dict = {}
    catalog = support.Catalog.load()

    def _blocked(callable_obj, *call_args, **call_kwargs) -> bool:
        try:
            callable_obj(*call_args, **call_kwargs)
            return False
        except support.EvaluationBlocked:
            return True

    item = {
        "id": "q-selfcheck", "intent": "alias", "split": "dev", "family": "selfcheck",
        "purpose": "search", "baseline_name": "自检", "filters": dict.fromkeys(rt.MAPPING_FIELDS),
        "expect_empty": False, "label_status": "approved",
        "labels": [{"id": "0001", "grade": 3}, {"id": "0002", "grade": 1}, {"id": "0003", "grade": 0}],
    }
    row = support.per_query_record(item, ["0009", "0002", "0001"])
    checks["grade_positive_only_relevance"] = sorted(row["relevant"]) == ["0001", "0002"]
    checks["recall_denominator_is_relevant_set"] = abs(row["recall@10"] - 1.0) < 1e-12
    checks["mrr_uses_first_relevant_rank"] = abs(row["mrr@10"] - 0.5) < 1e-12
    checks["ndcg_le_one"] = 0.0 <= row["ndcg@10"] <= 1.0
    beyond = support.per_query_record(item, [exercise.id for exercise in catalog.all()[100:115]])
    checks["metrics_only_first_ten"] = len(beyond["top10"]) == 10 and beyond["recall@10"] == 0.0
    checks["mrr_zero_without_hit"] = beyond["mrr@10"] == 0.0
    empty_gold = dict(item, labels=[], expect_empty=True)
    checks["empty_gold_skips_ranking_metrics"] = all(
        key not in support.per_query_record(empty_gold, []) for key in ("recall@10", "mrr@10", "ndcg@10")
    )
    checks["expect_empty_counts_any_candidate"] = support.per_query_record(empty_gold, ["0001"])["false_return"] is True
    checks["expect_empty_legal_empty_result"] = support.per_query_record(empty_gold, [])["false_return"] is False
    checks["false_return_rate_denominator"] = aggregate([
        support.per_query_record(empty_gold, ["0001"]), support.per_query_record(empty_gold, [])
    ])["false_return_rate"] == 0.5

    candidates = [
        RetrievalCandidate(id="0002", score=0.5, name="乙", **dict.fromkeys(rt.MAPPING_FIELDS, "x")),
        RetrievalCandidate(id="0001", score=0.4, name="甲", **dict.fromkeys(rt.MAPPING_FIELDS, "x")),
    ]
    checks["threshold_boundary_inclusive"] = [
        candidate.id for candidate in support.apply_threshold(candidates, 0.4)
    ] == ["0002", "0001"]
    checks["threshold_above_all_drops_everything"] = support.apply_threshold(candidates, 0.9) == []
    checks["threshold_none_keeps_candidates"] = support.apply_threshold(candidates, None) == candidates

    evaluated = support.explicit_filter_conditions({"filters": dict.fromkeys(rt.MAPPING_FIELDS)})
    empty_projection = support.project_response(catalog, [], evaluated, threshold=None, scored=False)
    checks["empty_projection_legal"] = (
        empty_projection["returned"] == [] and empty_projection["has_more"] is False
        and empty_projection["remaining_ids"] == [] and empty_projection["budget_compliant"] is True
        and empty_projection["serialized_valid_json"] is True
    )
    rows = candidate_rows(catalog, 40)
    unfiltered = support.project_response(catalog, rows, evaluated, threshold=None, scored=False)
    filtered = support.project_response(catalog, rows, evaluated, threshold=0.995, scored=True)
    checks["projection_budget_compliant"] = (
        unfiltered["budget_compliant"] and unfiltered["serialized_chars"] <= support.SEARCH_BUDGET
        and len(unfiltered["returned"]) <= 10
    )
    checks["projection_records_every_drop"] = (
        set(unfiltered["recalled"])
        == set(unfiltered["returned"]) | set(unfiltered["dropped_by_budget"])
    )
    checks["projection_threshold_drop_recorded"] = (
        set(filtered["dropped_by_threshold"])
        == set(filtered["recalled"]) - set(filtered["after_threshold"])
        and filtered["after_threshold"] != filtered["recalled"]
    )
    checks["projection_keeps_full_candidates_before_budget"] = (
        unfiltered["after_threshold"] == unfiltered["recalled"]
        and unfiltered["remaining_ids"] == unfiltered["recalled"][len(unfiltered["returned"]):]
    )
    checks["remaining_ids_only_recalled"] = len(unfiltered["remaining_ids"]) + len(unfiltered["returned"]) == len(unfiltered["recalled"])
    checks["hard_constraint_violations_zero"] = unfiltered["hard_constraint_violations"] == []

    identity = {
        "model": "selfcheck-model", "dimensions": 8, "encoding_rule_version": "selfcheck",
        "normalization": "NFKC+whitespace-collapse+casefold",
        "service_fingerprint": support.endpoint_fingerprint("https://selfcheck.example/compatible-mode/v1"),
        "service_class": "selfcheck.example/compatible-mode/v1",
        "sources": {}, "text_sha256": "selfcheck",
    }
    vector = [1.0] + [0.0] * 7
    text = "杠铃卧推"
    query = {"id": "q-selfcheck", "query": text}
    entry = {
        "id": "q-selfcheck", "query": text,
        "normalized_sha256": support.sha256_text(support.normalized_query(text)),
        "vector": vector, "model": identity["model"],
    }
    cache = support.cache_path(identity)
    cache.parent.mkdir(parents=True, exist_ok=True)

    def write_cache(entries: list[dict]) -> None:
        cache.write_text(json.dumps({"identity": identity, "entries": entries}), encoding="utf-8")

    write_cache([entry])
    checks["cache_absent_reads_empty"] = support.read_cached_entries({**identity, "model": "no-such-model"}) == {}
    checks["cache_identity_has_no_workspace_host"] = (
        "service_host" not in identity and len(identity["service_fingerprint"]) == 64
    )
    try:
        cached_vectors, cached_entries = support.read_query_cache(identity, [query])
        checks["cache_compatible_identity_reads"] = (
            cached_vectors == {"q-selfcheck": vector} and cached_entries["q-selfcheck"]["id"] == "q-selfcheck"
        )
        for label, mutated in (
            ("model", {**identity, "model": "other"}),
            ("sources", {**identity, "sources": {"x": 1}}),
            ("encoding", {**identity, "encoding_rule_version": "other"}),
            ("service", {**identity, "service_fingerprint": "0" * 64}),
        ):
            checks[f"cache_{label}_mismatch_blocked"] = _blocked(support.read_query_cache, mutated, [query])
        checks["cache_missing_query_blocked"] = _blocked(
            support.read_query_cache, identity, [query, {"id": "q-missing", "query": "缺失"}]
        )
        checks["cache_changed_query_blocked"] = _blocked(
            support.read_query_cache, identity, [{"id": "q-selfcheck", "query": "换了文本"}]
        )
        other_a, other_b = "动作甲", "动作乙"
        write_cache([
            {**entry, "id": "q-selfcheck", "query": other_a,
             "normalized_sha256": support.sha256_text(support.normalized_query(other_a))},
            {**entry, "id": "q-other", "query": other_b,
             "normalized_sha256": support.sha256_text(support.normalized_query(other_b))},
        ])
        checks["cache_swapped_id_blocked"] = _blocked(
            support.read_cached_entries, identity, {"q-selfcheck": other_b, "q-other": other_a}
        )
        write_cache([dict(entry, query="篡改文本", normalized_sha256="0" * 64)])
        checks["cache_tampered_input_hash_rejected"] = _blocked(support.read_cached_entries, identity)
        write_cache([dict(entry, vector=[0.0] * 8)])
        checks["cache_zero_norm_vector_rejected"] = _blocked(support.read_cached_entries, identity)
    finally:
        cache.unlink()
    for label, candidate_vector in (
        ("dimensions", [1.0] * 7),
        ("nonfinite", [float("nan")] + [0.0] * 7),
        ("zero_norm", [0.0] * 8),
        ("type", ["1", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
    ):
        try:
            support.validate_cached_vector(candidate_vector, 8)
            checks[f"cache_vector_{label}_rejected"] = False
        except support.EvaluationBlocked:
            checks[f"cache_vector_{label}_rejected"] = True

    approved_adaptation, purposes = support.load_adaptation()
    checks["adaptation_approved_loads_purposes"] = (
        len(purposes) == len(approved_adaptation["rows"])
        and set(purposes.values()) <= set(support.ONLINE_PURPOSES)
    )
    checks["adaptation_deferred_recorded"] = set(support.deferred_query_ids(approved_adaptation)) == {
        "q-cond-002", "q-cond-006", "q-cond-015",
    }
    original_adaptation = support.ADAPTATION_PATH
    pending_path = run.directory / "selfcheck-adaptation-pending.json"
    pending_path.write_text(json.dumps({"status": "pending_main_session_approval"}), encoding="utf-8")
    support.ADAPTATION_PATH = pending_path
    try:
        checks["adaptation_unapproved_blocked"] = _blocked(support.load_adaptation)
    finally:
        support.ADAPTATION_PATH = original_adaptation
    support.ADAPTATION_PATH = run.directory / "selfcheck-adaptation-missing.json"
    try:
        checks["adaptation_missing_blocked"] = _blocked(support.load_adaptation)
    finally:
        support.ADAPTATION_PATH = original_adaptation

    for label, arguments in (
        ("missing_name", {"purpose": "lookup"}),
        ("unknown_purpose", {"name": "卧推", "purpose": "search"}),
        ("blank_name", {"name": " ", "purpose": "lookup"}),
    ):
        try:
            CatalogSearchArguments.model_validate(arguments)
            checks[f"arguments_{label}_rejected"] = False
        except Exception:
            checks[f"arguments_{label}_rejected"] = True
    restricted_query = {
        "id": "q-selfcheck", "query": "卧推", "environment": None, "declared_equipment": [],
        "filters": dict.fromkeys(rt.MAPPING_FIELDS),
        "saved_profile_restrictions": {"unavailable_equipment": ["dumbbell"], "forbidden_exercise_ids": []},
    }
    generation = support.effective_conditions(restricted_query, catalog, rt.load_mappings(), "generation", {})
    lookup = support.effective_conditions(restricted_query, catalog, rt.load_mappings(), "lookup", {})
    checks["generation_applies_saved_unavailable"] = "dumbbell" not in generation.conditions.available_equipment
    checks["lookup_ignores_saved_unavailable"] = "dumbbell" in lookup.conditions.available_equipment
    checks["generation_and_lookup_share_explicit_fields"] = all(
        getattr(generation.conditions, field) is None and getattr(lookup.conditions, field) is None
        for field in rt.MAPPING_FIELDS
    )
    try:
        support.effective_conditions(restricted_query, catalog, rt.load_mappings(), "generation", {"excluded_ids": ["9999"]})
        checks["outside_catalog_reference_rejected"] = False
    except Exception as error:
        checks["outside_catalog_reference_rejected"] = type(error).__name__
    try:
        build_details_result(["9999"], catalog)
        checks["details_unknown_id_rejected"] = False
    except ExerciseNotFound:
        checks["details_unknown_id_rejected"] = True
    for label, ids in (
        ("duplicate", ["0001", "0001"]),
        ("over_limit", [exercise.id for exercise in catalog.all()[:6]]),
        ("empty", []),
    ):
        try:
            ExerciseDetailsArguments.model_validate({"ids": ids})
            checks[f"details_{label}_rejected"] = False
        except Exception:
            checks[f"details_{label}_rejected"] = True
    try:
        build_details_result([catalog.all()[0].id], catalog, budget=100)
        checks["details_single_over_budget_rejected"] = False
    except ExerciseDetailsTooLarge:
        checks["details_single_over_budget_rejected"] = True

    def cell(alpha: float, depth: int, quality: float, false_rows: list[float], drops: list[int]):
        thresholds = [
            {
                "threshold": threshold, "recall@10": quality, "mrr@10": quality, "ndcg@10": quality,
                "false_return_rate": value, "relevant_candidates_dropped": drop, "empty_queries": 0,
            }
            for threshold, value, drop in zip(
                support.THRESHOLD_CANDIDATES, false_rows, drops, strict=True
            )
        ]
        return {
            "alpha": alpha, "depth": depth, "bm25_depth": depth, "dense_depth": depth,
            "baseline_metrics": thresholds[0], "thresholds": thresholds,
        }

    permissive = [0.5 if index == 0 else 0.0 for index in range(len(support.THRESHOLD_CANDIDATES))]
    risky = [0.25 if index < 6 else 0.1 for index in range(len(support.THRESHOLD_CANDIDATES))]
    risky_drops = [0] * 8 + [3] * (len(support.THRESHOLD_CANDIDATES) - 8)
    cells = [
        cell(0.0, 20, 0.4, permissive, [0] * len(support.THRESHOLD_CANDIDATES)),
        cell(1.0, 100, 0.9, risky, risky_drops),
    ]
    selection = support.select_frozen(cells)
    checks["freeze_prefers_quality_cell"] = selection.get("alpha") == 1.0 and selection.get("bm25_depth") == 100
    checks["freeze_picks_lowest_false_return_among_no_drop"] = selection.get("threshold") == 0.3
    blocked_cells = [dict(cells[1], thresholds=[
        {"threshold": 0.0, "recall@10": 0.9, "mrr@10": 0.9, "ndcg@10": 0.9,
         "false_return_rate": 0.0, "relevant_candidates_dropped": 2, "empty_queries": 0},
        {"threshold": 0.05, "recall@10": 0.9, "mrr@10": 0.9, "ndcg@10": 0.9,
         "false_return_rate": 0.0, "relevant_candidates_dropped": 1, "empty_queries": 0},
    ])]
    checks["freeze_blocked_when_no_threshold_keeps_relevant"] = "blocked" in support.select_frozen(blocked_cells)
    checks["selection_rule_recorded"] = selection.get("selection_rule", {}).get("stage2_rule", "") != ""

    identity_snapshot = support.run_identity()
    checks["identity_records_head_and_file_hashes"] = (
        bool(identity_snapshot["head"])
        and all(len(value) == 64 for value in identity_snapshot["files"].values())
    )
    checks["identity_missing_file_blocks"] = _blocked(
        support.snapshot_file_hashes, ("app/application/business/does-not-exist.py",)
    )
    checks["identity_covers_four_group_sources"] = all(
        key in identity_snapshot["files"] for key in (
            "app/application/business/catalog.py",
            "app/application/business/retrieval_search.py",
            "dataset/action-retrieval-queries.json",
        )
    )

    complete_cells = [
        {
            "alpha": alpha, "depth": depth, "bm25_depth": depth, "dense_depth": depth,
            "baseline_metrics": {"recall@10": 0.5},
            "thresholds": [{"threshold": value} for value in support.THRESHOLD_CANDIDATES],
        }
        for alpha in support.ALPHA_CANDIDATES for depth in support.DEPTH_CANDIDATES
    ]
    checks["grid_cells_complete_accepted"] = not _blocked(support.validate_grid_cells, complete_cells)
    checks["grid_cells_incomplete_rejected"] = _blocked(support.validate_grid_cells, complete_cells[:-1])
    checks["grid_cells_missing_baseline_rejected"] = _blocked(
        support.validate_grid_cells,
        [dict(complete_cells[0], baseline_metrics={"recall@10": None}), *complete_cells[1:]],
    )

    def write_grid(name: str, payload: dict) -> Path:
        path = run.directory / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    # 负例使用隔离副本写入自检运行目录，不修改正在运行的共享源码或金标。
    checks["freeze_rejects_grid_without_complete_run"] = _blocked(
        support.validate_grid_source,
        write_grid("selfcheck-grid-adapted.json", {"split": "dev", "conditions_mode": "adapted"}),
        {},
    )
    checks["freeze_rejects_diagnostic_grid"] = _blocked(
        support.validate_grid_source,
        write_grid("selfcheck-grid-diagnostic.json", {"split": "dev", "conditions_mode": "explicit-filters"}),
        {},
    )
    checks["freeze_rejects_wrong_split"] = _blocked(
        support.validate_grid_source,
        write_grid("selfcheck-grid-holdout.json", {"split": "holdout", "conditions_mode": "adapted"}),
        {},
    )

    # 冻结原件缺失门禁：用隔离路径证明拒绝正式留出；真实冻结原件已存在时该负例依然成立。
    original_frozen = support.FROZEN_PATH
    support.FROZEN_PATH = run.directory / "selfcheck-frozen-missing.json"
    try:
        checks["holdout_requires_frozen_file"] = _blocked(support.load_frozen)
    finally:
        support.FROZEN_PATH = original_frozen
    synthetic_frozen = {
        "alpha": 0.5, "bm25_depth": 50, "dense_depth": 50, "threshold": None,
        "code_identity": {"head": "h", "file_sha256": {}, "dirty": False},
        "frozen_config_identity": {"sha256": "0" * 64}, "adaptation_sha256": "0" * 64,
        "index_identity": {"collection": "c", "manifest_sha256": "m"},
    }
    checks["holdout_stale_fingerprint_blocked"] = _blocked(
        support.require_frozen_holdout, synthetic_frozen,
        {"head": "other", "file_sha256": {}, "dirty": False}, synthetic_frozen["index_identity"],
        {"alpha": 0.5, "depth": 50, "threshold": None, "conditions": "adapted"},
    )

    original_ledger = support.HOLDOUT_LEDGER_PATH
    support.HOLDOUT_LEDGER_PATH = run.directory / "selfcheck-holdout-ledger.json"
    try:
        support.begin_holdout_execution("same-key", "selfcheck-1")
        checks["holdout_duplicate_execution_blocked"] = _blocked(
            support.begin_holdout_execution, "same-key", "selfcheck-2"
        )
    finally:
        support.HOLDOUT_LEDGER_PATH = original_ledger

    # 复验门禁负例：夹具全部写入自检运行目录与独立缓存身份，绝不触碰真实 REQUAL_* 路径、旧留出账本或网络。
    historical_ids = support.historical_holdout_query_ids()
    checks["requal_historical_ids_sorted_66"] = (
        len(historical_ids) == 66 and historical_ids == sorted(set(historical_ids))
    )
    checks["identity_covers_shared_agent_tool"] = "app/agent/tool.py" in identity_snapshot["files"]
    frozen_document = support.load_document(support.FROZEN_PATH)
    wiring = support.frozen_config_identity()
    requal_index = {"collection": "selfcheck-collection", "manifest_sha256": "selfcheck-manifest"}
    requal_params = {"alpha": 0.5, "depth": 100, "threshold": 0.0, "conditions": "adapted"}
    write_cache([entry])
    parent_digests = support.requalification_parent_digests(cache)
    requal_frozen = {
        "alpha": 0.5, "bm25_depth": 100, "dense_depth": 100, "threshold": 0.0,
        "queries_sha256": support.sha256(support.QUERIES_PATH),
        "adaptation_sha256": support.sha256(support.ADAPTATION_PATH),
        "frozen_config_identity": wiring,
        "index_identity": requal_index,
        "code_identity": frozen_document["code_identity"],
    }
    requal_auth = {
        "scope": support.REQUAL_SCOPE,
        "request_caps": dict(support.REQUAL_REQUEST_CAPS),
        "run_budget": {
            "formal_holdout_requalifications": 1, "dev_grid_runs": 0, "analyzer_ablations": 0,
            "embedding_requests": 0, "chat_requests": 0, "summary_requests": 0,
        },
        "frozen_parameters": {"alpha": 0.5, "bm25_depth": 100, "dense_depth": 100, "threshold": 0.0},
        "frozen_config_identity": wiring,
        "approved_inputs": {
            "queries_sha256": requal_frozen["queries_sha256"],
            "adaptation_sha256": requal_frozen["adaptation_sha256"],
        },
        "query_ids": list(historical_ids),
        "vector_cache": {
            "path": str(cache), "sha256": support.sha256(cache), "identity": identity,
            "entries": 128, "holdout_covered": len(historical_ids),
        },
        "code_identity": run.fingerprint(),
        "index_identity": requal_index,
        "parent": parent_digests,
    }

    def requal_gate(
        auth=requal_auth, frozen=requal_frozen, current=None, index=requal_index,
        params=requal_params, query_ids=None, cache_identity=identity,
    ) -> None:
        support.require_requalification(
            auth, frozen, run.fingerprint() if current is None else current, index, params,
            list(historical_ids) if query_ids is None else query_ids, cache_identity,
        )

    def changed_auth(**changes) -> dict:
        return dict(requal_auth, **changes)

    def changed_frozen(**changes) -> dict:
        return dict(requal_frozen, **changes)

    def changed_parent(name: str, digest: str) -> dict:
        record = dict(parent_digests[name], sha256=digest)
        return {**parent_digests, name: record}

    def changed_cache(**changes) -> dict:
        return {**requal_auth["vector_cache"], **changes}

    gate_mutations = {
        "scope": {"auth": changed_auth(scope="ordinary-holdout")},
        "request_caps": {"auth": changed_auth(request_caps={"embedding": 6, "chat": 0, "summary": 0})},
        "parent_frozen_sha256": {"auth": changed_auth(parent=changed_parent("frozen_parameters", "0" * 64))},
        "legacy_ledger_sha256": {"auth": changed_auth(parent=changed_parent("legacy_holdout_ledger", "1" * 64))},
        "frozen_parameters": {"frozen": changed_frozen(threshold=0.05)},
        "registered_frozen_parameters": {"auth": changed_auth(
            frozen_parameters={**requal_auth["frozen_parameters"], "alpha": 0.7}
        )},
        "frozen_config_sha256": {"frozen": changed_frozen(
            frozen_config_identity={**wiring, "sha256": "2" * 64}
        )},
        "frozen_config_values": {"frozen": changed_frozen(
            frozen_config_identity={**wiring, "values": {**wiring["values"], "bm25_depth": 50}}
        )},
        "registered_frozen_config": {"auth": changed_auth(frozen_config_identity={**wiring, "sha256": "3" * 64})},
        "adaptation_sha256": {"frozen": changed_frozen(adaptation_sha256="4" * 64)},
        "queries_sha256": {"frozen": changed_frozen(queries_sha256="5" * 64)},
        "approved_inputs_hash": {"auth": changed_auth(approved_inputs={
            **requal_auth["approved_inputs"], "queries_sha256": "6" * 64
        })},
        "vector_cache_identity": {"auth": changed_auth(vector_cache=changed_cache(
            identity={**identity, "model": "other-model"}
        ))},
        "vector_cache_sha256": {"auth": changed_auth(vector_cache=changed_cache(sha256="7" * 64))},
        "vector_cache_path": {"auth": changed_auth(vector_cache=changed_cache(
            path=str(run.directory / "other-cache.json")
        ))},
        "vector_cache_coverage": {"auth": changed_auth(vector_cache=changed_cache(holdout_covered=65))},
        "vector_cache_argument_identity": {"cache_identity": {**identity, "model": "other-model"}},
        "index_identity_registered": {"auth": changed_auth(index_identity={**requal_index, "collection": "other"})},
        "index_identity_frozen": {"frozen": changed_frozen(
            index_identity={**requal_index, "manifest_sha256": "8" * 64}
        )},
        "index_identity_current": {"index": {"collection": "current", "manifest_sha256": "9" * 64}},
        "code_identity": {"auth": changed_auth(code_identity={"head": "h", "file_sha256": {}, "dirty": False})},
        "query_ids_registered": {"auth": changed_auth(query_ids=historical_ids[:-1])},
        "query_ids_argument": {"query_ids": historical_ids[:65]},
        "conditions_mode": {"params": {**requal_params, "conditions": "explicit-filters"}},
        "alpha_override": {"params": {**requal_params, "alpha": 0.7}},
        "depth_override": {"params": {**requal_params, "depth": 50}},
        "threshold_override": {"params": {**requal_params, "threshold": 0.05}},
    }
    for label, changes in gate_mutations.items():
        checks[f"requal_gate_{label}_blocked"] = _blocked(requal_gate, **changes)
    checks["requal_gate_accepts_bound_identity"] = not _blocked(requal_gate)
    identity_diff = support.require_requalification(
        requal_auth, requal_frozen, run.fingerprint(), requal_index, requal_params,
        list(historical_ids), identity,
    )["identity_diff"]
    checks["requal_identity_diff_shape"] = (
        set(identity_diff) == {"changed", "added", "removed", "head_equal"}
        and isinstance(identity_diff["head_equal"], bool)
        and identity_diff["added"] == sorted(identity_diff["added"])
        and identity_diff["removed"] == sorted(identity_diff["removed"])
        and all(
            set(value) == {"historical", "current"}
            and all(len(digest) == 64 for digest in value.values())
            for value in identity_diff["changed"].values()
        )
    )
    checks["requal_ledger_key_binds_identity"] = (
        support.requalification_ledger_key(requal_auth)
        != support.requalification_ledger_key(changed_auth(query_ids=historical_ids[:65]))
    )

    cache.unlink()
    checks["requal_missing_vector_cache_file_blocked"] = _blocked(requal_gate)
    checks["requal_parent_digests_missing_file_blocked"] = _blocked(
        support.requalification_parent_digests, run.directory / "absent-vector-cache.json"
    )
    write_cache([entry])

    original_auth_file = support.REQUAL_AUTH_PATH
    original_requal_ledger = support.REQUAL_LEDGER_PATH
    legacy_ledger_sha = support.sha256(support.HOLDOUT_LEDGER_PATH)
    support.REQUAL_AUTH_PATH = run.directory / "selfcheck-requal-authorization.json"
    support.REQUAL_LEDGER_PATH = run.directory / "selfcheck-requal-executions.json"
    try:
        checks["requal_missing_authorization_blocked"] = _blocked(support.load_requalification_authorization)
        support.REQUAL_AUTH_PATH.write_text(
            json.dumps(requal_auth, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        requal_key = support.requalification_ledger_key(requal_auth)
        registration = support.begin_requalification_execution(requal_key, run.run_id)
        checks["requal_registration_records_running"] = (
            registration["status"] == "running"
            and len(registration["authorization_sha256"]) == 64
        )
        checks["requal_duplicate_registration_blocked"] = _blocked(
            support.begin_requalification_execution, requal_key, "selfcheck-2"
        )
        checks["requal_complete_unknown_key_blocked"] = _blocked(
            support.complete_requalification_execution, "other-key", "completed"
        )
        support.complete_requalification_execution(requal_key, "completed")
        requal_ledger = support.load_document(support.REQUAL_LEDGER_PATH)
        checks["requal_registration_completed"] = requal_ledger["executions"][0]["status"] == "completed"
        checks["requal_illegal_finish_status_blocked"] = _blocked(
            support.complete_requalification_execution, requal_key, "aborted"
        )
    finally:
        support.REQUAL_AUTH_PATH = original_auth_file
        support.REQUAL_LEDGER_PATH = original_requal_ledger
    checks["requal_legacy_holdout_ledger_unchanged"] = (
        support.sha256(support.HOLDOUT_LEDGER_PATH) == legacy_ledger_sha
        and original_requal_ledger != support.HOLDOUT_LEDGER_PATH
        and original_auth_file != support.HOLDOUT_LEDGER_PATH
    )
    rebinding_path = run.directory / "selfcheck-adaptation-rebinding.json"
    rebinding_path.write_text(
        json.dumps(dict(approved_adaptation, queries_sha256="0" * 64), ensure_ascii=False), encoding="utf-8"
    )
    support.ADAPTATION_PATH = rebinding_path
    try:
        checks["requal_adaptation_rebinding_blocked"] = _blocked(support.load_adaptation)
    finally:
        support.ADAPTATION_PATH = original_adaptation

    checks["inputs_gate_accepts_approved"] = not _blocked(
        require_approved_inputs, support.verify_inputs(catalog), "自检"
    )
    gold = support.load_document(support.QUERIES_PATH)
    tampered = json.loads(json.dumps(gold))
    tampered["queries"][0]["query"] = tampered["queries"][0]["query"] + "自检篡改"
    tampered_path = run.directory / "selfcheck-queries-tampered.json"
    tampered_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    original_queries = support.QUERIES_PATH
    support.QUERIES_PATH = tampered_path
    try:
        checks["inputs_gate_rejects_tampered_gold"] = _blocked(
            require_approved_inputs, support.verify_inputs(catalog), "自检"
        )
    finally:
        support.QUERIES_PATH = original_queries
    cache.unlink()

    failed = sorted(
        key for key, value in checks.items() if value is False or value is None
    )
    summary = {"passed": not failed, "failed": failed, "checks": checks}
    run.stage("selfcheck", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failed:
        print(f"FAIL 自检未通过: {failed}")
        run.finish()
        return 1
    print(f"PASS: 纯函数自检 {len(checks)} 项")
    run.finish()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="动作检索 T8 专项评测入口")
    parser.add_argument(
        "mode",
        choices=(
            "inputs", "selfcheck", "compare", "vectors", "grid", "freeze",
            "analyzer", "smoke", "requalify-prepare", "requalify-holdout",
        ),
    )
    parser.add_argument("--split", default="all", choices=("dev", "holdout", "all"))
    parser.add_argument("--groups", default=",".join(GROUPS))
    parser.add_argument("--conditions", default="explicit-filters", choices=("explicit-filters", "adapted"))
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--depth", type=int, default=50)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--approve-request-cap", type=int, default=0)
    parser.add_argument("--grid", default=None)
    parser.add_argument("--uri", default="http://127.0.0.1:19530")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    requested = tuple(item for item in args.groups.split(",") if item)
    if any(group not in GROUPS for group in requested):
        raise SystemExit(f"非法对照组: {requested}")
    args.groups = requested
    if args.threshold is not None and not 0.0 <= args.threshold <= 1.0:
        raise SystemExit("门槛必须为融合分数区间 [0,1] 内的数值")
    try:
        if args.mode == "selfcheck":
            return selfcheck(args)
        if args.mode == "inputs":
            return inputs(args)
        if args.mode == "analyzer":
            return analyzer(args)
        if args.mode == "smoke":
            return smoke(args)
        if args.mode == "freeze":
            return asyncio.run(freeze(args))
        if args.mode == "compare":
            return asyncio.run(compare(args))
        if args.mode == "vectors":
            return asyncio.run(vectors(args))
        if args.mode == "requalify-prepare":
            return asyncio.run(requalify_prepare(args))
        if args.mode == "requalify-holdout":
            return asyncio.run(requalify_holdout(args))
        return asyncio.run(grid(args))
    except support.EvaluationBlocked as blocked:
        missing = " 缺少: " + ", ".join(blocked.missing) if blocked.missing else ""
        print(f"BLOCKED {blocked.reason}{missing}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
