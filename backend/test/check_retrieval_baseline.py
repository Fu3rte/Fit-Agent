import json
import math
import subprocess
from pathlib import Path

from app.agent.budget import CHARS_PER_TOKEN
from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog

DATASET = Path(__file__).resolve().parents[1] / "dataset"
QUERIES = DATASET / "action-retrieval-queries.json"
REPO = Path(__file__).resolve().parents[2]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def est_tokens(chars: int) -> int:
    # 与 app.agent.budget.CHARS_PER_TOKEN 相同的估算口径，非真实分词器计数。
    return math.ceil(chars / CHARS_PER_TOKEN)


def percentile(values: list[int], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return float(ordered[low])
    return ordered[low] * (high - position) + ordered[high] * (position - low)


def dcg(grades: list[int]) -> float:
    return sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(grades))


def run_baseline(catalog: Catalog, queries: list[dict]) -> dict[str, list]:
    # 主对照口径：与真实查询一致的原始 query 文本 + 结构化筛选条件；
    # baseline_name 的人工名称改写不进入主对照，仅用于单独报告（见 name_rewrites）。
    results = {}
    for item in queries:
        filters = item["filters"]
        results[item["id"]] = catalog.search(
            item["query"],
            equipment=filters["equipment"],
            body_part=filters["body_part"],
            target=filters["target"],
            muscle_group=filters["muscle_group"],
        )
    return results


def name_rewrites(queries: list[dict]) -> list[dict]:
    return [
        {"id": item["id"], "query": item["query"], "baseline_name": item["baseline_name"]}
        for item in queries
        if item["query"] != item["baseline_name"]
    ]


def per_query_record(item: dict, ids: list[str]) -> dict:
    # 指标口径：grade > 0 视为相关；分母为该查询已标注的相关集合；@10 只取前 10 个候选。
    # 空金标查询不参与 Recall/MRR/nDCG 平均，expect_empty 单独记录误返回。
    top = ids[:10]
    relevant = {label["id"]: label["grade"] for label in item["labels"] if label["grade"] > 0}
    record = {
        "id": item["id"],
        "intent": item["intent"],
        "split": item["split"],
        "family": item["family"],
        "purpose": item["purpose"],
        "baseline_name": item["baseline_name"],
        "filters": item["filters"],
        "expect_empty": item["expect_empty"],
        "label_status": item["label_status"],
        "relevant": relevant,
        "total": len(ids),
        "top10": top,
    }
    if item["expect_empty"]:
        record["false_return"] = bool(ids)
    if relevant:
        found = [rid for rid in top if rid in relevant]
        record["recall@10"] = len(set(found)) / len(relevant)
        ranks = [top.index(rid) + 1 for rid in set(found)]
        record["mrr@10"] = 1 / min(ranks) if ranks else 0.0
        gains = [relevant.get(rid, 0) for rid in top]
        ideal = sorted(relevant.values(), reverse=True)[:10]
        ideal_dcg = dcg(ideal)
        record["ndcg@10"] = dcg(gains) / ideal_dcg if ideal_dcg else 0.0
    return record


def aggregate(rows: list[dict]) -> dict:
    # 宏平均：每条有标注查询等权；无答案误返回率按 expect_empty 查询单独统计。
    labelled = [row for row in rows if row.get("relevant")]
    empties = [row for row in rows if row["expect_empty"]]
    return {
        "queries": len(rows),
        "with_labels": len(labelled),
        "recall@10": sum(row["recall@10"] for row in labelled) / len(labelled) if labelled else None,
        "mrr@10": sum(row["mrr@10"] for row in labelled) / len(labelled) if labelled else None,
        "ndcg@10": sum(row["ndcg@10"] for row in labelled) / len(labelled) if labelled else None,
        "empty_expectation_queries": len(empties),
        "false_return_rate": sum(1 for row in empties if row["false_return"]) / len(empties) if empties else None,
    }


def evaluate(queries: list[dict], results: dict[str, list]) -> tuple[dict, list[dict]]:
    per_query = [
        per_query_record(item, [hit.id for hit in results[item["id"]]]) for item in queries
    ]
    aggregate_by_intent = {
        intent: aggregate([row for row in per_query if row["intent"] == intent])
        for intent in sorted({item["intent"] for item in queries})
    }
    return aggregate_by_intent, per_query


def project_brief(exercise) -> dict:
    return {
        "id": exercise.id,
        "name": exercise.name,
        "equipment": exercise.equipment,
        "target": exercise.target,
    }


def length_stats(catalog: Catalog, queries: list[dict], results: dict[str, list]) -> dict:
    mappings = rt.load_mappings()
    entries = rt.supplement_index(rt.load_supplements())
    exercises = catalog.all()

    unified = [
        rt.build_unified_text(exercise, entries.get(exercise.id), mappings)
        for exercise in exercises
    ]
    unified_chars = [len(text) for text in unified]
    unified_bytes = [len(text.encode("utf-8")) for text in unified]
    unified_by_id = {exercise.id: len(text) for exercise, text in zip(exercises, unified)}
    unified_bytes_by_id = {
        exercise.id: len(text.encode("utf-8")) for exercise, text in zip(exercises, unified)
    }

    full_details = {
        exercise.id: json.dumps(exercise.model_dump(), ensure_ascii=False)
        for exercise in exercises
    }
    full_chars = {eid: len(text) for eid, text in full_details.items()}
    full_bytes = {eid: len(text.encode("utf-8")) for eid, text in full_details.items()}

    steps_zh = {exercise.id: len(" ".join(exercise.steps.zh)) for exercise in exercises}
    steps_en = {exercise.id: len(" ".join(exercise.steps.en)) for exercise in exercises}

    brief_records = []
    for item in queries:
        hits = results[item["id"]]
        briefs = [project_brief(hit) for hit in hits[:10]]
        brief_records.append(
            {
                "id": item["id"],
                "returned": len(hits),
                "chars": len(json.dumps(briefs, ensure_ascii=False)),
            }
        )
    nonempty_briefs = [r for r in brief_records if r["returned"] > 0]
    empty_brief_ids = [r["id"] for r in brief_records if r["returned"] == 0]
    brief_chars = [r["chars"] for r in nonempty_briefs]
    brief_max = max(nonempty_briefs, key=lambda r: r["chars"])

    batches = {}
    ordered_ids = [exercise.id for exercise in exercises]
    for size in (5, 10):
        # 全量连续批次：覆盖目录内任意 offset，开头抽样无法代表任意五条详情的体积。
        payloads = [
            json.dumps(
                [json.loads(full_details[eid]) for eid in ordered_ids[offset : offset + size]],
                ensure_ascii=False,
            )
            for offset in range(0, len(ordered_ids), size)
        ]
        payload_chars = [len(p) for p in payloads]
        payload_bytes = [len(p.encode("utf-8")) for p in payloads]
        # 任意五条详情的最大体积上界：单条最长的 size 条组合成一条完整响应。
        longest_ids = sorted(full_details, key=lambda eid: full_chars[eid], reverse=True)[:size]
        longest_payload = json.dumps(
            [json.loads(full_details[eid]) for eid in longest_ids], ensure_ascii=False
        )
        batches[size] = {
            "batch_count": len(payloads),
            "chars_p50": round(percentile(payload_chars, 0.5)),
            "chars_p95": round(percentile(payload_chars, 0.95)),
            "chars_max": max(payload_chars),
            "bytes_p50": round(percentile(payload_bytes, 0.5)),
            "bytes_p95": round(percentile(payload_bytes, 0.95)),
            "bytes_max": max(payload_bytes),
            "longest_combo": {
                "ids": longest_ids,
                "chars": len(longest_payload),
                "bytes": len(longest_payload.encode("utf-8")),
            },
        }

    def top(source: dict[str, int], count: int = 5):
        return sorted(source.items(), key=lambda kv: kv[1], reverse=True)[:count]

    return {
        "口径说明": {
            "chars": "字符数口径，用于工具字符预算",
            "bytes": "UTF-8 字节数口径，用于索引容量（中文字节数大于字符数）",
            "detail_batches": "完整详情 JSON 数组按目录顺序切批，全量批次统计；longest_combo 为任意一批的最大体积上界",
            "briefs": "搜索返回 top10 的 project_brief JSON 长度；空结果单独计数，不混入分位数",
        },
        "unified_chars": {
            "n": len(unified_chars),
            "p50": round(percentile(unified_chars, 0.5)),
            "p95": round(percentile(unified_chars, 0.95)),
            "max": max(unified_chars),
            "max_ids": top(unified_by_id),
            "est_tokens_p95": est_tokens(round(percentile(unified_chars, 0.95))),
        },
        "unified_bytes": {
            "n": len(unified_bytes),
            "p50": round(percentile(unified_bytes, 0.5)),
            "p95": round(percentile(unified_bytes, 0.95)),
            "max": max(unified_bytes),
            "max_ids": top(unified_bytes_by_id),
        },
        "brief_top10_chars": {
            "n": len(brief_chars),
            "n_empty": len(empty_brief_ids),
            "empty_query_ids": empty_brief_ids,
            "p50": round(percentile(brief_chars, 0.5)) if brief_chars else 0,
            "p95": round(percentile(brief_chars, 0.95)) if brief_chars else 0,
            "max": max(brief_chars) if brief_chars else 0,
            "max_query_id": brief_max["id"] if nonempty_briefs else None,
            "est_tokens_p95": est_tokens(round(percentile(brief_chars, 0.95))) if brief_chars else 0,
        },
        "detail_nine_field_chars": {
            "n": len(full_chars),
            "p50": round(percentile(list(full_chars.values()), 0.5)),
            "p95": round(percentile(list(full_chars.values()), 0.95)),
            "max": max(full_chars.values()),
            "max_ids": top(full_chars),
            "bytes_max": max(full_bytes.values()),
            "est_tokens_p95": est_tokens(round(percentile(list(full_chars.values()), 0.95))),
            "est_tokens_max": est_tokens(max(full_chars.values())),
        },
        "steps_zh_chars": {
            "p50": round(percentile(list(steps_zh.values()), 0.5)),
            "p95": round(percentile(list(steps_zh.values()), 0.95)),
            "max": max(steps_zh.values()),
            "max_ids": top(steps_zh),
        },
        "steps_en_chars": {
            "p50": round(percentile(list(steps_en.values()), 0.5)),
            "p95": round(percentile(list(steps_en.values()), 0.95)),
            "max": max(steps_en.values()),
            "max_ids": top(steps_en),
        },
        "detail_batches": batches,
    }


def git_identity() -> dict:
    def run(*args):
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
        "changed_paths": run("status", "--porcelain").splitlines(),
    }


def check() -> None:
    catalog = Catalog.load()
    document = load(QUERIES)
    queries = document["queries"]
    results = run_baseline(catalog, queries)
    aggregate_by_intent, per_query = evaluate(queries, results)

    total_returned = sum(len(hits) for hits in results.values())
    over_ten = sum(1 for hits in results.values() if len(hits) > 10)
    empty = sum(1 for hits in results.values() if not hits)

    print(f"基线：目录 {len(catalog)} 条，查询 {len(queries)} 条，label_status={document['label_status']}")
    print(f"返回：总候选 {total_returned}，空结果 {empty} 条查询，超过 10 条 {over_ten} 条查询")
    for intent, stats in aggregate_by_intent.items():
        print(f"  {intent:16s} {stats}")
    print(f"  {'__overall__':16s} {aggregate(per_query)}")

    unsupported = sorted({key for item in queries for key in item["temporary_restrictions"]})
    print(f"当前实现不支持的条件：环境默认、器械集合、画像限制、显式排除；涉及 {len(unsupported)} 类临时限制")

    print("长度统计：")
    print(json.dumps(length_stats(catalog, queries, results), ensure_ascii=False, indent=2))
    print("提交身份：")
    print(json.dumps(git_identity(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    check()
