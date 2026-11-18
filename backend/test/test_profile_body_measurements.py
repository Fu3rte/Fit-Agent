import pytest
from pydantic import ValidationError

from app.agent.prompts import SYSTEM_PROMPT
from app.application.business.plans import PlanService
from app.application.business.profile import validate_content
from app.domain.business.errors import InvalidBusinessPayload, ProfileRequired, ProfileVersionConflict
from app.domain.business.models import PlanSnapshot, ProfileContent, ProfileRecord


PROFILE = {
    "goal": None, "experience": None, "environment": None, "availability": None,
    "health_notes": None, "movement_restrictions": None,
    "unavailable_equipment": None, "forbidden_exercise_ids": None,
    "height_cm": 175.5, "body_weight_kg": 70.25, "body_weight_measured_on": "2024-02-29",
}


@pytest.mark.parametrize("field", ["height_cm", "body_weight_kg"])
@pytest.mark.parametrize("value", [None, True, "70", 0, -1, float("inf"), float("nan")])
def test_measurements_reject_invalid_values(field, value):
    with pytest.raises(InvalidBusinessPayload) as caught:
        validate_content(ProfileContent, {**PROFILE, field: value})
    assert any(error.path == f"/payload/{field}" for error in caught.value.errors)


@pytest.mark.parametrize("field", ["height_cm", "body_weight_kg", "body_weight_measured_on"])
def test_measurements_require_keys(field):
    with pytest.raises(InvalidBusinessPayload) as caught:
        validate_content(ProfileContent, {key: value for key, value in PROFILE.items() if key != field})
    assert any(error.path == f"/payload/{field}" for error in caught.value.errors)


def test_measurement_date_and_precision():
    assert ProfileContent.model_validate(PROFILE).model_dump() == PROFILE
    assert ProfileContent.model_validate({**PROFILE, "body_weight_measured_on": None}).body_weight_measured_on is None
    for value in ("2023-02-29", "2024-2-29", "tomorrow"):
        with pytest.raises(ValidationError):
            ProfileContent.model_validate({**PROFILE, "body_weight_measured_on": value})
    assert "仅更新身高时保留体重及其日期" in SYSTEM_PROMPT
    assert "更新体重时记录本次测量日期，未提供则置 null" in SYSTEM_PROMPT


def test_plan_profile_basis():
    service = object.__new__(PlanService)
    profile = ProfileRecord(version=1, content=ProfileContent.model_validate(PROFILE), updated_at=1)
    for kind in ("generation", "adjustment"):
        assert isinstance(service._plan_profile_conflict(None, None, kind), ProfileRequired)
        assert service._plan_profile_conflict(profile, 1, kind) is None
        assert isinstance(service._plan_profile_conflict(profile, 2, kind), ProfileVersionConflict)
    assert service._plan_profile_conflict(None, None, "import") is None
    assert "生成和调整" in str(ProfileRequired())


def test_adjustment_snapshot_requires_saved_profile_version():
    snapshot = {
        "proposal_id": "00000000-0000-4000-8000-000000000001",
        "session_id": "00000000-0000-4000-8000-000000000002",
        "request_entry_id": "00000000-0000-4000-8000-000000000003",
        "source_entry_id": "00000000-0000-4000-8000-000000000004",
        "base_profile_version": None, "base_plan_id": None,
        "payload": {"repeat": None, "days": [{"kind": "rest", "focus": None, "exercises": [], "notes": None}],
                    "notes": None, "suggested_fields": []},
        "status": "pending", "created_at": 1,
    }
    for kind in ("generation", "adjustment"):
        with pytest.raises(ValidationError):
            PlanSnapshot.model_validate({**snapshot, "preparation_kind": kind})
    assert PlanSnapshot.model_validate({**snapshot, "preparation_kind": "import"}).base_profile_version is None
