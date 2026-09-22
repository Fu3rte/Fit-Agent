# 动作数据集工具（阶段 2，独立于正在重构的节点）：把已入库的 exercises.zh-en.json 包装成两个
# 只读工具，供模型匹配动作与查询动作。命中返回数据集行身份与中英 facet，详情返回中英指导语与分步。
# 写库用的稳定 exercise_id 仍来自 curated 动作目录；本工具给出的 id 是数据集参考身份，不参与写入。
# facet 取值在校验阶段就归一为规范英文：中文或英文都收，越界值走 args_schema 的参数校验错误路径。

from dataclasses import dataclass
from typing import Annotated

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BeforeValidator, Field

from app.application.agent.harness.declaration import HarnessContext, HarnessState
from app.application.agent.harness.tools.training import (
    HarnessToolArgs,
    dump_tool_payload,
)
from app.application.ports import ExerciseDataset
from app.domain.actions.dataset import facet_options_zh, normalize_facet


@dataclass(frozen=True, slots=True)
class ExerciseDatasetHarnessContext(HarnessContext):
    """动作数据集工具上下文：在通用 Harness 依赖之外注入只读数据集检索端口。"""

    dataset: ExerciseDataset


def _facet(field: str) -> BeforeValidator:
    """facet 参数在校验期归一：留空即 None，取值按中英词表折成数据集规范英文，越界即校验错误。"""

    def normalize(value: object) -> object:
        return value if value is None else normalize_facet(field, str(value))

    return BeforeValidator(normalize)


def _facet_field(field: str) -> Field:
    return Field(
        default=None,
        description=f"中文或英文。可选：{'、'.join(facet_options_zh(field))}",
    )


class SearchExerciseLibraryArgs(HarnessToolArgs):
    """动作检索参数：文本用英文动作名子串，facet 接受中文或英文取值，命中上限 limit 有界。"""

    query: Annotated[str | None, Field(default=None, min_length=1)] = None
    body_part: Annotated[str | None, _facet_field("body_part"), _facet("body_part")] = None
    equipment: Annotated[str | None, _facet_field("equipment"), _facet("equipment")] = None
    target: Annotated[str | None, _facet_field("target"), _facet("target")] = None
    muscle_group: Annotated[
        str | None, _facet_field("muscle_group"), _facet("muscle_group")
    ] = None
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


EXERCISE_DATASET_TOOLS = (
    search_exercise_library,
    get_exercise_detail,
)
