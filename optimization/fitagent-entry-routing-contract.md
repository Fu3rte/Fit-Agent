# 入口路由数据结构契约

## 1. 输入 state

- `current_input`：当前用户节点、原文和附件元信息。
- `conversation_context`：当前有效分支中理解指代所需的对话序列。
- `unfinished_tasks`：仍需处理的请求序列，每项保留来源节点。
- `pending_proposals`：当前有效待确认提案序列，包含真实身份、状态、准备类型、关联节点及必要摘要。
- `business_summary`：当前画像、当前计划等必要业务事实摘要。

约束：

- 身份与节点 ID 使用现有标准 UUID 约束。
- 用户原文允许为空，以表达纯附件输入。
- 附件元信息复用现有附件类型，内容保持数据身份。
- 提案引用复用现有业务类型、状态与展示绑定约束。
- 计划准备类型为 `generation`、`import`、`adjustment`。
- 对话、待办与摘要保留真实来源。

## 2. 固定题库 questions

题库包含：

- 业务标签及其判断说明。
- 交互类型及其判断说明。
- 复合动作的拆分、顺序和歧义规则。

题库版本由后端记录。

## 3. 输出 actions

```json
{
  "actions": [
    {"tasks": ["plan_generation"], "interaction": "cancel_pending"},
    {"tasks": ["workout"], "interaction": "new_request"}
  ]
}
```

字段：

- `actions`：动作对象列表。
- `actions[].tasks`：业务标签列表。
- `actions[].interaction`：交互类型。

### tasks 枚举

- `profile`：画像。
- `plan_import`：录入已有计划。
- `plan_generation`：生成计划及对应待确认提案。
- `plan_adjustment`：调整计划。
- `workout`：实际训练记录。
- `query`：查询事实或操作状态。
- `other`：其他对话。

### interaction 枚举

- `new_request`：新业务请求。
- `revise_pending`：修改待确认提案。
- `cancel_pending`：取消待确认提案。
- `clarify`：任务或目标存在歧义。

### 校验约束

- 顶层仅包含 `actions`，每项仅包含 `tasks` 与 `interaction`。
- 三个字段均为必填；`actions` 和每项 `tasks` 非空。
- 严格校验字段类型，拒绝额外字段和非法枚举。
- 同一动作的 `tasks` 禁止重复，`other` 必须独立使用。
- 不同动作允许包含相同任务。
- 动作顺序保留用户请求中的依赖。

## 4. 提案取消工具

参数类型复用 `PendingProposalArguments`：

- `business_kind`：`profile`、`plan` 或 `workout`。
- `proposal_id`：标准 UUID 字符串。

返回类型 `PendingProposalCancellation`：

- `business_kind`：原业务类型。
- `proposal_id`：原提案 UUID。
- `status`：固定为 `invalidated`。

参数与返回值严格校验，拒绝额外字段。

会话、运行、请求节点与来源节点由后端绑定。
