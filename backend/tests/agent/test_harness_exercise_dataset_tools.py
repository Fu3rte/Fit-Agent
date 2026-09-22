# 动作数据集工具的 Harness 契约：两个只读工具经真实 ToolNode 与注入上下文运行，输出是标准 JSON
# 文本，未声明参数被 args_schema 拒绝，越界 facet 走可修正的参数错误路径，未知身份返回 null，
# limit 上下界由 Schema 表达。事实用真实的内存数据集（已入库语料），模型与预算只用不可调用的替身
# ——工具只读数据集，绝不触达模型入口、也不扣工具预算（不经 policy wrapper）。

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from app.application.agent.harness.declaration import HarnessState
from app.application.agent.harness.tools.exercise_dataset import (
    EXERCISE_DATASET_TOOLS,
    ExerciseDatasetHarnessContext,
)
from app.application.ports import ModelGateway
from app.infrastructure.datasets.exercise_dataset import InMemoryExerciseDataset

KNOWN_ID = "0001"
UNKNOWN_ID = "999999"
CHEST_ZH = "胸部"
BAD_FACET = "不存在的部位"
EXTRA_ARGUMENT = "limit"

SEARCH_VIEW_KEYS = {
    "id",
    "name",
    "body_part",
    "body_part_zh",
    "equipment",
    "equipment_zh",
    "target",
    "target_zh",
    "muscle_group",
    "muscle_group_zh",
}


class _UnusedBudget:
    """替身预算：只读数据集工具不经 policy wrapper，被调用即失败。"""

    def begin_request(self) -> float:
        raise AssertionError("只读数据集工具不发起模型请求")

    def take_tool_call(self) -> None:
        raise AssertionError("只读数据集工具不扣工具预算")


async def _unavailable_model(*_args: Any) -> Any:
    raise AssertionError("只读数据集工具不触达模型入口")


_MODEL = ModelGateway(
    text=_unavailable_model, structured=_unavailable_model, tools=_unavailable_model
)


class _Harness:
    def __init__(self, graph: CompiledStateGraph, context: ExerciseDatasetHarnessContext) -> None:
        self._graph = graph
        self._context = context

    async def call(self, name: str, args: Mapping[str, Any] | None = None) -> ToolMessage:
        state = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": dict(args or {}),
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        }
        result = await self._graph.ainvoke(state, context=self._context)
        message = result["messages"][-1]
        assert isinstance(message, ToolMessage)
        return message

    async def payload(self, name: str, args: Mapping[str, Any] | None = None) -> Any:
        message = await self.call(name, args)
        assert message.status == "success", message.content
        return json.loads(message.content)


@asynccontextmanager
async def _harness(tmp_path: Path) -> AsyncIterator[_Harness]:
    # 数据集语料与 tmp_path 无关，这里只用它保证测试隔离命名。
    context = ExerciseDatasetHarnessContext(
        model=_MODEL,
        budget=_UnusedBudget(),
        dataset=InMemoryExerciseDataset(),
    )
    graph = StateGraph(HarnessState, context_schema=ExerciseDatasetHarnessContext)
    graph.add_node("tools", ToolNode(list(EXERCISE_DATASET_TOOLS)))
    graph.add_edge(START, "tools")
    yield _Harness(graph=graph.compile(), context=context)


def _leaf_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _leaf_values(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _leaf_values(item)]
    return [value]


def test_dataset_tool_schemas_forbid_extra_fields_and_hide_runtime() -> None:
    """两个工具的 args_schema 都 additionalProperties: false；注入的 runtime 不进模型可见 Schema。"""
    assert [tool.name for tool in EXERCISE_DATASET_TOOLS] == [
        "search_exercise_library",
        "get_exercise_detail",
    ]
    visible: dict[str, set[str]] = {}
    for tool in EXERCISE_DATASET_TOOLS:
        assert tool.args_schema.model_json_schema()["additionalProperties"] is False
        properties = tool.tool_call_schema.model_json_schema()["properties"]
        assert "runtime" not in properties
        visible[tool.name] = set(properties)
    assert visible == {
        "search_exercise_library": {
            "query",
            "body_part",
            "equipment",
            "target",
            "muscle_group",
            "limit",
        },
        "get_exercise_detail": {"exercise_id"},
    }


async def test_search_returns_standard_json_rows_with_bilingual_facets(
    tmp_path: Path,
) -> None:
    """检索输出是标准 JSON 文本，每行给出数据集身份、英文名与中英 facet 标签。"""
    async with _harness(tmp_path) as h:
        message = await h.call("search_exercise_library", {"query": "squat", "limit": 3})
        assert message.status == "success", message.content
        assert json.dumps(json.loads(message.content), ensure_ascii=False) == message.content

        rows = json.loads(message.content)
        assert len(rows) == 3
        assert all(set(row) == SEARCH_VIEW_KEYS for row in rows)
        assert all("squat" in row["name"].lower() for row in rows)
        assert {type(leaf) for leaf in _leaf_values(rows)} <= {str, int}


async def test_search_facet_filter_accepts_chinese_value(tmp_path: Path) -> None:
    """中文 facet 取值归一后过滤：命中行的该 facet 全部等于目标。"""
    async with _harness(tmp_path) as h:
        rows = await h.payload("search_exercise_library", {"body_part": CHEST_ZH, "limit": 5})
        assert rows and all(row["body_part"] == "chest" for row in rows)
        assert all(row["body_part_zh"] == CHEST_ZH for row in rows)


async def test_search_with_no_match_returns_empty_list(tmp_path: Path) -> None:
    """无解组合返回空列表，不编造候选。"""
    async with _harness(tmp_path) as h:
        rows = await h.payload(
            "search_exercise_library", {"query": "squat", "body_part": "chest"}
        )
        assert rows == []


async def test_detail_returns_full_bilingual_instruction(tmp_path: Path) -> None:
    """详情按身份给出双语指导语与分步。"""
    async with _harness(tmp_path) as h:
        detail = await h.payload("get_exercise_detail", {"exercise_id": KNOWN_ID})
        assert detail["id"] == KNOWN_ID
        assert set(detail) > SEARCH_VIEW_KEYS
        assert detail["instructions"]["zh"] and detail["instructions"]["en"]
        assert detail["steps"]["zh"] and detail["steps"]["en"]


async def test_detail_for_unknown_id_returns_null(tmp_path: Path) -> None:
    """未知身份返回 null，不抛异常。"""
    async with _harness(tmp_path) as h:
        assert await h.payload("get_exercise_detail", {"exercise_id": UNKNOWN_ID}) is None


async def test_search_rejects_undeclared_argument(tmp_path: Path) -> None:
    """未声明字段被拒绝：进入可修正的参数校验错误。"""
    async with _harness(tmp_path) as h:
        message = await h.call("search_exercise_library", {"query": "squat", "offset": 1})
        assert message.status == "error"
        assert "offset" in message.content
        assert "Extra inputs are not permitted" in message.content


async def test_search_rejects_out_of_range_limit(tmp_path: Path) -> None:
    """limit 越界（0 与 26）由 Schema 边界拦下，端点 1／25 合法。"""
    async with _harness(tmp_path) as h:
        assert isinstance(await h.payload("search_exercise_library", {"limit": 1}), list)
        assert isinstance(await h.payload("search_exercise_library", {"limit": 25}), list)
        for limit in (0, 26):
            message = await h.call("search_exercise_library", {"limit": limit})
            assert message.status == "error", message.content
            assert "limit" in message.content


@pytest.mark.parametrize("facet", ["body_part", "equipment", "target", "muscle_group"])
async def test_search_rejects_value_outside_facet_vocabulary(
    tmp_path: Path, facet: str
) -> None:
    """词表外的 facet 取值作为可修正错误返回，指明字段并列出中文可选值。"""
    async with _harness(tmp_path) as h:
        message = await h.call("search_exercise_library", {facet: BAD_FACET})
        assert message.status == "error", message.content
        assert facet in message.content
        assert "可选" in message.content or "不在词表内" in message.content
