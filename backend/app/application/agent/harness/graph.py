import asyncio
from collections.abc import Sequence

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.runtime import Runtime

from app.application.agent.harness.cache import ToolResultCache
from app.application.agent.harness.declaration import HarnessContext, HarnessState
from app.application.agent.harness.policy import build_tool_call_wrapper
from app.application.ports import InvalidModelResponse


def build_tool_harness(
    tools: Sequence[BaseTool],
    *,
    timeout_seconds: float,
    cache: ToolResultCache | None = None,
    tools_node_name: str = "tools",
) -> CompiledStateGraph:
    """构建 model → tools → model 的 Harness agent loop。

    工具固化为一个 tuple，model node 与 ToolNode 共用。循环由 LangGraph 表达，node 内无 while；
    预算与超时是权威终止边界，耗尽或异常一律向上抛出交给外层 Run 错误处理；不配置 checkpointer。
    ``cache`` 由装配方一次性创建并注入；为 None 时工具调用不查缓存、不写缓存。
    ``tools_node_name`` 由装配方给出，同一 Run 内多个 ToolNode 各自独立命名。
    """
    offered = tuple(tools)

    async def model(
        state: HarnessState,
        runtime: Runtime[HarnessContext],
    ) -> dict[str, list[AIMessage]]:
        context = runtime.context
        timeout = context.budget.begin_request()
        async with asyncio.timeout(timeout):
            response = await context.model.tools(state["messages"], offered)
        if not isinstance(response, AIMessage):
            raise InvalidModelResponse(f"Harness 模型响应不是 AIMessage：{type(response).__name__}")
        return {"messages": [response]}

    graph = StateGraph(HarnessState, context_schema=HarnessContext)
    graph.add_node("model", model)
    graph.add_node(
        tools_node_name,
        ToolNode(
            offered,
            awrap_tool_call=build_tool_call_wrapper(
                timeout_seconds=timeout_seconds,
                cache=cache,
            ),
        ),
    )
    graph.add_edge(START, "model")
    graph.add_conditional_edges(
        "model", tools_condition, {"tools": tools_node_name, END: END}
    )
    graph.add_edge(tools_node_name, "model")
    return graph.compile()
