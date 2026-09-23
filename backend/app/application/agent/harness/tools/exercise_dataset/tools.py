# 动作数据集工具的对外工具面：canonical search_exercises 是八工具之一；数据集库检索与详情读取只作内部
# 能力保留，不进业务 Registry。facet 取值在校验阶段就归一为规范英文：中文或英文都收，越界值走
# args_schema 的参数校验错误路径。canonical 身份与数据集映射的核对由 store 在启动期完成，本文件不解析
# source_ref、不读 JSON、不建第二套映射规则。

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BeforeValidator, Field, model_validator

from app.application.agent.harness.declaration import HarnessContext, HarnessState
from app.application.agent.harness.tools.common import (
    HarnessToolArgs,
    StrictModel,
    TrainingHarnessContext,
    dump_tool_payload,
)
from app.application.agent.harness.tools.exercise_dataset.store import (
    CanonicalExerciseHit,
    ExerciseCatalogUnavailable,
    ExerciseDataset,
)
from app.application.agent.harness.tools.exercise_dataset.vocab import (
    facet_options_zh,
    normalize_facet,
)
from app.domain.actions.schema import LoadConvention, RecordType


@dataclass(frozen=True, slots=True)
class ExerciseDatasetHarnessContext(HarnessContext):
    """动作数据集内部工具的上下文：在通用 Harness 依赖之外注入只读数据集检索端口。"""

    dataset: ExerciseDataset


def _facet_param(field: str) -> Any:
    """facet 参数类型：描述里列出中文可选值，校验期按中英词表折成规范英文，越界即校验错误。"""

    def normalize(value: object) -> object:
        return value if value is None else normalize_facet(field, str(value))

    return Annotated[
        str | None,
        Field(
            default=None,
            description=f"中文或英文。可选：{'、'.join(facet_options_zh(field))}",
        ),
        BeforeValidator(normalize),
    ]


def _facet_description(field: str) -> str:
    return f"中文或英文，可多值。可选：{'、'.join(facet_options_zh(field))}"


def _normalize_facet_tuple(field: str, value: Any) -> Any:
    """facet 元组：每个元素折成规范英文；越界即抛可修正的校验错误。"""
    if value is None:
        return ()
    return tuple(normalize_facet(field, str(item)) for item in value)


class SearchExerciseLibraryArgs(HarnessToolArgs):
    """动作数据集检索参数：文本用英文动作名子串，facet 接受中文或英文取值，命中上限 limit 有界。"""

    query: Annotated[str | None, Field(default=None, min_length=1)] = None
    body_part: _facet_param("body_part") = None
    equipment: _facet_param("equipment") = None
    target: _facet_param("target") = None
    muscle_group: _facet_param("muscle_group") = None
    limit: int = Field(default=8, ge=1, le=25)


class GetExerciseDetailArgs(HarnessToolArgs):
    """动作详情参数：按数据集身份取一行。"""

    exercise_id: str = Field(min_length=1)


@tool(args_schema=SearchExerciseLibraryArgs)
async def search_exercise_library(
    query: str | None,
    body_part: str | None,
    equipment: str | None,
    target: str | None,
    muscle_group: str | None,
    limit: int,
    runtime: ToolRuntime[ExerciseDatasetHarnessContext, HarnessState],
) -> str:
    """在完整动作库中按名称与属性匹配动作，返回候选的身份、名称与中英属性摘要。

    query 用英文动作名做子串匹配（数据集动作名为英文）；facet 参数接受中文或英文取值。
    命中后按 id 调 get_exercise_detail 取动作要领。
    """
    results = await runtime.context.dataset.search(
        text=query,
        body_part=body_part,
        equipment=equipment,
        target=target,
        muscle_group=muscle_group,
        limit=limit,
    )
    return dump_tool_payload([row.search_view() for row in results])


@tool(args_schema=GetExerciseDetailArgs)
async def get_exercise_detail(
    exercise_id: str,
    runtime: ToolRuntime[ExerciseDatasetHarnessContext, HarnessState],
) -> str:
    """按数据集身份读取单个动作的完整详情：属性、二级肌群与中英动作要领（分步）。"""
    row = await runtime.context.dataset.get_detail(exercise_id)
    return dump_tool_payload(None if row is None else row.detail_view())


class SearchExercisesArgs(HarnessToolArgs):
    """canonical 目录检索参数：query 与三个 facet 至少一个非空，facet 中英皆收并归一。"""

    query: str | None = Field(default=None, min_length=1, max_length=100)
    muscle_groups: tuple[str, ...] = Field(
        default=(), max_length=8, description=_facet_description("muscle_group")
    )
    equipment: tuple[str, ...] = Field(
        default=(), max_length=8, description=_facet_description("equipment")
    )
    movement_patterns: tuple[str, ...] = Field(
        default=(), max_length=8, description=_facet_description("movement_pattern")
    )
    limit: int = Field(default=20, ge=1, le=25)

    @model_validator(mode="before")
    @classmethod
    def _normalize_facets(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        normalized = dict(data)
        for field, vocab_field in (
            ("muscle_groups", "muscle_group"),
            ("equipment", "equipment"),
            ("movement_patterns", "movement_pattern"),
        ):
            if field in normalized:
                normalized[field] = _normalize_facet_tuple(
                    vocab_field, normalized[field]
                )
        return normalized

    @model_validator(mode="after")
    def require_filter(self) -> "SearchExercisesArgs":
        if not (
            self.query or self.muscle_groups or self.equipment or self.movement_patterns
        ):
            raise ValueError("至少提供一个动作检索条件")
        return self


class ExerciseInstruction(StrictModel):
    """动作要领的中英双语文段。"""

    zh: str
    en: str


class ExerciseSearchHit(StrictModel):
    """一条 canonical 动作检索结果：canonical 身份与记录口径 ＋ 数据集提供的中英 facet 与指导语。"""

    exercise_id: str
    standard_name_zh: str
    name_en: str
    aliases: tuple[str, ...]
    equipment: str
    equipment_zh: str
    muscle_groups: tuple[str, ...]
    muscle_groups_zh: tuple[str, ...]
    movement_patterns: tuple[str, ...]
    record_type: RecordType
    load_convention: LoadConvention | None
    recommendable: bool
    instructions: ExerciseInstruction | None


class SearchExercisesPayload(StrictModel):
    """检索载荷：稳定排序后的命中与命中数。"""

    exercises: tuple[ExerciseSearchHit, ...]
    returned: int = Field(ge=0)


@tool(args_schema=SearchExercisesArgs)
async def search_exercises(
    query: str | None,
    muscle_groups: tuple[str, ...],
    equipment: tuple[str, ...],
    movement_patterns: tuple[str, ...],
    limit: int,
    runtime: ToolRuntime[TrainingHarnessContext, HarnessState],
) -> str:
    """按 canonical 目录检索动作：query 同时匹配中文标准名、别名与数据集英文名。

    三个 facet 中英皆收：跨 facet 取 AND，同 facet 多值取 OR。命中按 recommendable 降序、
    标准名、``exercise_id`` 稳定排序后取前 limit 条；合法无命中即空 ``exercises``。身份只取
    canonical ``exercises.id``，数据集 id 仅用于内部联表，不出现在结果里。
    """
    dataset = runtime.context.dataset
    if dataset is None:
        raise ExerciseCatalogUnavailable("canonical 动作数据集未注入：无法核对动作目录")
    hits = await dataset.search_canonical(
        query=query,
        muscle_groups=muscle_groups,
        equipment=equipment,
        movement_patterns=movement_patterns,
        limit=limit,
    )
    return dump_tool_payload(
        SearchExercisesPayload(
            exercises=tuple(_hit_view(hit) for hit in hits),
            returned=len(hits),
        )
    )


def _hit_view(hit: CanonicalExerciseHit) -> ExerciseSearchHit:
    """联表行 → 工具输出视图：指导语缺失即 null。"""
    instructions = (
        None
        if hit.instructions_zh is None or hit.instructions_en is None
        else ExerciseInstruction(zh=hit.instructions_zh, en=hit.instructions_en)
    )
    return ExerciseSearchHit(
        exercise_id=hit.exercise_id,
        standard_name_zh=hit.standard_name_zh,
        name_en=hit.name_en,
        aliases=hit.aliases,
        equipment=hit.equipment,
        equipment_zh=hit.equipment_zh,
        muscle_groups=hit.muscle_groups,
        muscle_groups_zh=hit.muscle_groups_zh,
        movement_patterns=hit.movement_patterns,
        record_type=hit.record_type,
        load_convention=hit.load_convention,
        recommendable=hit.recommendable,
        instructions=instructions,
    )
