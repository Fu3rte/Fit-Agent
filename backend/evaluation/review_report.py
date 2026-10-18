import argparse
import html
import json
from pathlib import Path

from evaluation.dataset import DatasetError, load_for_evaluation, load_for_review
from evaluation.input_audit import AuditReport, audit_dataset

STYLE = """
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f7f7f5;color:#1f2328;margin:0;padding:32px 40px;line-height:1.6}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:19px;margin:36px 0 12px;padding-bottom:6px;border-bottom:1px solid #d8d8d4}
h3{font-size:15px;margin:0 0 6px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin:8px 0 16px}
th,td{border:1px solid #dcdcd8;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#efefec;font-weight:600}
pre{background:#fff;border:1px solid #dcdcd8;padding:12px;overflow-x:auto;font-size:12px;line-height:1.5;margin:6px 0 12px;white-space:pre-wrap;word-break:break-word}
article{background:#fff;border:1px solid #dcdcd8;padding:14px 16px;margin:12px 0}
.meta{font-size:12px;color:#57606a;margin-bottom:6px}
.old{background:#fff4f4}
.new{background:#f2fbf4}
.flag{color:#8a1f11;font-weight:600}
code{background:#efefec;padding:1px 4px;font-size:12px}
"""


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _json_block(value) -> str:
    return f"<pre>{_escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>"


def _table(headers: list[str], rows: list[list[str]], classes: list[str] | None = None) -> str:
    head = "".join(f"<th>{_escape(item)}</th>" for item in headers)
    body = []
    for row in rows:
        cells = "".join(f"<td>{cell}</td>" for cell in row)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _cell(value) -> str:
    if value is None:
        return "<span class=\"meta\">null</span>"
    return _escape(str(value))


def _review_summary(dataset) -> str:
    review = dataset.review
    counts = review["counts"]
    identity = dataset.identity()
    file_rows = "<br>".join(
        f"{_escape(name)}: <code>{_escape(digest)}</code>" for name, digest in identity["files"].items()
    )
    rows = [
        ["数据版本", _escape(dataset.version)],
        ["版本目录", f"<code>{_escape(identity['version_dir'])}</code>"],
        ["manifest SHA-256", f"<code>{dataset.manifest_sha256}</code>"],
        ["数据文件 SHA-256", file_rows],
        ["manifest 内容清单摘要（当前计算值）", f"<code>{dataset.content_sha256}</code>"],
        ["corpus 内容身份", f"<code>{dataset.corpus_sha256}</code>"],
        [
            "数量",
            "corpus {corpus} / queries {queries} / qrels {qrels} / scenarios {scenarios} / assets {assets}".format(
                **counts
            ),
        ],
        [
            "配额",
            "用途 {p}；无答案 {e}；场景类别 {c}".format(
                p=json.dumps(review["quotas"]["purposes"], ensure_ascii=False),
                e=json.dumps(review["quotas"]["expect_empty"], ensure_ascii=False),
                c=json.dumps(review["quotas"]["scenario_categories"], ensure_ascii=False),
            ),
        ],
        [
            "审阅状态",
            "queries {q}；qrels {r}；scenarios {s}".format(
                q=json.dumps(review["review_counts"]["queries"], ensure_ascii=False),
                r=json.dumps(review["review_counts"]["qrels"], ensure_ascii=False),
                s=json.dumps(review["review_counts"]["scenarios"], ensure_ascii=False),
            ),
        ],
        [
            "release",
            "status={status}；content_sha256={digest}".format(
                status=_escape(review["release"]["status"]),
                digest=_escape(str(review["release"]["content_sha256"])),
            ),
        ],
        [
            "审阅入口",
            "load_for_review 通过，未就绪项 {count} 条（逐条审批与发布批准未完成）".format(
                count=len(review["issues"])
            ),
        ],
    ]
    return _table(["项目", "值"], rows)


def _formal_entry(dataset) -> str:
    try:
        load_for_evaluation(dataset.version_dir)
    except DatasetError:
        issues = dataset.review["issues"]
        first = _escape(issues[0]) if issues else "无"
        return (
            "<p>正式评测入口 load_for_evaluation 拒绝当前草稿，未就绪项 {count} 条，首项：{first}</p>".format(
                count=len(issues), first=first
            )
        )
    return "<p class=\"flag\">正式评测入口未拒绝当前数据。</p>"


def _audit_section(report: AuditReport) -> str:
    rows = [[_escape(name), _escape(str(count))] for name, count in report.coverage.items()]
    rows.append(["动作目录 ID 数量", _escape(str(report.catalog_size))])
    rows.append(["命中项", _escape(str(len(report.findings)))])
    table = _table(["检查对象", "数量"], rows)
    if not report.findings:
        return table
    findings = _table(
        ["定位", "字段", "类型", "命中值", "上下文"],
        [
            [_escape(item.location), _escape(item.field), _escape(item.kind), f"<code>{_escape(item.token)}</code>", _escape(item.context)]
            for item in report.findings
        ],
    )
    return table + findings


def _revision_section(revision: dict) -> str:
    rows = []
    for entry in revision["entries"]:
        rows.append(
            [
                f"<code>{_escape(entry['file'])}:{entry['line']}</code>",
                f"<code>{_escape(entry['record_id'])}</code>",
                f"<code>{_escape(entry['field'])}</code>",
                f"<span class=\"old\">{_escape(entry['before'])}</span>",
                f"<span class=\"new\">{_escape(entry['after'])}</span>",
                _escape(entry["basis"]),
            ]
        )
    return _table(["文件与行号", "记录 ID", "字段", "修订前", "修订后", "依据"], rows)


def _query_sections(dataset) -> str:
    qrels_by_query: dict[str, list[dict]] = {}
    for qrel in dataset.qrels:
        qrels_by_query.setdefault(qrel["query_id"], []).append(qrel)
    names = {row["corpus_id"]: row["text"].split("\n")[0] for row in dataset.corpus}
    parts = []
    for number, query in enumerate(dataset.queries, 1):
        query_id = query["query_id"]
        qrels = sorted(qrels_by_query.get(query_id, []), key=lambda item: item["corpus_id"])
        qrel_rows = [
            [
                f"<code>{_escape(qrel['corpus_id'])}</code>",
                _escape(names.get(qrel["corpus_id"], "")),
                _escape(str(qrel["grade"])),
                _escape(qrel["rationale"]),
                _escape(qrel["review"]["status"]),
            ]
            for qrel in qrels
        ]
        parts.append(
            "<article id=\"{qid}\"><h3>{qid}</h3>"
            "<p class=\"meta\">JSONL 行号 {line}；用途 {purpose}；expect_empty {empty}；标签 {tags}；家族 {family}；审阅状态 {status}</p>"
            "{body}{qrels}</article>".format(
                qid=_escape(query_id),
                line=number,
                purpose=_escape(query["purpose"]),
                empty=_escape(str(query["expect_empty"])),
                tags=_escape(", ".join(query["tags"])),
                family=_escape(query["family"]),
                status=_escape(query["review"]["status"]),
                body=_json_block(query),
                qrels=_table(["corpus_id", "动作名称", "grade", "依据", "审阅状态"], qrel_rows),
            )
        )
    return "".join(parts)


def _scenario_sections(dataset) -> str:
    parts = []
    for number, scenario in enumerate(dataset.scenarios, 1):
        parts.append(
            "<article id=\"{sid}\"><h3>{sid}</h3>"
            "<p class=\"meta\">JSONL 行号 {line}；标签 {tags}；家族 {family}；审阅状态 {status}；附件 {assets}</p>"
            "{body}</article>".format(
                sid=_escape(scenario["scenario_id"]),
                line=number,
                tags=_escape(", ".join(scenario["tags"])),
                family=_escape(scenario["family"]),
                status=_escape(scenario["review"]["status"]),
                assets=_escape(", ".join(sorted(dataset.attachment_hashes(scenario))) or "无"),
                body=_json_block(scenario),
            )
        )
    return "".join(parts)


def _index(dataset) -> str:
    query_rows = [
        [f"<code>{number}</code>", f"<a href=\"#{_escape(query['query_id'])}\">{_escape(query['query_id'])}</a>", _escape(query["purpose"])]
        for number, query in enumerate(dataset.queries, 1)
    ]
    scenario_rows = [
        [f"<code>{number}</code>", f"<a href=\"#{_escape(item['scenario_id'])}\">{_escape(item['scenario_id'])}</a>", _escape(item["tags"][0])]
        for number, item in enumerate(dataset.scenarios, 1)
    ]
    return _table(["行号", "query_id", "用途"], query_rows) + _table(["行号", "scenario_id", "主要类别"], scenario_rows)


def render(version_dir: Path, revision_path: Path | None) -> str:
    dataset = load_for_review(version_dir)
    report = audit_dataset(dataset)
    revision = json.loads(revision_path.read_text(encoding="utf-8")) if revision_path else None
    sections = [
        "<h1>评测数据审阅材料</h1>",
        f"<p class=\"meta\">版本 {_escape(dataset.version)}；由 load_for_review 加载并完成结构、来源、引用、语义与配额校验。</p>",
        "<h2>数据身份与审阅状态</h2>",
        _review_summary(dataset),
        _formal_entry(dataset),
        "<h2>用户输入动作标识扫描</h2>",
        _audit_section(report),
    ]
    if revision is not None:
        sections.extend(["<h2>本次修订清单</h2>", _revision_section(revision)])
    sections.extend(
        [
            "<h2>索引</h2>",
            _index(dataset),
            "<h2>检索请求</h2>",
            _query_sections(dataset),
            "<h2>业务场景</h2>",
            _scenario_sections(dataset),
        ]
    )
    body = "".join(sections)
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>评测数据审阅材料</title>"
        f"<style>{STYLE}</style></head><body>{body}</body></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", type=Path, default=None)
    args = parser.parse_args()
    document = render(args.version_dir, args.revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    dataset = load_for_review(args.version_dir)
    report = audit_dataset(dataset)
    print(f"输出 {args.output}，长度 {len(document)}")
    print(f"覆盖 {report.coverage}，命中 {len(report.findings)}")
    print(f"审阅未就绪项 {len(dataset.review['issues'])}，数量 {dataset.review['counts']}")


if __name__ == "__main__":
    main()
