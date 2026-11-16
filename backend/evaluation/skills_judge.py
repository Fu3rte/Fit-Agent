import json
import re
from datetime import datetime
from pathlib import Path

from evaluation.skills_generation import identity_digest
from evaluation.skills_scoring import (
    EVALUATORS,
    JUDGE_METHOD,
    KEYS,
    MODEL,
    PROGRAMMATIC_CHANNEL,
    PROVIDER,
    QUESTIONS,
    build_payload,
    judge_score_id,
    require,
    scoring_dir,
)

CATEGORIES = {key: tuple(EVALUATORS[key]["categories"]) for key in KEYS}
KEY_RE = re.compile(r"\{\{(\w+)\}\}")
SCORE_PAGE_LIMIT = 100


def render(template: str, row: dict) -> str:
    # 与网页裁判变量一致：input/output/referenceOutput 各绑定整对象 JSON。
    values = {
        "input": json.dumps(row["upload"]["inputs"], ensure_ascii=False),
        "output": json.dumps(row["upload"]["outputs"], ensure_ascii=False),
        "referenceOutput": json.dumps(row["upload"]["reference_outputs"], ensure_ascii=False),
    }
    return KEY_RE.sub(lambda match: values.get(match.group(1), match.group(0)), template)


def ask(openai_client, key: str, row: dict):
    instruction = (
        "\n\n仅返回一个严格 JSON 对象，不要输出多余文本："
        f"{{\"{key}\": \"<类别>\", \"comment\": \"<简要中文理由>\"}}。"
        f"其中 {key} 只能是 {list(CATEGORIES[key])} 之一；comment 为理由文本。"
    )
    completion = openai_client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": render(QUESTIONS[key], row) + instruction}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    data = json.loads(completion.choices[0].message.content)
    category = data.get(key)
    if category not in CATEGORIES[key]:
        raise ValueError(f"裁判返回类别非法: {key}={category!r}")
    usage = getattr(completion, "usage", None)
    return category, (data.get("comment") or ""), {
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
    }


def trace_score_keys(client, trace_id: str) -> set[str]:
    # 幂等前置：分页读取该 trace 已有 score，跳过已齐键，避免重复触发真实裁判。
    present: set[str] = set()
    cursor = None
    while True:
        response = client.api.scores.get_many(trace_id=trace_id, fields="details",
                                              limit=SCORE_PAGE_LIMIT, cursor=cursor)
        present.update(score.name for score in response.data if score.name in KEYS)
        cursor = response.meta.cursor
        if not cursor:
            return present


def judge_score_metadata(key: str, usage: dict) -> dict:
    # 程序化通道写入 score metadata 的声明字段；collect 按 PROGRAMMATIC_CHANNEL 逐项核验。
    return {"ls_provider": PROVIDER, "model_name": MODEL,
            "evaluator_repo_handle": EVALUATORS[key]["repo_handle"],
            "evaluator_repo_id": EVALUATORS[key]["repo_id"],
            "evaluator_commit_hash": EVALUATORS[key]["commit_hash"],
            "method": JUDGE_METHOD, "judge_usage": usage}


def run_judge(*, source: Path, group: str | None, limit: int,
              langfuse_client, openai_client) -> dict:
    payload = build_payload(source)
    rows = [row for row in payload["rows"]
            if group is None or row["upload"]["metadata"]["group"] == group]
    receipt = json.loads((scoring_dir(source) / "experiment.json").read_text(encoding="utf-8"))
    require(receipt.get("run_id") == payload["run_id"]
            and receipt.get("payload_sha256") == identity_digest(payload),
            "实验回执与当前源证据不一致")
    missing_rows = [row["row_id"] for row in rows if row["row_id"] not in receipt["runs"]]
    require(not missing_rows, "实验回执缺少样本运行映射")
    # 本地直写幂等约定：score_id 由业务身份稳定派生，name 与 timestamp 固定为实验提交时刻。
    scored_at = datetime.fromisoformat(receipt["submitted_at"])
    processed = skipped = 0
    for row in rows:
        if processed >= limit:
            break
        trace_id = receipt["runs"][row["row_id"]]
        have = trace_score_keys(langfuse_client, trace_id)
        missing = [key for key in KEYS if key not in have]
        if not missing:
            skipped += 1
            continue
        for key in missing:
            category, comment, usage = ask(openai_client, key, row)
            langfuse_client.create_score(
                trace_id=trace_id,
                name=key,
                value=category,
                data_type=PROGRAMMATIC_CHANNEL["data_type"],
                comment=comment,
                score_id=judge_score_id(payload["run_id"], row["row_id"], key),
                metadata=judge_score_metadata(key, usage),
                timestamp=scored_at)
        processed += 1
    return {"rows_in_scope": len(rows), "scored_this_run": processed,
            "already_complete": skipped}
