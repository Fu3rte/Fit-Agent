from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from langchain_core.messages import BaseMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import Field

from app.application.agent.budget import request_structured_model
from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.common import (
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
    tool_call_records,
)
from app.application.agent.harness.tools.exercise_dataset.store import (
    ExerciseCatalogUnavailable,
)
from app.application.agent.prompts import NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT
from app.domain.actions.rules import UnknownExercise
from app.domain.actions.schema import Exercise, LoadConvention
from app.domain.conversations.context import ContextMessage, history_payload
from app.domain.plans.schema import PlanSession
from app.domain.records.schema import SetType, WorkoutSetInput


class ExtractedWorkoutSet(StrictModel):
    """一组训练事实（模型提取结果）。"""

    exercise_id: str
    set_no: int
    set_type: SetType
    reps: int | None = None
    load_convention: LoadConvention | None = None
    weight_kg: float | None = None
    duration_seconds: int | None = None


class ExtractedWorkout(StrictModel):
    """一次自然语言打卡的结构化提取结果：业务自然日 ＋ 全部组。"""

    performed_on: date
    sets: list[ExtractedWorkoutSet]


class WorkoutConfirmationPayload(StrictModel):
    """待确认的打卡载荷：组事实、候选日程与固定的确认标记。"""

    workout: dict[str, Any]
    candidate_plan_sessions: tuple[dict[str, Any], ...]
    requires_confirmation: Literal[True] = True


class PrepareWorkoutRecordArgs(HarnessToolArgs):
    """自然语言打卡候选：只收本次训练描述原文，业务日与目录由运行上下文注入。"""

    request: str = Field(min_length=1, max_length=4000)


async def prepare_workout_record_payload(
    context: TrainingHarnessContext,
    request: str,
    *,
    history: Sequence[ContextMessage] = (),
) -> WorkoutConfirmationPayload:
    """自然语言描述 → 已校验的打卡候选：日期、组事实与当天未完成计划日程，不入库。

    提取用的模型请求与本次 Run 共用同一份预算；``history`` 由直接调用方给出，经 harness 驱动时
    历史已在消息序列里。
    """
    catalog = await context.catalog.list_all()
    if not catalog:
        raise ExerciseCatalogUnavailable("canonical 动作目录为空：无法构造提取候选")
    canonical_ids = {exercise.id for exercise in catalog}
    extraction = await request_structured_model(
        context.model,
        NATURAL_LANGUAGE_RECORD_EXTRACTION_PROMPT,
        {
            "request": request,
            **history_payload(history),
            "business_day": context.business_day.isoformat(),
            "actions": [_action_payload(exercise) for exercise in catalog],
        },
        context.budget,
        ExtractedWorkout,
    )
    if unknown := [
        item.exercise_id
        for item in extraction.sets
        if item.exercise_id not in canonical_ids
    ]:
        raise UnknownExercise(f"动作身份不在 canonical 目录内：{unknown}")
    day, facts = await context.records.validate_record_facts(
        extraction.performed_on,
        tuple(_workout_set_input(item) for item in extraction.sets),
    )
    candidates = await context.records.list_unfinished_plan_sessions(day)
    return WorkoutConfirmationPayload(
        workout=_workout_payload(day, facts),
        candidate_plan_sessions=tuple(
            _candidate_plan_session_payload(session) for session in candidates
        ),
    )


@tool(args_schema=PrepareWorkoutRecordArgs)
async def prepare_workout_record(
    request: str,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """把一次训练描述提取为待确认的打卡候选；本 Tool 不写业务训练表，确认写入由确认端点承担。"""
    if runtime.context.snapshot is None:
        raise ExerciseCatalogUnavailable("缺少 Run 事实快照：动作目录与 revision 不可用")
    payload = await prepare_workout_record_payload(
        runtime.context, request, history=runtime.context.conversation_history
    )
    return dump_tool_payload(payload)


class MissingWorkoutRecordCandidate(ValueError):
    """本次 harness 没有唯一一次成功的 ``prepare_workout_record`` 调用：不得据此生成确认动作。"""


def workout_confirmation_payload(
    messages: Sequence[BaseMessage],
) -> WorkoutConfirmationPayload:
    """harness 消息序列 → 唯一一次成功 ``prepare_workout_record`` 的确认载荷；零次或多次即明确失败。"""
    records = [
        record
        for record in tool_call_records(messages)
        if record.tool_name == "prepare_workout_record"
    ]
    if len(records) != 1:
        raise MissingWorkoutRecordCandidate(
            f"成功的 prepare_workout_record 调用次数不是 1：{len(records)}"
        )
    return WorkoutConfirmationPayload.model_validate(records[0].payload)


def workout_confirmation_action(
    payload: WorkoutConfirmationPayload,
) -> dict[str, Any]:
    """``workout_confirmation`` 动作：待确认的训练事实与候选日程，确认前不写业务训练表。"""
    return {
        "type": "workout_confirmation",
        **payload.model_dump(mode="json", exclude={"requires_confirmation"}),
    }


def _workout_set_input(item: ExtractedWorkoutSet) -> WorkoutSetInput:
    """提取结果的一条组 → 写入侧的组事实。"""
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
        "aliases": list(exercise.aliases),
        "equipment_variant": exercise.equipment_variant,
        "modes": list(exercise.modes),
        "record_type": exercise.record_type,
        "load_convention": exercise.load_convention,
    }


def _workout_payload(day: date, facts: Sequence[WorkoutSetInput]) -> dict[str, Any]:
    """``workout_confirmation.workout``（前端确认 UI 的编辑数据源）：日期、组事实与关联日程默认值。"""
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
    """``workout_confirmation.candidate_plan_sessions`` 的一行。"""
    return {
        "id": session.id,
        "plan_id": session.plan_id,
        "scheduled_on": session.scheduled_on.isoformat(),
    }
