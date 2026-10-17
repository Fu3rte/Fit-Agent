"""v2 Embedding 预算账本：跨进程、持久化、原子更新的记账与暂停状态。

账本按模型分别累计已结算的真实用量、在途登记与未知用量。暂停阈值只读取已结算
`actual`：达到该模型上限即置位暂停状态，两个进程共享同一文件与同一把锁。
预占量仅作为登记字段保留，执行门禁角色取消。
"""

import hashlib
import json
import os
import time
from pathlib import Path

DEFAULT_LIMITS = {
    "qwen3.7-text-embedding-flash": 500_000,
    "text-embedding-v3": 500_000,
}
TOTAL_LIMIT = 1_000_000

# 共享停止：类别固定登记；连续运行错误阈值固定为 3。同一账本文件与同一把锁承载停止状态。
CONSECUTIVE_LIMIT = 3
STOP_CATEGORIES = (
    "unauthorized_write",
    "wrong_target_write",
    "leak",
    "production_drift",
    "frozen_identity_drift",
    "trace_write_failure",
    "milvus_unavailable",
    "consecutive_run_errors",
)
RUN_OUTCOMES = ("success", "business_failure", "run_error", "budget_paused", "cancelled", "expected_failure")


class BudgetExceeded(RuntimeError):
    def __init__(self, model: str, requested: int, remaining: int) -> None:
        super().__init__(f"预算不足：{model} 请求 {requested} 超过剩余 {remaining}")
        self.model = model
        self.requested = requested
        self.remaining = remaining


class BudgetExhausted(RuntimeError):
    # 已结算真实用量达到该模型上限：停止新增请求，等待在途结算并保留证据。
    def __init__(self, model: str, actual: int, limit: int) -> None:
        super().__init__(f"额度已用尽：{model} 实际用量 {actual} 达到上限 {limit}")
        self.model = model
        self.actual = actual
        self.limit = limit


class StopTriggered(RuntimeError):
    # 共享停止已置位：新增领取与新增请求一律拒绝，停止保持置位不自动解除。
    def __init__(self, stop: dict) -> None:
        super().__init__(
            f"共享停止已置位：{stop.get('reason_category')} ({stop.get('at')})"
        )
        self.stop = stop


class LedgerAttributionUnresolved(RuntimeError):
    # 旧账本归属未确认：拒绝新增领取、业务执行与模型请求，只读审计入口保持可用。
    def __init__(self, attribution: dict) -> None:
        super().__init__(
            "账本归属未确认，等待监督裁定：" + "; ".join(attribution.get("problems") or [])
        )
        self.attribution = attribution


class LedgerAttributionMigrationInvalid(RuntimeError):
    # 归属确认记录与账本现状不符：确认绑定的旧状态已被改写或指向其他实验，拒绝放行。
    def __init__(self, detail: str, record: dict) -> None:
        super().__init__(detail)
        self.record = record


def unsettled_model_attempts(bucket: dict) -> list[dict]:
    return [attempt for attempt in bucket.get("attempts", [])
            if attempt.get("state") not in {"settled", "reconciled"}]


class BudgetLedger:
    def __init__(self, path, limits: dict[str, int] | None = None, *, experiment_id: str | None = None):
        # 共享停止与连续计数按 experiment_id 隔离：同一实验的 worker/单进程/专项/追加共用一个作用域。
        # 记账查询本身允许省略作用域；停止与计数入口在缺少作用域时拒绝执行，避免跨实验串写。
        self.experiment_id = experiment_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = dict(DEFAULT_LIMITS if limits is None else limits)
        self._lock = self.path.with_suffix(".lock")

    def _acquire(self, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError("预算账本锁超时")
                time.sleep(0.02)

    def _release(self):
        try:
            os.remove(self._lock)
        except FileNotFoundError:
            pass

    @staticmethod
    def _legacy_state(data: dict) -> dict:
        # 旧顶层状态的完整取值：顶层 stop/consecutive 原样取出，作为确认绑定与迁移输入。
        return {key: data.get(key) for key in ("stop", "consecutive")}

    @staticmethod
    def _legacy_state_digest(data: dict) -> str:
        # 旧状态指纹：确认记录据此绑定"确认当时的旧状态"，禁止仅凭 stop.owner 字符串推断归属。
        payload = json.dumps(BudgetLedger._legacy_state(data), sort_keys=True,
                             ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _migrated(data: dict, record: dict) -> dict | None:
        # 迁移完成判定：同一实验已按该确认记录落实过继承，重复迁移幂等。
        experiment_id = record.get("experiment_id") if isinstance(record, dict) else None
        if not experiment_id:
            return None
        state = (data.get("experiments") or {}).get(experiment_id) or {}
        migration = state.get("legacy_attribution")
        if not migration:
            return None
        if migration.get("attribution_at") != record.get("at"):
            return None
        return migration

    @staticmethod
    def _attribution(data: dict) -> dict:
        # 归属问题只读呈现：原始顶层字段原样保留，源账本零改动。
        problems = []
        stop = data.get("stop")
        consecutive = data.get("consecutive")
        if consecutive and (consecutive.get("count") or consecutive.get("category")):
            problems.append("旧连续计数缺少可靠实验归属")
        if stop:
            owner = stop.get("owner")
            if not isinstance(owner, str) or not owner.strip():
                problems.append("旧停止缺少可靠实验归属")
            else:
                problems.append("旧停止仅凭 stop.owner 字符串不足以证明连续计数归属")
        confirmed = data.get("attribution_confirmed")
        migrated = BudgetLedger._migrated(data, confirmed) if confirmed else None
        if migrated is not None:
            status = "migrated"
        elif confirmed:
            status = "confirmed"
        elif problems:
            status = "unconfirmed"
        else:
            status = "not_applicable"
        return {
            "status": status,
            "problems": [] if status == "migrated" else problems,
            "evidence": confirmed,
            "legacy_state": BudgetLedger._legacy_state(data) if stop or consecutive else None,
            "legacy_state_digest": BudgetLedger._legacy_state_digest(data) if (stop or consecutive) else None,
            "migration": migrated,
            "migrated_experiment": (migrated or {}).get("experiment_id"),
            "legacy_top_level": {key: data[key] for key in ("stop", "consecutive") if key in data},
        }

    @staticmethod
    def _ensure(data: dict) -> dict:
        # 只补齐新实验所需结构；顶层旧状态原样保留，不搬移、不删除、不落盘 legacy_state。
        data.setdefault("experiments", {})
        data.setdefault("completion_seq", 0)
        return data

    @staticmethod
    def _require_attribution(data: dict, experiment_id: str | None = None) -> dict:
        # 归属校验只约束执行与迁移：归属未确认、或已确认但尚未完成同锁迁移，一律拒绝新增
        # 领取、业务执行与模型请求。确认只表示归属已裁定，迁移落实前执行保持阻塞。
        #
        # 迁移完成后，归属只对**获批实验**解除阻塞：旧状态已绑定到该实验，同一账本上的
        # 其他实验仍受未裁定状态约束，禁止借由他实验的迁移获得放行。
        attribution = BudgetLedger._attribution(data)
        migrated_experiment = attribution.get("migrated_experiment")
        if migrated_experiment is not None and experiment_id == migrated_experiment:
            return attribution
        if attribution["status"] != "not_applicable":
            raise LedgerAttributionUnresolved(attribution)
        return attribution

    def _experiment(self, data: dict, owner: str | None = None) -> dict:
        identity = owner or self.experiment_id
        if not identity or (owner and self.experiment_id and owner != self.experiment_id):
            raise ValueError("共享执行状态需要一致的 experiment_id")
        state = data["experiments"].setdefault(identity, {})
        state.setdefault("stop", None)
        state.setdefault("dispatch_halt", None)
        state.setdefault("consecutive", {"category": None, "count": 0, "updated_at": None})
        state.setdefault("outcomes", {})
        return state

    def _check_dispatch(self, data: dict) -> None:
        self._require_attribution(data, self.experiment_id)
        state = self._experiment(data)
        if state["stop"] or state["dispatch_halt"]:
            raise StopTriggered(state["stop"] or state["dispatch_halt"])
        for model, bucket in data["models"].items():
            limit = bucket["limit"]
            if limit is not None and bucket["actual"] >= limit:
                raise BudgetExhausted(model, bucket["actual"], limit)

    def check_dispatch(self) -> None:
        self._acquire()
        try:
            self._check_dispatch(self._load())
        finally:
            self._release()

    def _check_settled(self, data: dict) -> None:
        self._check_dispatch(data)
        for model, bucket in data["models"].items():
            attempts = unsettled_model_attempts(bucket)
            if attempts or bucket["reserved"] or bucket["unknown"]:
                detail = [(item["attempt_id"], item.get("state")) for item in attempts]
                raise ValueError(f"{model} 在途或 unknown 用量未结清：{detail}")

    def check_settled(self) -> None:
        self._acquire()
        try:
            self._check_settled(self._load())
        finally:
            self._release()

    def attribution(self) -> dict:
        # 只读审计入口：归属问题与原始顶层状态可查看，明确标记归属未确认。
        self._acquire()
        try:
            return self._attribution(self._load())
        finally:
            self._release()

    def attribution_guard(self) -> dict:
        # 执行入口安装包装前的归属门禁：归属未确认或迁移未落实即拒绝安装执行包装。
        self._acquire()
        try:
            return self._require_attribution(self._load(), self.experiment_id)
        finally:
            self._release()

    @staticmethod
    def _validate_evidence(evidence: dict) -> None:
        if not evidence or not evidence.get("confirmed_by") or not evidence.get("source"):
            raise ValueError("归属确认需要监督证据：confirmed_by 与 source")

    def confirm_attribution(self, *, experiment_id: str, evidence: dict) -> dict:
        # 归属确认只登记裁定与确认当时的旧状态身份；执行门禁在迁移完成前保持阻塞。
        # 确认记录绑定明确的旧状态指纹与获批实验：禁止仅凭 stop.owner 字符串推断归属。
        if not experiment_id or not str(experiment_id).strip():
            raise ValueError("归属确认需要明确的获批 experiment_id")
        self._validate_evidence(evidence)
        self._acquire()
        try:
            data = self._load()
            legacy_stop = data.get("stop")
            legacy_consecutive = data.get("consecutive")
            if not legacy_stop and not legacy_consecutive:
                raise ValueError("账本没有需要归属确认的旧顶层状态")
            record = {
                **evidence,
                "experiment_id": experiment_id,
                "at": self._now(),
                "legacy_state": self._legacy_state(data),
                "legacy_state_digest": self._legacy_state_digest(data),
                "declared_stop_owner": (legacy_stop or {}).get("owner"),
                "migrated": False,
            }
            data["attribution_confirmed"] = record
            self._store(data)
            return record
        finally:
            self._release()

    def migrate_attribution(self, *, experiment_id: str, evidence: dict) -> dict:
        """获批归属确认 + 同锁原子迁移：把对应实验的旧停止与连续计数继承进实验作用域。

        前置：账本旧顶层状态与确认记录绑定的身份逐项一致，否则拒绝迁移（冲突拒绝）。
        同锁内完成：写入实验作用域 stop/consecutive、登记迁移来源与原始证据、置位确认
        记录的迁移标记。重复迁移同一确认保持幂等，不重复继承、不推进完成序号。
        """
        if not experiment_id or not str(experiment_id).strip():
            raise ValueError("归属迁移需要明确的获批 experiment_id")
        self._validate_evidence(evidence)
        self._acquire()
        try:
            data = self._load()
            record = data.get("attribution_confirmed")
            if not record:
                # 迁移必须建立在已获批的归属确认之上：未确认账本保持执行阻塞。
                raise LedgerAttributionUnresolved(self._attribution(data))
            if record.get("experiment_id") != experiment_id:
                raise LedgerAttributionMigrationInvalid(
                    f"归属确认绑定实验 {record.get('experiment_id')}，与请求的 {experiment_id} 不符",
                    record,
                )
            if record.get("legacy_state_digest") != self._legacy_state_digest(data):
                raise LedgerAttributionMigrationInvalid(
                    "确认绑定的旧状态指纹与账本现状不符，禁止迁移", record,
                )
            if record.get("confirmed_by") != evidence.get("confirmed_by") or \
                    record.get("source") != evidence.get("source"):
                raise LedgerAttributionMigrationInvalid(
                    "迁移请求的监督证据与已登记确认记录不符", record,
                )
            existing = self._migrated(data, record)
            if existing is not None:
                # 重复迁移幂等：不重复继承、不新增证据、不推进完成序号。
                return {**existing, "duplicate": True}
            legacy_stop = data.get("stop")
            legacy_consecutive = data.get("consecutive")
            state = self._experiment(data, experiment_id)
            migrated_stop = None
            if legacy_stop and state["stop"] is None:
                # 旧停止原样继承至获批实验门禁：保留来源、触发身份与原始证据引用。
                migrated_stop = {
                    **legacy_stop,
                    "owner": experiment_id,
                    "inherited_from": "legacy_top_level",
                    "legacy_owner": legacy_stop.get("owner"),
                    "attribution_at": record.get("at"),
                    "attribution_evidence": record.get("source"),
                }
                state["stop"] = migrated_stop
            if legacy_consecutive and legacy_consecutive.get("category"):
                # 连续计数继承：类别与计数原样搬入，来源与确认身份单列留证。
                current = state["consecutive"]
                if not current.get("category") or (current.get("count") or 0) == 0:
                    state["consecutive"] = {
                        **legacy_consecutive,
                        "inherited_from": "legacy_top_level",
                        "attribution_at": record.get("at"),
                    }
            migration = {
                "experiment_id": experiment_id,
                "attribution_at": record.get("at"),
                "attribution_by": record.get("confirmed_by"),
                "attribution_source": record.get("source"),
                "legacy_state_digest": record.get("legacy_state_digest"),
                # 原始旧状态与证据完整保留在迁移记录中：不删除、不改写顶层字段。
                "legacy_state": self._legacy_state(data),
                "inherited_stop": migrated_stop,
                "inherited_consecutive": state["consecutive"],
                "at": self._now(),
            }
            state["legacy_attribution"] = migration
            data["attribution_confirmed"] = {**record, "migrated": True,
                                            "migrated_at": migration["at"]}
            self._store(data)
            return {**migration, "duplicate": False}
        finally:
            self._release()

    def _load(self) -> dict:
        if not self.path.exists():
            return self._ensure({"limits": self.limits, "models": {}})
        return self._ensure(json.loads(self.path.read_text(encoding="utf-8")))

    def _store(self, data: dict) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
        )
        os.replace(temp, self.path)

    def _model(self, data: dict, model: str) -> dict:
        bucket = data["models"].setdefault(
            model,
            {
                "limit": self.limits.get(model),
                "actual": 0,
                "reserved": 0,
                "unknown": 0,
                "paused_at": None,
                "trigger_actual": None,
                "attempts": [],
            },
        )
        if bucket.get("limit") is None:
            bucket["limit"] = self.limits.get(model)
        bucket.setdefault("paused_at", None)
        bucket.setdefault("trigger_actual", None)
        return bucket

    def _total(self, data: dict) -> tuple[int, int]:
        actual = sum(item["actual"] for item in data["models"].values())
        reserved = sum(item["reserved"] for item in data["models"].values())
        return actual, reserved

    def reserve(self, model: str, attempt_id: str, tokens: int, basis: str) -> dict:
        if tokens < 0:
            raise ValueError("预占 tokens 不得为负")
        self._acquire()
        try:
            data = self._load()
            self._require_attribution(data, self.experiment_id)
            bucket = self._model(data, model)
            limit = bucket["limit"]
            if limit is not None and bucket["actual"] + bucket["reserved"] + tokens > limit:
                raise BudgetExceeded(model, tokens, limit - bucket["actual"] - bucket["reserved"])
            actual, reserved = self._total(data)
            if actual + reserved + tokens > TOTAL_LIMIT:
                raise BudgetExceeded(model, tokens, TOTAL_LIMIT - actual - reserved)
            bucket["reserved"] += tokens
            bucket["attempts"].append(
                {
                    "attempt_id": attempt_id,
                    "model": model,
                    "state": "reserved",
                    "reserved": tokens,
                    "actual": None,
                    "basis": basis,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            self._store(data)
            return {"limit": limit, "remaining": None if limit is None else limit - bucket["actual"] - bucket["reserved"]}
        finally:
            self._release()

    def _attempt(self, data: dict, model: str, attempt_id: str) -> dict:
        for item in data["models"][model]["attempts"]:
            if item["attempt_id"] == attempt_id:
                return item
        raise KeyError(f"未知尝试：{attempt_id}")

    def reconcile(self, model: str, attempt_id: str, actual_tokens: int) -> dict:
        self._acquire()
        try:
            data = self._load()
            bucket = self._model(data, model)
            attempt = self._attempt(data, model, attempt_id)
            if attempt["state"] != "reserved":
                raise ValueError(f"尝试 {attempt_id} 状态为 {attempt['state']}，不可对账")
            bucket["reserved"] -= attempt["reserved"]
            bucket["actual"] += actual_tokens
            attempt["state"] = "reconciled"
            attempt["actual"] = actual_tokens
            self._store(data)
            return {"actual": bucket["actual"], "reserved": bucket["reserved"]}
        finally:
            self._release()

    def mark_unknown(self, model: str, attempt_id: str, reason: str | None = None) -> dict:
        # 失败或取消且用量未知：保留预占，标记实际费用未知，禁止记零或释放未经核实的预占。
        self._acquire()
        try:
            data = self._load()
            bucket = self._model(data, model)
            attempt = self._attempt(data, model, attempt_id)
            if attempt["state"] != "reserved":
                raise ValueError(f"尝试 {attempt_id} 状态为 {attempt['state']}，不可标记未知")
            attempt["state"] = "unknown"
            if reason:
                attempt["unknown_reason"] = reason
            bucket["unknown"] += attempt["reserved"]
            self._store(data)
            return {"unknown": bucket["unknown"]}
        finally:
            self._release()

    # ---------- 真实 usage 记账（本轮执行门禁口径） ----------

    def record_attempt(
        self, model: str, attempt_id: str, *, inputs: int, basis: str, context: dict | None = None
    ) -> dict:
        # 请求前登记一次尝试并读取暂停状态；阈值判定只读取已结算 actual。
        # 暂停按模型生效：其他模型仍可登记；共享停止与归属校验继续约束全部新增请求。
        self._acquire()
        try:
            data = self._load()
            self._require_attribution(data, self.experiment_id)
            state = self._experiment(data, self.experiment_id) if self.experiment_id else None
            if state and (state["stop"] or state["dispatch_halt"]):
                raise StopTriggered(state["stop"] or state["dispatch_halt"])
            bucket = self._model(data, model)
            limit = bucket["limit"]
            if limit is not None and bucket["actual"] >= limit:
                raise BudgetExhausted(model, bucket["actual"], limit)
            attempt = {
                "attempt_id": attempt_id,
                "model": model,
                "state": "sent",
                "inputs": inputs,
                "basis": basis,
                "reserved": None,
                "actual": None,
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if context:
                attempt["context"] = context
            bucket["attempts"].append(attempt)
            self._store(data)
            return {
                "limit": limit,
                "actual": bucket["actual"],
                "remaining": None if limit is None else limit - bucket["actual"],
            }
        finally:
            self._release()

    def settle(self, model: str, attempt_id: str, actual_tokens: int | None, *, reason: str | None = None, **extra) -> dict:
        # 用供应商返回的真实 usage 结算：数值缺失或非法时该次保持未知，tokens 记未知。
        self._acquire()
        try:
            data = self._load()
            bucket = self._model(data, model)
            attempt = self._attempt(data, model, attempt_id)
            if attempt["state"] not in {"sent", "reserved"}:
                raise ValueError(f"尝试 {attempt_id} 状态为 {attempt['state']}，不可结算")
            valid = isinstance(actual_tokens, int) and not isinstance(actual_tokens, bool) and actual_tokens >= 0
            if valid:
                bucket["actual"] += actual_tokens
                attempt["state"] = "settled"
                attempt["actual"] = actual_tokens
                if attempt["reserved"] is not None:
                    bucket["reserved"] -= attempt["reserved"]
            else:
                attempt["state"] = "unknown"
                attempt["actual"] = None
                attempt["unknown_reason"] = reason or "usage 缺失或非数值"
                if attempt["reserved"] is not None:
                    bucket["unknown"] += attempt["reserved"]
            for key, value in extra.items():
                attempt[key] = value
            limit = bucket["limit"]
            if limit is not None and bucket["actual"] >= limit and bucket["paused_at"] is None:
                bucket["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                bucket["trigger_actual"] = bucket["actual"]
            self._store(data)
            return {
                "actual": bucket["actual"],
                "unknown": bucket["unknown"],
                "paused_at": bucket["paused_at"],
            }
        finally:
            self._release()

    def is_paused(self, model: str) -> bool:
        self._acquire()
        try:
            data = self._load()
            bucket = self._model(data, model)
            limit = bucket["limit"]
            return limit is not None and bucket["actual"] >= limit
        finally:
            self._release()

    def paused_models(self) -> list[dict]:
        # 派发门禁读取共享账本：任一模型已结算 actual 达到上限即返回该项，供所有执行入口停止新增槽位。
        self._acquire()
        try:
            data = self._load()
            paused = []
            for model, bucket in data["models"].items():
                limit = bucket["limit"]
                if limit is not None and bucket["actual"] >= limit:
                    paused.append({
                        "model": model,
                        "actual": bucket["actual"],
                        "limit": limit,
                        "paused_at": bucket.get("paused_at"),
                        "trigger_actual": bucket.get("trigger_actual"),
                    })
            return paused
        finally:
            self._release()

    # ---------- 共享停止与连续运行错误计数（复用同一账本文件与同一把锁） ----------

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def stop_info(self, *, experiment_id: str | None = None) -> dict | None:
        self._acquire()
        try:
            state = self._experiment(self._load(), experiment_id or self.experiment_id)
            return state["stop"] or state["dispatch_halt"]
        finally:
            self._release()

    def request_stop(self, category, *, owner, trigger, slot=None, operation_id=None,
                     evidence_refs=None, detail=None) -> dict:
        # 首次触发即定稿：已置位不再覆盖，重启进程或换目录不隐式清除。
        if category not in STOP_CATEGORIES:
            raise ValueError(f"未登记的停止类别：{category}")
        self._acquire()
        try:
            data = self._load()
            state = self._experiment(data, owner)
            if state["stop"] is None:
                state["stop"] = {
                    "owner": owner,
                    "reason_category": category,
                    "at": self._now(),
                    "trigger": trigger,
                    "slot": slot,
                    "operation_id": operation_id,
                    "evidence_refs": list(evidence_refs or []),
                    "detail": detail,
                }
                self._store(data)
            return state["stop"]
        finally:
            self._release()

    def halt_dispatch(self, *, detail: dict) -> dict:
        self._acquire()
        try:
            data = self._load()
            state = self._experiment(data)
            if state["dispatch_halt"] is None:
                state["dispatch_halt"] = {"owner": self.experiment_id, "at": self._now(),
                                          "kind": "dispatch_halt", "detail": detail}
                self._store(data)
            return state["dispatch_halt"]
        finally:
            self._release()

    def record_outcome(self, outcome, *, execution_unit: str | None = None, error_type=None, owner,
                       trigger, slot=None, evidence_refs=None, detail=None) -> dict:
        # 首次终态、全局完成序号、实验计数与停止在同锁内一次落盘；同执行单元重复上报幂等。
        if outcome not in RUN_OUTCOMES:
            raise ValueError(f"未登记的运行结果：{outcome}")
        self._acquire()
        try:
            data = self._load()
            self._require_attribution(data, self.experiment_id)
            state = self._experiment(data, owner)
            if not execution_unit:
                raise ValueError("终态需要稳定执行单元身份")
            category = (error_type or "unclassified_run_error") if outcome == "run_error" else None
            terminal = {"outcome": outcome, "error_type": category}
            previous_terminal = state["outcomes"].get(execution_unit)
            if previous_terminal is not None:
                if previous_terminal["terminal"] != terminal:
                    raise ValueError(f"执行单元终态冲突：{execution_unit}")
                # 相同终态重复上报保持幂等：不新增计数、不重置计数、不推进完成序号。
                return {**previous_terminal["result"], "triggered": False, "duplicate": True}
            data["completion_seq"] += 1
            now = self._now()
            triggered = False
            # 停止置位后成功/业务不通过不再改写连续计数：保持触发时的类别与次数证据。
            if state["stop"] is None and outcome in ("success", "business_failure"):
                state["consecutive"] = {"category": None, "count": 0, "updated_at": now}
            elif state["stop"] is None and outcome == "run_error":
                previous = state["consecutive"]
                count = previous["count"] + 1 if previous.get("category") == category else 1
                state["consecutive"] = {"category": category, "count": count, "updated_at": now}
                if count >= CONSECUTIVE_LIMIT and state["stop"] is None:
                    triggered = True
                    state["stop"] = {
                        "owner": owner, "reason_category": "consecutive_run_errors",
                        "at": now, "trigger": trigger, "slot": slot,
                        "operation_id": None, "evidence_refs": list(evidence_refs or []),
                        "detail": {**(detail or {}), "error_type": category, "count": count},
                    }
            result = {"stopped": bool(state["stop"] or state["dispatch_halt"]),
                      "triggered": triggered, "stop": state["stop"],
                      "consecutive": state["consecutive"], "completion_seq": data["completion_seq"]}
            state["outcomes"][execution_unit] = {"terminal": terminal, "result": result,
                                                "completed_at": now, "trigger": trigger,
                                                "slot": slot, "evidence_refs": list(evidence_refs or []),
                                                "detail": detail}
            self._store(data)
            return result
        finally:
            self._release()

    def claim_guarded(self, create, *, require_settled=False):
        # 领取原子边界：与停止置位共用同一把锁；恢复另核实所有 attempt 已结清。
        self._acquire()
        try:
            data = self._load()
            if require_settled:
                self._check_settled(data)
            else:
                self._check_dispatch(data)
            return create()
        finally:
            self._release()

    def snapshot(self) -> dict:
        self._acquire()
        try:
            data = self._load()
            models = {}
            for model, bucket in data["models"].items():
                limit = bucket["limit"]
                unknown_attempts = [
                    {
                        "attempt_id": item["attempt_id"],
                        "at": item.get("at"),
                        "reason": item.get("unknown_reason"),
                        "context": item.get("context"),
                    }
                    for item in bucket["attempts"]
                    if item["state"] == "unknown"
                ]
                models[model] = {
                    "limit": limit,
                    "actual": bucket["actual"],
                    "reserved": bucket["reserved"],
                    "unknown": bucket["unknown"],
                    "remaining": None if limit is None else limit - bucket["actual"] - bucket["reserved"],
                    "paused_at": bucket.get("paused_at"),
                    "trigger_actual": bucket.get("trigger_actual"),
                    "attempts": len(bucket["attempts"]),
                    "unknown_attempts": unknown_attempts,
                    "unsettled_attempts": unsettled_model_attempts(bucket),
                }
            actual, reserved = self._total(data)
            attribution = self._attribution(data)
            snapshot = {
                "models": models,
                "total": {"actual": actual, "reserved": reserved, "limit": TOTAL_LIMIT,
                          "remaining": TOTAL_LIMIT - actual - reserved},
                "experiments": data["experiments"],
                "completion_seq": data["completion_seq"],
                "attribution": attribution,
            }
            if attribution["status"] == "unconfirmed":
                # 归属未确认：原始顶层状态与归属问题原样呈现，只读审计可用，执行保持阻塞。
                snapshot["attribution_unconfirmed"] = True
                return snapshot
            if attribution["status"] == "confirmed":
                # 归属已裁定但迁移未落实：执行与迁移保持阻塞，旧状态与证据原样可读。
                snapshot["attribution_migration_pending"] = True
                return snapshot
            if self.experiment_id:
                snapshot.update(
                    {key: self._experiment(data)[key]
                     for key in ("stop", "dispatch_halt", "consecutive")}
                )
            return snapshot
        finally:
            self._release()
