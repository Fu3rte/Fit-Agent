import argparse
import json
import os
import re
from pathlib import Path

from langsmith import Client
from langsmith.schemas import FeedbackSourceType
from openai import OpenAI

from evaluation.skills_scoring import (
    EVALUATORS,
    KEYS,
    MODEL,
    PROVIDER,
    QUESTIONS,
    build_payload,
    scoring_dir,
)

# 端点与模型来自已核验的网页Evaluator配置（glm-5.3-flash / OpenAI 兼容），非本地臆造。
BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
CATEGORIES = {key: tuple(EVALUATORS[key]["categories"]) for key in KEYS}
KEY_RE = re.compile(r"\{\{(\w+)\}\}")


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


def scored_keys(client: Client, run_ids: list[str]) -> dict[str, set]:
    present: dict[str, set] = {run_id: set() for run_id in run_ids}
    for item in client.list_feedback(run_ids=run_ids, limit=1000):
        if item.key in KEYS:
            present.setdefault(str(item.run_id), set()).add(item.key)
    return present


def main() -> int:
    parser = argparse.ArgumentParser(description="程序化运行已配置GLM Evaluator对真实回答打分并写回反馈")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--group")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--enable-judge", action="store_true")
    args = parser.parse_args()
    if not args.enable_judge:
        raise ValueError("必须显式--enable-judge；此操作调用真实GLM")
    key_value = os.environ.get("GLM_API_KEY")
    if not key_value:
        raise ValueError("缺少环境变量 GLM_API_KEY（bigmodel.cn 智谱密钥）；禁止以其它模型替代")

    payload = build_payload(args.source)
    rows = [row for row in payload["rows"]
            if args.group is None or row["upload"]["metadata"]["group"] == args.group]
    receipt = json.loads((scoring_dir(args.source) / "experiment.json").read_text(encoding="utf-8"))
    client = Client()
    openai_client = OpenAI(api_key=key_value, base_url=BASE_URL)

    run_of = {row["row_id"]: receipt["runs"][row["row_id"]] for row in rows}
    present = scored_keys(client, list(run_of.values()))
    processed = skipped = 0
    for row in rows:
        if processed >= args.limit:
            break
        run_id = run_of[row["row_id"]]
        meta = row["upload"]["metadata"]
        have = present.get(run_id, set())
        missing = [key for key in KEYS if key not in have]
        if not missing:
            skipped += 1
            continue
        for key in missing:
            category, comment, usage = ask(openai_client, key, row)
            client.create_feedback(
                run_id=run_id,
                key=key,
                value=category,
                comment=comment,
                feedback_source_type=FeedbackSourceType.MODEL,
                source_info={"ls_provider": PROVIDER, "model_name": MODEL,
                             "evaluator_repo_handle": EVALUATORS[key]["repo_handle"],
                             "evaluator_commit_hash": EVALUATORS[key]["commit_hash"],
                             "method": "sdk_programmatic_judge"},
                feedback_config={"type": "categorical",
                                 "categories": [{"value": float(index), "label": label}
                                                for index, label in enumerate(CATEGORIES[key])]},
                extra={"group": meta["group"], "case_id": meta["case_id"],
                       "judge_usage": usage},
            )
        processed += 1
    print(json.dumps({"rows_in_scope": len(rows), "scored_this_run": processed,
                      "already_complete": skipped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
