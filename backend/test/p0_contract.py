from typing import Any, Literal

from pydantic import BaseModel, Field

CATEGORY_QUOTAS: dict[str, int] = {
    "profile": 8,
    "plan_entry": 10,
    "plan_adjust": 8,
    "workout": 8,
    "confirm_idempotency": 10,
    "risk_unknown": 6,
}

CASE_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_QUOTAS)

CASE_ID_PREFIXES: dict[str, str] = {
    "profile": "P",
    "plan_entry": "L",
    "plan_adjust": "A",
    "workout": "W",
    "confirm_idempotency": "C",
    "risk_unknown": "S",
}

ROUNDS = 3

EXPERIMENT_ROOT_NAME = "p0-evaluation"

MODEL_IDENTITY: dict[str, Any] = {
    "provider": "stepfun",
    "api": "openai-completions",
    "model": "step-3.7-flash",
    "base_url": "https://api.stepfun.com/step_plan/v1/",
}

CheckKind = Literal["deterministic", "manual"]
CheckStatus = Literal["pass", "fail", "pending_review"]
RoundStatus = Literal["pass", "fail", "pending_review", "error"]
TerminalKind = Literal["done", "error", "unknown"]
BenchmarkTopic = Literal["tool_parallel", "compaction"]
BenchmarkStatus = Literal["complete", "incomplete", "blocked"]


class CaseTurn(BaseModel):
    text: str


class CaseCheck(BaseModel):
    check_id: str
    kind: CheckKind
    target: str
    rule: str
    expectation: Any = None


class ManualReviewItem(BaseModel):
    review_id: str
    standard: str
    evidence_refs: list[str] = Field(default_factory=list)


class P0Case(BaseModel):
    case_id: str
    category: str
    precondition: dict[str, Any] = Field(default_factory=dict)
    turns: list[CaseTurn]
    checks: list[CaseCheck]
    forbidden: list[str] = Field(default_factory=list)
    manual_review: list[ManualReviewItem] = Field(default_factory=list)
    product_basis: str
    case_version: int = 1


class PreconditionEvidence(BaseModel):
    case_id: str
    built: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class TurnObservation(BaseModel):
    case_id: str
    round_index: int
    turn_index: int
    request_text: str
    session_id: str
    operation_id: str
    run_id: str
    request_entry_id: str
    terminal: TerminalKind
    events: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    usage: dict[str, Any] | None = None
    first_text_delta_ms: float | None = None
    duration_ms: float | None = None


class CheckResult(BaseModel):
    case_id: str
    round_index: int
    check_id: str
    kind: CheckKind
    status: CheckStatus
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)


class CaseRoundObservation(BaseModel):
    case_id: str
    round_index: int
    session_id: str
    turns: list[TurnObservation]
    checks: list[CheckResult]
    usage: dict[str, Any] | None = None
    duration_ms: float | None = None
    status: RoundStatus
    error: str | None = None


class ExperimentEnv(BaseModel):
    experiment_id: str
    case_id: str
    round_index: int
    database_path: str
    evidence_dir: str
    business_date: str
    model_identity: dict[str, Any]


class BenchmarkResult(BaseModel):
    topic: BenchmarkTopic
    env: dict[str, Any]
    samples: list[dict[str, Any]]
    stats: dict[str, Any]
    correctness: dict[str, Any]
    missing: list[str] = Field(default_factory=list)
    review: list[dict[str, Any]] = Field(default_factory=list)
    status: BenchmarkStatus = "complete"


INTERFACE_NOTES = """
契约冻结版。三个任务 Agent 必须实现下列入口，签名与语义不得更改。

用例 Agent -> backend/test/p0_business_cases.py
    build_precondition(case: P0Case, *, service, business_context, session_id: str) -> PreconditionEvidence
        在真实隔离库中构造 case.precondition 描述的前置业务状态。
    evaluate(case: P0Case, *, observation: CaseRoundObservation, service, database_path: str, business_context) -> list[CheckResult]
        依据真实持久化状态逐项判定；确定性检查给出 pass/fail；
        需人工审阅的检查（kind=manual）返回 pending_review，reason 指向证据引用。

专项 Agent -> backend/test/check_tool_parallel_performance.py / check_compaction_performance.py
    run_benchmark(experiment_dir: str) -> BenchmarkResult
        运行后原始样本、统计、正确性、缺测与审阅项写入 experiment_dir 并由返回值携带。

框架 Agent -> backend/test/p0_eval_support.py
    load_cases(path) -> list[P0Case]
    validate_cases(cases) -> list[str]              # 返回错误列表，空表示通过
    freeze_model_config(source_root, experiment_dir) -> dict   # 冻结 Stepfun 快照，非敏感
    run_case_round(case, round_index, env, model_identity) -> CaseRoundObservation
    aggregate(rounds: list[CaseRoundObservation]) -> dict
    单入口 -> backend/test/run_p0_eval.py   # 从 backend 执行 uv run python -m test.run_p0_eval

用量汇总统一调用 app.agent.usage.summarize_usage(messages, compaction_usage=[...])，
按本次 run_id 汇总，摘要调用计入一次。
"""
