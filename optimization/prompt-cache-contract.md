# Fit-Agent 业务上下文消息契约

## 1. 状态与适用范围

本文件记录业务上下文消息的数据、存储与前后端接口契约。优化目标见 [prompt-cache-strategy.md](prompt-cache-strategy.md)。

已确认：独立 `custom_message` 会话节点、通用泛型消息与节点基类、强类型业务资料、完整基线与增量载荷、失效引用表示、只公开节点身份的历史接口、运行中的 SSE 身份通知、旧会话不回填。

运行中的资料节点通知契约见第 8 节。

执行 agent 遇到待确认项必须停下来询问用户。

## 2. 已确认的内部节点约束

- 节点 `type` 为 `custom_message`，独立参与会话树。
- 后端创建内部资料节点，普通发送、编辑、附件与 Steering 输入不能创建该类型。
- 资料载荷使用第 5 节已确认的强类型字段与校验规则。
- 节点有固定内容、来源与发生位置，既有历史内容保持不可变。
- 真实业务工具返回值继续采用既有 `toolResult`，遵循真实工具调用配对。
- 内部资料没有保存授权，也不作为真实用户输入展示。
- 后续模型转换在原位置输出 user-role 内容；会话节点类型与模型协议角色分别定义。

通用 `CustomMessage` 与 `CustomMessageEntry` 基类及其业务具体类型定义在 `backend/app/domain/session/models.py`。`SessionEntry` 登记 `BusinessContextEntry`，`AgentMessage` 登记 `BusinessContextMessage`；服务商 `Message` 联合保持原样。

已确认采用 `CustomMessage[TData]` 与 `CustomMessageEntry[TMessage]` 两个通用泛型基类。`BusinessContextMessage` 包含强类型 `BusinessContextData`；`BusinessContextEntry` 将节点的消息类型指定为 `BusinessContextMessage`。类型契约见第 5 节。

## 3. 已确认的公开历史契约

公开历史响应中的 `custom_message` 节点只返回以下身份字段：

| 字段 | 类型 | 语义 |
|---|---|---|
| type | 固定字符串 custom_message | 内部资料节点类型 |
| entry_id | string | 当前节点身份 |
| parent_id | string 或 null | 父节点身份，根节点允许 null |
| run_id | string 或 null | 通用节点允许 null；BusinessContextEntry 必须关联当前真实运行，值为非空 string |
| created_at | integer | 沿用当前节点的 UTC 毫秒时间 |

- 完整业务资料、模型可读文本及内部附加内容不进入公开历史响应。
- 前端识别该节点，保留祖先链与运行归属，不生成可见用户聊天条目。
- `active_leaf_id` 指向的路径必须完整，隐藏内容不会移除必要的身份节点。
- 后端公开投影使用白名单；Python 与 TypeScript 的字段定义和校验保持一致。

运行期间通过已确认的 `custom_message` SSE 事件通知资料节点，载荷沿用本节公开身份白名单。事件字段、提交后通知顺序与前端处理见第 8 节。

## 4. 已确认的历史处理策略

- 已有会话记录保持原样，不回填过去的临时资料。
- 后续运行生成并追加新的资料事件。
- 旧节点 ID、父子关系、业务数据、运行与确认绑定保持完整。
- 原本未保存的临时上下文不被声明为已原样恢复。
- 数据库迁移保留外键、不可变约束与数据完整性。
- 使用新增迁移支持获批结构，保持已执行的旧迁移文件完整。

## 5. 已确认的基类与业务资料结构

### 5.1 继承、包含与类型登记

- `CustomMessage[TData]` 定义通用内部消息信封，`TData` 约束为 `Model`，用于结构化 `details`。
- `CustomMessageEntry[TMessage]` 定义通用会话节点，`TMessage` 约束为 `CustomMessage`，节点包含单个 `message: TMessage`。
- `BusinessContextMessage` 继承 `CustomMessage[BusinessContextData]`，固定业务类型、隐藏展示及必填资料。
- `BusinessContextEntry` 继承 `CustomMessageEntry[BusinessContextMessage]`，要求非空 `run_id`。
- 通用消息层负责信封，强类型资料层负责字段与校验，会话节点层负责树身份与运行关联。
- 当前业务资料创建入口使用 `BusinessContextEntry`；`SessionEntry` 联合登记这个具体节点类型。
- `AgentMessage` 联合登记 `BusinessContextMessage`。服务商协议 `Message` 保持现有类型联合，内部消息经 `convert_to_llm` 转换后进入适配器。
- 既有 `BusinessContext` 继续用于工具执行身份，含 `session_id / run_id / request_entry_id / source_entry_id`；模型资料采用独立的 `BusinessContextData`。

类型字段契约：

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

`message.timestamp` 等于节点 `created_at`，均为 UTC 毫秒整数。`parent_id` 表达资料事件的发生位置。业务节点绑定当前真实运行；日期与请求来源由真实用户节点确定。

### 5.2 完整基线与增量载荷

`BusinessContextData` 为按 `mode` 判别的强类型联合：

```python
BusinessContextData = Annotated[
    BusinessContextSnapshot | BusinessContextDelta,
    Field(discriminator="mode"),
]
```

`BusinessContextSnapshot`：

| 字段 | 类型 | 必填与语义 |
|---|---|---|
| mode | Literal["snapshot"] | 完整资料基线 |
| request | RequestContext | 必填，当前请求资料 |
| message_nodes | list[MessageNodeReference] | 必填，模型可见的 user、assistant、toolResult 来源节点；允许 [] |
| pending_proposals | list[PendingProposalReference] | 必填，当前有效待确认提案集合；允许 [] |

`BusinessContextDelta`：

| 字段 | 类型 | 必填与语义 |
|---|---|---|
| mode | Literal["delta"] | 增量资料 |
| request | RequestContext 或 null | 必填；null 表示保持请求资料；对象表示整份替换 |
| upsert_message_nodes | list[MessageNodeReference] | 必填，新增或变化节点的完整记录 |
| remove_message_node_ids | list[OperationId] | 必填，移出模型可见集合的节点 ID |
| upsert_pending_proposals | list[PendingProposalReference] | 必填，新增或变化提案的完整引用 |
| remove_pending_proposals | list[RemovedPendingProposal] | 必填，移出待确认集合的引用及原因 |

- 增量列表允许 `[]`；所有列表为空且 `request=null` 的空增量拒绝入库。
- 每个列表拒绝重复键；同一键同时出现在新增与移除集合时校验失败。
- 节点记录按 `entry_id` 合并；提案记录按 `(business_kind, proposal_id)` 合并。
- 首次资料事件生成完整基线；已有基线的分支在资料变化时追加增量。
- 当前分支的资料状态从最近完整基线开始，按顺序应用后续增量。

### 5.3 请求与消息来源

`RequestContext`：

| 字段 | 类型 | 来源与校验 |
|---|---|---|
| timezone | Literal["Asia/Shanghai"] | 必填，当前后端业务时区 |
| business_date | YYYY-MM-DD 字符串 | 必填，按真实用户节点 created_at 换算；校验真实日期 |
| request_entry_id | OperationId | 必填，当前真实用户节点；Steering 消费后更新 |

所有节点与提案 ID 复用现有标准 UUID 校验。后端核验节点角色、当前路径与真实关联。重放使用发生时的业务日期。

`MessageNodeReference` 按 `role` 判别三个强类型记录，所有记录必填 `entry_id`：

| 资料类 | 固定 role | 其他必填字段 |
|---|---|---|
| UserNodeReference | user | confirmation: SaveConfirmation 或 null；proposal: ProposalKey 或 null |
| AssistantNodeReference | assistant | tool_call_ids: list[str]，允许 [] |
| ToolResultNodeReference | toolResult | tool_name: str；tool_call_id: str；proposal: ProposalKey 或 null |

`ProposalKey` 必填 `business_kind` 与 `proposal_id`，业务类型为 `profile / plan / workout`，提案 ID 使用 `OperationId`。

`confirmation` 复用既有 `SaveConfirmation`，记录已有后端确认绑定。`proposal` 取后端已保存的确认或展示关联。工具结果的展示节点就是该记录的 `entry_id`；工具调用 ID 保持真实配对。

### 5.4 待确认提案与移出引用

`PendingProposalReference`：

| 字段 | 类型与空值规则 | 来源 |
|---|---|---|
| business_kind | profile / plan / workout，必填 | 快照业务类型 |
| proposal_id | OperationId，必填 | 快照身份 |
| status | pending / processing，必填 | 快照当前状态 |
| request_entry_id | OperationId，必填 | 准备时真实用户节点 |
| source_entry_id | OperationId，必填 | 发起准备调用的 assistant 节点 |
| display_entry_id | OperationId 或 null，必填 | 已绑定展示节点；尚未绑定时为 null |
| confirmation_entry_id | OperationId 或 null，必填 | 已有后端确认操作绑定 |

- `processing` 要求展示与确认节点均已绑定；确认节点有值时，展示节点必须有值。
- 提案引用使用完整当前分支核验；被压缩的来源节点仍可保留受控读取引用。
- `message_nodes` 使用当前模型可见集合，两类资料分别确定有效范围。
- 完整提案内容继续通过真实业务工具受控读取。

`RemovedPendingProposal`：

| 字段 | 类型 | 语义 |
|---|---|---|
| business_kind | profile / plan / workout | 必填，提案业务类型 |
| proposal_id | OperationId | 必填，移出的提案身份 |
| reason | saved / invalidated / conflicted / deleted / out_of_scope | 必填，后端核实的移出原因 |

`saved / invalidated / conflicted` 表达真实状态变化；`deleted` 表达记录实际删除；`out_of_scope` 表达离开当前有效范围。移除只改变事件之后的有效集合，历史资料保持完整。

### 5.5 存储、生成与重放

- `details` 是结构化资料依据，`content` 是该事件固定的模型投影。
- 后端创建入口校验 `details`，用固定 JSON 序列化生成 `content`，加入后端资料来源与无保存授权标记。
- 投影一致性在创建入口与写入服务校验；消息模型层保持字段校验，历史读取不依赖当前模板重新计算内容。
- `details` 与 `content` 在同一事务落库；重放直接使用已保存的 `content`。
- 渲染模板更新影响新事件，历史文本保持原值。
- Pydantic 沿用 `strict=True` 与 `extra="forbid"`；未知资料类型在校验处失败。
- 下一次模型请求前比较真实资料，发生变化时追加节点并更新执行位置。
- 准备调用的全部结果节点与展示绑定完成后，再追加该批资料事件，保持工具调用与全部结果的连续配对。
- Steering 消费后更新请求资料及业务日期，随后追加相应资料事件。
- 资料事件自身不进入 `message_nodes`，避免递归资料增长。
- 压缩后持久化完整新资料基线；基线与压缩提交共同保证恢复完整性。
- `transform_context` 读取当前分支的固定资料事件；`convert_to_llm` 在原位置仅输出 `content` 与时间，协议角色为 user。
- `custom_type / display / details` 不作为额外协议字段发送；业务资料已经包含在固定 `content` 中。
- `display=false` 控制展示；公开接口独立使用第 3 节白名单。

### 5.6 JSON 样例

以下均为示意数据，表达已确认的字段与边界。

首次完整基线的 `details`：

```json
{
  "mode": "snapshot",
  "request": {
    "timezone": "Asia/Shanghai",
    "business_date": "2026-08-15",
    "request_entry_id": "00000000-0000-4000-8000-000000000001"
  },
  "message_nodes": [
    {
      "entry_id": "00000000-0000-4000-8000-000000000001",
      "role": "user",
      "confirmation": null,
      "proposal": null
    }
  ],
  "pending_proposals": []
}
```

准备画像后新增的增量 `details`：

```json
{
  "mode": "delta",
  "request": null,
  "upsert_message_nodes": [
    {
      "entry_id": "00000000-0000-4000-8000-000000000002",
      "role": "assistant",
      "tool_call_ids": ["call_prepare_1"]
    },
    {
      "entry_id": "00000000-0000-4000-8000-000000000003",
      "role": "toolResult",
      "tool_name": "prepare_profile_update",
      "tool_call_id": "call_prepare_1",
      "proposal": {
        "business_kind": "profile",
        "proposal_id": "00000000-0000-4000-8000-000000000004"
      }
    }
  ],
  "remove_message_node_ids": [],
  "upsert_pending_proposals": [
    {
      "business_kind": "profile",
      "proposal_id": "00000000-0000-4000-8000-000000000004",
      "status": "pending",
      "request_entry_id": "00000000-0000-4000-8000-000000000001",
      "source_entry_id": "00000000-0000-4000-8000-000000000002",
      "display_entry_id": "00000000-0000-4000-8000-000000000003",
      "confirmation_entry_id": null
    }
  ],
  "remove_pending_proposals": []
}
```

提案失效事件的完整持久化节点：

```json
{
  "session_id": "00000000-0000-4000-8000-000000000100",
  "id": "00000000-0000-4000-8000-000000000005",
  "parent_id": "00000000-0000-4000-8000-000000000003",
  "run_id": "00000000-0000-4000-8000-000000000101",
  "type": "custom_message",
  "created_at": 1786766400000,
  "message": {
    "role": "custom",
    "custom_type": "business_context",
    "display": false,
    "timestamp": 1786766400000,
    "content": "后端业务资料，仅供读取，不构成保存授权。\n<business_context>\n{\"mode\":\"delta\",\"request\":null,\"upsert_message_nodes\":[],\"remove_message_node_ids\":[],\"upsert_pending_proposals\":[],\"remove_pending_proposals\":[{\"business_kind\":\"profile\",\"proposal_id\":\"00000000-0000-4000-8000-000000000004\",\"reason\":\"invalidated\"}]}\n</business_context>",
    "details": {
      "mode": "delta",
      "request": null,
      "upsert_message_nodes": [],
      "remove_message_node_ids": [],
      "upsert_pending_proposals": [],
      "remove_pending_proposals": [
        {
          "business_kind": "profile",
          "proposal_id": "00000000-0000-4000-8000-000000000004",
          "reason": "invalidated"
        }
      ]
    }
  }
}
```

同一节点的公开历史投影：

```json
{
  "type": "custom_message",
  "entry_id": "00000000-0000-4000-8000-000000000005",
  "parent_id": "00000000-0000-4000-8000-000000000003",
  "run_id": "00000000-0000-4000-8000-000000000101",
  "created_at": 1786766400000
}
```

出现新的未确认业务决策或功能边界冲突时，执行 agent 必须停下来询问用户。

## 6. 安全与资料读取

- 日期与执行身份继续由后端绑定。
- 后端独立核验来源、当前路径、展示节点、提案状态及业务限制。
- 模型可见资料提供读取线索，不创建保存授权。
- 确认保存继续由独立后端接口执行。
- 资料内容保持固定重放，后续状态变化通过新事实表达。
- 内部内容不能因公开序列化或前端展示而泄露。

## 7. 验证与文档归属

每项改动完成必要的实际运行、局部功能验证和相关小范围测试，保证本任务改动能够正常使用。验证覆盖该次改动影响到的存储、迁移、接口与安全边界。

每项任务无需执行全量测试或重新测量量化指标。真实缓存观测仍是优化功能范围，其指标重测不作为每项任务的交付要求。

最终业务验收由用户执行。执行 agent 交付时明确已经运行的检查与剩余未验证范围，不能将未运行的检查标为通过。

方案与契约仅在设计约束变更时更新。任务进度、实现事实与验证结果集中记录在独立的 [实施简报](prompt-cache-implementation-report.md)，保持精炼。文档由主会话维护；执行 agent 仅回复需记录的事实，不修改设计文档。

模型与 TTL 能力核验按用户要求暂停。

## 8. 已确认的运行中节点通知

### 8.1 事件与公开载荷

通过当前运行的既有 SSE 事件流发送 `custom_message` 事件。事件 `data` 复用第 3 节公开身份白名单：

| 字段 | 类型 | 约束 |
|---|---|---|
| type | Literal["custom_message"] | 必填，节点类型 |
| entry_id | OperationId | 必填，已提交资料节点身份 |
| parent_id | OperationId 或 null | 必填，真实父节点身份 |
| run_id | OperationId | 必填，非空且与当前运行事件流一致 |
| created_at | integer | 必填，节点的 UTC 毫秒时间 |

完整业务资料、模型文本与内部附加内容保持私有。Python 与 TypeScript 同步定义事件类型、字段校验与公开投影。

事件样例沿用第 5.6 节示意节点：

```json
{
  "event": "custom_message",
  "data": {
    "type": "custom_message",
    "entry_id": "00000000-0000-4000-8000-000000000005",
    "parent_id": "00000000-0000-4000-8000-000000000003",
    "run_id": "00000000-0000-4000-8000-000000000101",
    "created_at": 1786766400000
  }
}
```

### 8.2 节点身份通知顺序

- 资料节点成功落库后发送身份通知，通知先于后续模型消息事件。
- 前端先获知父节点身份，再收到新增资料节点身份；父子关系由 `parent_id` 表达。
- 历史已经公开的节点身份继续复用。
- 同批工具结果全部完成提交与通知后，再发送该批资料节点身份。

三类节点的身份通知与后续资料通知关系如下：

| 已提交的节点 | 先发送的现有事件 | 后发送的资料事件 |
|---|---|---|
| 工具结果节点 | tool_result | 该批资料节点的 custom_message |
| 运行中补充的用户消息 | steering_status，status=consumed | 消费后资料节点的 custom_message |
| 压缩检查点 | compaction_end | 新资料基线节点的 custom_message |

例如工具结果节点 T 已提交，资料节点 C 的 `parent_id=T`：发送 `tool_result(T)` 后发送 `custom_message(C)`，随后开始下一次模型消息。后续助手节点 A 提交时，其 `parent_id=C`。

### 8.3 前端处理与恢复

- 前端只登记隐藏的结构节点与运行归属，保留完整祖先链，不生成可见用户聊天条目。
- 按 `entry_id` 合并身份节点；相同身份的字段冲突按协议错误处理。
- 同步更新前端事件类型、解析与隐藏节点处理。
- 连接中断与刷新沿用既有运行查询及历史读取恢复流程，历史身份节点保持完整。
