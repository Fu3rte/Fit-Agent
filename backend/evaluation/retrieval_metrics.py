import math
from importlib.metadata import version
from statistics import mean

from mteb._evaluators.retrieval_metrics import calculate_retrieval_scores, mrr

DEFAULT_KS = (5,)
GAIN = {1: 1, 2: 3, 3: 7}
FIELDS = {"hit": "success", "recall": "recall", "precision": "P", "mrr": None, "ndcg": "ndcg_cut"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def metric_aliases(ks) -> dict[str, str]:
    return {
        f"{name}@{k}": f"{field}_{k}" if field else f"MRR@{k}"
        for k in ks
        for name, field in FIELDS.items()
    }


def score(queries: list[dict], rankings: dict[str, list[str]], ks=DEFAULT_KS) -> dict:
    require(bool(queries), "评测查询为空")
    require(bool(ks) and all(type(k) is int and k > 0 for k in ks), "k 必须为正整数")
    query_ids = [item["id"] for item in queries]
    require(len(set(query_ids)) == len(query_ids), "查询 ID 重复")
    require(set(rankings) == set(query_ids), "检索结果必须逐条覆盖查询，缺失结果不能视为空返回")
    labelled = []
    rows = {}
    for item in queries:
        qid, ids = item["id"], rankings[item["id"]]
        require(isinstance(ids, list), f"候选必须为有序列表: {qid}")
        require(all(isinstance(eid, str) and eid for eid in ids), f"动作 ID 必须为非空字符串: {qid}")
        require(len(set(ids)) == len(ids), f"候选 ID 重复: {qid}")
        labels = item["labels"]
        require(len({label["id"] for label in labels}) == len(labels), f"金标 ID 重复: {qid}")
        require(all(isinstance(label["id"], str) and label["id"] for label in labels), f"金标 ID 非法: {qid}")
        require(all(type(label["grade"]) is int and 1 <= label["grade"] <= 3 for label in labels), f"金标等级非法: {qid}")
        require(bool(labels) != item["expect_empty"], f"必须为有金标或预期空结果查询: {qid}")
        tags = item.get("tags", [])
        require(isinstance(tags, list) and all(isinstance(tag, str) and tag for tag in tags), f"标签非法: {qid}")
        require(len(tags) == len(set(tags)), f"标签重复: {qid}")
        require("intent" in item or tags, f"必须提供 intent 或 tags: {qid}")
        rows[qid] = {
            "id": qid,
            "intent": item.get("intent"),
            "tags": list(tags),
            "split": item["split"],
            "family": item["family"],
            "expected_ids": {label["id"]: label["grade"] for label in labels},
            "returned_ids": ids,
            "scores": {},
        }
        if item["expect_empty"]:
            rows[qid]["scores"]["false_return"] = float(bool(ids))
        else:
            labelled.append(item)
    aliases = metric_aliases(ks)
    if labelled:
        qrels = {
            item["id"]: {eid: GAIN[grade] for eid, grade in rows[item["id"]]["expected_ids"].items()}
            for item in labelled
        }
        # 单调递减分数仅编码实际返回顺序；库内按前 max(ks) 条评分，完整返回保留在证据中。
        limit = max(ks)
        results = {}
        for item in labelled:
            qid = item["id"]
            ids = rankings[qid][:limit]
            results[qid] = {eid: float(len(ids) - rank) for rank, eid in enumerate(ids)}
        # mrr() 以 results 的插入顺序返回逐查询列表，显式保留该顺序并校验数量。
        mrr_order = [item["id"] for item in labelled]
        result = calculate_retrieval_scores(results, qrels, list(ks))
        mrr_values = mrr(qrels, results, list(ks))
        for k in ks:
            require(len(mrr_values[aliases[f"mrr@{k}"]]) == len(mrr_order), f"MRR@{k} 逐查询数量不一致")
        for index, qid in enumerate(mrr_order):
            require(qid in result.all_scores, f"评分结果缺少查询: {qid}")
            raw = result.all_scores[qid]
            scores = {
                key: float(raw[aliases[key]])
                for key in (f"{name}@{k}" for k in ks for name in FIELDS if name != "mrr")
            }
            scores |= {f"mrr@{k}": float(mrr_values[aliases[f"mrr@{k}"]][index]) for k in ks}
            require(all(math.isfinite(value) and 0 <= value <= 1 for value in scores.values()), f"指标超出范围: {qid}")
            rows[qid]["scores"] = scores

    def summarize(selected: list[dict]) -> dict:
        positives = [row for row in selected if row["expected_ids"]]
        empties = [row for row in selected if not row["expected_ids"]]
        return {
            "queries": len(selected),
            "with_labels": len(positives),
            "empty_expectation_queries": len(empties),
            **{key: mean(row["scores"][key] for row in positives) if positives else None for key in aliases},
            "false_return_rate": mean(row["scores"]["false_return"] for row in empties) if empties else None,
        }

    return {
        "engine": {
            "name": "mteb",
            "version": version("mteb"),
            "entry": "mteb._evaluators.retrieval_metrics.calculate_retrieval_scores",
            "mrr_entry": "mteb._evaluators.retrieval_metrics.mrr",
            "aliases": aliases,
            "relevance": "grade>=1",
            "ndcg_gain": "2^grade-1",
            "gain_mapping": {str(grade): gain for grade, gain in GAIN.items()},
            "precision_denominator": "k",
            "averaging": "macro over labelled queries from per-query raw scores; expect_empty scored separately",
            "truncation": "top-k by final response order",
            "mrr_alignment": "results insertion order",
        },
        "overall": summarize(list(rows.values())),
        "by_intent": {
            intent: summarize([row for row in rows.values() if row["intent"] == intent])
            for intent in sorted({item["intent"] for item in queries if item.get("intent") is not None})
        },
        "by_tag": {
            tag: summarize([row for row in rows.values() if tag in row["tags"]])
            for tag in sorted({tag for item in queries for tag in item.get("tags", [])})
        },
        "per_query": rows,
    }
