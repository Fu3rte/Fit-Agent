# Fit-Agent 业务上下文消息契约

设计见 [Prompt Cache 优化方案](prompt-cache-strategy.md)。

## 1. 内部节点

- 资料节点 `type="custom_message"`，独立参与会话树，仅由后端创建。
- 普通发送、编辑、附件和 Steering 输入不能创建内部资料类型。
- 节点内容、来源及发生位置不可变；真实业务工具结果继续使用 `toolResult`，保持真实调用配对。
- 内部资料不构成保存授权，不作为用户聊天条目展示；模型转换在原位置输出 user-role 内容。

类型定义位于 `backend/app/domain/session/models.py`：

```python
TData = TypeVar("TData", bound=Model)

class CustomMessage(Model, Generic[TData]):
    role: Literal["custom"]
    custom_type: str
    content: str | list[UserContent]
    display: bool
    details: TData | None = None
    timestamp: int

class BusinessContextMessage(CustomMessage[BusinessContextData]):
    custom_type: Literal["business_context"]
    content: str = Field(min_length=1)
    display: Literal[False]
    details: BusinessContextData = Field(...)

TMessage = TypeVar("TMessage", bound=CustomMessage)

class CustomMessageEntry(Model, Generic[TMessage]):
    session_id: str
    id: str
    parent_id: str | None
    run_id: str | None
    type: Literal["custom_message"]
    message: TMessage
    created_at: int

class BusinessContextEntry(CustomMessageEntry[BusinessContextMessage]):
    run_id: str
```

`SessionEntry` 登记 `BusinessContextEntry`，`AgentMessage` 登记 `BusinessContextMessage`。服务商协议 `Message` 联合保持原样，内部消息经 `convert_to_llm` 转换。

业务节点 `run_id` 非空且关联当前真实运行。`message.timestamp` 等于 `created_at`，均为 UTC 毫秒整数；`parent_id` 表达发生位置。

工具执行身份继续使用后端绑定的 `BusinessContext`，包含 `session_id/run_id/request_entry_id/source_entry_id`；模型资料使用独立的 `BusinessContextData`。Pydantic 校验采用 `strict=True` 和 `extra="forbid"`，未知资料类型直接失败。

## 2. 完整基线与增量

`BusinessContextData` 为按 `mode` 判别的 `BusinessContextSnapshot | BusinessContextDelta` 联合。

`BusinessContextSnapshot` 的必填字段：

- `mode: Literal["snapshot"]`。
- `request: RequestContext`。
- `message_nodes: list[MessageNodeReference]`，模型可见的 user、assistant、toolResult 来源节点，允许 `[]`。
- `pending_proposals: list[PendingProposalReference]`，当前有效待确认提案，允许 `[]`。

`BusinessContextDelta` 的必填字段：

- `mode: Literal["delta"]`。
- `request: RequestContext | null`，`null` 表示保持原值，对象表示整份替换。
- `upsert_message_nodes: list[MessageNodeReference]`，新增或变化节点的完整记录。
- `remove_message_node_ids: list[OperationId]`，移出模型可见集合的节点 ID。
- `upsert_pending_proposals: list[PendingProposalReference]`，新增或变化提案的完整引用。
- `remove_pending_proposals: list[RemovedPendingProposal]`，移出提案及原因。

增量列表允许 `[]`；所有列表为空且 `request=null` 时拒绝入库。列表拒绝重复键，同一键同时新增和移除时校验失败。

节点按 `entry_id`、提案按 `(business_kind, proposal_id)` 合并。首次资料事件生成完整基线；后续仅在变化时追加增量。分支状态从最近完整基线开始依次重放。

## 3. 请求与来源引用

### 3.1 RequestContext

全部字段必填：

- `timezone: Literal["Asia/Shanghai"]`。
- `business_date: YYYY-MM-DD`，按真实用户节点 `created_at` 换算并校验真实日期。
- `request_entry_id: OperationId`，当前真实用户节点，Steering 消费后更新。

节点和提案 ID 复用现有标准 UUID 校验。后端核验节点角色、当前路径及真实关联，重放保留发生时的日期。

### 3.2 MessageNodeReference

按 `role` 判别，全部记录必填 `entry_id`：

- `UserNodeReference`：`role="user"`，必填 `confirmation: SaveConfirmation | null`、`proposal: ProposalKey | null`。
- `AssistantNodeReference`：`role="assistant"`，必填 `tool_call_ids: list[str]`，允许 `[]`。
- `ToolResultNodeReference`：`role="toolResult"`，必填 `tool_name: str`、`tool_call_id: str`、`proposal: ProposalKey | null`。

`ProposalKey` 必填 `business_kind: profile/plan/workout` 和 `proposal_id: OperationId`。

`confirmation` 复用后端确认绑定；`proposal` 取已保存的确认或展示关联。工具结果的展示节点为该记录的 `entry_id`，调用 ID 保持真实配对。资料事件自身不进入 `message_nodes`。

### 3.3 PendingProposalReference

全部字段必填：

- `business_kind: profile/plan/workout`。
- `proposal_id: OperationId`。
- `status: pending/processing`。
- `request_entry_id: OperationId`，准备时的真实用户节点。
- `source_entry_id: OperationId`，发起准备调用的 assistant 节点。
- `display_entry_id: OperationId | null`，尚未绑定展示时为 `null`。
- `confirmation_entry_id: OperationId | null`，后端确认操作绑定。

`processing` 要求展示及确认节点均已绑定；确认节点有值时展示节点必须有值。

消息来源采用模型可见集合，提案引用按完整当前分支核验；被压缩来源保留受控读取关联，完整提案通过真实业务工具读取。

### 3.4 RemovedPendingProposal

必填 `business_kind`、`proposal_id` 和 `reason`：

- `saved/invalidated/conflicted`：后端核实的真实状态变化。
- `deleted`：快照缺失且无保存记录。
- `out_of_scope`：pending/processing 提案离开当前有效集合。

`BusinessService.removed_proposal_reason()` 只读核验原因。移除仅改变事件之后的有效集合，历史资料保持完整。

## 4. 采集、持久化与模型转换

- `application/session/business_context.py` 负责采集、分支状态重放和增量生成；无变化不追加。
- `details` 为结构化依据；`build_business_context_message()` 使用固定 JSON 序列化生成 `content`，包含后端资料来源和无保存授权标记。
- 创建入口及写入服务校验投影一致性，`details/content` 同事务落库。历史重放直接读取固定 `content`，不依赖当前模板重新生成。
- 下一次模型请求前追加变化资料，提交后同步执行位置。
- 准备调用的全部结果和展示绑定完成后追加该批资料；保持工具调用与全部结果连续配对，完整工具批次提交及通知后才消费 Steering。
- Steering 消费后更新对应真实用户节点、请求资料及业务日期。
- `business_context_to_llm()` 和 `convert_to_llm()` 保留固定 `content/timestamp`，协议角色为 user；`custom_type/display/details` 不作为额外协议字段发送。
- `build_effective_context()` 按原位置投影并映射真实资料节点 ID；`transform_context()` 从持久化分支重放，每条资料仅出现一次。
- 业务资料不参与动态 system 注入，不作为真实用户回合或压缩切点。
- `read_request_state()` 共用只读采集；`request_messages()` 与 `estimate_request()` 均计入本次待追加资料，文本创建及预算共用 `build_business_context_message()`。

## 5. 压缩与历史兼容

- 摘要输入和保留预算包含资料固定 `content`。
- `commit_compaction()` 同事务提交检查点和完整 Snapshot 基线，运行末节点及会话叶指向基线；失败回滚，同身份提交幂等，凭据与授权继续校验。
- 新基线按模型可见集合重建消息来源，待确认提案按完整分支核验；连续压缩及压缩后的独立按钮确认保持有效。
- 旧会话不回填过去的临时资料。旧节点 ID、父子关系、业务数据、运行及确认绑定保持完整。
- 数据库通过新增迁移支持内部节点，保留已执行的旧迁移、外键、不可变约束及数据完整性。

## 6. 公开历史与前端

公开历史中的 `custom_message` 仅包含：

- `type: Literal["custom_message"]`。
- `entry_id: string`，当前节点身份。
- `parent_id: string | null`，父节点身份。
- `run_id: string | null`，通用节点可空，业务资料节点必须非空。
- `created_at: integer`，UTC 毫秒时间。

完整业务资料、模型文本和内部附加内容保持私有，公开投影使用白名单，Python 与 TypeScript 字段及校验一致。

前端使用 `ReActRound.hidden` 保留隐藏结构节点、祖先链及运行归属，不生成可见用户聊天条目。`active_leaf_id` 对应路径保持完整；按 `entry_id` 合并历史和流式身份，相同身份字段冲突按协议错误处理。断线及刷新沿用运行查询和历史恢复。

## 7. SSE 通知

现有运行事件流发送 `custom_message`，`data` 使用第 6 节五字段白名单；ID 使用 `OperationId` 校验，`run_id` 必填、非空且匹配当前流。

通知顺序：

- 资料成功落库后通知，先于后续模型消息。
- 父节点身份先于资料节点身份，已公开身份继续复用。
- 完整批次的 `tool_result` 提交及通知后，发送该批 `custom_message`。
- `steering_status(status=consumed)` 后，发送消费后的资料通知。
- `compaction_end` 后，发送新基线的资料通知。

后续助手节点以资料节点为父节点。完整资料不进入 SSE，前端事件定义、解析及隐藏节点处理与历史白名单一致。

## 8. 缓存观测

`app/agent/cache_observation.py` 统一负责最终投影指纹、差异定位、真实调用记录、跨运行及重启比较、读取汇总和 CLI。

- 调用关联 `session_id/run_id`；普通调用绑定真实 `message_id`，历史摘要和前缀摘要绑定 `compaction_id`。
- 记录请求时间、模型及 endpoint、system 和工具声明 hash、历史首个差异、已知上下文事件及真实四类 token 用量。
- 按真实调用身份去重，命中率为 `cache_read / (input + cache_read + cache_write)`；零分母为 `null`。
- `_usage_totals()` 和 `_bucket()` 分别按 `first/append/summary/reset` 输出调用数、有效及缺失用量数、`input/cache_read/cache_write/output` 和命中率。
- `_observed()` 通过 `aclosing(source)` 关闭底层流，包装流关闭返回前完成底层收尾，观测结局只写一次。
- 生产产物写入 `backend/data/cache-observation/`，验证产物写入 `backend/temp/` 下已忽略的运行目录；禁止记录凭据。
- 读取入口为 `python -m app.agent.cache_observation [--directory D] [--session S]`。
- 观测保持被动和故障隔离，不改变业务结局，不扩展公开历史、SSE 白名单或前端配置。

## 9. 安全与一致性

- 日期和执行身份由后端绑定，独立核验来源、当前路径、展示节点、提案状态及业务限制。
- 模型资料仅提供读取线索；确认保存由独立后端接口执行，保持授权、幂等和未授权零提交。
- 资料内容固定重放，状态变化通过新事件表达，内部内容不得通过公开序列化或展示泄露。
- 预算使用共享 `RequestBudget`，存储检查跟随 `SCHEMA_VERSION`，不得硬编码旧版本。
- 一致性验证覆盖持久化及迁移、分支重放、预算和最终投影、完整工具批次、Steering、压缩恢复、公开身份及确认授权。
- 前端确认展示验证使用真实 `ChatTranscript`、`TooltipProvider` 和 `QueryClientProvider` 组件链路。
