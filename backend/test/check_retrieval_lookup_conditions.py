import json

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import evaluate_search_conditions
from app.domain.business.models import CatalogSearchArguments, ProfileContent
from test.regression_support import temporary_root

EVIDENCE = temporary_root("action-retrieval-t6") / "fix-lookup"
PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周三次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


def arguments(**overrides) -> CatalogSearchArguments:
    base = {"purpose": "lookup", "name": "核实动作"}
    base.update(overrides)
    return CatalogSearchArguments.model_validate(base)


def evaluate(catalog, mappings, profile, **overrides):
    return evaluate_search_conditions(arguments(**overrides), profile, catalog, mappings)


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    profile = ProfileContent.model_validate(PROFILE)
    barbell = catalog.get("0023")
    bodyweight = catalog.get("0001")
    dumbbell = next(item for item in catalog.all() if item.equipment == "dumbbell")
    assert barbell is not None and barbell.equipment == "barbell", "0023 应为杠铃动作"
    assert bodyweight is not None and bodyweight.equipment == "body weight", "0001 应为徒手动作"

    # 1) 本次临时不可用器械在 lookup 下真实过滤：杠铃动作被排除，徒手动作保留。
    unavailable = evaluate(catalog, mappings, profile, unavailable_equipment=["barbell"])
    assert unavailable.conditions.matches(barbell) is False
    assert unavailable.conditions.matches(bodyweight) is True
    assert set(unavailable.conditions.available_equipment or ()) == set(catalog.equipment) - {"barbell"}

    # 2) 可用声明是放行声明，不缩成“仅这些器械”：徒手动作仍可核实。
    declared = evaluate(catalog, mappings, profile, available_equipment=["dumbbell"])
    assert declared.conditions.matches(bodyweight) is True
    assert declared.conditions.matches(barbell) is True
    assert set(declared.conditions.available_equipment or ()) == set(catalog.equipment)
    assert not any("可用器械" in note for note in declared.condition_notes)

    # 3) 显式精确器械筛选在 lookup 下仍生效。
    only_dumbbell = evaluate(catalog, mappings, profile, equipment="dumbbell")
    assert only_dumbbell.conditions.equipment == "dumbbell"
    assert only_dumbbell.conditions.matches(barbell) is False
    assert only_dumbbell.conditions.matches(dumbbell) is True

    # 4) 本次临时排除在 lookup 下仍生效。
    excluded = evaluate(catalog, mappings, profile, excluded_ids=["0023"])
    assert excluded.conditions.matches(barbell) is False
    assert excluded.conditions.matches(bodyweight) is True
    assert excluded.condition_notes == ("本次排除1个动作",)

    (EVIDENCE / "lookup-conditions.json").write_text(
        json.dumps(
            {
                "temporary_unavailable_matches_barbell": unavailable.conditions.matches(barbell),
                "declaration_matches_bodyweight": declared.conditions.matches(bodyweight),
                "explicit_equipment_matches_barbell": only_dumbbell.conditions.matches(barbell),
                "temporary_excluded_matches_barbell": excluded.conditions.matches(barbell),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("PASS: lookup 临时不可用/精确筛选/临时排除生效；可用声明不收窄")


if __name__ == "__main__":
    check()
