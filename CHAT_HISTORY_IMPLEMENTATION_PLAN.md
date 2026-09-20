# 对话历史、上下文恢复与压缩实现计划

## 1. 目标与交付边界

### 1.1 用户能力

- 页面刷新、切换路由、重启服务后可以恢复对话。
- 支持新建、列出、切换、删除会话。
- 后续输入能够引用当前会话中的历史语义。
- 等待确认的计划和训练记录卡片可以恢复。
- 失败、中止、部分输出可以展示，并从后续模型上下文中排除。
- 长会话自动压缩为摘要加最近完整轮次。
- 原始历史始终保留，压缩只改变模型上下文投影。

### 1.2 本次暂不包含

- 缓存实现。
- 全文搜索、会话重命名、历史分页。
- 分支、fork、rewind 的交互界面。
- SSE 断点续传。
- Durable Harness 式副作用恢复状态机。
- 自动清理历史。

缓存接缝在本次设计中保留，缓存键和失效条件在第 11 节固定。

## 2. 必须保持的架构约束

1. SQLite 是会话事实源。
2. 浏览器状态只承担当前页面的即时渲染。
3. LangGraph Checkpoint 继续承担单次工作流恢复。
4. 对话 Entry 提供语言语境；业务表提供训练计划、打卡、指标等权威事实。
5. 持久化记录、UI 投影、模型上下文投影保持三层分离。
6. 对话记录采用 append-only；状态变化使用新 Entry 或 Run 状态更新表达。
7. 数据库事务只覆盖数据库操作，不跨越模型调用、工具调用和 SSE 推送。
8. 每个完整语义事件先落库，再推送给客户端。
9. `failed`、`aborted`、`partial` Assistant 内容可展示，不进入后续模型上下文。
10. 压缩不得删除原始 Entry。
11. 当前工作树已有未提交修改；每个目标文件修改前重新读取，保留现有改动。

### 2.1 参考优先级

实现与验收采用以下强制优先级：

1. `D:/repository/pi` 当前源码及其测试。
2. Fit_Agent 当前业务不变量、数据库约束和安全约束。
3. 本计划中的设计说明。
4. 仓库历史文档、旧架构文档和 refactor 记录。
5. 外部资料。

出现语义冲突时，以 Pi 当前源码及对应测试为准，并同步修订本计划后继续实现。禁止仅凭本计划复刻算法；实现前必须打开对应 Pi 源文件和测试，基于当前内容移植。

### 2.2 Pi 源码复用索引

#### Session Entry 与 append-only 树

- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:25-168`：Session Header、Entry 基类和 Entry 联合类型。
- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:1041-1067`：append-only 写入。
- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:1288-1331`：当前 branch、context entries 和全部 entries 三种读取视图。
- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:1388-1393`：leaf/branch 切换语义。
- 本项目只复用上述 append-only 追加与 context entries 重建；会话内顺序由 `sequence` 承载，不移植 leaf/branch 切换。
- 对应测试：
  - `D:/repository/pi/packages/coding-agent/test/session-manager/build-context.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/session-manager/tree-traversal.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/session-manager/save-entry.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/session-manager/load-entries.test.ts`

#### Context Entries 重建与消息投影

- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:392-421`：`sessionEntryToContextMessages()`。
- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:429-470`：`buildContextEntries()` 的 Compaction 感知重建。
- `D:/repository/pi/packages/coding-agent/src/core/messages.ts:148-200`：AgentMessage 到模型 Message 的转换。
- `D:/repository/pi/packages/agent/src/agent-loop.ts:339-362`：上下文变换、LLM 转换和 Provider 调用顺序。
- `D:/repository/pi/packages/coding-agent/src/core/sdk.ts:190-200,375-378`：Session 恢复到 Agent State。
- 对应测试：
  - `D:/repository/pi/packages/agent/test/agent-loop.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/sdk-session-manager.test.ts`

#### Assistant 失败过滤与 Tool 配对

- `D:/repository/pi/packages/ai/src/api/transform-messages.ts:158-232`：孤立 Tool Call 补结果、失败 Assistant 过滤、消息顺序修复。
- `D:/repository/pi/packages/ai/src/api/transform-messages.ts:196-206`：`error`、`aborted` Assistant 排除规则。
- 对应测试：
  - `D:/repository/pi/packages/ai/test/tool-call-without-result.test.ts`
  - `D:/repository/pi/packages/ai/test/tool-call-id-normalization.test.ts`
  - `D:/repository/pi/packages/ai/test/transform-messages-copilot-openai-to-anthropic.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/agent-session-retry.test.ts`

#### System Prompt 回放

- `D:/repository/pi/packages/agent/src/agent.ts:81-86`：初始 System Message 位置。
- `D:/repository/pi/packages/ai/src/utils/transcript.ts:58-129`：System Message 与 Tool Loadout 回放、折叠和解析。
- `D:/repository/pi/packages/coding-agent/src/core/system-prompt.ts:204-216`：System Prompt Section 差量。
- `D:/repository/pi/packages/coding-agent/src/core/agent-session.ts:1115-1172`：Prompt 更新和 Tool Loadout 恢复。
- 对应测试：
  - `D:/repository/pi/packages/ai/test/system-message-replay.test.ts`
  - `D:/repository/pi/packages/ai/test/transcript-tool-changes.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/system-prompt.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/system-prompt-updates.test.ts`

#### Compaction

- `D:/repository/pi/packages/coding-agent/src/core/compaction/compaction.ts:147-253`：默认设置、token 估算和触发条件。
- `D:/repository/pi/packages/coding-agent/src/core/compaction/compaction.ts:323-478`：合法切点和最近轮次保留。
- `D:/repository/pi/packages/coding-agent/src/core/compaction/compaction.ts:762-1024`：Compaction 准备、摘要和结果。
- `D:/repository/pi/packages/coding-agent/src/core/session-manager.ts:409-470,1122-1147`：Compaction Entry 投影、持久化和上下文恢复。
- `D:/repository/pi/packages/coding-agent/src/core/agent-session.ts:2227-2455`：压缩触发、overflow 恢复和 Agent State 重建。
- 对应测试：
  - `D:/repository/pi/packages/coding-agent/test/compaction.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/agent-session-compaction.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/agent-session-auto-compaction-queue.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/compaction-serialization.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/compaction-summary-reasoning.test.ts`

#### UI 恢复视图

- `D:/repository/pi/packages/coding-agent/src/modes/interactive/interactive-mode.ts:3584-3770`：消息、Tool Result 和 Display-only 内容投影。
- `D:/repository/pi/packages/coding-agent/src/modes/interactive/interactive-mode.ts:3788-3944`：Session 恢复和 Compaction 后重建。
- 对应测试：
  - `D:/repository/pi/packages/coding-agent/test/interactive-mode-compaction.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/interactive-mode-tree-navigation.test.ts`
  - `D:/repository/pi/packages/coding-agent/test/custom-message.test.ts`

#### 中断、重试和重启

- `D:/repository/pi/packages/coding-agent/src/core/agent-session.ts:689-708`：Assistant `message_end` 持久化时机。
- `D:/repository/pi/packages/coding-agent/src/core/agent-session.ts:2991-3042`：重试时保留 Session 记录并清理 Agent State。
- `D:/repository/pi/packages/agent/src/agent-loop.ts:127-141,398-420`：继续执行与 Abort 终态。
- `D:/repository/pi/packages/agent/src/harness/runtime/drive/recovery.ts:22-60`：Durable Harness 中断恢复参考。
- Harness 只作为异常语义参考，本次不移植其 operation state machine。

## 3. 分层设计

### 3.1 存储层

**Pi 源码索引**：`session-manager.ts:25-168,1041-1067,1288-1331,1388-1393`；对应 `session-manager/save-entry.test.ts`、`tree-traversal.test.ts`。

新增迁移 `backend/storage/migrations/004_conversation_history.sql`。

#### `conversations`

- `id TEXT PRIMARY KEY`
- `title TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

会话头只承载身份、标题与时间戳；会话内顺序由 `conversation_entries.sequence` 承载，存储层没有 leaf 概念。

#### `conversation_entries`

- `id TEXT PRIMARY KEY`
- `conversation_id TEXT NOT NULL`
- `sequence INTEGER NOT NULL`
- `entry_type TEXT NOT NULL`
- `payload_json TEXT NOT NULL CHECK(json_valid(payload_json))`
- `created_at TEXT NOT NULL`
- 外键指向 `conversations`，删除会话时级联删除。
- `(conversation_id, created_at, id)` 索引。
- `UNIQUE (conversation_id, sequence)`：同一会话内追加序号唯一且单调，重复序号由数据库拒绝。

首批 `entry_type`：

- `message`
- `compaction`
- `confirmation`

`message.payload_json`：

- `role`: `user | assistant`
- `content`
- `status`: `complete | partial | failed | aborted`
- `run_id`
- 可选 `usage`、`provider`、`model`

`compaction.payload_json`：

- `summary`
- `first_kept_entry_id`
- `tokens_before`
- `usage`

`confirmation.payload_json`：

- `action`: `plan_confirmed | plan_rejected | workout_confirmed`
- 关联的 `run_id`、`draft_plan_id` 或 workout record id。
- 面向模型的稳定文本投影。

#### `conversation_runs`

- `id TEXT PRIMARY KEY`
- `conversation_id TEXT NOT NULL`
- `thread_id TEXT NOT NULL UNIQUE`
- `client_request_id TEXT NOT NULL UNIQUE`
- `user_entry_id TEXT NOT NULL`
- `assistant_entry_id TEXT`
- `status TEXT NOT NULL`
- `error_code TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

状态：

- `pending`
- `running`
- `waiting`
- `completed`
- `failed`
- `cancelled`

#### `conversation_run_events`

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `run_id TEXT NOT NULL`
- `sequence INTEGER NOT NULL`
- `event_type TEXT NOT NULL`
- `payload_json TEXT NOT NULL CHECK(json_valid(payload_json))`
- `created_at TEXT NOT NULL`
- `UNIQUE(run_id, sequence)`

该表保存 `node/message/waiting/done/error`，用于 UI 重建、等待卡恢复和诊断。模型上下文只读取 `conversation_entries`。

### 3.2 Repository 层

新增：

- `backend/domain/conversations/__init__.py`
- `backend/domain/conversations/schema.py`
- `backend/domain/conversations/repo.py`

职责：

- 创建、列出、读取、删除会话。
- 在一个事务中追加 Entry：事务内取下一个 `sequence`，同事务更新 `updated_at`。
- 创建 Run 与用户 Entry。
- 幂等读取 `client_request_id` 对应 Run。
- 追加有序 Run Event。
- 更新 Run 状态。
- 按 `sequence` 升序读取会话全部 Entry。
- 读取最新 Compaction Entry（按 `sequence` 降序取第一条）。
- 启动时收敛遗留 `pending/running` Run。

追加 Entry 的序号在同一事务内取 `MAX(sequence) + 1`；同一会话并发发送由 `UNIQUE (conversation_id, sequence)` 拒绝，数据库不会产生分叉。

### 3.3 Session 投影层

**Pi 源码索引**：`session-manager.ts:392-470`、`messages.ts:148-200`、`agent-loop.ts:339-362`、`transform-messages.ts:158-232`；对应 `build-context.test.ts`、`agent-loop.test.ts`、`tool-call-without-result.test.ts`。

新增 `backend/domain/conversations/context.py`，复用 Pi 的核心机制：

- `build_context_entries(entries)`：应用最新 Compaction Entry。
- `entry_to_context_messages(entry)`：Entry 投影为模型消息。
- `build_conversation_display(entries, runs, events)`：生成响应就绪的轮次与压缩分隔。

投影规则：

- `user + complete` 进入上下文。
- `assistant + complete` 进入上下文。
- `assistant + partial/failed/aborted` 仅用于展示。
- `confirmation` 转为明确的用户操作语义。
- `compaction` 转为带边界标记的摘要消息。
- `conversation_run_events` 不直接进入模型上下文。

上下文排列固定为：

1. 当前 System Prompt。
2. 最新 Compaction Summary。
3. `first_kept_entry_id` 开始的完整 Entry。
4. Compaction 之后的新 Entry。
5. 当前用户消息。
6. 当前业务事实块。

System Prompt 继续由当前应用配置生成。动态业务事实不写入对话 Entry。

### 3.4 压缩层

**Pi 源码索引**：`compaction.ts:147-253,323-478,762-1024`、`session-manager.ts:409-470,1122-1147`、`agent-session.ts:2227-2455`；对应 `compaction.test.ts`、`agent-session-compaction.test.ts`、`agent-session-auto-compaction-queue.test.ts`。

新增 `backend/domain/conversations/compaction.py`。

直接复用 Pi 的行为规则：

- 在新一轮发送前检查压缩阈值。
- Assistant 正常结束后再次检查。
- 为输出预留 token。
- 保留最近完整轮次。
- 切点不能落在确认动作和对应等待结果之间。
- 压缩结果追加为 `compaction` Entry。
- 原始 Entry 保持不变。
- 最新 Compaction Entry 已覆盖的区间不重复摘要。

Token 估算顺序：

1. 使用最后一条有效 Assistant usage 作为锚点。
2. 只估算锚点后的 Entry。
3. 缺少 usage 时使用字符估算，并集中放在单一函数中。

压缩设置从当前模型 context window 计算：

- `reserve_tokens`：预留输出、工具调用和安全余量。
- `keep_recent_tokens`：最近完整轮次预算。
- 触发条件：`estimated_context_tokens > context_window - reserve_tokens`。

压缩摘要固定包含：

- 用户目标与明确偏好。
- 已讨论和已接受的方案。
- 已拒绝内容。
- 当前指代对象。
- 尚未解决的问题。
- 需要继续遵守的对话约束。

计划状态、训练记录、指标数值在每次请求时重新读取业务数据库。

### 3.5 Agent 上下文装配层

**Pi 源码索引**：`agent.ts:81-86`、`transcript.ts:58-129`、`agent-session.ts:1115-1172`、`sdk.ts:190-200,375-378`；对应 `system-message-replay.test.ts`、`transcript-tool-changes.test.ts`、`sdk-session-manager.test.ts`。

扩展 `backend/graph/context.py`：

- 保留现有业务事实装配。
- 增加 `conversation_messages`。
- 在意图识别前加载会话上下文。
- 当前用户消息只加入一次。
- 将历史消息以 role message 形式传给模型。
- 将业务事实放入独立、带明确边界的上下文块。

目标上下文结构：

```text
System Prompt
Conversation Summary
Recent Complete Conversation Messages
Current Business Facts
Current User Message
```

确认与拒绝操作完成后追加 `confirmation` Entry，使“已经确认”“已经拒绝”可以在下一轮正确理解。

### 3.6 API 层

新增 `backend/api/routes_conversations.py`，在静态文件兜底路由前注册。

端点：

- `GET /api/conversations`
- `POST /api/conversations`
- `GET /api/conversations/{conversation_id}`
- `DELETE /api/conversations/{conversation_id}`

扩展现有 Agent 请求：

- `chat_id`：稳定会话 ID。
- `conversation_id`：当前 LangGraph `thread_id`，保持现有确认流程兼容。
- `client_request_id`：本轮创建幂等键。
- `request`：用户输入。

`POST /api/agent/run` 流程：

1. 校验会话存在。
2. 根据 `client_request_id` 执行幂等检查。
3. 在单事务中追加用户 Entry 并创建 `pending` Run。
4. 必要时执行上下文压缩。
5. 构建对话上下文和业务事实。
6. 更新 Run 为 `running`。
7. 执行 LangGraph。
8. 每个语义事件先追加 Run Event，再发送 SSE。
9. `waiting` 时更新 Run 为 `waiting`。
10. `done` 时追加完整 Assistant Entry并更新 Run 为 `completed`。
11. `error` 时保存失败信息和可展示内容，更新 Run 为 `failed`。

### 3.7 前端数据层与页面层

**Pi 源码索引**：`interactive-mode.ts:3584-3770,3788-3944`；对应 `interactive-mode-compaction.test.ts`、`interactive-mode-tree-navigation.test.ts`。

扩展：

- `frontend/src/lib/contract.ts`
- `frontend/src/lib/api.ts`

增加：

- Conversation list/detail wire types。
- 创建、读取、删除会话 API。
- `AgentRunBody.chat_id`。
- `AgentRunBody.client_request_id`。

路由：

- `/chat`
- `/chat/:chatId`

页面行为：

- `/chat` 展示新会话状态。
- 首次发送时创建会话，跳转到 `/chat/:chatId`。
- `/chat/:chatId` 通过 React Query 加载会话详情。
- SSE 事件更新当前会话 Query Cache。
- 刷新后由服务端详情替换页面临时状态。
- 会话列表按 `updated_at DESC` 排序。
- 新建会话跳转 `/chat`。
- 删除当前会话后跳转 `/chat`。
- 等待卡根据未关闭的 `waiting` Event 恢复。
- `failed/aborted/partial` 显示明确状态。

现有 `ChatTranscript`、`WorkoutConfirmCard` 和计划确认组件继续复用。

## 4. 实施顺序与阶段闭环

### 阶段 0：基线冻结

1. 记录 `git status --short`。
2. 重新读取所有待修改文件，确认现有未提交改动。
3. 运行当前后端测试、前端构建和 Stage 校验。
4. 保存基线失败项及日志路径。

退出条件：已知当前基线，后续回归可以准确归因。

### 阶段 1：迁移与 Repository

1. 添加 004 迁移。
2. 添加 schema 和 repo。
3. 实现 append-only Entry、会话内 `sequence` 取号、Run 幂等、Event 顺序约束。
4. 实现遗留 Run 收敛。
5. 添加数据库级测试。

验证：

- 新数据库迁移到版本 4。
- 现有版本 3 数据库无损升级。
- Entry 追加后 `sequence` 在同一会话内单调递增。
- 重复的 `(conversation_id, sequence)` 被数据库拒绝。
- 相同 `client_request_id` 只创建一个 Run。
- Event sequence 重复被拒绝。
- 删除会话级联清理。

退出条件：存储不变量全部通过。

### 阶段 2：Session 与上下文投影

1. 完整读取本计划 2.2 中 Session、Context 和失败过滤对应的 Pi 源码及测试。
2. 将 Pi 测试场景映射为 Fit_Agent 测试清单。
3. 实现按 `sequence` 升序读取会话 Entry。
4. 实现 Entry 到 display/context 两套投影。
5. 实现失败 Assistant 过滤。
6. 实现 confirmation 投影。
7. 添加 Pi 行为对齐测试。

测试用例：

- 完整 user/assistant 进入上下文。
- partial/failed/aborted Assistant 保留在 display，缺席于 context。
- Entry 按 `sequence` 升序重建。
- confirmation 被投影为稳定语义。
- display-only Run Event 不进入 context。

退出条件：同一持久化数据可以稳定生成两种视图。

### 阶段 3：上下文压缩

1. 完整读取本计划 2.2 中 Compaction 对应的 Pi 源码及测试。
2. 逐项建立 Pi 行为用例与 Fit_Agent 测试的对应关系。
3. 移植 token 估算和切点选择规则。
4. 实现 Compaction Entry。
5. 实现 summary 加最近消息的重建。
6. 接入现有模型调用生成摘要。
7. 添加压缩算法测试。

测试用例：

- 阈值以内不压缩。
- 超过阈值只生成一次 Compaction Entry。
- 最近完整轮次保留。
- 原始 Entry 数量不减少。
- 二次压缩从上次边界继续。
- 摘要失败时原对话保持可用，本轮请求就地失败并返回明确错误。

退出条件：压缩前后关键语义存在，原始历史完整。

### 阶段 4：Agent 执行链持久化

1. 扩展 DTO。
2. 在 `run_agent` 前创建用户 Entry 和 Run。
3. 在 `_sse_frames` 上游接入 Event 持久化。
4. 接入 waiting/done/error 状态转换。
5. 在确认、拒绝、打卡确认端点追加 confirmation Entry。
6. 在意图识别前装配历史上下文。

验证：

- 普通问答下一轮可以引用上一轮。
- 计划生成等待状态可以恢复。
- 打卡等待状态可以恢复。
- 确认后上下文包含确认语义。
- SSE 发送前数据库已存在对应 Event。
- 非计划意图同样写入历史。

退出条件：所有 Agent 分支使用同一套持久化与上下文路径。

### 阶段 5：会话 API

1. 添加列表、创建、详情、删除端点。
2. 详情返回前端可直接渲染的 rounds。
3. 注册路由。
4. 添加 API 集成测试。

验证：

- 列表排序正确。
- 详情重建顺序正确。
- 删除后无法读取。
- 非法 ID 和不存在 ID 返回一致错误。
- API 不泄露内部 prompt、堆栈和敏感配置。

退出条件：前端恢复只依赖公开 API。

### 阶段 6：前端恢复与会话导航

1. 添加 contract 和 API 方法。
2. 添加 `/chat/:chatId`。
3. 接入 React Query。
4. 实现会话列表、新建和删除。
5. 恢复普通消息、错误态和等待卡。
6. 同步现有源码断言脚本。

验证：

- 刷新恢复。
- 切换路由后返回恢复。
- 多会话互不串线。
- 删除后刷新不复活。
- 等待卡确认后刷新不复活。
- SSE 期间 Query Cache 与最终详情一致。

退出条件：浏览器内存清空后仍能从服务端完整重建页面。

### 阶段 7：重启与异常恢复

1. 应用启动时将遗留 `pending/running` Run 标记为 `failed`。
2. 保留已提交 Run Events 供展示。
3. 排除不完整 Assistant 上下文。
4. 验证用户可以继续发送新一轮。

验证：

- Run 中途终止服务。
- 重启服务。
- 页面显示未完成状态和已保存片段。
- 下一轮模型只收到最后一个有效上下文。

退出条件：异常不会污染模型上下文，也不会阻止会话继续。

### 阶段 8：全量闭环

按顺序执行：

1. 后端目标测试。
2. 后端全量测试。
3. 前端类型检查和构建。
4. `verify:stage6` 及现有脚本校验。
5. 启动真实后端和前端。
6. 手工执行完整对话、刷新、切换、等待确认、删除、重启、长对话压缩。
7. 检查 SQLite 数据和 UI 展示一致。
8. 检查 Git diff，确认未覆盖已有修改。
9. 发现失败后回到对应阶段修复并重新执行完整闭环。

退出条件：第 5 节所有验收场景通过，工作树只包含计划内变更和原有修改。

## 5. 验收场景

1. 新建会话，发送普通问题，刷新后用户和 Assistant 内容完整。
2. 连续提问“改成周三”，模型能引用上一轮目标。
3. 创建第二个会话，两个会话上下文完全隔离。
4. 生成计划进入 waiting，刷新后确认卡恢复。
5. 确认计划，刷新后确认卡消失，下一轮知道计划已经确认。
6. 自然语言打卡进入 waiting，刷新后可继续确认。
7. Run 中途刷新或关闭页面，已持久化内容可见。
8. failed/aborted/partial Assistant 内容不进入下一轮模型请求。
9. 服务重启后遗留 Run 收敛为 failed。
10. 长对话触发 Compaction，摘要和最近消息共同进入模型。
11. Compaction 后原始 Entry 仍可查询。
12. 删除会话后相关 Entry、Run、Event 全部删除。
13. 相同 `client_request_id` 重放不会产生重复消息。
14. 现有 dashboard、plans、确认流程和 Stage 校验无回归。

## 6. 最小测试集合

### 后端

- `backend/tests/test_conversation_repo.py`
- `backend/tests/test_conversation_context.py`
- `backend/tests/test_conversation_compaction.py`
- `backend/tests/test_conversation_routes.py`
- 扩展现有 Agent 分支和确认流程测试。

每个测试文件覆盖一个层次，跨层场景集中在 routes/agent 集成测试中。

### 前端

- 会话详情到 `ChatRound` 的纯转换测试。
- waiting Event 恢复草稿的纯函数测试。
- 路由和 Query 行为使用现有项目测试能力；若当前未配置组件测试框架，使用现有验证脚本扩展关键源码与构建断言。

### 运行验证

- 后端全量测试。
- 前端构建。
- Stage 脚本。
- 一次真实 Provider 连续对话。
- 一次真实压缩触发验证。

## 7. 数据一致性与故障规则

- 用户 Entry 与 `pending` Run 同事务提交。
- Run Event 使用 `(run_id, sequence)` 保证幂等和顺序。
- Assistant 完成 Entry 与 Run `completed` 同事务提交。
- waiting Event 与 Run `waiting` 同事务提交。
- 确认动作 Entry 与对应业务事务成功后提交。
- Entry append 失败时停止发送对应 SSE。
- 上下文重建失败时停止模型调用。
- 压缩失败时保留原始历史并返回明确错误。
- 前端写入失败不使用本地历史冒充服务端事实。

## 8. 安全与隐私

- 请求原文和 Assistant 内容只写入业务数据库。
- 日志不输出完整消息、摘要和 waiting payload。
- API DTO 不返回 System Prompt、模型凭据和内部异常堆栈。
- 删除会话执行数据库级级联删除。
- 所有 payload 通过现有 Pydantic DTO 校验后写入。

## 9. 性能边界

- 单机 SQLite 继续复用现有 `Database` 锁和事务。
- 会话历史通过 `conversation_entries.sequence` 升序重建。
- 会话列表只返回标题、时间和最后消息预览。
- 详情一次返回完整当前会话；分页在出现可测量延迟后加入。
- Compaction 降低模型输入规模，同时保留数据库原始历史。

## 10. 文件改动范围

预计新增：

- `backend/storage/migrations/004_conversation_history.sql`
- `backend/domain/conversations/__init__.py`
- `backend/domain/conversations/schema.py`
- `backend/domain/conversations/repo.py`
- `backend/domain/conversations/context.py`
- `backend/domain/conversations/compaction.py`
- `backend/api/routes_conversations.py`
- 对应后端测试文件。

预计修改：

- `backend/api/app.py`
- `backend/api/dto.py`
- `backend/api/routes_agent.py`
- `backend/graph/context.py`
- `backend/graph/workflow.py`
- `frontend/src/app/App.tsx`
- `frontend/src/features/chat/ChatPage.tsx`
- `frontend/src/features/chat/utils/chatRound.ts`
- `frontend/src/features/chat/components/ChatTranscript.tsx`
- `frontend/src/lib/api.ts`
- `frontend/src/lib/contract.ts`
- 受影响的现有验证脚本。

最终改动以重新读取后的当前实现为准。

## 11. 后续缓存设计接缝

本次上下文构建保持纯输入输出边界：

```text
conversation_id
last_entry_sequence
latest_compaction_entry_id
business_data_revision
model_context_window
→ ContextBundle
```

未来缓存键：

- `conversation_id`
- `last_entry_sequence`
- `latest_compaction_entry_id`
- `business_data_revision`
- prompt/version 标识

失效条件：

- 追加任何 Conversation Entry。
- 生成新 Compaction Entry。
- 计划、打卡、身体指标等业务事实变化。
- System Prompt 或 Skill 版本变化。
- 模型 context window 变化。

本次不添加缓存表、内存缓存和失效代码。

## 12. 完成定义

- 已逐项读取 2.2 索引中的 Pi 源码和关键测试。
- 每项复用机制都有 Pi 源码坐标、Fit_Agent 实现坐标和行为对齐测试。
- 发生语义冲突时已经按 Pi 当前源码修订实现与本计划。
- 所有验收场景通过。
- 全量自动化检查通过。
- 真实 Provider 验证通过。
- 刷新和服务重启不会丢失已提交历史。
- 下一轮模型能够使用有效历史语义。
- 失败和部分 Assistant 内容不会污染上下文。
- 长对话可以压缩并继续。
- 原始历史保持完整。
- 现有未提交修改得到保留。
- 缓存边界固定，未提前实现缓存。
