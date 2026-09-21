from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, TypedDict

from langgraph.graph.state import CompiledStateGraph

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.memory import MemoryAssembler
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
from app.domain.plans.schema import Plan, PlanDraft

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

TerminationReason = Literal["safety_stop", "archive_draft", "reject_draft"]


class WorkflowState(TypedDict, total=False):
    conversation_id: str
    request: str
    intent: Intent | None
    context: Any
    loaded_skill: Any
    draft_plan_id: int | None
    draft_plan: Any
    evaluation: Any
    revision_count: int
    confirmation: ConfirmationStatus | None
    termination_reason: TerminationReason | None


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
