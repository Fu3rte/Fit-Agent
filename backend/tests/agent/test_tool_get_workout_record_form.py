import json
from types import SimpleNamespace

from app.application.agent.harness.tools.common import WORKOUT_FORM_FIELDS
from app.application.agent.harness.tools.get_workout_record_form import (
    GetWorkoutRecordFormArgs,
    WorkoutFormPayload,
    get_workout_record_form,
)
from app.application.agent.prompts import FORM_RECORD_GUIDE

REQUIRED_FIELDS = (
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


async def _call() -> str:
    return await get_workout_record_form.ainvoke(
        {"runtime": SimpleNamespace(context=None)}
    )


def test_tool_call_schema_hides_runtime_and_requires_no_model_arguments() -> None:
    """模型可见 Schema 不含 runtime，且没有必填参数。"""
    visible = get_workout_record_form.tool_call_schema.model_json_schema()

    assert visible["properties"] == {}
    assert visible.get("required", []) == []
    assert get_workout_record_form.args_schema is GetWorkoutRecordFormArgs
    assert set(GetWorkoutRecordFormArgs.model_json_schema()["properties"]) == {
        "runtime"
    }
    assert GetWorkoutRecordFormArgs.model_json_schema()["additionalProperties"] is False


def test_fields_carry_the_nine_required_entries_in_a_stable_order() -> None:
    """fields 覆盖打卡表单的九个必需字段，且相对顺序稳定。"""
    assert set(REQUIRED_FIELDS) <= set(WORKOUT_FORM_FIELDS)
    positions = [WORKOUT_FORM_FIELDS.index(name) for name in REQUIRED_FIELDS]

    assert positions == sorted(positions)


async def test_legal_call_returns_a_valid_workout_form_payload() -> None:
    """无模型参数即可调用，输出是 WorkoutFormPayload 的 JSON 投影。"""
    content = await _call()

    payload = WorkoutFormPayload.model_validate(json.loads(content))
    assert payload.form == "workout_record"
    assert payload.fields == WORKOUT_FORM_FIELDS
    assert payload.guide == FORM_RECORD_GUIDE
