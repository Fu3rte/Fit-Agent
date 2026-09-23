"""工具结果缓存：规范化键（固定排序紧凑 JSON）、依赖 namespace revision 与有界 LRU。

缓存只保存成功调用的载荷与观察量，不保存 ``ToolMessage`` 对象与 ``tool_call_id``：命中时按当前
``request.tool_call`` 的身份重建消息。批次内的缓存是进程内单实例，revision 负责写入后的逻辑失效。
"""

import json
import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, cast

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from pydantic import ValidationError

from app.application.ports import ToolCacheRevisions

LOGGER = logging.getLogger(__name__)

#: 进程内 LRU 容量上限：条目只放成功调用的短输出，固定容量避免无界增长。
DEFAULT_CACHE_CAPACITY = 128


@dataclass(frozen=True, slots=True)
class ToolCacheDimensions:
    """一个工具结果的失效维度：依赖的 namespace revision、business_day 与 schema_version。"""

    namespaces: tuple[str, ...]
    business_day: bool
    schema_version: bool


#: 首批只读工具的键维度：只有输出直接携带或用 business_day 现算的工具才加该维度；
#: 目录（exercises）内容变化必然伴随一条新迁移，由 schema_version 承担失效。
TOOL_CACHE_DIMENSIONS: Mapping[str, ToolCacheDimensions] = {
    "read_active_plan": ToolCacheDimensions(
        namespaces=("plans",), business_day=True, schema_version=True
    ),
    "read_training_calendar": ToolCacheDimensions(
        namespaces=("plans", "workouts"), business_day=False, schema_version=False
    ),
    "read_training_history": ToolCacheDimensions(
        namespaces=("workouts",), business_day=False, schema_version=False
    ),
    "read_progress": ToolCacheDimensions(
        namespaces=("workouts", "metrics"), business_day=True, schema_version=True
    ),
    "search_exercises": ToolCacheDimensions(
        namespaces=(), business_day=False, schema_version=True
    ),
}


@dataclass(frozen=True, slots=True)
class CachedToolResult:
    """一条缓存载荷：成功调用的定界内容、artifact、status 与两个观察量。"""

    content: str
    artifact: Any
    status: str
    truncated: bool
    result_bytes: int

    def to_message(self, request: ToolCallRequest) -> ToolMessage:
        """按当前工具调用的 ``id``／``name`` 重建 ToolMessage：旧 call id 不进新对话。"""
        tool_call = request.tool_call
        return ToolMessage(
            content=self.content,
            artifact=self.artifact,
            status=self.status,
            tool_call_id=tool_call["id"],
            name=tool_call["name"],
        )


def _validated_args(request: ToolCallRequest) -> dict[str, Any] | None:
    """用工具对模型公开的参数 Schema 校验后的参数载荷；未注册工具或校验失败即 None。

    ``tool_call_schema`` 是模型可见参数的权威 Schema：Schema 默认值由此补齐，类型归一由 pydantic
    完成（``"4"`` 与 ``4`` 同键），注入参数不进该 Schema。
    该 Schema 由 langchain-core 的子集模型派生，不继承 ``args_schema`` 的 ``extra="forbid"``，多出的键
    会被静默丢弃：因此这里显式拒绝未声明键，让该次调用交给 ToolNode 用 ``args_schema`` 报参数错误。
    """
    tool = request.tool
    if tool is None:
        return None
    args = request.tool_call["args"]
    try:
        validated = tool.tool_call_schema.model_validate(args)
    except ValidationError:
        return None
    if not set(args) <= set(validated.__class__.model_fields):
        return None
    return validated.model_dump(mode="json")


class HasBusinessDay(Protocol):
    """缓存键需要的运行时上下文：业务工具上下文提供 ``business_day``。"""

    business_day: date


class ToolResultCache:
    """有界 LRU ＋ 键构造：键由工具名、规范化参数、依赖 revisions、业务日与 schema_version 组成。"""

    def __init__(
        self,
        *,
        revisions: ToolCacheRevisions,
        schema_version: int,
        capacity: int = DEFAULT_CACHE_CAPACITY,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"缓存容量必须为正数：{capacity!r}")
        self._revisions = revisions
        self._schema_version = schema_version
        self._capacity = capacity
        self._entries: OrderedDict[str, CachedToolResult] = OrderedDict()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        return len(self._entries)

    async def key_for(self, request: ToolCallRequest) -> str | None:
        """该次调用的缓存键；工具不在缓存维度表内或参数未过工具 Schema 校验即 None。

        写工具、未登记工具与非法参数一律不缓存：非法参数的报错语义仍由 ToolNode 承担。
        """
        dimensions = TOOL_CACHE_DIMENSIONS.get(request.tool_call["name"])
        if dimensions is None:
            return None
        args = _validated_args(request)
        if args is None:
            return None
        revisions = await self._revisions.read_all()
        missing = [
            namespace for namespace in dimensions.namespaces if namespace not in revisions
        ]
        if missing:
            raise RuntimeError(f"缓存依赖的 namespace revision 缺失：{missing}")
        business_day: str | None = None
        if dimensions.business_day:
            business_day = cast(HasBusinessDay, request.runtime.context).business_day.isoformat()
        payload = {
            "tool": request.tool_call["name"],
            "args": args,
            "business_day": business_day,
            "revisions": {
                namespace: revisions[namespace] for namespace in dimensions.namespaces
            },
            "schema_version": self._schema_version if dimensions.schema_version else None,
        }
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    def get(self, key: str) -> CachedToolResult | None:
        """命中即返回载荷并刷新 LRU 顺序；未命中即 None。"""
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: str, result: CachedToolResult) -> None:
        """写入载荷；超出容量即淘汰最久未使用的一条。"""
        self._entries[key] = result
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
