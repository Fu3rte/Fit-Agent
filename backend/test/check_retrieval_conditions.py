import asyncio
import json
import os
from pathlib import Path

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_conditions import evaluate_search_conditions
from app.domain.business.errors import (
    EquipmentRestrictionConflict,
    InvalidSearchCondition,
)
from app.domain.business.models import CatalogSearchArguments, ProfileContent
from test.check_profile_confirmation import (
    SEARCH_EXERCISES,
    Fixture,
    message_text,
)
from test.regression_support import temporary_root

EVIDENCE = temporary_root("action-retrieval-t6") / "conditions"
RETRIEVAL_VARIABLES = (
    "FIT_AGENT_MILVUS_URI",
    "FIT_AGENT_MILVUS_TIMEOUT",
    "FIT_AGENT_MILVUS_TOKEN",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_WORKSPACE_ID",
)

PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "居家",
    "availability": "每周三次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": ["barbell"],
    "forbidden_exercise_ids": ["0001"],
}


def now_ms() -> int:
    import time

    return time.time_ns() // 1_000_000


def arguments(**overrides) -> CatalogSearchArguments:
    base = {"purpose": "generation", "name": "练背"}
    base.update(overrides)
    return CatalogSearchArguments.model_validate(base)


def evaluate(catalog, mappings, profile, **overrides):
    return evaluate_search_conditions(arguments(**overrides), profile, catalog, mappings)


def condition_cases() -> dict:
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    profile = ProfileContent.model_validate(PROFILE)
    evidence: dict = {}

    # 1. 居家默认徒手；未知环境沿用目录全量。
    home = evaluate(catalog, mappings, profile, name="练背")
    assert home.conditions.available_equipment == ("body weight",), home.conditions
    no_env = ProfileContent.model_validate({**PROFILE, "environment": None})
    unknown = evaluate(catalog, mappings, no_env, name="练背")
    assert set(unknown.conditions.available_equipment or ()) == set(catalog.equipment) - {"barbell"}
    evidence["home_default"] = list(home.conditions.available_equipment)
    for environment in ("在家", "在家训练", " 在家训练 ", "家庭", "家庭训练", "居家", "家里", "家中", "户外", "室外"):
        saved_home = ProfileContent.model_validate({**PROFILE, "environment": environment})
        for purpose in ("generation", "replacement"):
            saved = evaluate(catalog, mappings, saved_home, purpose=purpose)
            task = evaluate(catalog, mappings, no_env, purpose=purpose, task_environment=environment)
            assert saved.conditions == task.conditions
            assert saved.conditions.available_equipment == ("body weight",)
            declared = evaluate(catalog, mappings, saved_home, purpose=purpose, available_equipment=["哑铃"])
            assert set(declared.conditions.available_equipment) == {"body weight", "dumbbell"}
        lookup_home = evaluate(catalog, mappings, saved_home, purpose="lookup", task_environment=environment)
        assert set(lookup_home.conditions.available_equipment) == set(catalog.equipment)
        assert lookup_home.conditions.excluded_ids == ()

    for environment in ("健身房", "家庭健身房", "在家附近的健身房"):
        saved_gym = ProfileContent.model_validate({**PROFILE, "environment": environment})
        for purpose in ("generation", "replacement"):
            saved = evaluate(catalog, mappings, saved_gym, purpose=purpose)
            task = evaluate(catalog, mappings, profile, purpose=purpose, task_environment=environment)
            assert saved.conditions == task.conditions
            assert set(saved.conditions.available_equipment) == set(catalog.equipment) - {"barbell"}
            assert saved.conditions.excluded_ids == ("0001",)
        for lookup_gym in (
            evaluate(catalog, mappings, saved_gym, purpose="lookup"),
            evaluate(catalog, mappings, profile, purpose="lookup", task_environment=environment),
        ):
            assert set(lookup_gym.conditions.available_equipment) == set(catalog.equipment)
            assert lookup_gym.conditions.excluded_ids == ()

    # 2. 家里有哑铃、练背：徒手与哑铃可用，body_part=back。
    with_dumbbell = evaluate(
        catalog, mappings, profile, name="练背",
        task_environment="家里", available_equipment=["哑铃"], body_part="背",
    )
    assert set(with_dumbbell.conditions.available_equipment) == {"body weight", "dumbbell"}
    assert with_dumbbell.conditions.body_part == "back"
    evidence["home_dumbbell_back"] = sorted(with_dumbbell.conditions.available_equipment)

    # 3. 只用哑铃：精确筛选与有效集合同时生效。
    only = evaluate(catalog, mappings, profile, name="卧推", equipment="dumbbell", available_equipment=["哑铃"])
    assert only.conditions.equipment == "dumbbell"
    assert set(only.conditions.available_equipment) == {"body weight", "dumbbell"}

    # 4. 长期不可用与临时不可用合并；可用声明与限制冲突明确报错。
    merged = evaluate(catalog, mappings, profile, name="练背", unavailable_equipment=["cable"])
    assert set(merged.conditions.available_equipment) == {"body weight"}
    assert merged.conditions.excluded_ids == ("0001",)
    try:
        evaluate(catalog, mappings, profile, name="练背", available_equipment=["barbell"])
    except EquipmentRestrictionConflict as error:
        evidence["conflict"] = error.code
    else:
        raise AssertionError("可用声明与已保存不可用限制冲突未被拒绝")

    # 5. lookup 允许核实受限动作；generation 排除。
    lookup = evaluate(catalog, mappings, profile, purpose="lookup", name="卧推")
    assert set(lookup.conditions.available_equipment or ()) == set(catalog.equipment)
    assert lookup.conditions.excluded_ids == ()
    assert lookup.conditions.equipment is None
    forbidden = catalog.get("0001")
    assert forbidden is not None and lookup.conditions.matches(forbidden) is True
    generation = evaluate(catalog, mappings, profile, name="卧推")
    assert generation.conditions.matches(forbidden) is False

    # 6. null / 空数组 / 非法器械 / 未知ID / 重复。
    empty_available = evaluate(catalog, mappings, profile, name="练背", available_equipment=[])
    assert empty_available.conditions.available_equipment == ("body weight",)
    empty_excluded = evaluate(catalog, mappings, profile, name="练背", excluded_ids=[])
    assert empty_excluded.conditions.excluded_ids == ("0001",)
    rejected = {}
    for label, overrides in (
        ("equipment", {"name": "卧推", "equipment": "不存在器械"}),
        ("body_part", {"name": "练背", "body_part": "不存在部位"}),
        ("available", {"name": "练背", "available_equipment": ["不存在器械"]}),
        ("excluded", {"name": "练背", "excluded_ids": ["9999"]}),
    ):
        try:
            evaluate(catalog, mappings, profile, **overrides)
        except InvalidSearchCondition as error:
            rejected[label] = error.code
        else:
            raise AssertionError(f"非法 {label} 未被拒绝")
    evidence["rejected"] = rejected

    # 标识规则：前导零保留，canonical 精确筛选直接使用。
    keep_zero = evaluate(catalog, mappings, profile, name="卧推", excluded_ids=["0002"])
    assert "0002" in keep_zero.conditions.excluded_ids
    evidence["zero_padded"] = list(keep_zero.conditions.excluded_ids)
    return evidence


async def unavailable_case(root: Path) -> dict:
    # 检索依赖按需初始化：缺省配置只让检索请求明确失败，画像浏览与零写入不受影响。
    fixture = await Fixture(root / "unavailable.db").seeded()
    try:
        session_id = await fixture.session("检索不可用会话")
        context = fixture.context(session_id, session_id, session_id)
        async with fixture.repository.transaction():
            await fixture.repository.save_profile(
                ProfileContent.model_validate(PROFILE), 1, now_ms()
            )
        saved = os.environ.copy()
        for name in RETRIEVAL_VARIABLES:
            os.environ.pop(name, None)
        try:
            fetched = await fixture.invoke(
                SEARCH_EXERCISES, {"purpose": "lookup", "name": "卧推"}, context
            )
        finally:
            os.environ.clear()
            os.environ.update(saved)
        assert fetched.is_error is True
        assert json.loads(message_text(fetched))["code"] == "tool_execution_failed"
        # 画像读取与已保存内容不受影响，检索未产生任何长期写入。
        profile = await fixture.business.profile.get_profile()
        assert profile.version == 1 and profile.content.model_dump() == PROFILE
        return {"error": message_text(fetched)[:80], "profile_version": profile.version}
    finally:
        await fixture.close()


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    evidence = {"conditions": condition_cases()}
    evidence["unavailable"] = asyncio.run(
        unavailable_case(EVIDENCE.parent / "db")
    )
    (EVIDENCE / "conditions.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("PASS: 环境默认、可用声明、不可用合并、lookup/generation 区分、条件校验与检索不可用明确失败")


if __name__ == "__main__":
    check()
