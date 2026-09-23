import json
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.domain.actions.schema import Exercise
from app.domain.body_metrics.schema import BodyMetric
from app.domain.conversations.schema import (
    Conversation,
    ConversationEntry,
    ConversationRun,
    RunEvent,
)
from app.domain.plans.schema import Plan, PlanSession, PlanStatus
from app.domain.profile.schema import Profile
from app.domain.records.schema import WorkoutSession, WorkoutSetInput
from app.domain.stats.schema import ValidWorkSet, WorkoutFact

if TYPE_CHECKING:
    from app.application.agent.contracts import SkillMetadata, SkillReference

TModel = TypeVar("TModel", bound=BaseModel)

#: 文本形态模型调用：返回模型文本，供看板解释、打卡摘要与知识问答等无 Schema 的答复使用。
ModelCall = Callable[[str, str], Awaitable[str]]

#: 结构化形态模型调用：Schema 由所选结构化输出机制约束，直接返回 Pydantic 实例。
StructuredModelCall = Callable[[str, str, type[TModel]], Awaitable[TModel]]

#: 工具调用形态模型调用：原生消息序列与 offered 工具 tuple → 含 ``tool_calls`` 的 AIMessage。
ToolModelCall = Callable[
    [Sequence[BaseMessage], Sequence[BaseTool]], Awaitable[AIMessage]
]


@dataclass(frozen=True, slots=True)
class ModelGateway:
    """唯一模型入口的三个形态：每次调用都重新解析 Provider 配置。"""

    text: ModelCall
    structured: StructuredModelCall
    tools: ToolModelCall


class InvalidModelResponse(ValueError):
    """模型响应不是合法文本或不符合目标 Schema。"""


class ModelCallFailed(ValueError):
    """Provider／SDK 模型调用失败。"""


class TransientModelError(RuntimeError):
    """Provider 的瞬时故障：连接中断、超时、限流、服务端 5xx 与显式过载。

    Provider SDK 的瞬时异常由模型网关（infrastructure/llm/gateway.py）映射为该类型；模型请求的重试层
    只识别这一个统一类型，因此不依赖任何 Provider SDK。
    """


class ModelConfigurationError(ValueError):
    """模型配置缺失或非法：provider.json 缺失或字段为空即配置错误，不落默认端点、不猜 URL。"""


class ConversationNotFound(LookupError):
    """会话身份不存在：调用方给了不存在的 conversation_id。"""


MODEL_CALL_FAILED_MESSAGE = "模型调用失败：本次运行未产生计划写入，请稍后重试"


def dump_model_payload(payload: Mapping[str, Any]) -> str:
    """载荷 → 模型输入文本：日期等非 JSON 原生值按文本写出，排序固定便于复现。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


CacheNamespace = Literal["plans", "workouts", "metrics", "profile"]

#: namespace 与只读工具实际读取的业务表一一对应（exercises 由 schema_version 承担）。
CACHE_NAMESPACES: tuple[CacheNamespace, ...] = ("plans", "workouts", "metrics", "profile")


# ---------- 只读 Repository 端口 ----------


class ProfileReads(Protocol):
    """画像只读端口：交给只读事实工具与计划子图的那一份，不含 ``write``。"""

    async def read(self) -> Profile | None:
        """当前画像；未建档即 None。"""


class Profiles(ProfileReads, Protocol):
    """画像的读写端口。"""

    async def write(self, profile: Profile) -> None:
        """整份覆盖写入画像。"""


class ExerciseCatalog(Protocol):
    """动作目录只读端口。"""

    async def get_by_id(self, exercise_id: str) -> Exercise | None:
        """按稳定身份读一行动作；不存在即 None。"""

    async def list_all(self) -> tuple[Exercise, ...]:
        """动作目录全量。"""


class Plans(Protocol):
    """计划版本与日程的只读端口。"""

    async def read_active(self) -> Plan | None:
        """当前 active 计划；没有正式启用的计划即 None。"""

    async def read_by_id(self, plan_id: int) -> Plan | None:
        """按身份读一个计划版本（含历史版本）。"""

    async def read_draft(self) -> Plan | None:
        """业务库唯一 draft；没有即 None。"""

    async def list_versions(self) -> tuple[Plan, ...]:
        """全部计划版本，按版本号升序。"""

    async def list_sessions(self, plan_id: int) -> tuple[PlanSession, ...]:
        """某个计划的全部日程（含已取消行）。"""


class Records(Protocol):
    """训练记录只读端口。"""

    async def read(self, session_id: int) -> WorkoutSession | None:
        """按身份读一次训练及其全部组。"""

    async def list_all(self) -> tuple[WorkoutSession, ...]:
        """全部训练，按发生日期排序。"""

    async def list_recent(
        self,
        limit: int,
        *,
        from_on: date | None = None,
        to_on: date | None = None,
        exercise_ids: Collection[str] = (),
    ) -> tuple[WorkoutSession, ...]:
        """最近 ``limit`` 次训练；日期闭区间与动作集合条件取 AND，命中会话返回全部组。"""

    async def list_unfinished_plan_sessions(
        self, scheduled_on: date
    ) -> tuple[PlanSession, ...]:
        """某个业务日可关联的计划日程候选。"""


class BodyMetrics(Protocol):
    """身体指标只读端口。"""

    async def read(self, metric_id: int) -> BodyMetric | None:
        """按身份读一条身体指标。"""

    async def list_all(self) -> tuple[BodyMetric, ...]:
        """全部身体指标，按发生日期排序。"""


class Stats(Protocol):
    """统计源数据的只读端口：只取事实，不做计算。"""

    async def list_valid_work_sets(self) -> tuple[ValidWorkSet, ...]:
        """全部有效工作组事实。"""

    async def list_body_metrics_between(
        self, from_on: date, to_on: date
    ) -> tuple[BodyMetric, ...]:
        """区间内的身体指标。"""

    async def read_latest_two_weights(self) -> tuple[BodyMetric, ...]:
        """最近两条记录了体重的身体指标，按日期降序。"""

    async def read_latest_two_body_fats(self) -> tuple[BodyMetric, ...]:
        """最近两条记录了体脂的身体指标，按日期降序。"""

    async def read_last_workout_on(self) -> date | None:
        """最近一次训练的发生日期。"""

    async def list_workouts_between(
        self, from_on: date, to_on: date
    ) -> tuple[WorkoutFact, ...]:
        """区间内的训练事实。"""

    async def list_linked_workouts(self) -> tuple[WorkoutFact, ...]:
        """全部关联了计划日程的训练事实。"""


class Conversations(Protocol):
    """会话、Run 与 Entry 的端口：只读方法走唯一锁，生命周期方法自开事务。"""

    async def list_conversations(self) -> tuple[Conversation, ...]:
        """全部会话，按 ``updated_at`` 降序。"""

    async def read_conversation(self, conversation_id: str) -> Conversation | None:
        """按身份读会话。"""

    async def list_entries(self, conversation_id: str) -> tuple[ConversationEntry, ...]:
        """会话全部 Entry，按追加序号升序。"""

    async def read_run_by_client_request_id(
        self, client_request_id: str
    ) -> ConversationRun | None:
        """按幂等键读 Run。"""

    async def read_run_by_thread_id(self, thread_id: str) -> ConversationRun | None:
        """按 LangGraph thread 身份读 Run。"""

    async def list_runs(self, conversation_id: str) -> tuple[ConversationRun, ...]:
        """会话全部 Run，按开始时间升序。"""

    async def read_confirmations(
        self, run_id: str, action: str
    ) -> tuple[ConversationEntry, ...]:
        """某个 Run 上指定动作的确认 Entry。"""

    async def list_run_events(self, run_id: str) -> tuple[RunEvent, ...]:
        """Run 已提交的全部事件，按序号升序。"""

    async def create_conversation(
        self, *, conversation_id: str, title: str, created_at: str
    ) -> Conversation:
        """新建空会话；身份由调用方给出。"""

    async def delete_conversation(self, conversation_id: str) -> bool:
        """删除会话；返回是否删除了行。"""

    async def converge_unfinished_runs(
        self, *, error_code: str, updated_at: str
    ) -> int:
        """把所有未结束 Run 收敛为 ``failed``，返回收敛行数。"""

    async def cancel_active_run(self, run_id: str, *, updated_at: str) -> bool:
        """把指定 Run 收敛为 ``cancelled``；已结束时即 False。"""


class ToolCacheRevisions(Protocol):
    """工具缓存 revision 的只读端口。"""

    async def read_all(self) -> dict[str, int]:
        """当前全部 namespace revision。"""


class SkillSource(Protocol):
    """共享 Skill 端口：启动时枚举元数据，正文与 reference 按需独立读取。"""

    def list_metadata(self) -> tuple["SkillMetadata", ...]:
        """返回扫描到的 Skill 元数据与内部可解析位置。"""
        ...

    def read_skill(self, name: str) -> str:
        """按已扫描名称读取 SKILL.md 正文。"""
        ...

    def read_reference(self, name: str, relative_path: str) -> "SkillReference":
        """按 Skill 名与明确相对路径读取单个 reference。"""
        ...


class WorkoutRecords(Protocol):
    """训练记录用例的调用面：Agent Run 依赖的读取与确认前校验。"""

    async def list_recent(
        self,
        limit: int,
        *,
        from_on: date | None = None,
        to_on: date | None = None,
        exercise_ids: Collection[str] = (),
    ) -> tuple[WorkoutSession, ...]:
        """最近 ``limit`` 次训练；日期闭区间与动作集合条件取 AND，命中会话返回全部组。"""

    async def list_unfinished_plan_sessions(
        self, scheduled_on: date
    ) -> tuple[PlanSession, ...]:
        """某个业务日可关联的计划日程候选。"""

    async def validate_record_facts(
        self, performed_on: date, facts: Sequence[WorkoutSetInput]
    ) -> tuple[date, tuple[WorkoutSetInput, ...]]:
        """确认前校验：返回归一化后的业务日与组事实；不写库。"""


# ---------- 事务内的持久化契约（conn 由 Database.transaction() 提供，调用方原样传给 Repository） ----------


@dataclass(frozen=True, slots=True)
class ActivePlanSnapshot:
    """写路径前后的原 active 快照（stage4.md §9.2）：身份／状态／版本／内容／确认时间 ＋ active 行数。

    Application 需要的持久化比较契约：写事务前后读同一次，逐字段相等才允许提交。
    """

    count: int
    id: int | None
    status: PlanStatus | None
    version: int | None
    structured_content: Any
    confirmed_at: str | None


class HealthProbe(Protocol):
    """组合根装配的健康探针：传输层只读探针结果，不持有 Database 与 Provider 配置能力。"""

    def database_is_open(self) -> bool:
        """数据库连接当前是否打开。"""

    def provider_has_api_key(self) -> bool:
        """Provider 配置里是否已写入 API key。"""
