import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import jsonlines
from jsonpointer import JsonPointerException, resolve_pointer
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from app.application.business import retrieval_text as rt
from app.application.business.retrieval_conditions import evaluate_search_conditions
from app.application.business.retrieval_index import RetrievalCorpus
from app.domain.business.models import (
    CatalogSearchArguments,
    PlanRecord,
    ProfileContent,
    WorkoutRecord,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SCHEMA_DIR = BACKEND / "dataset" / "evaluation" / "schemas"
SCHEMA_IDS = {
    "defs": "https://fit-agent.local/evaluation/defs.schema.json",
    "manifest": "https://fit-agent.local/evaluation/manifest.schema.json",
    "corpus": "https://fit-agent.local/evaluation/corpus.schema.json",
    "queries": "https://fit-agent.local/evaluation/queries.schema.json",
    "qrels": "https://fit-agent.local/evaluation/qrels.schema.json",
    "scenarios": "https://fit-agent.local/evaluation/scenarios.schema.json",
}
SCHEMA_PATHS = {
    name: f"backend/dataset/evaluation/schemas/{name}.schema.json" for name in SCHEMA_IDS
}
DATA_FILES = {"corpus": "corpus.jsonl", "queries": "queries.jsonl", "qrels": "qrels.jsonl", "scenarios": "scenarios.jsonl"}
SOURCE_PATHS = {
    "catalog": "backend/dataset/actions.agent.json",
    "supplements": "backend/dataset/action-supplements.published.json",
    "mappings": "backend/dataset/action-field-mappings.json",
}
QUERY_COUNT = 150
SCENARIO_COUNT = 80
PURPOSE_QUOTAS = {"lookup": 60, "generation": 45, "replacement": 45}
EMPTY_QUOTAS = {"lookup": 10, "generation": 10, "replacement": 10}
SCENARIO_CATEGORIES = {
    "action_verification": 8,
    "profile_update": 8,
    "plan_import_file": 8,
    "incomplete_plan_structure": 8,
    "plan_generation": 8,
    "replacement_constraints": 8,
    "performance_adjustment": 8,
    "workout_logging_query": 8,
    "confirmation_cancellation_state": 8,
    "clarification_authorization_medical_risk": 8,
}
METRICS = ("faithfulness", "factual_correctness", "deterministic_rules")
PROPOSAL_KINDS = ("profile", "plan", "workout")
ASSERTION_KINDS = (
    "state_unchanged",
    "saved_data_unchanged",
    "equipment_restriction",
    "forbidden_exercise",
    "parameter_preserved",
    "snapshot_integrity",
    "save_authorization",
    "state_transition",
    "evidence_present",
    "no_saved_state",
    "saved_state",
)
SAVED_STATUS = "saved"
INVALIDATED_STATUS = "invalidated"
MESSAGE_ANSWER_MIN_CHARS = 8


class DatasetError(ValueError):
    pass


def _fail(message: str, where: str | None = None) -> None:
    raise DatasetError(f"{where}: {message}" if where else message)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _strict_json(text: str, where: str) -> Any:
    def reject_constant(value: str) -> None:
        _fail(f"不允许非有限数 {value}", where)

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                _fail(f"重复 JSON 键 {key}", where)
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except DatasetError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        _fail(f"JSON 解析失败: {exc}", where)


def _read_json_file(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        _fail("文件缺失或不是普通文件", str(path))
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("不允许 UTF-8 BOM", str(path))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"不是合法 UTF-8: {exc}", str(path))
    return _strict_json(text, str(path))


def _load_schemas() -> tuple[dict[str, dict], Registry]:
    schemas = {}
    resources = []
    for name, relative in SCHEMA_PATHS.items():
        path = REPO / relative
        if path.is_symlink() or not path.is_file():
            _fail("固定本地 Schema 缺失", relative)
        document = _read_json_file(path)
        if not isinstance(document, dict) or document.get("$id") != SCHEMA_IDS[name]:
            _fail(f"Schema 身份不符，期望 {SCHEMA_IDS[name]}", relative)
        schemas[name] = document
        resources.append((document["$id"], Resource.from_contents(document, default_specification=DRAFT202012)))
    return schemas, Registry().with_resources(resources)


def _validator(schema: dict, registry: Registry) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def _validate(instance: Any, validator: Draft202012Validator, where: str) -> None:
    errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        _fail(f"Schema 校验失败 {path}: {error.message}", where)


def _read_jsonl(path: Path, validator: Draft202012Validator, name: str) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        _fail("JSONL 文件缺失或不是普通文件", f"{name}.jsonl")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("不允许 UTF-8 BOM", f"{name}.jsonl")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"不是合法 UTF-8: {exc}", f"{name}.jsonl")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    for number, line in enumerate(lines, 1):
        if not line.strip():
            _fail("不允许 JSONL 空行", f"{name}.jsonl:{number}")
    state = {"line": 0}

    def loads(line: str) -> Any:
        state["line"] += 1
        return _strict_json(line, f"{name}.jsonl:{state['line']}")

    try:
        rows = list(jsonlines.Reader(lines, loads=loads))
    except DatasetError:
        raise
    except (jsonlines.Error, ValueError) as exc:
        _fail(f"JSONL 解析失败: {exc}", f"{name}.jsonl")
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            _fail("记录必须为 JSON 对象", f"{name}.jsonl:{number}")
        _validate(row, validator, f"{name}.jsonl:{number}")
    return rows


def _relative_posix(value: str, field: str, where: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or value.startswith("/"):
        _fail(f"{field} 必须为版本目录内的相对路径", where)
    path = PurePosixPath(value)
    if ".." in path.parts or any(part in ("", ".") for part in path.parts):
        _fail(f"{field} 不允许越出版本目录", where)
    return path


def _inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _strict_iso(value: str, where: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("时间必须为 ISO-8601", where)
    if parsed.tzinfo is None:
        _fail("时间必须携带时区", where)


def _strip_review(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_review(item) for key, item in value.items() if key != "review"}
    if isinstance(value, list):
        return [_strip_review(item) for item in value]
    return value


def _normalize_text(value: str) -> str:
    return "".join(value.split())


def proposal_ref(business_kind: str, source_event_id: str) -> str:
    return f"{business_kind}-proposal-{source_event_id}"


def display_ref(business_kind: str, source_event_id: str) -> str:
    return f"{business_kind}-display-{source_event_id}"


def confirmation_ref(business_kind: str, source_event_id: str) -> str:
    return f"{business_kind}-confirm-{source_event_id}"


def _split_proposal_ref(value: str, where: str) -> tuple[str, str]:
    kind, separator, source = value.rpartition("-proposal-")
    if not separator or kind not in PROPOSAL_KINDS or not source:
        _fail("proposal_ref 必须为 <business_kind>-proposal-<触发提案的事件>", where)
    return kind, source


def _saved_kinds(state: dict) -> set[str]:
    kinds = set()
    if state["profile"] is not None:
        kinds.add("profile")
    if state["plans"]:
        kinds.add("plan")
    if state["workouts"]:
        kinds.add("workout")
    return kinds


def _scenario_asset_ids(scenario: dict) -> set[str]:
    asset_ids = set(scenario["input"]["attachments"])
    for event in scenario["events"]:
        asset_ids.update(event["attachments"])
    for item in scenario["reference"]:
        for fact in item["facts"]:
            if fact["asset_id"] is not None:
                asset_ids.add(fact["asset_id"])
    return asset_ids


def qrel_digest(qrel: dict) -> str:
    return _sha256_text(_canonical(_strip_review(qrel)))


def query_digest(query: dict, related_qrels: Iterable[dict]) -> str:
    payload = {
        "query": _strip_review(query),
        "qrels": sorted((_strip_review(qrel) for qrel in related_qrels), key=lambda item: item["corpus_id"]),
    }
    return _sha256_text(_canonical(payload))


def scenario_digest(scenario: dict, asset_hashes: dict[str, str]) -> str:
    payload = {"scenario": _strip_review(scenario), "assets": dict(sorted(asset_hashes.items()))}
    return _sha256_text(_canonical(payload))


def release_digest(manifest: dict) -> str:
    payload = {key: value for key, value in manifest.items() if key != "release"}
    return _sha256_text(_canonical(payload))


def corpus_identity(rows: Iterable[dict]) -> str:
    payload = [
        {"corpus_id": row["corpus_id"], "text": row["text"], **row["metadata"]}
        for row in rows
    ]
    return _sha256_text(_canonical(payload))


def retrieval_corpus_identity(corpus: RetrievalCorpus) -> str:
    payload = [
        {"corpus_id": row["id"], "text": row["text"], **{field: row[field] for field in rt.MAPPING_FIELDS}}
        for row in corpus.records
    ]
    return _sha256_text(_canonical(payload))


@dataclass(frozen=True)
class EvaluationDataset:
    version_dir: Path
    manifest: dict
    corpus: tuple[dict, ...]
    queries: tuple[dict, ...]
    qrels: tuple[dict, ...]
    scenarios: tuple[dict, ...]
    assets: dict[str, Path]
    review: dict
    manifest_sha256: str
    content_sha256: str
    corpus_sha256: str

    @property
    def version(self) -> str:
        return self.manifest["dataset_version"]

    def qrels_by_query(self) -> dict[str, tuple[dict, ...]]:
        grouped: dict[str, list[dict]] = {}
        for qrel in self.qrels:
            grouped.setdefault(qrel["query_id"], []).append(qrel)
        return {query_id: tuple(rows) for query_id, rows in grouped.items()}

    def scoring_queries(self) -> list[dict]:
        positives: dict[str, list[dict]] = {}
        for qrel in self.qrels:
            if qrel["grade"] >= 1:
                positives.setdefault(qrel["query_id"], []).append({"id": qrel["corpus_id"], "grade": qrel["grade"]})
        return [
            {
                "id": query["query_id"],
                "tags": list(query["tags"]),
                "family": query["family"],
                "split": self.version,
                "labels": positives.get(query["query_id"], []),
                "expect_empty": query["expect_empty"],
            }
            for query in self.queries
        ]

    def query_cache_queries(self) -> list[dict]:
        return [{"id": item["query_id"], "query": item["query"]} for item in self.queries]

    def reviewed_corpus_ids(self) -> dict[str, set[str]]:
        reviewed: dict[str, set[str]] = {}
        for qrel in self.qrels:
            reviewed.setdefault(qrel["query_id"], set()).add(qrel["corpus_id"])
        return reviewed

    def unreviewed_candidates(self, rankings: dict[str, list[str]]) -> dict[str, list[str]]:
        reviewed = self.reviewed_corpus_ids()
        unknown: dict[str, list[str]] = {}
        for query_id, ids in rankings.items():
            seen = reviewed.get(query_id, set())
            missing = [corpus_id for corpus_id in ids if corpus_id not in seen]
            if missing:
                unknown[query_id] = missing
        return unknown

    def attachment_hashes(self, scenario: dict) -> dict[str, str]:
        return {asset_id: _sha256_file(self.assets[asset_id]) for asset_id in sorted(_scenario_asset_ids(scenario))}

    def identity(self) -> dict:
        relative = self.version_dir
        if _inside(REPO, relative):
            relative_text = relative.resolve().relative_to(REPO.resolve()).as_posix()
        else:
            relative_text = str(relative.resolve())
        return {
            "version": self.version,
            "version_dir": relative_text,
            "manifest_sha256": self.manifest_sha256,
            "content_sha256": self.content_sha256,
            "corpus_sha256": self.corpus_sha256,
            "schemas": self.manifest["schemas"],
            "source": self.manifest["source"],
            "files": {name: row["sha256"] for name, row in self.manifest["files"].items()},
        }


def _review_issues(review: dict, where: str, digest: str) -> list[str]:
    issues = []
    status = review["status"]
    if status != "approved":
        issues.append(f"{where}: 审阅状态为 {status}")
        return issues
    if review["reviewer_type"] != "user":
        issues.append(f"{where}: 审阅者必须为用户")
    if not review["reviewer"]:
        issues.append(f"{where}: 缺少审阅者")
    if not review["decision_ref"]:
        issues.append(f"{where}: 缺少用户决定引用")
    if review["reviewed_at"] is None or review["content_sha256"] is None:
        issues.append(f"{where}: 批准记录不完整")
    elif review["content_sha256"] != digest:
        issues.append(f"{where}: 批准内容哈希与当前内容不一致")
    return issues


def _checked_local_file(path: Path, root: Path, where: str) -> None:
    current = path
    while current != root:
        if current.is_symlink():
            _fail("路径包含符号链接", where)
        current = current.parent
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _fail("声明文件缺失或不是普通文件", where)
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("路径解析越出版本目录", where)
    if not resolved.is_file():
        _fail("声明文件缺失或不是普通文件", where)


def _validate_manifest(
    version_dir: Path,
    manifest: dict,
    schemas: dict[str, dict],
    registry: Registry,
) -> tuple[dict[str, Path], dict[str, Path], dict[str, str]]:
    _validate(manifest, _validator(schemas["manifest"], registry), str(version_dir / "manifest.json"))
    if manifest["dataset_version"] != version_dir.name:
        _fail("dataset_version 必须与版本目录名一致", "manifest.json:dataset_version")
    _strict_iso(manifest["created_at"], "manifest.json:created_at")
    for name, expected_path in SCHEMA_PATHS.items():
        declared = manifest["schemas"][name]
        if declared["path"] != expected_path:
            _fail(f"Schema 路径必须为固定本地资源 {expected_path}", "manifest.json:schemas")
        actual = _sha256_file(REPO / expected_path)
        if declared["sha256"] != actual:
            _fail(f"Schema 哈希不符: {name}", "manifest.json:schemas")
    files = {}
    for name, expected_name in DATA_FILES.items():
        declared = manifest["files"][name]
        if declared["path"] != expected_name:
            _fail(f"文件路径必须为 {expected_name}", f"manifest.json:files.{name}")
        relative = _relative_posix(declared["path"], "path", f"manifest.json:files.{name}")
        path = version_dir / relative
        _checked_local_file(path, version_dir, f"manifest.json:files.{name}")
        if _sha256_file(path) != declared["sha256"]:
            _fail("声明文件哈希不符", f"manifest.json:files.{name}")
        files[name] = path
    assets = {}
    for index, declared in enumerate(manifest["assets"]):
        relative = _relative_posix(declared["path"], "path", f"manifest.json:assets[{index}]")
        if relative.parts[0] != "assets" or len(relative.parts) < 2:
            _fail("附件必须位于 assets 目录", f"manifest.json:assets[{index}]")
        path = version_dir / relative
        _checked_local_file(path, version_dir, f"manifest.json:assets[{index}]")
        if _sha256_file(path) != declared["sha256"]:
            _fail("附件哈希不符", f"manifest.json:assets[{index}]")
        if declared["asset_id"] in assets:
            _fail("附件 ID 重复", f"manifest.json:assets[{index}]")
        assets[declared["asset_id"]] = path
    if manifest["counts"]["assets"] != len(assets):
        _fail("附件数量与清单不符", "manifest.json:counts.assets")
    allowed_files = {"manifest.json"} | {row["path"] for row in manifest["files"].values()} | {
        row["path"] for row in manifest["assets"]
    }
    allowed_dirs = set()
    for relative in allowed_files:
        parts = PurePosixPath(relative).parts
        for index in range(1, len(parts)):
            allowed_dirs.add("/".join(parts[:index]))
    for path in version_dir.rglob("*"):
        relative = path.relative_to(version_dir).as_posix()
        if path.is_dir() and relative in allowed_dirs and not path.is_symlink():
            continue
        if path.is_file() and relative in allowed_files and not path.is_symlink():
            continue
        _fail("版本目录存在未声明文件或目录", relative)
    return files, assets, {name: row["sha256"] for name, row in manifest["schemas"].items()}


def _validate_corpus(rows: list[dict], manifest: dict, corpus: RetrievalCorpus) -> None:
    if len(rows) != manifest["counts"]["corpus"]:
        _fail("corpus 数量与清单不符", "manifest.json:counts.corpus")
    if len(rows) != len(corpus.records):
        _fail("corpus 快照与真实目录数量不符", "corpus.jsonl")
    expected_source = {
        "catalog": {
            "path": SOURCE_PATHS["catalog"],
            "sha256": corpus.sources["catalog"]["sha256"],
            "count": len(corpus.catalog),
        },
        "supplements": {
            "path": SOURCE_PATHS["supplements"],
            "sha256": corpus.sources["published"]["sha256"],
            "version": corpus.sources["published"]["version"],
        },
        "mappings": {
            "path": SOURCE_PATHS["mappings"],
            "sha256": corpus.sources["mappings"]["sha256"],
        },
        "encoding_rule_version": rt.ENCODING_RULE_VERSION,
    }
    if manifest["source"] != expected_source:
        _fail("manifest 来源与当前目录、补充数据或编码版本不一致", "manifest.json:source")
    records = {record["id"]: record for record in corpus.records}
    seen = set()
    for index, row in enumerate(rows, 1):
        where = f"corpus.jsonl:{index}"
        corpus_id = row["corpus_id"]
        if corpus_id in seen:
            _fail("corpus_id 重复", where)
        seen.add(corpus_id)
        record = records.get(corpus_id)
        if record is None:
            _fail("corpus_id 不在真实动作目录", where)
        if row["source"] != expected_source:
            _fail("source 与 manifest 来源不一致", where)
        if row["text"] != record["text"]:
            _fail("text 与真实索引编码结果不一致", where)
        if row["metadata"] != {field: record[field] for field in rt.MAPPING_FIELDS}:
            _fail("metadata 与真实目录筛选字段不一致", where)
    if seen != set(records):
        _fail("corpus 未完整覆盖真实动作目录", "corpus.jsonl")


def _validate_assertion_timing(
    assertion: dict,
    event_ids: dict[str, int],
    event_types: dict[str, str],
    event_targets: dict[str, dict | None],
    where: str,
) -> None:
    kind = assertion["kind"]
    event_id = assertion["event_id"]
    event_where = f"{where}:assertion:{assertion['checkpoint_id']}"
    if kind in ("save_authorization", "snapshot_integrity"):
        proposal_kind, source = _split_proposal_ref(assertion["proposal_ref"], event_where)
        if source not in event_ids:
            _fail("提案引用指向未知事件", event_where)
        if event_types[source] != "message":
            _fail("提案必须由用户消息事件触发", event_where)
        if event_ids[source] > event_ids[event_id]:
            _fail("提案引用不得指向尚未发生的事件", event_where)
        if kind == "snapshot_integrity":
            return
        if event_types[event_id] == "message":
            if assertion["expected_saved"]:
                _fail("消息检查点必须验证保存前零提交", event_where)
            return
        if not assertion["expected_saved"]:
            return
        if event_types[event_id] != "confirm":
            _fail("保存成功检查点必须绑定确认事件", event_where)
        target = event_targets[event_id]
        if target is None or target["proposal_ref"] != assertion["proposal_ref"]:
            _fail("保存断言与确认事件的目标不一致", event_where)
        if target["business_kind"] != proposal_kind:
            _fail("保存断言的业务类型与确认目标不一致", event_where)
    if kind == "state_transition":
        if assertion["to_status"] == SAVED_STATUS and event_types[event_id] != "confirm":
            _fail("保存状态迁移必须发生在确认事件检查点", event_where)
        if assertion["to_status"] == INVALIDATED_STATUS and event_types[event_id] != "cancel":
            _fail("失效状态迁移必须发生在取消事件检查点", event_where)


def _validate_scenarios(rows: list[dict], assets: dict[str, Path]) -> None:
    seen = set()
    for index, scenario in enumerate(rows, 1):
        where = f"scenarios.jsonl:{index}:{scenario.get('scenario_id', '<unknown>')}"
        scenario_id = scenario["scenario_id"]
        if scenario_id in seen:
            _fail("scenario_id 重复", where)
        seen.add(scenario_id)
        state = scenario["initial_state"]
        if state["profile"] is not None:
            ProfileContent.model_validate(state["profile"])
        plan_ids = []
        current = []
        for plan in state["plans"]:
            PlanRecord.model_validate(plan)
            plan_ids.append(plan["id"])
            if plan["is_current"]:
                current.append(plan["id"])
        if len(plan_ids) != len(set(plan_ids)):
            _fail("初始状态计划 ID 重复", where)
        for workout in state["workouts"]:
            WorkoutRecord.model_validate(workout)
        if len(current) > 1:
            _fail("初始状态最多一个当前计划", where)
        if state["current_plan_id"] is not None:
            if state["current_plan_id"] not in plan_ids:
                _fail("current_plan_id 未引用初始状态计划", where)
            if current != [state["current_plan_id"]]:
                _fail("current_plan_id 与计划 is_current 不一致", where)
        elif current:
            _fail("存在 is_current 计划但缺少 current_plan_id", where)
        _strict_iso(state["now"], f"{where}:initial_state.now")
        event_ids = {}
        for order, event in enumerate(scenario["events"]):
            event_id = event["event_id"]
            if event_id in event_ids:
                _fail("event_id 重复", where)
            event_ids[event_id] = order
        event_types = {event["event_id"]: event["type"] for event in scenario["events"]}
        event_targets = {event["event_id"]: event["target"] for event in scenario["events"]}
        for event in scenario["events"]:
            event_where = f"{where}:event:{event['event_id']}"
            for asset_id in event["attachments"]:
                if asset_id not in assets:
                    _fail("附件引用未在 manifest 声明", event_where)
            if event["type"] in ("confirm", "cancel"):
                source = event["source_event_id"]
                if source not in event_ids or event_ids[source] >= event_ids[event["event_id"]]:
                    _fail("确认或取消事件必须引用更早事件", event_where)
                if event_types[source] != "message":
                    _fail("确认或取消事件必须引用触发提案的用户消息事件", event_where)
                target = event["target"]
                if target is None:
                    _fail("确认或取消事件缺少目标", event_where)
                kind = target["business_kind"]
                if target["proposal_ref"] != proposal_ref(kind, source):
                    _fail("提案引用必须唯一指向触发该提案的用户事件", event_where)
                if event["type"] == "confirm":
                    if target["display_ref"] != display_ref(kind, source):
                        _fail("确认事件的展示引用必须来自同一提案", event_where)
                    if target["confirmation_ref"] != confirmation_ref(kind, source):
                        _fail("确认事件的确认入口必须来自同一提案", event_where)
                elif target["display_ref"] is not None or target["confirmation_ref"] is not None:
                    _fail("取消事件不携带展示与确认入口", event_where)
        for asset_id in scenario["input"]["attachments"]:
            if asset_id not in assets:
                _fail("输入附件引用未在 manifest 声明", where)
        messages = {event["event_id"] for event in scenario["events"] if event["type"] == "message"}
        checkpoints = {}

        def register(checkpoint_id: str, event_id: str, label: str) -> None:
            if event_id not in event_types:
                _fail(f"{label} 引用了未知事件", where)
            if checkpoint_id in checkpoints and checkpoints[checkpoint_id] != event_id:
                _fail("检查点的 event_id 不一致", where)
            checkpoints.setdefault(checkpoint_id, event_id)

        reference_ids = set()
        for item in scenario["reference"]:
            checkpoint_id = item["checkpoint_id"]
            if checkpoint_id in reference_ids:
                _fail("checkpoint_id 重复", where)
            reference_ids.add(checkpoint_id)
            register(checkpoint_id, item["event_id"], "reference")
            if item["event_id"] not in messages:
                _fail("reference 必须引用消息事件", where)
            fact_ids = [fact["fact_id"] for fact in item["facts"]]
            if len(fact_ids) != len(set(fact_ids)):
                _fail("fact_id 重复", where)
            for fact in item["facts"]:
                if fact["asset_id"] is not None and fact["asset_id"] not in assets:
                    _fail("事实引用了未声明附件", where)
        rubric_ids = set()
        for item in scenario["rubric"]:
            checkpoint_id = item["checkpoint_id"]
            if checkpoint_id in rubric_ids:
                _fail("checkpoint_id 重复", where)
            rubric_ids.add(checkpoint_id)
            if checkpoint_id not in reference_ids:
                _fail("rubric 引用了无参考回答的检查点", where)
            if item["event_id"] not in messages:
                _fail("rubric 必须引用消息事件", where)
            if item["event_id"] != checkpoints.get(checkpoint_id):
                _fail("rubric 的 event_id 与检查点不一致", where)
        if rubric_ids != reference_ids:
            _fail("rubric 未完整覆盖回答检查点", where)
        answers = [_normalize_text(item["answer"]) for item in scenario["reference"]]
        for event in scenario["events"]:
            if event["type"] != "message" or not event["content"]:
                continue
            content = _normalize_text(event["content"])
            for answer in answers:
                if len(answer) >= MESSAGE_ANSWER_MIN_CHARS and answer in content:
                    _fail("消息事件不得承载预写助理回答", f"{where}:event:{event['event_id']}")
        assertion_ids = {assertion["checkpoint_id"] for assertion in scenario["assertions"]}
        for assertion in scenario["assertions"]:
            if assertion["kind"] not in ASSERTION_KINDS:
                _fail("未知断言类型", f"{where}:assertion:{assertion['checkpoint_id']}")
            register(assertion["checkpoint_id"], assertion["event_id"], "assertions")
            if assertion["kind"] == "no_saved_state":
                saved_kinds = _saved_kinds(state)
                limit = event_ids[assertion["event_id"]]
                for event in scenario["events"]:
                    if event_ids[event["event_id"]] > limit:
                        break
                    if event["type"] == "confirm" and event["target"] is not None:
                        saved_kinds.add(event["target"]["business_kind"])
                if assertion["business_kind"] in saved_kinds:
                    _fail(
                        "no_saved_state 与检查点已保存记录矛盾",
                        f"{where}:assertion:{assertion['checkpoint_id']}",
                    )
            _validate_assertion_timing(assertion, event_ids, event_types, event_targets, where)
        metric_rows = {}
        for row in scenario["metric_applicability"]:
            register(row["checkpoint_id"], row["event_id"], "metric_applicability")
            metric_rows.setdefault(row["checkpoint_id"], []).append(row)
        generation_checkpoints = set()
        for checkpoint_id, event_id in checkpoints.items():
            rows = metric_rows.get(checkpoint_id, [])
            if len(rows) != len(METRICS) or {row["metric"] for row in rows} != set(METRICS):
                _fail("每个检查点必须逐项声明三项指标", where)
            by_metric = {row["metric"]: row for row in rows}
            faithfulness = by_metric["faithfulness"]
            factual = by_metric["factual_correctness"]
            deterministic = by_metric["deterministic_rules"]
            generation = faithfulness["applicable"] or factual["applicable"]
            if generation:
                generation_checkpoints.add(checkpoint_id)
                if checkpoint_id not in reference_ids:
                    _fail("生成指标适用时必须提供参考回答", where)
            elif checkpoint_id in reference_ids:
                _fail("生成指标不适用时不得声明参考回答", where)
            if not generation:
                for row in (faithfulness, factual):
                    if not row["reason"]:
                        _fail("生成指标不适用时必须写明原因", where)
            if deterministic["applicable"]:
                if checkpoint_id not in assertion_ids:
                    _fail("确定性规则适用时至少需要一条断言", where)
            else:
                if not deterministic["reason"]:
                    _fail("确定性规则不适用时必须写明原因", where)
                if checkpoint_id in assertion_ids:
                    _fail("检查点存在断言时确定性规则必须适用", where)
            if event_types[event_id] != "message":
                if generation:
                    _fail("业务事件检查点不得适用生成指标", where)
                if not deterministic["applicable"]:
                    _fail("业务事件检查点必须适用确定性规则", where)
                if checkpoint_id not in assertion_ids:
                    _fail("业务事件检查点至少需要一条断言", where)
            if checkpoint_id not in reference_ids and checkpoint_id not in assertion_ids:
                _fail("检查点必须提供参考回答或至少一条断言", where)
        if generation_checkpoints != reference_ids:
            _fail("参考回答与生成指标适用性不一致", where)
        categories = [tag for tag in scenario["tags"] if tag in SCENARIO_CATEGORIES]
        if len(categories) != 1:
            _fail("场景必须且只能包含一个主要业务类别标签", where)


def _query_profile(query: dict) -> ProfileContent:
    saved = query["conditions"]["saved_profile"]
    return ProfileContent(
        goal=None,
        experience=None,
        environment=None,
        availability=None,
        health_notes=None,
        movement_restrictions=None,
        unavailable_equipment=saved["unavailable_equipment"] or None,
        forbidden_exercise_ids=saved["forbidden_exercise_ids"] or None,
    )


def _query_arguments(query: dict) -> CatalogSearchArguments:
    conditions = query["conditions"]
    return CatalogSearchArguments(
        purpose=query["purpose"],
        name=query["query"],
        equipment=conditions["equipment"],
        body_part=conditions["body_part"],
        target=conditions["target"],
        muscle_group=conditions["muscle_group"],
        task_environment=conditions["environment"],
        available_equipment=conditions["declared_equipment"] or None,
        unavailable_equipment=conditions["unavailable_equipment"] or None,
        excluded_ids=conditions["excluded_ids"] or None,
    )


def query_conditions(query: dict, catalog, mappings: dict):
    return evaluate_search_conditions(_query_arguments(query), _query_profile(query), catalog, mappings)


def _validate_queries(
    rows: list[dict],
    manifest: dict,
    catalog,
    mappings: dict,
    scenario_ids: set[str],
) -> dict[str, Any]:
    if len(rows) != manifest["counts"]["queries"]:
        _fail("queries 数量与清单不符", "manifest.json:counts.queries")
    seen = set()
    evaluated = {}
    for index, query in enumerate(rows, 1):
        where = f"queries.jsonl:{index}:{query['query_id']}"
        query_id = query["query_id"]
        if query_id in seen:
            _fail("query_id 重复", where)
        seen.add(query_id)
        if query["scenario_id"] is not None and query["scenario_id"] not in scenario_ids:
            _fail("scenario_id 引用不存在的场景", where)
        if len(query["tags"]) != len(set(query["tags"])):
            _fail("tags 重复", where)
        try:
            evaluated[query_id] = query_conditions(query, catalog, mappings)
        except ValueError as exc:
            _fail(f"条件语义校验失败: {exc}", where)
    return evaluated


def _evidence_documents(manifest: dict) -> dict[str, Any]:
    documents = {}
    for key in ("catalog", "supplements", "mappings"):
        relative = manifest["source"][key]["path"]
        documents[relative] = _read_json_file(REPO / relative)
    return documents


def _validate_qrels(
    rows: list[dict],
    manifest: dict,
    queries: list[dict],
    corpus_ids: set[str],
    catalog,
    evaluated: dict[str, Any],
) -> None:
    if len(rows) != manifest["counts"]["qrels"]:
        _fail("qrels 数量与清单不符", "manifest.json:counts.qrels")
    query_ids = {query["query_id"] for query in queries}
    evidence_documents = _evidence_documents(manifest)
    seen = set()
    positives = Counter()
    for index, qrel in enumerate(rows, 1):
        where = f"qrels.jsonl:{index}:{qrel['query_id']}/{qrel['corpus_id']}"
        if qrel["query_id"] not in query_ids:
            _fail("query_id 引用不存在的查询", where)
        if qrel["corpus_id"] not in corpus_ids:
            _fail("corpus_id 引用不存在的语料", where)
        key = (qrel["query_id"], qrel["corpus_id"])
        if key in seen:
            _fail("query_id 与 corpus_id 组合重复", where)
        seen.add(key)
        source_path = qrel["evidence"]["source_path"]
        if source_path not in evidence_documents:
            _fail("evidence.source_path 不在冻结来源文件中", where)
        try:
            resolve_pointer(evidence_documents[source_path], qrel["evidence"]["json_pointer"])
        except (JsonPointerException, TypeError) as exc:
            _fail(f"evidence.json_pointer 无法解析: {exc}", where)
        if qrel["grade"] >= 1:
            positives[qrel["query_id"]] += 1
            exercise = catalog.get(qrel["corpus_id"])
            if exercise is None or not evaluated[qrel["query_id"]].conditions.matches(exercise):
                _fail("grade>=1 的正例不符合该请求用途的有效硬条件", where)
    for query in queries:
        query_id = query["query_id"]
        if query["expect_empty"] and positives[query_id]:
            _fail("expect_empty 查询不得包含 grade>=1 的判断", f"queries.jsonl:{query_id}")
        if not query["expect_empty"] and not positives[query_id]:
            _fail("有答案查询至少需要一个 grade>=1 的判断", f"queries.jsonl:{query_id}")


def _quota_report(queries: list[dict], scenarios: list[dict]) -> dict:
    purposes = Counter(query["purpose"] for query in queries)
    empty = Counter(query["purpose"] for query in queries if query["expect_empty"])
    categories = Counter(
        tag
        for scenario in scenarios
        for tag in scenario["tags"]
        if tag in SCENARIO_CATEGORIES
    )
    return {
        "purposes": {name: purposes[name] for name in PURPOSE_QUOTAS},
        "expect_empty": {name: empty[name] for name in EMPTY_QUOTAS},
        "scenario_categories": {name: categories[name] for name in SCENARIO_CATEGORIES},
    }


def _approval_report(
    manifest: dict,
    queries: list[dict],
    qrels: list[dict],
    scenarios: list[dict],
    assets: dict[str, Path],
) -> dict:
    issues = []
    qrels_by_query: dict[str, list[dict]] = {}
    for qrel in qrels:
        qrels_by_query.setdefault(qrel["query_id"], []).append(qrel)
    for query in queries:
        issues.extend(
            _review_issues(
                query["review"],
                f"queries.jsonl:{query['query_id']}",
                query_digest(query, qrels_by_query.get(query["query_id"], [])),
            )
        )
        if not query["judgement_scope"]["complete"]:
            issues.append(f"queries.jsonl:{query['query_id']}: judgement_scope.complete 未确认")
    for qrel in qrels:
        issues.extend(
            _review_issues(
                qrel["review"],
                f"qrels.jsonl:{qrel['query_id']}/{qrel['corpus_id']}",
                qrel_digest(qrel),
            )
        )
    for scenario in scenarios:
        asset_hashes = {
            asset_id: _sha256_file(assets[asset_id])
            for asset_id in sorted(_scenario_asset_ids(scenario))
        }
        issues.extend(
            _review_issues(
                scenario["review"],
                f"scenarios.jsonl:{scenario['scenario_id']}",
                scenario_digest(scenario, asset_hashes),
            )
        )
    release = manifest["release"]
    if release["status"] != "approved":
        issues.append(f"manifest.json:release 状态为 {release['status']}")
    else:
        if release["reviewer_type"] != "user":
            issues.append("manifest.json:release 审阅者必须为用户")
        if not release["reviewer"] or not release["decision_ref"] or release["reviewed_at"] is None:
            issues.append("manifest.json:release 批准记录不完整")
        if release["content_sha256"] is None or release["content_sha256"] != release_digest(manifest):
            issues.append("manifest.json:release 内容摘要与当前清单不一致")
    return {"issues": issues}


def _load(version_dir: Path) -> EvaluationDataset:
    version_dir = Path(version_dir)
    if not version_dir.exists() or not version_dir.is_dir():
        _fail("数据版本目录不存在", str(version_dir))
    version_dir = version_dir.resolve()
    manifest_path = version_dir / "manifest.json"
    if manifest_path.is_symlink():
        _fail("manifest 不允许符号链接", str(manifest_path))
    manifest = _read_json_file(manifest_path)
    if not isinstance(manifest, dict):
        _fail("manifest 必须为 JSON 对象", str(manifest_path))
    schemas, registry = _load_schemas()
    files, assets, _ = _validate_manifest(version_dir, manifest, schemas, registry)
    corpus_rows = _read_jsonl(files["corpus"], _validator(schemas["corpus"], registry), "corpus")
    query_rows = _read_jsonl(files["queries"], _validator(schemas["queries"], registry), "queries")
    qrel_rows = _read_jsonl(files["qrels"], _validator(schemas["qrels"], registry), "qrels")
    scenario_rows = _read_jsonl(files["scenarios"], _validator(schemas["scenarios"], registry), "scenarios")
    if len(qrel_rows) != manifest["counts"]["qrels"]:
        _fail("qrels 数量与清单不符", "manifest.json:counts.qrels")
    if len(scenario_rows) != manifest["counts"]["scenarios"]:
        _fail("scenarios 数量与清单不符", "manifest.json:counts.scenarios")
    try:
        corpus = RetrievalCorpus.load()
    except (ValueError, RuntimeError) as exc:
        _fail(f"真实检索语料未通过批准校验: {exc}", "corpus.jsonl")
    _validate_corpus(corpus_rows, manifest, corpus)
    mappings = rt.load_mappings(REPO / SOURCE_PATHS["mappings"])
    _validate_scenarios(scenario_rows, assets)
    scenario_ids = {scenario["scenario_id"] for scenario in scenario_rows}
    evaluated = _validate_queries(query_rows, manifest, corpus.catalog, mappings, scenario_ids)
    _validate_qrels(
        qrel_rows,
        manifest,
        query_rows,
        {row["corpus_id"] for row in corpus_rows},
        corpus.catalog,
        evaluated,
    )
    quota = _quota_report(query_rows, scenario_rows)
    counts = {
        "corpus": len(corpus_rows),
        "queries": len(query_rows),
        "qrels": len(qrel_rows),
        "scenarios": len(scenario_rows),
        "assets": len(assets),
    }
    if counts != manifest["counts"]:
        _fail("实际数量与 manifest counts 不一致", "manifest.json:counts")
    issues = _approval_report(manifest, query_rows, qrel_rows, scenario_rows, assets)["issues"]
    for name, expected in PURPOSE_QUOTAS.items():
        if quota["purposes"][name] != expected:
            issues.append(f"queries: purpose {name} 数量为 {quota['purposes'][name]}，正式配额为 {expected}")
    for name, expected in EMPTY_QUOTAS.items():
        if quota["expect_empty"][name] != expected:
            issues.append(f"queries: {name} 无答案数量为 {quota['expect_empty'][name]}，正式配额为 {expected}")
    if counts["queries"] != QUERY_COUNT:
        issues.append(f"queries: 数量为 {counts['queries']}，正式要求 {QUERY_COUNT}")
    if counts["scenarios"] != SCENARIO_COUNT:
        issues.append(f"scenarios: 数量为 {counts['scenarios']}，正式要求 {SCENARIO_COUNT}")
    for name, expected in SCENARIO_CATEGORIES.items():
        if quota["scenario_categories"][name] != expected:
            issues.append(
                f"scenarios: 类别 {name} 数量为 {quota['scenario_categories'][name]}，正式配额为 {expected}"
            )
    if manifest["quotas"] != {
        "purposes": PURPOSE_QUOTAS,
        "expect_empty": EMPTY_QUOTAS,
        "scenario_categories": SCENARIO_CATEGORIES,
    }:
        issues.append("manifest.json:quotas 与正式配额不一致")
    review_counts = {
        name: dict(Counter(row["review"]["status"] for row in rows))
        for name, rows in (
            ("queries", query_rows),
            ("qrels", qrel_rows),
            ("scenarios", scenario_rows),
        )
    }
    review = {
        "ready": not issues,
        "issues": issues,
        "counts": counts,
        "quotas": quota,
        "review_counts": review_counts,
        "release": {
            "status": manifest["release"]["status"],
            "content_sha256": manifest["release"]["content_sha256"],
            "digest_valid": manifest["release"]["content_sha256"] == release_digest(manifest),
        },
    }
    return EvaluationDataset(
        version_dir=version_dir,
        manifest=manifest,
        corpus=tuple(corpus_rows),
        queries=tuple(query_rows),
        qrels=tuple(qrel_rows),
        scenarios=tuple(scenario_rows),
        assets=assets,
        review=review,
        manifest_sha256=_sha256_file(manifest_path),
        content_sha256=release_digest(manifest),
        corpus_sha256=corpus_identity(corpus_rows),
    )


def load_for_review(version_dir: Path) -> EvaluationDataset:
    return _load(version_dir)


def load_for_evaluation(version_dir: Path) -> EvaluationDataset:
    dataset = _load(version_dir)
    if dataset.review["issues"]:
        _fail("正式评测数据未就绪: " + "；".join(dataset.review["issues"]), str(dataset.version_dir))
    return dataset
