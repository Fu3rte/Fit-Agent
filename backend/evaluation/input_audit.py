import re
from dataclasses import dataclass

from evaluation.dataset import EvaluationDataset

# 目录动作 ID 是四位数字原始字符串，前导零有意义，按文本原样匹配。
FOUR_DIGIT = re.compile(r"(?<![0-9A-Za-z._])([0-9]{4})(?![0-9._])")
LONG_DIGIT = re.compile(r"(?<![0-9A-Za-z._])([0-9]{5,})(?![0-9._])")
DATE = re.compile(r"[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}")
# 数字后紧跟计量单位时属于组次、重量、时长等正常数值。
UNIT = re.compile(
    r"\s*(分钟|公斤|千克|小时|kcal|kg|lb|cm|mm|ml|克|磅|组|次|秒|分|度|米|天|周|元|g|L|l|%)"
)
# 数字前带序数或量词时属于训练参数。
LEAD = re.compile(r"(第|每组|每次|共|约|各)\s*$")
IDENTIFIER_WORDING = (
    re.compile(r"动作\s*ID", re.IGNORECASE),
    re.compile(r"动作\s*编号"),
    re.compile(r"目录\s*编号"),
    re.compile(r"内部\s*(ID|编号|标识)", re.IGNORECASE),
    re.compile(r"exercise_id", re.IGNORECASE),
)
# 助理回答的固定开场与指令句式，只用于定位待人工复核的可疑消息事件。
ASSISTANT_OUTPUT = (
    re.compile(r"^\s*(完整)?(画像|计划|训练记录|替换|调整|频率调整|结构调整|参数调整|修正后的记录)快照[:：]"),
    re.compile(r"^\s*合并结果[:：]"),
    re.compile(r"^\s*已(完整)?读取"),
    re.compile(r"^\s*已保存画像[:：]"),
    re.compile(r"^\s*重启后核对"),
    re.compile(r"^\s*明白了[:：]"),
    re.compile(r"请点击[^。]{0,16}确认保存按钮"),
    re.compile(r"^\s*目前有两份待确认内容"),
)
CONTEXT_RADIUS = 16


@dataclass(frozen=True)
class Finding:
    location: str
    field: str
    kind: str
    token: str
    context: str


@dataclass(frozen=True)
class AuditReport:
    findings: tuple[Finding, ...]
    coverage: dict[str, int]
    catalog_size: int

    @property
    def clean(self) -> bool:
        return not self.findings


def _context(text: str, start: int, end: int) -> str:
    return text[max(0, start - CONTEXT_RADIUS) : end + CONTEXT_RADIUS]


def _is_temporal(text: str, start: int) -> bool:
    if DATE.match(text, start):
        return True
    return text[start + 4 : start + 5] == "年"


def _is_measurement(text: str, start: int, end: int) -> bool:
    if LEAD.search(text[:start]):
        return True
    return UNIT.match(text, end) is not None


def _looks_like_year(token: str) -> bool:
    # 四位年份单独出现时按日期处理，避免把 2026 这类年份当成目录编号。
    return 1900 <= int(token) <= 2100


def scan_text(text: str, catalog_ids: set[str], location: str, field: str) -> list[Finding]:
    if not catalog_ids:
        raise ValueError("动作目录 ID 集合为空")
    findings = []
    lower = min(int(corpus_id) for corpus_id in catalog_ids)
    upper = max(int(corpus_id) for corpus_id in catalog_ids)
    for match in FOUR_DIGIT.finditer(text):
        if _is_temporal(text, match.start()) or _is_measurement(text, match.start(), match.end()):
            continue
        token = match.group(1)
        if token in catalog_ids:
            kind = "catalog_id"
        elif lower <= int(token) <= upper and not _looks_like_year(token):
            kind = "catalog_range"
        else:
            continue
        findings.append(Finding(location, field, kind, token, _context(text, match.start(), match.end())))
    for match in LONG_DIGIT.finditer(text):
        if _is_measurement(text, match.start(), match.end()):
            continue
        findings.append(
            Finding(location, field, "long_digit", match.group(1), _context(text, match.start(), match.end()))
        )
    for pattern in IDENTIFIER_WORDING:
        for match in pattern.finditer(text):
            findings.append(
                Finding(location, field, "identifier_wording", match.group(0), _context(text, match.start(), match.end()))
            )
    return findings


def scan_assistant_output(text: str, location: str, field: str) -> list[Finding]:
    findings = []
    for pattern in ASSISTANT_OUTPUT:
        for match in pattern.finditer(text):
            findings.append(
                Finding(location, field, "assistant_output", match.group(0).strip(), _context(text, match.start(), match.end()))
            )
    return findings


def audit_dataset(dataset: EvaluationDataset) -> AuditReport:
    catalog_ids = {row["corpus_id"] for row in dataset.corpus}
    findings: list[Finding] = []
    coverage = {
        "queries": 0,
        "scenario_messages": 0,
        "prior_user_messages": 0,
        "event_messages": 0,
        "assets": 0,
    }
    for number, query in enumerate(dataset.queries, 1):
        location = f"queries.jsonl:{number}:{query['query_id']}"
        findings.extend(scan_text(query["query"], catalog_ids, location, "query"))
        coverage["queries"] += 1
    for number, scenario in enumerate(dataset.scenarios, 1):
        location = f"scenarios.jsonl:{number}:{scenario['scenario_id']}"
        findings.extend(scan_text(scenario["input"]["message"], catalog_ids, location, "input.message"))
        findings.extend(scan_assistant_output(scenario["input"]["message"], location, "input.message"))
        coverage["scenario_messages"] += 1
        for index, prior in enumerate(scenario["input"]["prior_messages"]):
            if prior["role"] != "user":
                continue
            findings.extend(
                scan_text(
                    prior["content"],
                    catalog_ids,
                    location,
                    f"input.prior_messages[{index}]",
                )
            )
            findings.extend(scan_assistant_output(prior["content"], location, f"input.prior_messages[{index}]"))
            coverage["prior_user_messages"] += 1
        for event in scenario["events"]:
            if event["type"] != "message" or not event["content"]:
                continue
            findings.extend(
                scan_text(
                    event["content"],
                    catalog_ids,
                    f"{location}:{event['event_id']}",
                    f"events[{event['event_id']}].content",
                )
            )
            findings.extend(
                scan_assistant_output(
                    event["content"],
                    f"{location}:{event['event_id']}",
                    f"events[{event['event_id']}].content",
                )
            )
            coverage["event_messages"] += 1
    for asset_id in sorted(dataset.assets):
        path = dataset.assets[asset_id]
        findings.extend(
            scan_text(path.read_text(encoding="utf-8"), catalog_ids, f"assets/{path.name}", "asset")
        )
        coverage["assets"] += 1
    return AuditReport(tuple(findings), coverage, len(catalog_ids))
