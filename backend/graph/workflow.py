"""生成计划子图与 Agent Run 入口：计划链路不实现 Router（stage4.md §2.2、§5.3、§6 Subtask 04；
stage5.md §3.6／§3.9）。

拓扑（stage4.md §6 Subtask 04 任务 2、讨论总结 §4.2；调整分支按 stage5.md §3.4、确认分支按
stage5.md §3.3 加入）：

```text
safety_check
├─ hit → safety_stop（结束：不生成计划）
├─ clear 且 generate_plan → validate_required_profile → load_context → load_skill → planner
└─ clear 且 adjust_plan → require_active_plan → validate_required_profile → load_context → load_skill → planner
    ↓
  evaluator
    ├─ pass → persist_draft → wait_for_confirmation
    │    ├─ resume(action=confirm) → activate_plan（结束）
    │    └─ resume(action=reject) → archive_draft（结束）
    ├─ 阻断失败且 revision_count == 0 → revise_once → evaluator
    └─ 阻断失败且 revision_count >= 1 → reject_draft（结束）
```

硬边界：

- **不包装第三个 Agent**：计划子图没有意图分类节点；Router 是 ``graph/router.py`` 里的函数，由
  :func:`invoke_agent_run` 在进入子图前先调用（stage5.md §3.6／§3.9）。
- **一次修订上限**：``_route_after_evaluator`` 在 ``revision_count >= 1`` 时只走 ``reject_draft``，
  不再回到 Planner；``revise_once`` 因此最多执行一次。
- **运行预算**：每次 invocation 由调用方传入 ``graph.nodes.GeneratePlanRun``（业务日期 ＋
  模型请求预算：单次请求 60 秒、单 Run 180 秒、最多 5 次）；该 Run 时限由
  :func:`invoke_generate_plan`（计划子图的收集形态）与 :func:`stream_agent_run`（Agent Run 的事件
  形态）分别包住**完整一次** Graph 调用（含非模型节点），预算本身不写 State、不落 checkpoint。
  调用方不得绕过这两个入口直接 ``graph.ainvoke``／``graph.astream``。
- **Checkpointer 由调用方给出**：``wait_for_confirmation`` 的 interrupt 需要它；编译本子图不改变
  Stage 3 的存档生命周期（``graph/checkpointer.py``）。
- **确认只有一个提交入口**：``activate_plan``／``archive_draft`` 只调 ``PlanActivationService``；
  确认／拒绝经 :func:`invoke_confirmation` 进入（checkpoint 优先 ＋ 唯一 draft 兜底，两条路径同一
  服务、服务内幂等，stage5.md §3.1–§3.3）。
- **安全优先于 Router 与「已有 draft」的判定**：:func:`stream_agent_run` 用 ``domain.profile.safety`` 的
  同一份封闭词表**先于 Router**预检一次请求（对每个请求、每个 intent 都成立，A1 裁决），命中即不调分类
  模型、不判定已有 draft 的复用／冲突／同类 regenerate、也不预读 active，直接进子图由 ``safety_check``
  终止（stage5.md §3.5「任一命中即进入 safety_stop」、§3.4 第 5–6 条）。
- **运行只有一个入口、事件只有五类**：:func:`stream_agent_run` 包住 Router 与后续完整一次 Graph 调用
  并逐个产出 :class:`AgentEvent`（``node``／``message``／``waiting``／``done``；``error`` 由 SSE 层在
  捕获运行错误后构造），:func:`invoke_agent_run` 是同一条实现的收集形态；事件只带 §3.8 表格给出的键，
  不含原始 LangGraph 块、模型事件、系统提示词或 Provider 配置。
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Interrupt, StateSnapshot
from pydantic import BaseModel, ConfigDict

from domain.actions.rules import RecordLoadMismatch, UnknownExercise
from domain.actions.schema import Exercise, LoadConvention
from domain.actions.service import ActionCatalogService
from domain.plans.schema import EvaluationResult, Plan, PlanSession
from domain.plans.service import (
    PlanDraftConflict,
    PlanNotFound,
    PlanPersistenceService,
    PlanReadService,
)
from domain.profile.safety import message_red_flag_hits
from domain.records.rules import InvalidRecordFact
from domain.records.schema import SetType, WorkoutSetInput
from domain.records.service import WorkoutRecordsService
from domain.stats.service import StatsService
from graph.checkpointer import thread_config
from graph.model import ModelCall, parse_model_json
from graph.nodes import (
    ADJUST_PLAN_INTENT,
    ConfirmationConflict,
    GeneratePlanDeps,
    GeneratePlanNodes,
    GeneratePlanRun,
    ModelRequestBudget,
)
from graph.router import classify_intent
from graph.state import Intent, TerminationReason, WorkflowState

#: 确认等待节点名：恢复入口靠它判断「图是否正停在确认 interrupt」（stage5.md §3.3 第 1 条）。
WAITING_CONFIRMATION_NODE = "wait_for_confirmation"

#: 确认／拒绝的动作取值：resume 载荷的 ``action``，恰为 ``graph.nodes.CONFIRMATION_ACTIONS``。
ConfirmationAction = Literal["confirm", "reject"]

#: 生成计划子图的节点名（顺序即拓扑顺序：``safety_check`` 是入口，随后是 ``adjust_plan`` 的 active
#: 预读节点，再是两条 intent 共用的计划链路）。
NODE_NAMES: tuple[str, ...] = (
    "safety_check",
    "safety_stop",
    "require_active_plan",
    "validate_required_profile",
    "load_context",
    "load_skill",
    "planner",
    "evaluator",
    "revise_once",
    "persist_draft",
    WAITING_CONFIRMATION_NODE,
    "activate_plan",
    "archive_draft",
    "reject_draft",
)


def _route_after_safety_check(
    state: WorkflowState,
) -> Literal["hit", "adjust", "generate"]:
    """安全分流与调整分支：``safety_check`` 命中封闭词表时已写入 ``termination_reason='safety_stop'``。

    判定本身只在 ``graph/nodes.py::GeneratePlanNodes.safety_check`` 里做（``message_red_flag_hits``），
    条件边只读结果；未命中的分支也会写明 ``termination_reason=None``，因此这里读到的是本次 Run 的
    结论。计划分支只看 ``state['intent']``（Router 在进入子图前写入的冻结字段）：``adjust_plan`` 先
    预读 active，生成计划直接进画像前提（stage5.md §3.4）。
    """
    if state.get("termination_reason") == "safety_stop":
        return "hit"
    return "adjust" if state.get("intent") == ADJUST_PLAN_INTENT else "generate"


def _route_after_evaluator(state: WorkflowState) -> Literal["pass", "revise", "reject"]:
    """评估后分流：通过进持久化；阻断失败只在 ``revision_count == 0`` 时回 Planner 一次。"""
    evaluation: EvaluationResult = state["evaluation"]
    if evaluation.passed:
        return "pass"
    if state.get("revision_count") or 0:
        return "reject"
    return "revise"


def _route_after_confirmation(state: WorkflowState) -> Literal["confirm", "reject"]:
    """确认边：只在 ``wait_for_confirmation`` 写下 ``confirmed``／``rejected`` 之后分流。

    这两个值只能由 resume 载荷写入（``graph/nodes.py::GeneratePlanNodes.wait_for_confirmation``）；
    读到别的值说明本次没有合法的用户动作，此时明确冲突比默认放行确认更安全。
    """
    confirmation = state.get("confirmation")
    if confirmation == "confirmed":
        return "confirm"
    if confirmation == "rejected":
        return "reject"
    raise ConfirmationConflict(f"确认分支缺少用户动作：confirmation={confirmation!r}")


def build_generate_plan_graph(
    deps: GeneratePlanDeps,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """按上述拓扑编译生成计划子图；节点构造时注入 ``deps``（含模型 callable）。

    调用方按每次 invocation 传入运行上下文：经 :func:`invoke_generate_plan` 执行
    （Run 时限包住完整一次 Graph 调用），不直接 ``ainvoke``。
    """
    nodes = GeneratePlanNodes(deps)
    builder = StateGraph(WorkflowState, context_schema=GeneratePlanRun)
    builder.add_node("safety_check", nodes.safety_check)
    builder.add_node("safety_stop", nodes.safety_stop)
    builder.add_node("require_active_plan", nodes.require_active_plan)
    builder.add_node(
        "validate_required_profile", nodes.validate_required_profile
    )
    builder.add_node("load_context", nodes.load_context)
    builder.add_node("load_skill", nodes.load_skill)
    builder.add_node("planner", nodes.planner)
    builder.add_node("evaluator", nodes.evaluator)
    builder.add_node("revise_once", nodes.revise_once)
    builder.add_node("persist_draft", nodes.persist_draft)
    builder.add_node(WAITING_CONFIRMATION_NODE, nodes.wait_for_confirmation)
    builder.add_node("activate_plan", nodes.activate_plan)
    builder.add_node("archive_draft", nodes.archive_draft)
    builder.add_node("reject_draft", nodes.reject_draft)

    builder.add_edge(START, "safety_check")
    builder.add_conditional_edges(
        "safety_check",
        _route_after_safety_check,
        {
            "hit": "safety_stop",
            "adjust": "require_active_plan",
            "generate": "validate_required_profile",
        },
    )
    builder.add_edge("safety_stop", END)
    builder.add_edge("require_active_plan", "validate_required_profile")
    builder.add_edge("validate_required_profile", "load_context")
    builder.add_edge("load_context", "load_skill")
    builder.add_edge("load_skill", "planner")
    builder.add_edge("planner", "evaluator")
    builder.add_conditional_edges(
        "evaluator",
        _route_after_evaluator,
        {
            "pass": "persist_draft",
            "revise": "revise_once",
            "reject": "reject_draft",
        },
    )
    builder.add_edge("revise_once", "evaluator")
    builder.add_edge("persist_draft", WAITING_CONFIRMATION_NODE)
    # 确认分支：resume 载荷决定动作，节点写入 ``confirmation`` 后由条件边分流（stage5.md §3.3）。
    builder.add_conditional_edges(
        WAITING_CONFIRMATION_NODE,
        _route_after_confirmation,
        {"confirm": "activate_plan", "reject": "archive_draft"},
    )
    builder.add_edge("activate_plan", END)
    builder.add_edge("archive_draft", END)
    builder.add_edge("reject_draft", END)
    return builder.compile(checkpointer=checkpointer)


async def invoke_generate_plan(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
) -> dict[str, Any]:
    """一次生成计划 invocation 的唯一入口：用 Run 时限包住**完整**一次 Graph 调用。

    ``asyncio.timeout`` 的时限取自本次 invocation 的运行上下文
    （``run.budget.remaining_run_seconds()``），因此 180 秒上限不只限制模型调用资格：任何节点
    （包括 ``load_context`` 等非模型节点）超出 Run 时限都会以 :class:`TimeoutError` 终止本次 Run。
    若超时发生在持久化前，不写 draft／rejected、不动原 active；若业务 draft 已提交后才超时，则遵守
    stage4.md §3.10／§9.1 的跨库边界，不伪装回滚已提交的业务记录。单次请求 60 秒与最多 5 次请求的
    上限仍由 ``graph.nodes.ModelRequestBudget.begin_request`` 把握。

    生产接线的 Agent Run 走 :func:`stream_agent_run`（同一份 Run 时限，逐块产出节点更新供 SSE 使用）；
    直接调用计划子图的调用方用本函数，两者都不得绕过 Run 时限自行 ``ainvoke``。
    """
    async with asyncio.timeout(run.budget.remaining_run_seconds()):
        return await graph.ainvoke(state, config, context=run)


# ---------- 确认／拒绝唯一入口：checkpoint 优先 ＋ 唯一 draft 兜底（stage5.md §3.3） ----------


async def invoke_confirmation(
    graph: CompiledStateGraph,
    *,
    conversation_id: str,
    plan_id: int,
    action: ConfirmationAction,
    run: GeneratePlanRun,
    deps: GeneratePlanDeps,
) -> Plan:
    """确认／拒绝的唯一入口（stage5.md §3.3）：checkpoint 优先，无法恢复时按唯一 draft 兜底。

    1. ``conversation_id`` 就是 Checkpointer 的 ``thread_id``（不另建映射）；仅当图确实停在确认
       interrupt 且 interrupt 里的 ``draft_plan_id`` 等于请求 ``plan_id`` 时，才用
       ``Command(resume={"action", "plan_id"})`` 进入确认分支；不相等即
       :class:`~graph.nodes.ConfirmationConflict`，不写任何行；
    2. 没有等待任务（checkpoint 不存在、无法恢复或已跑完）时读业务库唯一 ``status='draft'``：存在且
       id 不等于请求 ``plan_id`` 即明确冲突；没有 draft 时不在这里判死——已 active／archived／rejected
       的重复请求仍交给领域服务，否则「已完成的 checkpoint 重复请求」会被绕过领域幂等服务；
    3. 两条路径最终调用**同一个** ``deps.activation``；本入口不做幂等判断，也不缓存结果。

    返回落库后的计划行（调用方按 §3.7 映射响应）。确认分支本身不调模型，也不额外写 checkpoint；
    禁止两条路径同时提交。
    """
    config = thread_config(conversation_id)
    waiting_plan_id = _waiting_confirmation_plan_id(await graph.aget_state(config))
    if waiting_plan_id is not None:
        if waiting_plan_id != plan_id:
            raise ConfirmationConflict(
                "请求 plan_id 与等待确认的 draft 不一致："
                f"{plan_id} != {waiting_plan_id}"
            )
        await graph.ainvoke(
            Command(resume={"action": action, "plan_id": plan_id}),
            config,
            context=run,
        )
        confirmed = await deps.plans.get_by_id(plan_id)
        if confirmed is None:
            raise PlanNotFound(f"计划不存在：{plan_id}")
        return confirmed
    draft = await deps.persistence.get_unique_draft()
    if draft is not None and draft.id != plan_id:
        raise ConfirmationConflict(
            f"请求 plan_id 与业务库唯一 draft 不一致：{plan_id} != {draft.id}"
        )
    timestamp = deps.now().isoformat()
    if action == "confirm":
        return await deps.activation.activate(
            plan_id,
            business_day=run.business_day,
            confirmed_at=timestamp,
            archived_at=timestamp,
        )
    return await deps.activation.reject(plan_id, archived_at=timestamp)


def _waiting_confirmation_plan_id(snapshot: StateSnapshot) -> int | None:
    """图是否正停在确认 interrupt；是则返回 interrupt 载荷里的 ``draft_plan_id``，否则 ``None``。

    只认 ``wait_for_confirmation`` 的 interrupt：没有等待任务、停在别的节点或载荷里没有计划身份
    都走唯一 draft 兜底（stage5.md §3.3 第 1–2 条）。
    """
    if WAITING_CONFIRMATION_NODE not in snapshot.next:
        return None
    for pending in snapshot.interrupts:
        value = pending.value
        if isinstance(value, Mapping) and isinstance(value.get("draft_plan_id"), int):
            return value["draft_plan_id"]
    return None


# ---------- Agent Run 入口：Router ＋ 三类非计划分支 ＋ 计划子图事件流（stage5.md §3.4／§3.6／§3.8／§3.9） ----------

#: ``form_record`` 的可见引导：只指向既有打卡表单 API，不代写任何训练事实（§3.6 行为表）。
FORM_RECORD_GUIDE = (
    "打卡记录请使用打卡表单：在「记录」页填写训练日期、动作、组数、次数与重量后提交，"
    "由既有记录接口写入；本流程不代写训练数据。"
)

#: ``natural_language_record`` 的结构化提取 Schema（模型输出）：只含 ``performed_on`` 与 ``sets``。
#: 字段名与形状由这里唯一定义；模型把自然语言动作匹配为稳定 ``exercise_id``，负重口径与记录
#: 口径的确认与字段必填／互斥仍由既有领域规则复验（stage6.md §2.2）。
class ExtractedWorkoutSet(BaseModel):
    """一组训练事实（模型提取结果）：组序号、组类型、次数、负重口径、重量与计时秒数。

    未声明字段一律拒绝（与计划 Schema／Router Schema 同口径）；取值范围、负重口径与动作目录
    是否一致都不在这里判定，由既有 ``domain.records`` 规则与目录复验拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_no: int
    set_type: SetType
    reps: int | None = None
    load_convention: LoadConvention | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ExtractedWorkout(BaseModel):
    """一次自然语言打卡的结构化提取结果：业务自然日 ＋ 全部组。"""

    model_config = ConfigDict(extra="forbid")

    performed_on: date
    sets: list[ExtractedWorkoutSet]


_EXTRACTED_WORKOUT_SCHEMA_TEXT = json.dumps(
    ExtractedWorkout.model_json_schema(), ensure_ascii=False, sort_keys=True
)

#: 自然语言打卡提取的系统提示词：只做结构化提取，不写库、不选日程、不算统计（stage6.md §2.1／§3.1）。
NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT = (
    "你是 Fit-Agent 的自然语言打卡提取器。只把用户这次训练描述提取为结构化事实，不写库、"
    "不计算任何统计、不决定候选计划日程。硬要求：\n"
    "1. 只使用 payload.actions 里给出的稳定 exercise_id；匹配不到的动作不要编造，也不要改写 id。\n"
    "2. performed_on 是训练发生的业务自然日（YYYY-MM-DD）；用户说“今天”／“昨天”时按 "
    "payload.business_day 折算。\n"
    "3. 每条组给出 set_no（同一动作内从 1 开始）、set_type（work／warmup／assisted）；外加重量"
    "动作给 weight_kg ＋ reps ＋ 与目录一致的 load_convention；纯自重动作只给 reps；计时动作"
    "只给 duration_seconds。\n"
    "4. 只输出一个 JSON 对象，字段与下方 Schema 完全一致，不输出任何解释文字或额外字段。\n"
    "提取 Schema：" + _EXTRACTED_WORKOUT_SCHEMA_TEXT
)

#: 自然语言打卡摘要的系统提示词：把确定性提取结果与数据库候选日程写成可读文本（stage6.md §2.4.3）。
#: 零候选与多候选未选择同口径：都只提示用户显式选择，不承诺任何自动写入（stage6.md §2.1「零候选」行）。
NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT = (
    "你是 Fit-Agent 的打卡摘要生成器。把 payload.workout（后端已校验的结构化提取结果）写成一段"
    "面向用户的中文摘要，让用户确认日期、动作、组数、次数、重量与时长是否与本次训练一致。"
    "payload.candidate_plan_sessions 是数据库给出的当天未完成计划日程：恰一个时提示将自动关联；"
    "零个时不得许诺任何自动写入，必须提示用户显式选择「额外训练」才写为额外训练；"
    "多于一个时必须提示用户选择某个日程或标记为额外训练，"
    "不得替用户选择，也不得编造日程 ID。只输出这一段中文文本：不输出 JSON、不输出额外字段、"
    "不重算任何数值、不提 RIR／完成率／估算 1RM。"
)

#: 提取结果未通过 Pydantic／领域／目录校验时的可见失败原因（不写库，stage6.md §2.2）。
def invalid_natural_language_record_message(error: Exception) -> str:
    """确认前校验失败的可见文本：照原样给出本项目自己的领域错误，并明确本次不写库。"""
    return (
        f"自然语言打卡未通过校验：{error}。本次不写库；请修正训练描述后重试，或改用打卡表单。"
    )

#: ``safety_stop`` 的可见提示：停止计划生成并建议专业咨询，不诊断、不输出训练计划（讨论总结 §9.2）。
SAFETY_STOP_MESSAGE = (
    "本次请求包含急性伤病相关描述：不生成训练计划，也不写入任何计划数据；"
    "请先咨询专业医疗人员，再回来安排训练。"
)

#: ``reject_draft`` 的可见说明：二次阻断失败只说明原因，不产生可激活计划，原 active 不变（讨论总结 §3.4）。
REJECT_DRAFT_MESSAGE = (
    "计划未通过评估（二次评估仍未通过）：本次不产生可激活计划，原计划保持不变。"
)

#: ``view_progress`` 的解释提示词：数值由既有 ``StatsService`` 现算，模型只解释、不重算（§3.6）。
VIEW_PROGRESS_SYSTEM_PROMPT = (
    "你是 Fit-Agent 的进步看板解释器。payload 里的 personal_bests 与 trend_summary 都是后端已经"
    "算好的确定性统计结果：只能解释它们，不得重算、补算或推断任何数值，也不得编造记录里没有的成绩。"
    "不评价进步／退步／停滞／疲劳，不输出 RIR、估算 1RM、训练容量或完成率。"
    "只输出一段面向用户的中文说明文本。"
)

#: SSE 的五类产品事件名（stage5.md §3.8）：事件集合封闭，不发别的名字。
AgentEventName = Literal["node", "message", "waiting", "done", "error"]

#: 不进入计划子图、不写业务库的三类分支（§3.6 行为表）：只产出阶段名、可见文本与正常结束；
#: ``natural_language_record`` 额外产出一个 ``waiting``（stage6.md §2.4.3），其余两类不发 ``waiting``。
NON_PLAN_INTENTS: tuple[Intent, ...] = (
    "form_record",
    "natural_language_record",
    "view_progress",
)

#: ``astream`` 的 interrupt 块键：图停在 ``wait_for_confirmation`` 时出现，是 ``waiting`` 事件的来源。
INTERRUPT_EVENT_KEY = "__interrupt__"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一条产品事件：``event`` 是五类之一，``data`` 只含该事件表格给出的键。

    不携带原始 LangGraph 块、原始模型事件、系统提示词或 Provider 配置；``node`` 的 ``name`` 是 Graph
    节点／阶段名（不是隐藏推理）；``waiting`` 按路径带载荷：计划路径是 ``draft_plan_id``，自然语言打卡
    路径是 ``workout`` ＋ ``candidate_plan_sessions``（stage6.md §2.4.3）。
    """

    event: AgentEventName
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExistingDraftTarget:
    """计划分支对业务库唯一 draft 的处置（§3.4 第 5–6 条）：两者互斥，且都不进 Graph。

    ``reused`` 非空即「普通请求 ＋ 同类既有 draft」：直接返回该 draft，不调模型、不写第二条 draft；
    ``replacement_id`` 非空即「同类 regenerate」：进 Graph，并把它作为 ``persist_draft`` 的条件替换目标
    （同一 id/version，来源不变）。
    """

    reused: Plan | None = None
    replacement_id: int | None = None


@dataclass(frozen=True, slots=True)
class AgentRunDeps:
    """``invoke_agent_run``／``stream_agent_run`` 的构造期依赖：Router、三类非计划分支与「已有 draft」
    判定（§3.4 第 5–6 条）需要的东西。

    ``model`` 必须与计划子图 ``GeneratePlanDeps.model`` 是同一个入口（唯一模型入口，stage5.md §4.3）；
    ``stats`` 是既有 ``StatsService``，只供 ``view_progress`` 现算确定性统计（不写库、不重算）；
    ``plans``／``persistence`` 是计划只读与 draft 读写的既有服务，只用于判断本次请求是复用还是同类替换
    既有 draft（本运行入口不自己写 draft）；``catalog`` 与 ``records`` 供自然语言打卡分支读取
    可读动作目录、按 ``performed_on`` 查候选日程并在确认前复用既有领域校验（都不写库）。
    """

    model: ModelCall
    stats: StatsService
    plans: PlanReadService
    persistence: PlanPersistenceService
    catalog: ActionCatalogService
    records: WorkoutRecordsService


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent Run 的产品结果：``invoke_agent_run`` 的返回值（stage5.md §3.8）。

    ``messages`` 是本次 Run 面向用户的可见文本（非计划分支的引导、说明与统计解释；计划分支的安全提示与
    二次阻断说明）。``termination_reason`` 与 ``draft_plan_id`` 取计划子图的最终 State：非计划分支没有
    计划写入，两者都是 ``None``。``intent`` 是 Router 的结论；封闭词表安全命中先于 Router，本次 Run 没有
    结论，因此是 ``None``（A1 裁决）。
    """

    intent: Intent | None
    messages: tuple[str, ...]
    termination_reason: TerminationReason | None
    draft_plan_id: int | None


async def stream_agent_run(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> AsyncIterator[AgentEvent]:
    """一次 Agent Run 的唯一实现（事件形态）：先做封闭词表安全预检，再跑确定性 Router 并按 intent 分派
    （§3.4／§3.5／§3.6／§3.8／§3.9）。

    - Run 时限（``run.budget.remaining_run_seconds()``）包住安全预检、Router 与后续**完整**一次 Graph 调用；
    - **安全预检先于 Router**（A1 裁决）：``domain.profile.safety`` 的同一份封闭词表对每个请求、每个
      intent 都先判定一次；命中即不调分类模型、不按 intent 分派、不判定 §3.4 第 5–6 条的已有 draft
      （复用／冲突／同类 regenerate）、不读 active，直接进子图由 ``safety_check``→``safety_stop`` 终止
      （§3.5），``done.intent`` 为 ``None``；
    - Router 与计划链路共享同一份 ``run.budget``：分类最多 1 次，加上计划链路的 4 次
      （Planner、Evaluator、一次修订与修订后的 Evaluator）仍在每 Run 5 次上限内；
    - ``generate_plan``／``adjust_plan``（未命中封闭词表）：按 §3.4 第 5–6 条判定已有唯一 draft 的处置，再把
      Router 结论写进冻结 State 字段 ``intent`` 后调用计划子图（调整计划的 active 预读在子图的
      ``require_active_plan`` 节点，早于 Planner）；
    - ``form_record``／``view_progress``：不进入计划子图、不写业务库，只产出阶段名、可见文本与
      ``done``（行为表见 stage5.md §3.6）；``natural_language_record``：模型提取 ``performed_on``＋
      ``sets``、通过既有领域校验后按 ``performed_on`` 查数据库候选日程，事件顺序恰为 ``node`` →
      ``message`` → ``waiting``（``workout`` ＋ ``candidate_plan_sessions``）→ ``done``，确认前不写库
      （stage6.md §2.1／§2.4.3）。

    事件在 Run 时限内逐个产出（前端要看到进度，不能等整次 Run 结束再发）：计划分支的 ``node`` 来自
    ``graph.astream`` 的节点更新，``waiting`` 来自 ``wait_for_confirmation`` 的 interrupt 块；自然语言
    打卡分支的 ``waiting`` 由 :func:`_natural_language_record` 直接产出。运行错误
    （模型配置、超时、Router 非法输出、无 active、draft 冲突等）一律向上抛：SSE 层按 §3.7 发一个
    ``error`` 事件后关闭，不在这里吞掉或改成业务结果。客户端断开不触发任何写入，也不回滚已完成的
    draft 持久化（断线不是取消、确认或拒绝信号）。
    """
    async with asyncio.timeout(run.budget.remaining_run_seconds()):
        # 封闭词表命中先于 Router（对每个请求、每个 intent 都成立，A1 裁决）：命中时不调分类模型、不按
        # intent 分派、不判定已有 draft 的复用／冲突／同类 regenerate、不读 active，直接进子图由
        # ``safety_check``→``safety_stop`` 终止（§3.5 优先于 §3.4 第 5–6 条），零业务写入、无 ``waiting``，
        # ``done.intent`` 为 ``None``（本次 Run 没有 Router 结论）。
        intent: Intent | None = None
        target = ExistingDraftTarget()
        if not message_red_flag_hits(state["request"]):
            intent = await classify_intent(
                state["request"], model=deps.model, budget=run.budget
            )
            if intent != ADJUST_PLAN_INTENT:
                # 同一份运行上下文被后续请求复用时，不允许上一次请求预读的 active 决定本次 Run 的模式。
                run.adjustment = None
            if intent in NON_PLAN_INTENTS:
                yield AgentEvent("node", {"name": intent})
                if intent == "natural_language_record":
                    # 自然语言打卡（stage6.md §2.1／§2.4.3）：提取 → 校验 → 候选日程 → 可读摘要，
                    # 事件顺序恰为 node → message → waiting → done；成功与失败都不写业务库。
                    outcome = await _natural_language_record(
                        state["request"], run=run, deps=deps
                    )
                    yield AgentEvent("message", {"text": outcome.message})
                    if outcome.waiting is not None:
                        yield AgentEvent("waiting", outcome.waiting)
                    yield _done_event(intent, termination_reason=None, draft_plan_id=None)
                    return
                yield AgentEvent(
                    "message",
                    {
                        "text": await _non_plan_text(
                            intent, state["request"], run=run, deps=deps
                        )
                    },
                )
                yield _done_event(intent, termination_reason=None, draft_plan_id=None)
                return
            target = await _existing_draft_target(
                intent, regenerate=run.regenerate, deps=deps
            )
            if target.reused is not None:
                yield AgentEvent("waiting", {"draft_plan_id": target.reused.id})
                yield _done_event(
                    intent, termination_reason=None, draft_plan_id=target.reused.id
                )
                return

        updates: dict[str, Any] = {}
        async for chunk in graph.astream(
            # ``draft_plan_id`` 显式写成本次 Run 的替换目标：不能让同一 thread 上一次 Run 留下的 id
            # 决定本次是插入新 draft 还是条件替换（§3.4 第 6 条）。
            {**state, "intent": intent, "draft_plan_id": target.replacement_id},
            config,
            context=run,
            stream_mode="updates",
        ):
            for name, update in chunk.items():
                if name == INTERRUPT_EVENT_KEY:
                    for pending in update:
                        if not isinstance(pending, Interrupt):
                            continue
                        if (event := _waiting_event(pending.value)) is not None:
                            yield event
                    continue
                if isinstance(update, Mapping):
                    updates.update(update)
                yield AgentEvent("node", {"name": name})
        termination_reason = updates.get("termination_reason")
        if termination_reason == "safety_stop":
            yield AgentEvent("message", {"text": SAFETY_STOP_MESSAGE})
        elif termination_reason == "reject_draft":
            yield AgentEvent("message", {"text": REJECT_DRAFT_MESSAGE})
        yield _done_event(
            intent,
            termination_reason=termination_reason,
            # 本次 Run 相关的 draft 身份：持久化或复用成功即新身份；同类 regenerate 被评估阻断时原 draft
            # 仍等待确认，因此仍报它（失败候选不改原 draft，§3.4 第 6 条）。
            draft_plan_id=updates.get("draft_plan_id", target.replacement_id),
        )


async def invoke_agent_run(
    graph: CompiledStateGraph,
    state: WorkflowState,
    config: RunnableConfig,
    run: GeneratePlanRun,
    deps: AgentRunDeps,
) -> AgentRunResult:
    """一次 Agent Run 的唯一入口（收集形态）：把 :func:`stream_agent_run` 的事件流收成结果对象。

    SSE 端点消费事件形态；测试与脚本需要一次性结果时用本函数。两条形态共用同一份 Router、共享预算与
    分派逻辑，不存在第二套实现。运行错误照常向上抛，不在这里吞掉或改写成业务结果。``intent`` 为 ``None``
    是正常结果（封闭词表命中先于 Router），只有整条流没有 ``done`` 才是实现错误。
    """
    intent: Intent | None = None
    termination_reason: TerminationReason | None = None
    draft_plan_id: int | None = None
    produced_done = False
    messages: list[str] = []
    async for event in stream_agent_run(graph, state, config, run, deps):
        if event.event == "message":
            messages.append(str(event.data["text"]))
        elif event.event == "done":
            produced_done = True
            intent = event.data["intent"]
            termination_reason = event.data["termination_reason"]
            draft_plan_id = event.data["draft_plan_id"]
    if not produced_done:
        raise RuntimeError("Agent Run 没有产出 done 事件：调用方不得把它当成正常结束")
    return AgentRunResult(
        intent=intent,
        messages=tuple(messages),
        termination_reason=termination_reason,
        draft_plan_id=draft_plan_id,
    )


def _done_event(
    intent: Intent | None,
    *,
    termination_reason: TerminationReason | None,
    draft_plan_id: int | None,
) -> AgentEvent:
    """``done`` 事件的唯一构造：正常结束的四个键由 §3.8 表格给出，不增不减。"""
    return AgentEvent(
        "done",
        {
            "ok": True,
            "intent": intent,
            "termination_reason": termination_reason,
            "draft_plan_id": draft_plan_id,
        },
    )


def _waiting_event(payload: Any) -> AgentEvent | None:
    """``waiting`` 事件：只取 interrupt 载荷里的 ``draft_plan_id``（§3.8）；形状不符就不发。"""
    if isinstance(payload, Mapping) and isinstance(payload.get("draft_plan_id"), int):
        return AgentEvent("waiting", {"draft_plan_id": payload["draft_plan_id"]})
    return None


async def _existing_draft_target(
    intent: Intent, *, regenerate: bool, deps: AgentRunDeps
) -> ExistingDraftTarget:
    """按 §3.4 第 5–6 条判定已有唯一 draft 的处置；冲突即 :class:`PlanDraftConflict`，不调模型、不写入。

    调用方 :func:`stream_agent_run` 已把安全命中的请求短路在进图前（§3.5），因此这里不会被安全命中
    的请求调用；本函数只判定「已有 draft 的复用／冲突／同类替换」。

    - 没有 draft：按本次 intent 正常生成（``replacement_id`` 为 ``None``）；
    - 已有生成 draft（``source_plan_id IS NULL``）：``generate_plan`` 的普通请求直接复用既有 draft；
      ``regenerate=true`` 才替换同一 id/version；``adjust_plan`` 请求是跨类型冲突；
    - 已有调整 draft：``adjust_plan`` 的普通请求一律明确失败；``regenerate=true`` 时来源计划必须仍是
      本次读取的当前 active（否则冲突）；``generate_plan`` 请求是跨类型冲突。
    """
    draft = await deps.persistence.get_unique_draft()
    if draft is None:
        return ExistingDraftTarget()
    is_adjust_draft = draft.source_plan_id is not None
    if is_adjust_draft != (intent == ADJUST_PLAN_INTENT):
        raise PlanDraftConflict(
            f"已有{'调整' if is_adjust_draft else '生成'}计划 draft，不能按本次 intent 替换：{draft.id}"
        )
    if not regenerate:
        if intent == ADJUST_PLAN_INTENT:
            raise PlanDraftConflict(
                f"已有调整计划 draft，普通调整请求不覆盖既有 draft：{draft.id}"
            )
        return ExistingDraftTarget(reused=draft)
    if intent == ADJUST_PLAN_INTENT:
        active = await deps.plans.get_active()
        if active is None or active.id != draft.source_plan_id:
            raise PlanDraftConflict(
                "既有调整计划 draft 的来源计划已不是当前 active："
                f"source_plan_id={draft.source_plan_id}"
            )
    return ExistingDraftTarget(replacement_id=draft.id)


async def _non_plan_text(
    intent: Intent, request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> str:
    """两类非计划分支的可见文本：表单引导，或 ``view_progress`` 的统计解释。

    两个分支都不进入计划子图、不写业务库；模型只在 ``view_progress`` 里解释既有 ``StatsService`` 的
    现算结果，不重算数值（§3.6 行为表）。``natural_language_record`` 不走这里：它有专用的提取与
    ``waiting`` 载荷（:func:`_natural_language_record`）。
    """
    if intent == "view_progress":
        return await _progress_explanation(
            request,
            business_day=run.business_day,
            budget=run.budget,
            deps=deps,
        )
    return FORM_RECORD_GUIDE


@dataclass(frozen=True, slots=True)
class NaturalLanguageRecordOutcome:
    """自然语言打卡分支的一次结果：可见摘要 ＋ ``waiting`` 载荷（校验失败时为 ``None``）。

    ``waiting`` 非空即提取与领域校验通过，载荷恰含 ``workout`` 与 ``candidate_plan_sessions`` 两个键
    （stage6.md §2.4.3）；为 ``None`` 表示确认前校验未通过：只发可见失败文本，不发 ``waiting``、不写库。
    """

    message: str
    waiting: dict[str, Any] | None


async def _natural_language_record(
    request: str, *, run: GeneratePlanRun, deps: AgentRunDeps
) -> NaturalLanguageRecordOutcome:
    """自然语言打卡的唯一实现（stage6.md §2.1／§2.4.3）：提取 → 校验 → 候选日程 → 可读摘要。

    - 模型只做结构化提取与可读摘要两个动作，与 Router／计划链路共享同一份 Run 预算；
    - Pydantic 结构校验失败（缺字段、非法枚举、非 JSON、非对象等）是**运行错误**，与 Router／计划
      链路同口径（stage6.md §3.1「非法输出为 Run error」）；
    - 领域规则／目录口径校验失败以可读失败文本返回，不发 ``waiting``、不写库；
    - ``candidate_plan_sessions`` 直接来自数据库按 ``performed_on`` 的查询结果，模型不得生成候选 ID；
    - 本函数全程不写业务库：只有 ``POST /api/agent/confirm-workout`` 成功才落行。
    """
    catalog = await deps.catalog.list_all()
    extraction = parse_model_json(
        await _request_model(
            deps.model,
            NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
            {
                "request": request,
                "business_day": run.business_day.isoformat(),
                "actions": [_action_payload(exercise) for exercise in catalog],
            },
            run.budget,
        ),
        ExtractedWorkout,
    )
    try:
        day, facts = await deps.records.validate_record_facts(
            extraction.performed_on,
            tuple(_workout_set_input(item) for item in extraction.sets),
        )
    except (InvalidRecordFact, UnknownExercise, RecordLoadMismatch) as error:
        return NaturalLanguageRecordOutcome(
            message=invalid_natural_language_record_message(error), waiting=None
        )
    candidates = await deps.records.list_unfinished_plan_sessions(day)
    workout = _workout_payload(day, facts)
    candidate_payloads = [
        _candidate_plan_session_payload(session) for session in candidates
    ]
    message = (
        await _request_model(
            deps.model,
            NATURAL_LANGUAGE_RECORD_MESSAGE_PROMPT,
            {
                "request": request,
                "workout": workout,
                "candidate_plan_sessions": candidate_payloads,
            },
            run.budget,
        )
    ).strip()
    return NaturalLanguageRecordOutcome(
        message=message,
        waiting={
            "workout": workout,
            "candidate_plan_sessions": candidate_payloads,
        },
    )


def _workout_set_input(item: ExtractedWorkoutSet) -> WorkoutSetInput:
    """提取结果的一条组 → 写入侧的组事实；取值范围、负重口径与必填／互斥字段由既有规则复验。"""
    return WorkoutSetInput(
        exercise_id=item.exercise_id,
        set_no=item.set_no,
        reps=item.reps,
        set_type=item.set_type,
        load_convention=item.load_convention,
        weight_kg=item.weight_kg,
        duration_seconds=item.duration_seconds,
    )


def _action_payload(exercise: Exercise) -> dict[str, Any]:
    """可读动作目录的一行：模型据此把自然语言动作匹配为稳定 ``exercise_id``。"""
    return {
        "exercise_id": exercise.id,
        "standard_name_zh": exercise.standard_name_zh,
        "record_type": exercise.record_type,
        "load_convention": exercise.load_convention,
    }


def _workout_payload(
    day: date, facts: Sequence[WorkoutSetInput]
) -> dict[str, Any]:
    """``waiting.workout``（前端确认 UI 的编辑数据源）：日期、组事实与关联日程默认值（§2.4.3）。

    初始 ``plan_session_id`` 为 ``None`` 且 ``auto_link`` 为 ``true``：用户可在确认 UI 改选具体日程或
    显式标记为额外训练；多候选不在这里替用户选择。
    """
    return {
        "performed_on": day.isoformat(),
        "sets": [
            {
                "exercise_id": fact.exercise_id,
                "set_no": fact.set_no,
                "set_type": fact.set_type,
                "load_convention": fact.load_convention,
                "weight_kg": fact.weight_kg,
                "reps": fact.reps,
                "duration_seconds": fact.duration_seconds,
            }
            for fact in facts
        ],
        "plan_session_id": None,
        "auto_link": True,
    }


def _candidate_plan_session_payload(session: PlanSession) -> dict[str, Any]:
    """``waiting.candidate_plan_sessions`` 的一行：数据库候选日程的业务事实，模型不生成。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
    }


async def _request_model(
    model: ModelCall,
    system_prompt: str,
    payload: Mapping[str, Any],
    budget: ModelRequestBudget,
) -> str:
    """一次模型请求：先扣本次 Run 的请求预算，再在剩余时限内调用注入的 callable。

    模型调用不包在任何数据库事务里；超时或调用失败都向上抛，作为运行错误终止本次 Run。
    """
    timeout = budget.begin_request()
    async with asyncio.timeout(timeout):
        return await model(system_prompt, _dump_payload(payload))


def _dump_payload(payload: Mapping[str, Any]) -> str:
    """载荷 → 模型输入文本：日期等非 JSON 原生值按文本写出，排序固定便于复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


async def _progress_explanation(
    request: str,
    *,
    business_day: date,
    budget: ModelRequestBudget,
    deps: AgentRunDeps,
) -> str:
    """``view_progress`` 的模型解释：统计由既有 ``StatsService`` 现算，模型只解释这些数值。

    不写库、不重算：模型载荷只有当前请求与两个确定性统计结果；模型请求与计划链路共享同一份 Run
    预算（超时／调用失败都是运行错误）。
    """
    payload = {
        "request": request,
        "personal_bests": [asdict(best) for best in await deps.stats.list_personal_bests()],
        "trend_summary": asdict(await deps.stats.trend_summary(business_day)),
    }
    text = await _request_model(
        deps.model, VIEW_PROGRESS_SYSTEM_PROMPT, payload, budget
    )
    return text.strip()
