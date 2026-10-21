import asyncio
import json
from pathlib import Path

from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import EvaluatedConditions
from app.application.business.retrieval_response import (
    DETAILS_BUDGET,
    SEARCH_BUDGET,
    build_details_result,
    build_search_result,
    build_search_stage,
)
from app.application.business.retrieval_search import (
    RetrievalCandidate,
    SearchConditions,
)
from app.domain.business.errors import ExerciseDetailsTooLarge, ExerciseNotFound
from app.domain.business.models import ExerciseDetailResult
from test.check_profile_confirmation import (
    GET_EXERCISE_DETAILS,
    SEARCH_EXERCISES,
    Fixture,
    envelope,
)
from test.regression_support import temporary_root

EVIDENCE = temporary_root("action-retrieval-t6") / "response"
SEARCH_ITEM_FIELDS = {"id", "name", "equipment", "target", "matches"}
DETAIL_FIELDS = {
    "id", "name", "body_part", "equipment", "target",
    "muscle_group", "secondary_muscles", "load_convention", "steps",
}


def encoded(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def candidate_rows(catalog: Catalog, count: int) -> list[RetrievalCandidate]:
    return [
        RetrievalCandidate(
            id=exercise.id, score=1.0 - index * 0.001, name=exercise.name,
            equipment=exercise.equipment, body_part=exercise.body_part,
            target=exercise.target, muscle_group=exercise.muscle_group,
        )
        for index, exercise in enumerate(catalog.all()[:count])
    ]


def evaluated(fields=("body_part",), notes=("环境=居家",)) -> EvaluatedConditions:
    return EvaluatedConditions(
        SearchConditions(body_part="back"), tuple(fields), tuple(notes)
    )


def search_cases() -> dict:
    catalog = Catalog.load()
    evidence: dict = {}
    recalled = candidate_rows(catalog, 12)
    stage = build_search_stage(recalled, evaluated(), rerank=None, threshold=0.0)
    limit_budget = stage.response
    assert len(stage.candidate_pool) == 12
    assert len(limit_budget.items) <= 5
    assert limit_budget.has_more is True
    assert not hasattr(limit_budget, "remaining_ids")
    assert all(set(item.model_dump()) == SEARCH_ITEM_FIELDS for item in limit_budget.items)
    payload = encoded(limit_budget.model_dump())
    assert len(payload) <= SEARCH_BUDGET, len(payload)
    assert set(json.loads(payload)) == {"items", "has_more"}
    # matches 只含可核实目录字段与有效条件。
    assert limit_budget.items[0].matches[0].startswith("body_part=")
    assert "环境=居家" in limit_budget.items[0].matches
    hidden = [item for item in recalled[5:]]
    for item in hidden:
        assert f'"id": "{item.id}"' not in payload
        assert f'"name": "{item.name}"' not in payload
    evidence["limit"] = {"items": len(limit_budget.items), "chars": len(payload)}

    trimmed = build_search_result(recalled, evaluated(), budget=600)
    assert trimmed.has_more is True
    assert len(trimmed.items) < 5
    assert len(encoded(trimmed.model_dump())) <= 600
    evidence["trimmed"] = {"items": len(trimmed.items)}

    empty_stage = build_search_stage([], evaluated(), rerank=None, threshold=0.0)
    empty = empty_stage.response
    assert empty.items == [] and empty.has_more is False
    return evidence


def details_cases() -> dict:
    catalog = Catalog.load()
    evidence: dict = {}
    longest = max(
        len(encoded(
            ExerciseDetailResult(
                items=[exercise], has_more=False, remaining_ids=[]
            ).model_dump()
        ))
        for exercise in catalog.all()
    )
    # T2 最长详情数据适配 7000 字符预算：单条完整详情可容纳。
    assert longest <= DETAILS_BUDGET, longest
    evidence["longest_detail_chars"] = longest

    ids = [exercise.id for exercise in catalog.all()[:5]]
    five = build_details_result(ids, catalog)
    assert [item.id for item in five.items] == ids
    assert five.has_more is False and five.remaining_ids == []
    assert all(set(item.model_dump()) == DETAIL_FIELDS for item in five.items)
    assert len(encoded(five.model_dump())) <= DETAILS_BUDGET
    evidence["five"] = len(five.items)

    trimmed = build_details_result(ids, catalog, budget=900)
    assert 0 < len(trimmed.items) < 5
    assert trimmed.has_more is True
    assert trimmed.remaining_ids == ids[len(trimmed.items):]
    assert len(encoded(trimmed.model_dump())) <= 900

    try:
        build_details_result([ids[0]], catalog, budget=100)
    except ExerciseDetailsTooLarge as error:
        evidence["too_large"] = error.code
    else:
        raise AssertionError("单条超预算详情未被拒绝")

    try:
        build_details_result(["9999"], catalog)
    except ExerciseNotFound as error:
        evidence["unknown"] = error.code
    else:
        raise AssertionError("未知动作 ID 未被拒绝")
    return evidence


async def tool_cases(root: Path) -> dict:
    fixture = await Fixture(root / "tools.db").seeded()
    try:
        session_id = await fixture.session("详情工具会话")
        context = fixture.context(session_id, session_id, session_id)
        detailed = await fixture.invoke(GET_EXERCISE_DETAILS, {"ids": ["0001"]}, context)
        assert detailed.is_error is False
        assert set(envelope(detailed)) == {"items", "has_more", "remaining_ids"}
        unknown = await fixture.invoke(GET_EXERCISE_DETAILS, {"ids": ["9999"]}, context)
        assert unknown.is_error is True and envelope(unknown)["code"] == "exercise_not_found"
        # 参数层拒绝：空、重复、超上限与缺失字段，不产生任何业务写入。
        for arguments in (
            {"ids": []},
            {"ids": ["0001", "0001"]},
            {"ids": ["0001", "0002", "0003", "0004", "0005", "0006"]},
            {},
        ):
            message = await fixture.invoke(GET_EXERCISE_DETAILS, arguments, context)
            assert message.is_error is True
        # 检索参数必填 purpose：缺失在参数层拒绝。
        for arguments in ({"name": "卧推"}, {"purpose": "lookup"}):
            message = await fixture.invoke(SEARCH_EXERCISES, arguments, context)
            assert message.is_error is True
        assert (await fixture.business.profile.get_profile()).version is None
        return {"detail": envelope(detailed)["items"][0]["id"], "unknown": "exercise_not_found"}
    finally:
        await fixture.close()


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence = {"search": search_cases(), "details": details_cases()}
    evidence["tools"] = asyncio.run(tool_cases(EVIDENCE.parent / "db"))
    (EVIDENCE / "response.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("PASS: 候选池与最终输入上限、预算、has_more 含义、隐藏身份、详情九字段与预算、参数层拒绝")


if __name__ == "__main__":
    check()
