import math
from importlib.metadata import version
from statistics import mean

from ranx import Qrels, Run, evaluate

DEFAULT_KS = (5, 10)
METRICS = {
    "hit": "hit_rate",
    "recall": "recall",
    "precision": "precision",
    "mrr": "mrr",
    "ndcg": "ndcg_burges",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


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
        rows[qid] = {
            "id": qid,
            "intent": item["intent"],
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
    aliases = {f"{name}@{k}": f"{alias}@{k}" for k in ks for name, alias in METRICS.items()}
    if labelled:
        qrels = Qrels({item["id"]: rows[item["id"]]["expected_ids"] for item in labelled})
        # 单调 rank score 仅编码实际返回顺序，避免分数并列时库重新排序。
        runs = {
            item["id"]: {eid: float(len(rankings[item["id"]]) - rank) for rank, eid in enumerate(rankings[item["id"]])}
            for item in labelled if rankings[item["id"]]
        }
        run = Run(runs) if runs else Run()
        evaluate(qrels, run, list(aliases.values()), make_comparable=True, threads=1)
        for item in labelled:
            qid = item["id"]
            rows[qid]["scores"] = {name: float(run.scores[alias][qid]) for name, alias in aliases.items()}
            require(all(math.isfinite(value) and 0 <= value <= 1 for value in rows[qid]["scores"].values()), f"指标超出范围: {qid}")

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
        "engine": {"name": "ranx", "version": version("ranx"), "aliases": aliases,
                   "relevance": "grade>=1", "ndcg_gain": "2^grade-1", "precision_denominator": "k",
                   "averaging": "macro over labelled queries; expect_empty scored separately"},
        "overall": summarize(list(rows.values())),
        "by_intent": {intent: summarize([row for row in rows.values() if row["intent"] == intent]) for intent in sorted({item["intent"] for item in queries})},
        "per_query": rows,
    }
