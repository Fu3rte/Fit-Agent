"""Stage 4 运行时对外错误码封闭集合（S4-01 契约冻结；08 8.1、8.2、8.5、8.7）。

本模块是**契约**，不是调度实现：列出允许出现在传输面与 Run 终态的机器可读错误码，
供前端做封闭联合、供 S4-05/S4-07 复用。新增码必须同时改本模块与契约文档，
不允许在路由或调度代码里临时拼字符串。

两类码分开表达，不混成一套：

- :data:`CONVERSATION_BUSY`：**HTTP** 错误（409）。请求未被接受，不创建 Run 或消息，
  由 :class:`storage.errors.ConversationBusy` 从创建事务内抛出（08 8.2）。
- 其余四个是 **Run 终态失败原因**：随 Run 查询结果与状态事件给出可理解原因，
  不是 HTTP 错误码，也不替代 Run 的五态权威状态（08 8.1/8.4/8.5/8.8）。
"""

#: 已有活跃 Run 时拒绝不同请求（08 8.2；HTTP 409，映射登记在 api.dto）。
CONVERSATION_BUSY = "conversation_busy"

#: 服务重启后遗留 pending/running 统一标 failed 的原因（08 8.4）。
INTERRUPTED_BY_RESTART = "interrupted_by_restart"

#: 单次模型请求超时（08「首事件与流空闲超时」；重试用尽/时间不足同样以此结束）。
MODEL_REQUEST_TIMEOUT = "model_request_timeout"

#: Run 总时限到期（同上）。
RUN_TIMEOUT = "run_timeout"

#: 上下文预算超限、无法安全压缩后结束（08「容量、估算与溢出」）。
CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"

#: Run 终态失败原因全集；写入 ``runs.error_code`` 的值只允许来自这里。
RUN_FAILURE_CODES: tuple[str, ...] = (
  INTERRUPTED_BY_RESTART,
  MODEL_REQUEST_TIMEOUT,
  RUN_TIMEOUT,
  CONTEXT_BUDGET_EXCEEDED,
)

#: 普通执行失败可用的原因（即除重启中断外的全部）：``interrupted_by_restart`` 只由
#: 启动恢复写给遗留 Run，普通 running 失败不得使用它（08 8.1/8.4 分开表达）。
ORDINARY_FAILURE_CODES: tuple[str, ...] = tuple(
  code for code in RUN_FAILURE_CODES if code != INTERRUPTED_BY_RESTART
)

#: 对外错误码封闭全集（HTTP 错误码 + Run 终态失败原因），前端据此做穷尽联合。
RUNTIME_ERROR_CODES: frozenset[str] = frozenset((CONVERSATION_BUSY, *RUN_FAILURE_CODES))


def is_runtime_error_code(code: object) -> bool:
  """该值是否在本轮冻结的封闭错误码集合内。"""
  return isinstance(code, str) and code in RUNTIME_ERROR_CODES
