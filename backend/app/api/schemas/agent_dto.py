"""Agent 三端点的请求体模型（Pydantic 只管形状、必填与基础类型）。"""

from datetime import date
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentRunBody(BaseModel):
    """``POST /api/agent/run`` 的请求体。

    ``chat_id`` 是稳定会话身份，``conversation_id`` 是本次用的 LangGraph thread 身份，
    ``client_request_id`` 是本轮幂等键；幂等键与请求原文的空白值在 DTO 层就拒绝（§8）。
    """

    model_config = ConfigDict(extra="forbid")

    chat_id: UUID
    conversation_id: UUID
    client_request_id: Annotated[str, Field(min_length=1)]
    request: Annotated[str, Field(min_length=1)]
    regenerate: bool = False


class AgentPlanBody(BaseModel):
    """``POST /api/agent/confirm``／``reject`` 的请求体：会话身份 ＋ 目标计划身份。"""

    model_config = ConfigDict(extra="forbid")

    chat_id: UUID
    conversation_id: UUID
    plan_id: int


class WorkoutSetBody(BaseModel):
    """自然语言打卡确认提交的一组训练事实。"""

    model_config = ConfigDict(extra="forbid")

    exercise_id: str
    set_no: int
    set_type: str
    reps: int | None = None
    load_convention: str | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ConfirmWorkoutBody(BaseModel):
    """``POST /api/agent/confirm-workout`` 的请求体：完整确认载荷。"""

    model_config = ConfigDict(extra="forbid")

    chat_id: UUID
    conversation_id: UUID
    performed_on: date
    sets: list[WorkoutSetBody]
    plan_session_id: int | None = None
    auto_link: bool = False
