from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.memory import MemoryAssembler, MemoryContext
from app.application.ports import (
    ExerciseCatalog,
    ModelGateway,
    Plans,
    SkillSource,
    Stats,
    WorkoutRecords,
)
from app.application.services.plans_service import (
    PlanActivationError,
    PlanActivationService,
    PlanPersistenceService,
)
from app.application.services.profile_service import ProfileService
from app.application.services.stats_service import StatsService
from app.domain.conversations.context import ContextMessage
from app.domain.plans.schema import EvaluationResult, Plan, PlanDraft

Intent = Literal[
    "form_record",
    "natural_language_record",
    "view_progress",
    "generate_plan",
    "adjust_plan",
    "view_schedule",
    "general",
]

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

    # 计划子图现行键：与 ``plan_draft``／``evaluation_result`` 同义，读取方假定存在
    context: MemoryContext
    loaded_skill: "LoadedSkill"
    draft_plan: PlanDraft
    evaluation: EvaluationResult

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


class SkillBundle(BaseModel):
    """Node 可依赖的 Skill 接口：指令与参考材料；不含用户事实、Repository 与写入能力。"""

    model_config = ConfigDict(extra="forbid")

    names: tuple[str, ...]
    system_instructions: str
    references: tuple[str, ...]
    version: str


ToolRevisionDomain = Literal["profile", "workouts", "plans", "catalog"]


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


class ToolEvidence(BaseModel):
    """一条 ToolResult 读取的事实域 revision：同 Run 的候选与评审按它核对快照一致性。"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    revision_domain: ToolRevisionDomain
    revision: int


class ToolResult[T](BaseModel):
    """只读 Tool 的最小返回封装：结构化载荷 ＋ 读取时使用的事实域 revision。"""

    model_config = ConfigDict(extra="forbid")

    data: T
    evidence: tuple[ToolEvidence, ...]


PLANNING_SKILL_NAME = "workout-planning"

ADJUSTMENT_SKILL_NAME = "plan-adjustment"

ADJUST_PLAN_INTENT: Intent = "adjust_plan"

CONFIRMATION_ACTIONS: tuple[str, ...] = ("confirm", "reject")

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
    persistence: PlanPersistenceService
    catalog: ExerciseCatalog
    records: WorkoutRecords
    skills: SkillSource
    tool_harnesses: Mapping[Intent, CompiledStateGraph]


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
class GeneratePlanDeps:
    """生成计划子图的构造期依赖：领域服务、Skill 来源、注入的模型 callable 与时钟。"""

    profiles: ProfileService
    catalog: ExerciseCatalog
    stats: Stats
    assembler: MemoryAssembler
    skills: SkillSource
    persistence: PlanPersistenceService
    plans: Plans
    activation: PlanActivationService
    model: ModelGateway
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Agent 三端点的装配。"""

    graph: CompiledStateGraph
    deps: GeneratePlanDeps
    run_deps: AgentRunDeps
