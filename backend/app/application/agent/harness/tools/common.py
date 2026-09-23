import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

from fastapi.encoders import jsonable_encoder
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.tools import InjectedToolArg
from pydantic import BaseModel, ConfigDict

from app.application.agent.budget import ModelRequestBudget
from app.application.agent.contracts import ToolExecutionContext
from app.application.agent.harness.declaration import HarnessContext
from app.application.agent.harness.tools.exercise_dataset.store import (
    CanonicalExerciseDataset,
)
from app.application.ports import ExerciseCatalog, Plans, ProfileReads, WorkoutRecords
from app.application.services.stats_service import StatsService


class CatalogReferentialIntegrityError(RuntimeError):
    """计划引用的 canonical 动作缺失或名称无法解析：阻断 Planner 与 Evaluator。"""


@dataclass(frozen=True, slots=True)
class TrainingHarnessContext(HarnessContext):
    """业务工具上下文：注入的业务日期、共享请求预算与既有只读入口。

    ``budget`` 收窄为 :class:`ModelRequestBudget`：业务工具内的模型请求（自然语言提取）与 agent loop
    的模型请求共用同一份次数与时限。
    """

    budget: ModelRequestBudget
    business_day: date
    profiles: ProfileReads
    plans: Plans
    catalog: ExerciseCatalog
    records: WorkoutRecords
    stats: StatsService
    #: canonical 目录 × 数据集的联表只读端口；由组合根构造单例并注入。
    dataset: CanonicalExerciseDataset | None = None
    #: 本次 Run 的事实快照键；由 Runtime 在每次 Run 注入，未提供时为 None。
    snapshot: ToolExecutionContext | None = None


class HarnessToolArgs(BaseModel):
    """Harness 工具参数基线：未声明字段一律拒绝，并声明 ToolNode 注入的上下文参数。

    langchain-core 1.6.3 的 ``BaseTool._parse_input`` 用 ``args_schema`` 校验注入后的参数，
    ``runtime`` 必须声明为字段才通得过校验；它带 ``InjectedToolArg``，因此不出现在模型可见的
    ``tool_call_schema`` 里（``convert_to_openai_tool`` 也就看不到它）。
    """

    model_config = ConfigDict(extra="forbid")

    runtime: Annotated[Any, InjectedToolArg]


class StrictModel(BaseModel):
    """Tool 输出视图的共同底线：未声明字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")


#: ``workout_form`` 的字段清单：前端按它渲染，服务端字段校验仍由既有记录接口承担。
WORKOUT_FORM_FIELDS: tuple[str, ...] = (
    "performed_on",
    "exercise_id",
    "set_no",
    "set_type",
    "reps",
    "load_convention",
    "weight_kg",
    "duration_seconds",
    "plan_session_id",
)


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """一次真实完成的只读 Tool 调用：工具名与它返回给模型的载荷。"""

    tool_name: str
    payload: Any


def tool_call_records(messages: Sequence[BaseMessage]) -> tuple[ToolCallRecord, ...]:
    """harness 消息序列 → 真实完成的工具调用轨迹：只含 ToolNode 已成功执行的调用。"""
    records: list[ToolCallRecord] = []
    for message in messages:
        if (
            not isinstance(message, ToolMessage)
            or message.status != "success"
            or message.name is None
        ):
            continue
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"工具 {message.name} 的输出不是合法 JSON：{message.content[:120]!r}"
            ) from error
        records.append(ToolCallRecord(tool_name=message.name, payload=payload))
    return tuple(records)


def dump_tool_payload(payload: Any) -> str:
    """工具输出文本：jsonable_encoder 归一化领域对象后交给标准 json.dumps。"""
    return json.dumps(jsonable_encoder(payload), ensure_ascii=False)
