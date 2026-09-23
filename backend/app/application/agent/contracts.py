from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

from app.application.agent.budget import ModelRequestBudget
from app.application.ports import (
    ExerciseCatalog,
    ModelGateway,
    Plans,
    ProfileReads,
    SkillSource,
    ToolCacheRevisions,
    WorkoutRecords,
)
from app.application.services.plans_service import (
    PlanActivationError,
    PlansService,
)
from app.application.services.stats_service import StatsService
from app.domain.conversations.context import ContextMessage
from app.domain.plans.schema import (
    DeterministicResult,
    EvaluationResult,
    Plan,
    PlanDraft,
    ToolEvidence,
)

Intent = Literal[
    "form_record",
    "natural_language_record",
    "view_progress",
    "generate_plan",
    "adjust_plan",
    "view_schedule",
    "general",
]

#: 本产品的稳定单用户身份：由 API／Runtime 边界注入，不由模型参数与库内列推导。
LOCAL_USER_ID = "local-user"

ConfirmationStatus = Literal["pending", "confirmed", "rejected"]

TerminationReason = Literal["safety_stop", "archive_draft", "discard_failed_candidate"]


class WorkflowState(TypedDict, total=False):
    """一次 Run 的编排状态：身份与业务日由 API／Runtime 注入，候选与终态由节点写入。"""

    # Run 身份
    run_id: str
    conversation_id: str
    user_id: str
    client_request_id: str
    business_day: date

    # 请求与路由
    request: str
    intent: Intent | None
    safety_hits: tuple[str, ...]

    # Agent loop：节点只返回增量消息，由 reducer 追加
    messages: Annotated[list[AnyMessage], add_messages]
    ui_actions: tuple[dict[str, object], ...]

    # 计划候选与评审
    plan_draft: PlanDraft | None
    evaluation_result: EvaluationResult | None
    revision_count: Literal[0, 1]
    revision_feedback: tuple[str, ...]
    #: Planner 生成候选时读到的事实 revision：Evaluator 按它核对同一 Run 的快照。
    planner_evidence: tuple[ToolEvidence, ...]
    # 计划子图现行键：与 ``plan_draft``／``evaluation_result`` 同义；Run 入口节点会把它归零
    loaded_skill: "LoadedSkill"
    draft_plan: PlanDraft
    deterministic_result: DeterministicResult | None
    evaluation: EvaluationResult | None

    # 持久化衔接：``draft_plan_id`` 只由 ``persist_draft`` 写入
    draft_plan_id: int | None
    confirmation: ConfirmationStatus | None

    # 终态：``final_result`` 只由终态收敛节点写入
    termination_reason: TerminationReason | None
    final_result: "AgentRunResult | None"


def initial_workflow_state(
    *,
    run_id: str,
    conversation_id: str,
    user_id: str,
    client_request_id: str,
    business_day: date,
    request: str,
) -> WorkflowState:
    """Run 初始状态：身份与业务日由 API 注入，候选与终态留空，``revision_count`` 从 0 开始。"""
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "client_request_id": client_request_id,
        "business_day": business_day,
        "request": request,
        "intent": None,
        "safety_hits": (),
        "plan_draft": None,
        "evaluation_result": None,
        "revision_count": 0,
        "revision_feedback": (),
        "planner_evidence": (),
        "draft_plan_id": None,
        "confirmation": None,
        "termination_reason": None,
        "final_result": None,
    }


def thread_config(conversation_id: str) -> dict[str, dict[str, str]]:
    """Agent 调用的 RunnableConfig：``thread_id`` 直接用 conversation id。"""
    return {"configurable": {"thread_id": conversation_id}}


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SkillReference:
    path: str
    text: str


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    metadata: SkillMetadata
    body: str
    references: tuple[SkillReference, ...]


@dataclass(frozen=True, slots=True)
class PlanToolHarnesses:
    """计划路径两个 ToolNode 的装配结果：各自独立的白名单元组、节点名、预算与调用轨迹。"""

    planning: CompiledStateGraph
    evaluation: CompiledStateGraph


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """一次 Run 的事实快照键：身份与四个 revision 由 Runtime 注入 ToolNode，模型参数里不出现。"""

    user_id: str
    run_id: str
    business_day: date
    profile_revision: int
    workouts_revision: int
    plans_revision: int
    catalog_revision: int


class ToolResult[T](BaseModel):
    """只读 Tool 的最小返回封装：结构化载荷 ＋ 读取时使用的事实域 revision。"""

    model_config = ConfigDict(extra="forbid")

    data: T
    evidence: tuple[ToolEvidence, ...]


PLANNING_SKILL_NAME = "workout-planning"

ADJUSTMENT_SKILL_NAME = "plan-adjustment"

ADJUST_PLAN_INTENT: Intent = "adjust_plan"

CONFIRMATION_ACTIONS: tuple[str, ...] = ("confirm", "reject")

#: 确认 interrupt 的业务种类：等待载荷与恢复载荷都按它判别，不由调用方猜。
PLAN_CONFIRMATION_KIND = "plan_confirmation"

#: Evaluator 的固定评审 Skill：每次评审候选 ``PlanDraft`` 时随请求加载。
PLAN_EVALUATION_SKILL_NAME = "plan-evaluation"

AgentEventName = Literal["node", "message", "waiting", "done", "error"]


class RequiredProfileMissing(ValueError):
    """画像缺失必需事实（未建档或 ``weekly_frequency`` 不是明确值）。"""


class RequiredActivePlanMissing(ValueError):
    """没有 active 计划：调整计划前明确失败。"""


class ConfirmationConflict(PlanActivationError):
    """确认／拒绝的请求身份与等待中的 interrupt 不一致。"""


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一条产品事件。"""

    event: AgentEventName
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExistingDraftTarget:
    """计划分支对业务库唯一 draft 的处置。"""

    reused: Plan | None = None
    replacement_id: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRunDeps:
    """``invoke_agent_run``／``stream_agent_run`` 的构造期依赖。"""

    model: ModelGateway
    stats: StatsService
    plans: Plans
    plan_writes: PlansService
    catalog: ExerciseCatalog
    records: WorkoutRecords
    profiles: ProfileReads
    skills: SkillSource
    tool_harnesses: Mapping[Intent, CompiledStateGraph]
    revisions: ToolCacheRevisions
    schema_version: int
    #: canonical 目录 × 数据集的联表只读端口；组合根构造一次，所有 Run 共享。
    dataset: Any = None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent Run 的产品结果。"""

    intent: Intent | None
    messages: tuple[str, ...]
    termination_reason: TerminationReason | None
    draft_plan_id: int | None


@dataclass(frozen=True, slots=True)
class AdjustmentContext:
    """调整计划的目标：预读的 active 身份、其统一 ``PlanDraft`` 与关联日程训练身份。"""

    plan_id: int
    active_draft: PlanDraft
    linked_workout_session_ids: tuple[int, ...]


@dataclass(slots=True)
class GeneratePlanRun:
    """一次 invocation 的运行上下文。"""

    business_day: date
    budget: ModelRequestBudget = field(default_factory=ModelRequestBudget)
    adjustment: AdjustmentContext | None = None
    regenerate: bool = False
    #: 本次请求之前的完整对话上下文投影；当前用户消息不在其中（由 ``request`` 单独给出）。
    conversation_messages: tuple[ContextMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanLlmNodeDeps:
    """Planner／Evaluator LLM Node 的构造期依赖：唯一模型入口 ＋ 各自的只读事实边界。

    只读事实边界是该路径自己的 ToolNode ＋ 调用它所需的只读端口与事实快照来源；不含 Repository
    写入口、数据库连接、事务对象、业务写 Service 与 ``MemoryAssembler``，事实只经 ``harness``
    的真实调用返回。
    """

    model: ModelGateway
    harness: CompiledStateGraph
    profiles: ProfileReads
    records: WorkoutRecords
    catalog: ExerciseCatalog
    plans: Plans
    stats: StatsService
    revisions: ToolCacheRevisions
    schema_version: int
    #: canonical 目录 × 数据集的联表只读端口；组合根构造一次，两条计划路径共享。
    dataset: Any = None


@dataclass(frozen=True, slots=True)
class PlanDeterministicDeps:
    """确定性节点的只读事实边界：领域规则与画像前置需要的只读端口，无写入能力。"""

    profiles: ProfileReads
    catalog: ExerciseCatalog
    plans: Plans
    stats: StatsService


@dataclass(frozen=True, slots=True)
class PlanWriteDeps:
    """计划写入边界的业务写依赖：确认端点与图内确认节点共用的计划写服务、注入的业务时钟。"""

    plans: PlansService
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class GeneratePlanDeps:
    """生成计划子图的构造期依赖：按节点职责拆分的四个边界 ＋ 统一 SkillSource。"""

    planner: PlanLlmNodeDeps
    evaluator: PlanLlmNodeDeps
    deterministic: PlanDeterministicDeps
    writes: PlanWriteDeps
    skills: SkillSource


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Agent 三端点的装配。"""

    graph: CompiledStateGraph
    deps: GeneratePlanDeps
    run_deps: AgentRunDeps
