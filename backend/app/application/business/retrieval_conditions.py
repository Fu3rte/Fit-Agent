from dataclasses import dataclass

from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog
from app.application.business.retrieval_search import SearchConditions
from app.domain.business.errors import (
    EquipmentRestrictionConflict,
    InvalidSearchCondition,
)
from app.domain.business.models import CatalogSearchArguments, ProfileContent

# 四个精确筛选字段必须映射为目录 canonical；器械声明可命中一个或多个 canonical。
EXACT_FIELDS = ("equipment", "body_part", "target", "muscle_group")
BODY_WEIGHT = "body weight"
# 居家/户外默认仅徒手；在家/家庭别名按完整规范值匹配。
HOME_ENVIRONMENT_TOKENS = ("居家", "家里", "家中")
HOME_ENVIRONMENT_VALUES = ("在家", "在家训练", "家庭", "家庭训练")
OUTDOOR_ENVIRONMENT_TOKENS = ("户外", "室外")


@dataclass(frozen=True)
class EvaluatedConditions:
    # 求值结果：检索过滤条件、显式筛选字段与有效条件说明，供结果再次核验与说明渲染。
    conditions: SearchConditions
    filtered_fields: tuple[str, ...]
    condition_notes: tuple[str, ...]


def _allowed_values(field: str, catalog: Catalog) -> frozenset[str]:
    if field == "equipment":
        return catalog.equipment
    return frozenset(getattr(exercise, field) for exercise in catalog.all())


def _resolve_field(field: str, raw: str, catalog: Catalog, mappings: dict) -> str:
    allowed = _allowed_values(field, catalog)
    if raw in allowed:
        return raw
    hits = rt.query_canonical(mappings, field, raw)
    if not hits:
        raise InvalidSearchCondition(f"{field}: 取值无法映射到动作目录。")
    if len(hits) > 1:
        raise InvalidSearchCondition(f"{field}: 取值存在多个目录映射，无法唯一定位。")
    canonical = next(iter(hits))
    if canonical not in allowed:
        raise InvalidSearchCondition(f"{field}: 取值映射到目录外标识。")
    return canonical


def _resolve_equipment_terms(
    terms: list[str], catalog: Catalog, mappings: dict, field: str
) -> set[str]:
    resolved: set[str] = set()
    for term in terms:
        if term in catalog.equipment:
            resolved.add(term)
            continue
        hits = rt.query_canonical(mappings, "equipment", term)
        if not hits:
            raise InvalidSearchCondition(f"{field}: 器械引用无法映射到动作目录。")
        resolved |= hits
    return resolved


def _require_catalog_ids(ids: list[str], catalog: Catalog) -> list[str]:
    for exercise_id in ids:
        if exercise_id not in catalog.exercise_ids:
            raise InvalidSearchCondition("excluded_ids: 动作 ID 不存在于动作目录。")
    return list(ids)


def _is_home_or_outdoor(environment: str | None) -> bool:
    if not environment:
        return False
    normalized = rt.normalize(environment)
    return normalized in HOME_ENVIRONMENT_VALUES or any(
        token in normalized for token in (*HOME_ENVIRONMENT_TOKENS, *OUTDOOR_ENVIRONMENT_TOKENS)
    )


def evaluate_search_conditions(
    arguments: CatalogSearchArguments,
    profile: ProfileContent | None,
    catalog: Catalog,
    mappings: dict,
) -> EvaluatedConditions:
    filtered: dict[str, str] = {}
    filtered_fields: list[str] = []
    for field in EXACT_FIELDS:
        raw = getattr(arguments, field)
        if raw is None:
            continue
        filtered[field] = _resolve_field(field, raw, catalog, mappings)
        filtered_fields.append(field)

    saved_unavailable = set(profile.unavailable_equipment or []) if profile else set()
    saved_forbidden = list(profile.forbidden_exercise_ids or []) if profile else []

    declared = (
        _resolve_equipment_terms(arguments.available_equipment, catalog, mappings, "available_equipment")
        if arguments.available_equipment is not None
        else set()
    )
    temporary_unavailable = (
        _resolve_equipment_terms(arguments.unavailable_equipment, catalog, mappings, "unavailable_equipment")
        if arguments.unavailable_equipment is not None
        else set()
    )
    temporary_excluded = _require_catalog_ids(list(arguments.excluded_ids or []), catalog)

    notes: list[str] = []
    excluded: list[str] = []

    if arguments.purpose == "lookup":
        # 事实核实允许读取画像受限动作：不施加环境默认与已保存限制。
        # 基础范围取可核实目录范围，只扣除本次明确临时不可用器械；
        # 可用声明是放行声明，不缩成“仅这些器械”，精确器械筛选仍由 equipment 表达。
        available = set(catalog.equipment) - temporary_unavailable
        if temporary_unavailable:
            notes.append("本次不可用器械含" + "/".join(sorted(temporary_unavailable)))
        excluded = sorted(set(temporary_excluded))
        if excluded:
            notes.append(f"本次排除{len(excluded)}个动作")
    else:
        environment = (
            arguments.task_environment
            if arguments.task_environment is not None
            else (profile.environment if profile else None)
        )
        conflict = declared & (saved_unavailable | temporary_unavailable)
        if conflict:
            raise EquipmentRestrictionConflict(
                "available_equipment: 本次可用器械与已保存或临时不可用限制冲突。"
            )
        default = {BODY_WEIGHT} if _is_home_or_outdoor(environment) else set(catalog.equipment)
        removed = saved_unavailable | temporary_unavailable
        available = (default | declared) - removed
        excluded = sorted(set(saved_forbidden) | set(temporary_excluded))
        if environment is not None:
            notes.append(f"环境={environment}")
        if declared:
            notes.append("可用器械含" + "/".join(sorted(declared)))
        if removed:
            notes.append("不可用器械含" + "/".join(sorted(removed)))
        if excluded:
            notes.append(f"排除{len(excluded)}个受限动作")

    conditions = SearchConditions(
        equipment=filtered.get("equipment"),
        body_part=filtered.get("body_part"),
        target=filtered.get("target"),
        muscle_group=filtered.get("muscle_group"),
        available_equipment=tuple(sorted(available)),
        excluded_ids=tuple(excluded),
    )
    return EvaluatedConditions(conditions, tuple(filtered_fields), tuple(notes))
