"""v2 量化评测冻结契约：用例、步骤与观测模型。

v1 契约（p0_contract.py）保持原样。v2 独立版本与入口，用例 version=3、三轮。
"""

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
CASE_VERSION = 3
EXPERIMENT_ROOT_NAME = "v2-evaluation"
BUSINESS_KINDS: tuple[str, ...] = ("profile", "plan", "workout")
PREPARE_TOOLS: dict[str, str] = {
    "prepare_profile_update": "profile",
    "prepare_workout": "workout",
    "prepare_plan": "plan",
    "prepare_plan_import": "plan",
    "prepare_plan_adjustment": "plan",
}

MODEL_IDENTITY: dict[str, Any] = {
    "provider": "stepfun",
    "api": "openai-completions",
    "model": "step-3.7-flash",
    "base_url": "https://api.stepfun.com/step_plan/v1/",
}

StepKind = Literal[
    "user",
    "confirm",
    "confirm_repeat",
    "confirm_negative",
    "operation_query",
    "edit",
]
CheckKind = Literal["deterministic", "manual"]
CheckStatus = Literal["pass", "fail", "pending_review"]
RoundStatus = Literal["pass", "fail", "pending_review", "error"]
StepStatus = Literal["done", "failed", "blocked", "cancelled", "unknown"]
TerminalKind = Literal["done", "error", "unknown"]
BenchmarkTopic = Literal["tool_parallel", "compaction", "save_confirmation"]
BenchmarkStatus = Literal["complete", "incomplete", "blocked"]
NegativeEntry = Literal["run", "edit", "steering", "confirmation"]
NEGATIVE_ENTRIES = ("run", "edit", "steering", "confirmation")
NEGATIVE_BREAK_FIELDS = (
    "authorization_injection",
    "display_entry_id",
    "cross_session_target",
    "expired_snapshot",
)


class AttachmentSpec(BaseModel):
    # 真实上传附件：由评测器编码为 run 请求的 upload 附件并绑定当前消息。
    file_name: str
    text: str


class V2Step(BaseModel):
    kind: StepKind
    text: str | None = None
    attachments: list[AttachmentSpec] = Field(default_factory=list)
    business_kind: Literal["profile", "plan", "workout"] | None = None
    operation: str | None = None
    target: str | None = None
    query: str | None = None
    note: str | None = None
    # 负向步骤：实际派发端点、被破坏的单一条件、期望受控错误码。
    entry: NegativeEntry | None = None
    break_field: str | None = None
    expect_code: str | None = None
    # 确认请求的字段覆盖：display_entry_id / proposal_id / business_kind 等。
    overrides: dict[str, Any] = Field(default_factory=dict)


class V2Check(BaseModel):
    check_id: str
    kind: CheckKind
    target: str | None = None
    rule: str
    expectation: Any = None


class ManualReviewItem(BaseModel):
    review_id: str
    standard: str
    evidence_refs: list[str] = Field(default_factory=list)


class V2Case(BaseModel):
    case_id: str
    category: str
    precondition: dict[str, Any] = Field(default_factory=dict)
    steps: list[V2Step]
    checks: list[V2Check]
    forbidden: list[str] = Field(default_factory=list)
    manual_review: list[ManualReviewItem] = Field(default_factory=list)
    product_basis: str
    case_version: int = CASE_VERSION


class PreconditionEvidence(BaseModel):
    case_id: str
    built: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class ToolCallRecord(BaseModel):
    tool_call_id: str
    name: str
    arguments: dict[str, Any] | None = None
    is_error: bool | None = None
    content: str | None = None
    entry_id: str | None = None


class TurnObservation(BaseModel):
    case_id: str
    round_index: int
    step_index: int
    kind: str
    request_text: str | None = None
    session_id: str
    operation_id: str
    run_id: str | None = None
    request_entry_id: str | None = None
    terminal: TerminalKind
    status: StepStatus
    events: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    # 逐字段用量来源：reported / unreported_defaulted / unknown；无法证明来源的字段保留 unknown。
    usage_sources: dict[str, str] | None = None
    # 客户端：请求发起至首个非空公开正文。
    client_first_body_ms: float | None = None
    duration_ms: float | None = None
    error: str | None = None
    error_type: str | None = None


class ConfirmationObservation(BaseModel):
    case_id: str
    round_index: int
    step_index: int
    kind: str
    business_kind: str | None = None
    operation_label: str | None = None
    operation_id: str
    target: str | None = None
    request: dict[str, Any]
    status_code: int | None = None
    response: dict[str, Any] | None = None
    rejected: bool = False
    rejection_code: str | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    duration_ms: float | None = None
    error: str | None = None
    # 负向步骤断言材料：实际命中端点、被破坏条件、期望错误码与路径会话身份。
    endpoint: str | None = None
    entry: str | None = None
    break_field: str | None = None
    expect_code: str | None = None
    path_session_id: str | None = None
    # 端点命中、条件破坏、受控错误码、零业务写入四项判定结果。
    assertions: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    case_id: str
    round_index: int
    check_id: str
    kind: CheckKind
    status: CheckStatus
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    # Agent 审阅判定材料：判定者身份与标记，保留原文引用与理由。
    reviewer: str | None = None
    reviewed_by_agent: bool = False


class RoundObservation(BaseModel):
    case_id: str
    round_index: int
    slot: int
    step_index: int = 0
    session_id: str
    process_index: int
    turns: list[TurnObservation] = Field(default_factory=list)
    confirmations: list[ConfirmationObservation] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    # 检查完整性派生标记：读取既有证据时按冻结用例核对覆盖与身份，
    # 只用于派生判断，不回写原始 status/checks。
    check_integrity: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    # 场景耗时只覆盖业务步骤区间；启动与前置构造分别单列。
    duration_ms: float | None = None
    startup_ms: float | None = None
    precondition_ms: float | None = None
    # 逐步骤业务耗时、评测器开销与总墙钟：业务时长在最后业务步骤确定终态处截止。
    step_timings: list[dict[str, Any]] = Field(default_factory=list)
    eval_overhead_ms: dict[str, Any] | None = None
    total_wall_ms: float | None = None
    status: RoundStatus
    error: str | None = None
    error_type: str | None = None


class ExperimentEnv(BaseModel):
    experiment_id: str
    # 追加运行使用父实验作用域，attempt 身份单独记录；预期观测故障仅限专项明确声明。
    experiment_scope: str | None = None
    attempt_id: str | None = None
    expected_trace_failure: bool = False
    case_id: str
    round_index: int
    slot: int
    process_index: int
    database_path: str
    trace_path: str
    attachment_root: str
    evidence_dir: str
    execution_root: str
    budget_ledger_path: str
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
v2 冻结契约。实现入口签名与语义不得更改。

用例 -> backend/test/v2_business_cases.py
    build_precondition(case, *, http, session_id, business_context, live) -> PreconditionEvidence
    read_state(database_path, session_id=None) -> dict
    evaluate(case, *, observation, service, database_path, business_context) -> list[CheckResult]

执行器 -> backend/test/v2_eval_support.py
    load_cases(path) -> list[V2Case]
    validate_cases(cases) -> list[str]
    freeze_model_config(source_root, experiment_dir) -> dict
    use_frozen_model_config(experiment_dir) -> dict
    install_embedding_recorder(ledger, *, experiment_id, process_index) -> None
    run_case_round(case, round_index, slot, env, model_identity) -> RoundObservation
    build_review_packet(rounds, cases) -> list[dict]
    apply_v2_review(rounds, reviews) -> list[RoundObservation]
    slot_plan(cases, smoke=False) -> list[dict]
    aggregate(rounds, case_ids, repeats=ROUNDS) -> dict

单入口 -> backend/test/run_v2_eval.py （从 backend 执行 python -m test.run_v2_eval）

用量汇总复用 app.agent.usage.summarize_usage。确认步骤零模型调用。
负向步骤按 step.entry 派发到 run/edit/steering/confirmation 四个真实端点，
step.break_field 记录本轮唯一被破坏的条件，step.expect_code 记录期望受控错误码。
预期端点由步骤声明与该会话真实 run_id 独立推导，endpoint_hit 比对实际请求路径；
condition_broken 依据库内真实快照的 status 与 session_id 核验被破坏条件；
zero_write 逐请求比对包含业务数据、历史计划、训练记录、快照状态与确认账本的受保护状态摘要。
场景 duration_ms 取首个业务步骤发起至最后步骤确定终态；startup_ms 与 precondition_ms 单列。
Embedding 记账以供应商真实 usage 结算；阈值判定只读取已结算 actual，unknown 单列；
所有执行入口在领取新槽前读取共享暂停状态，触达阈值即停止新增派发。
槽位计划写入 manifest.slot_plan，调度、执行账本与放行判定共用该冻结集合；冒烟每例一轮。
"""
