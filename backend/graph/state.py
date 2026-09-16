"""Graph 工作记忆 State：一次 LangGraph Run 的工作事实（讨论总结 §5.1／§4.1／§4.2、REFACTOR_PLAN
§8.1／§9.1／§9.2、stage3.md §3）。

本模块只冻结 State 字段与词汇表：节点、Checkpointer、MemoryAssembler 与 Skill Loader 由 Stage 3
其余子任务实现，Planner／Evaluator 与确认事务留 Stage 4／Stage 5。因此计划内容与评估结果在这里
只作为不透明载荷引用，不伪装成已定契约。

四条硬边界（REFACTOR_PLAN §8.1、stage3.md §3）：

- **单一身份**：``conversation_id`` 就是 Checkpointer 的 ``thread_id``，不另建线程映射或别名。
- **不复制业务库**：不装载完整训练历史，也不缓存 PB／趋势统计结果；装配结果只以 ``context`` 引用
  MemoryAssembler 的一次输出，业务事实每次从 SQLite 重读（讨论总结 §5.2）。
- **不携带机密**：不保存 API Key、Provider 配置或模型端点；密钥只从环境变量读取（讨论总结 §10）。
- **不预建运行状态机**：词汇表只转录讨论总结已冻结的路由分支与终止分支，不新增状态。

``total=False``：节点只回写自己改变的键，未回写的键保持缺省，Checkpointer 恢复时不要求全量字段。
"""

from typing import Any, Literal, TypedDict

#: Router 的五类已判定 intent（讨论总结 §4.1 路由表、REFACTOR_PLAN §9.1）；顺序即路由表顺序。
Intent = Literal[
    "form_record",
    "natural_language_record",
    "view_progress",
    "generate_plan",
    "adjust_plan",
]
INTENTS: tuple[Intent, ...] = (
    "form_record",
    "natural_language_record",
    "view_progress",
    "generate_plan",
    "adjust_plan",
)

#: 确认状态（讨论总结 §4.2：``wait_for_confirmation`` 等待中／用户确认后激活／用户拒绝后归档 draft）。
ConfirmationStatus = Literal["pending", "confirmed", "rejected"]

#: 终止原因（讨论总结 §4.2 的三个终止分支：急性关键词安全停止、用户拒绝归档 draft、二次评估失败）。
TerminationReason = Literal["safety_stop", "archive_draft", "reject_draft"]


class WorkflowState(TypedDict, total=False):
    """一次 Graph Run 的工作记忆（REFACTOR_PLAN §8.1 的九类内容）。"""

    #: 会话身份，直接用作 Checkpointer 的 ``thread_id``（REFACTOR_PLAN §5.6）。
    conversation_id: str
    request: str
    intent: Intent | None
    #: MemoryAssembler 的一次装配输出（只含装配范围，不复制业务库）；形状由子任务 02 定义。
    context: Any
    #: 命中后加载的 Skill 正文与其引用文件；形状由子任务 04 定义。
    loaded_skill: Any
    #: 待确认计划在 ``plans`` 中的身份：确认路径按此读取，不靠「取最新 draft」猜测。
    draft_plan_id: int | None
    #: 计划草稿内容；统一计划 Schema 由 Stage 4 定义，此处不做形状假设。
    draft_plan: Any
    #: Evaluator 结果；契约由 Stage 4 定义，此处不做形状假设。
    evaluation: Any
    revision_count: int
    confirmation: ConfirmationStatus | None
    termination_reason: TerminationReason | None
