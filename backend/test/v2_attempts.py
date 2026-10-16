import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from test.v2_budget import BudgetLedger, LedgerAttributionUnresolved
from test.v2_contract import ROUNDS


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_record(path, record):
    Path(path).write_text(json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")


def read_record(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def reserve_experiment(directory):
    # 目录创建同时阻止旧证据与并发入口覆盖。
    Path(directory).mkdir(parents=True, exist_ok=False)


def budget_path(directory):
    root = Path(directory).resolve()
    manifest = root / "manifest.json"
    if manifest.exists():
        recorded = read_record(manifest).get("budget_ledger_path")
        if recorded:
            return Path(recorded)
    if (root / "budget-ledger.json").exists():
        return root / "budget-ledger.json"
    shared = root.parent / "budget-ledger.json"
    if not shared.exists() and any(root.parent.glob("*/budget-ledger.json")):
        raise ValueError("存在旧式实验预算；新实验需要显式关联原实验")
    return shared


def execution_identity(backend, cases_path, model_identity, catalog_path):
    backend = Path(backend)
    paths = sorted(set(backend.joinpath("app").rglob("*.py")) | set(backend.joinpath("skills").rglob("*")) | set(backend.joinpath("test").rglob("*.py")))
    paths.extend([Path(cases_path), Path(catalog_path)])
    paths.extend(backend / name for name in (".env", "pyproject.toml", "uv.lock"))
    digest = hashlib.sha256()
    for path in paths:
        if path.is_file():
            digest.update(str(path.resolve()).encode())
            digest.update(path.read_bytes())
    return {"source_sha256": digest.hexdigest(), "model_identity": model_identity}


def claim_slot(env):
    # 领取与共享停止置位共用同一把锁：置位后新领取被拒（不创建 claim），保证原子边界。
    root = Path(env.execution_root)
    claim = root / "claims" / f"{env.case_id}-r{env.round_index}.json"
    record = {
        "experiment_id": env.experiment_scope or env.experiment_id, "case_id": env.case_id,
        "round_index": env.round_index, "status": "claimed", "started_at": timestamp(),
        "execution_unit": f"slot:{env.slot}", "process_index": env.process_index,
        "budget_ledger_path": env.budget_ledger_path,
        "evidence_dir": env.evidence_dir,
    }

    def create():
        claim.parent.mkdir(parents=True, exist_ok=True)
        with claim.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        return claim

    ledger = BudgetLedger(Path(env.budget_ledger_path),
                          experiment_id=env.experiment_scope or env.experiment_id)
    try:
        # 归属未确认：拒绝新增领取（不创建 claim），保留归属问题证据供监督裁定。
        ledger.claim_guarded(create)
    except LedgerAttributionUnresolved as unresolved:
        write_record(root / "ledger-attribution-unresolved.json", {
            "experiment_id": env.experiment_scope or env.experiment_id,
            "case_id": env.case_id, "round_index": env.round_index, "slot": env.slot,
            "status": "blocked_by_ledger_attribution",
            "attribution": unresolved.attribution, "at": timestamp(),
        })
        raise
    artifacts = [Path(env.evidence_dir), Path(env.attachment_root)]
    for database in (env.database_path, env.trace_path):
        artifacts.extend(Path(database + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
    if any(path.exists() for path in artifacts):
        record["status"] = "rejected_existing_artifacts"
        write_record(claim, record)
        raise FileExistsError(f"槽位已有证据：{env.case_id}/{env.round_index}")
    return claim


def finish_slot(claim, observation):
    # 终态落盘同时固定执行单元身份：同一槽位只结算一次，重复完成保持幂等。
    record = read_record(claim)
    record.update(status=observation.status, ended_at=timestamp(), result=observation.model_dump(),
                  execution_unit=f"slot:{observation.slot}")
    write_record(claim, record)


def create_attempt(directory, case_id, round_index, purpose, reason, identity):
    root = Path(directory).resolve()
    if purpose not in {"reproduction", "extra"} or not reason.strip():
        raise ValueError("追加运行需要 purpose 与原因")
    manifest = read_record(root / "manifest.json")
    if identity != manifest.get("execution_identity"):
        raise ValueError("冻结代码/配置身份不匹配；需要关联原槽位的新实验身份")
    parent = root / "cases" / case_id / f"round-{round_index}" / "round.json"
    claim = root / "claims" / f"{case_id}-r{round_index}.json"
    if not parent.exists() and not claim.exists():
        raise ValueError("原槽位未领取且无结果")
    attempt_id = uuid4().hex
    attempt_root = root / "reproductions" / attempt_id
    ledger = BudgetLedger(budget_path(root), experiment_id=manifest["experiment_id"])
    ledger.claim_guarded(lambda: attempt_root.mkdir(parents=True, exist_ok=False))
    record = {
        "attempt_id": attempt_id, "purpose": purpose, "reason": reason,
        "parent_slot": {"experiment_id": manifest["experiment_id"], "case_id": case_id, "round_index": round_index},
        "execution_identity": identity, "started_at": timestamp(), "ended_at": None,
        "status": "claimed", "failure_reproduced": None,
        "budget_ledger_path": str(budget_path(root)),
        "evidence_refs": {name: str(attempt_root / name) for name in ("cases", "dbs", "traces", "attachments")},
        "parent_evidence": str(parent),
    }
    write_record(attempt_root / "attempt.json", record)
    return attempt_root, record


def attempt_summary(directory):
    attempts = [read_record(path) for path in sorted((Path(directory) / "reproductions").glob("*/attempt.json"))]
    return {"count": len(attempts), "attempts": attempts}


def validate_unique_rounds(rounds):
    seen = set()
    for record in rounds:
        key = (record.case_id, record.round_index)
        if key in seen:
            raise ValueError(f"重复正式槽位：{key}")
        if record.round_index not in range(ROUNDS):
            raise ValueError(f"无效正式轮次：{key}")
        seen.add(key)
