# Stage 5：确认启用、调整计划与 Agent 传输

> 状态：**Stage 5 已完成**。Subtask 01–07 已实施；独立只读审查无 P0／P1；完整 Gate 全绿：Stage 5 四文件 **131 passed**，后端全量 **453 passed, 1 warning**，前端 build 与验证脚本通过；尚未提交。
> 当前分支：`refactor/langgraph`。
> 权威顺序：`Fit-Agent-LangGraph-重构讨论总结.md` > `LANGGRAPH_REFACTOR_PLAN.md` > 已合入源码。
> 本文件是 Stage 5 契约、最终实现与验收证据正本；Stage 1–4 日志仅作历史基线。

## 1. Stage 5 范围

### 1.1 已交付

1. 五类确定性 Intent Router；零命中或多命中时才调用模型分类。
2. draft 确认激活、拒绝归档；统一 `PlanActivationService` 负责事务与幂等。
3. checkpoint 优先恢复；无等待 checkpoint 时按唯一 draft 兜底。
4. 调整计划链路：强制读取 active、按 active 动作过滤 PB、加载 `plan-adjustment`、使用 progression-aware 校验。
5. Agent API：
   - `POST /api/agent/run` → SSE
   - `POST /api/agent/confirm`
   - `POST /api/agent/reject`
6. 五类 SSE 产品事件：`node`／`message`／`waiting`／`done`／`error`。
7. `regenerate` 同类 draft 替换规则。
8. 前端计划页：active、draft、Evaluator 摘要、确认/拒绝、历史版本、生成/调整、SSE 进度。
9. `view_progress`：读取既有 `StatsService` 确定性统计，由模型生成解释。
10. Stage 5 行为测试、完整 Gate、独立只读审查。

### 1.2 明确不做

- 自然语言打卡解析与专用确认（Stage 6）。
- `form_record` 经 Agent 写库。
- onboarding/review Skill、聊天摘要、向量库、通用草稿。
- RIR、估算 1RM、训练容量、完成率、主观疲劳、医学诊断、新训练阈值。
- Router 第三 Agent、Agent Swarm、A2A、插件注册中心。
- 修改 `WorkflowState` 11 字段、MemoryAssembler 六类边界、SkillLoader 语义。
- 新增计划状态或时间字段；用户拒绝不写 `rejected`。
- checkpoint 查询端点、拖拽排程、完整计划编辑器。

## 2. 冻结契约

### 2.1 激活事务

同一业务事务内顺序固定：

1. 读取 `plan_id`，要求仍为 `draft`；
2. 解析 `PlanDraft`，若 `starts_on < business_day` 则拒绝；
3. 再跑确定性校验；调整 draft 还要求 `source_plan_id == 当前 active.id` 并按 active 关联训练事实校验 progression；
4. 当前 active → `archived`，写 `archived_at`；
5. 取消旧 active 中 `scheduled_on >= business_day` 的 `plan_sessions`；
6. draft → `active`，写 `confirmed_at`，按 `training_days` 创建新 sessions；
7. 提交。

任一步失败整体回滚。模型调用不进入事务。日期新鲜度或再校验失败时 draft 保持可确认，原 active 不变。

### 2.2 confirm / reject 幂等

| 当前状态            | confirm      | reject                               |
| ------------------- | ------------ | ------------------------------------ |
| `draft`             | 执行激活事务 | `draft → archived`，写 `archived_at` |
| 同一 id 已 `active` | 返回既有结果 | 冲突，不写                           |
| `archived`          | 拒绝重新激活 | 返回既有结果                         |
| `rejected`          | 拒绝重新激活 | 冲突                                 |

`rejected` 仅表示 Evaluator 二次阻断失败；用户拒绝永不写 `rejected`。

### 2.3 确认恢复

1. `conversation_id` 作为 Checkpointer `thread_id`。
2. 优先读取 checkpoint；只有图停在确认 interrupt 且 `draft_plan_id == 请求 plan_id` 时才 resume。
3. checkpoint 不存在、无法恢复或已无等待任务时，读取业务库唯一 draft；其 id 仍必须等于请求 `plan_id`。
4. resume 与兜底均调用同一 `PlanActivationService`。
5. 重复请求必须进入领域幂等逻辑，不能仅返回 checkpoint 旧 State。
6. resume 载荷只允许 `{ action, plan_id }`。

### 2.4 调整计划

- 无 active：Planner 前失败；不调 Planner；不写 draft/rejected。
- 有 active：解析 active `PlanDraft`，稳定去重提取 `exercise_id`，用 `exercise_ids` 过滤上下文，加载 `plan-adjustment`。
- Planner/Evaluator/一次修订/持久化/等待确认继续复用同一计划子图。
- 调整校验用 active 的目标组数、次数区间、目标负荷与关联训练调用 `resolve_progression`，支持 `increase`／`keep`／`regress`／`needs_calibration`。
- 新 adjustment draft：`source_plan_id = 当前 active.id`。
- 未被调整证据推翻的训练日、动作和处方保持不变。

已有 draft 时：

- `regenerate=false`：
  - generate 只可复用 `source_plan_id IS NULL` 的 generate draft；
  - adjust 遇已有 draft直接冲突；
  - 跨类型冲突；
  - 不调模型、不写第二条 draft。
- `regenerate=true`：
  - generate 只替换 generate draft；
  - adjust 只替换 `source_plan_id == 当前 active.id` 的 adjustment draft；
  - 替换保持同 id/version/source；
  - 来源 active 已变化或跨类型均冲突。

### 2.5 安全

- 沿用 Stage 4 的 10 项精确安全子串，不扩词表、不诊断。
- 安全预检优先于 Router，对每个请求生效。
- 命中后直接进入既有 `safety_stop`：不调分类模型、不分派 intent、不读 active/draft、不处理 regenerate、不写业务数据、不发 `waiting`／`error`。
- 禁用动作只取画像 `known` 的 `exercise_id`；Planner 前过滤，Evaluator 再检。

### 2.6 Router

Router 为函数/节点，不是第三 Agent。请求先 trim；英文匹配忽略大小写。

| intent                    | 确定性命中条件                                               |
| ------------------------- | ------------------------------------------------------------ |
| `form_record`             | `打开打卡表单`／`使用表单记录`／`表单打卡`                   |
| `natural_language_record` | 记录动词（`记录`／`打卡`／`练了`／`完成了`） + 事实标记（`kg`／`公斤`／`次`／`组`／`秒`／`今天`／`昨天`） |
| `view_progress`           | `查看进步`／`训练进展`／`最近表现`／`个人最佳`／`PB`／`趋势`／`看板` |
| `generate_plan`           | `生成计划`／`制定计划`／`新训练计划`／`做个训练计划`         |
| `adjust_plan`             | `调整计划`／`修改计划`／`改计划`／`调整训练安排`             |

单一命中直接返回。零命中或多 intent 命中时只调用一次模型分类，严格输出：

```json
{ "intent": "<五类枚举之一>" }
```

非法枚举、额外字段、非对象或非法 JSON 均为 Run error。Router 与计划链路共享同一 `ModelRequestBudget`；限制仍为 **60s / 180s / 5 次模型请求**。

| intent                    | 行为                                |
| ------------------------- | ----------------------------------- |
| `generate_plan`           | 进入计划子图                        |
| `adjust_plan`             | 进入 adjustment 分支                |
| `form_record`             | 只引导使用既有表单 API              |
| `view_progress`           | 查询 `StatsService`，模型只解释结果 |
| `natural_language_record` | 明确 Stage 6 未实现                 |

### 2.7 HTTP

```text
POST /api/agent/run
body: { conversation_id: string, request: string, regenerate?: boolean }
→ text/event-stream

POST /api/agent/confirm
body: { conversation_id: string, plan_id: number }
→ 200 { plan: PlanWire }

POST /api/agent/reject
body: { conversation_id: string, plan_id: number }
→ 200 { plan: PlanWire }
```

规则：

- `conversation_id` 必须为 UUID。
- 请求 JSON/类型/UUID 非法：SSE 建立前使用既有 JSON 错误形状。
- `/run` 建流后的 Router、模型、超时、无 active、draft 冲突等错误：只发一个 SSE `error` 后关闭。
- confirm/reject 的不存在、ID 不匹配、状态冲突、过期、再校验失败：既有 JSON 错误形状 + 明确 HTTP 状态。
- 不回显 API Key、Base URL、模型名、SQL、文件路径或堆栈。

### 2.8 SSE

| event     | data                                                         |
| --------- | ------------------------------------------------------------ |
| `node`    | `{ "name": string }`                                         |
| `message` | `{ "text": string }`                                         |
| `waiting` | `{ "draft_plan_id": number }`                                |
| `done`    | `{ "ok": true, "intent": string/null, "termination_reason": string/null, "draft_plan_id": number/null }` |
| `error`   | `{ "message": string }`                                      |

禁止暴露隐藏推理、完整系统提示词、Provider 配置、原始 LangChain 事件。客户端断线不会确认、拒绝、取消或回滚已持久化 draft。页面恢复通过既有 `GET /api/plans` 定位 draft。

### 2.9 State 与模型边界

- `WorkflowState` 保持 11 字段不变。
- adjust/regenerate 与预读 active 身份放运行上下文，不复制到 State。
- MemoryAssembler 保持六类输出及 `exercise_ids` 入口。
- 模型不在数据库事务内。
- Provider/SDK 异常在生产模型入口转换为固定 `ModelCallFailed`，避免敏感 Provider 信息写入 checkpoint。

## 3. 最终实现

### 3.1 领域层

- `backend/domain/plans/repo.py`
  - 条件状态迁移：active→archived、draft→active、draft→archived；
  - 取消未到期 sessions；
  - 创建新 sessions；
  - draft 插入支持 `source_plan_id`。
- `backend/domain/plans/service.py`
  - `PlanActivationService.activate/reject`；
  - 日期新鲜度、确定性再校验、幂等、事务回滚、原 active 保护。
- `backend/domain/plans/rules.py`
  - 保留 generate 最近工作组规则；
  - 新增 adjustment progression-aware 校验，复用 `resolve_progression`。

### 3.2 Graph

- `backend/graph/router.py`：五类封闭 Router + 严格模型兜底。
- `backend/graph/nodes.py`：
  - `require_active_plan`；
  - adjustment 上下文与 `exercise_ids` 过滤；
  - `activate_plan`／`archive_draft`；
  - resume 载荷校验。
- `backend/graph/workflow.py`：
  - Router + 非计划分支 + 计划子图统一入口 `invoke_agent_run`；
  - 一份共享模型预算；
  - confirmation 条件边；
  - checkpoint resume + draft fallback 统一确认入口。
- `backend/graph/state.py`：11 字段保持不变。
- `backend/graph/context.py`／`skills.py`／`checkpointer.py`：既有语义保持。

### 3.3 API

- `backend/api/routes_agent.py`：run SSE、confirm、reject。
- `backend/api/app.py`：生产依赖、图、激活服务、checkpointer 与 `routes_agent` 装配。
- `backend/api/dto.py`：Agent DTO、UUID 校验、领域异常映射。
- 生产启动不要求立即读取模型环境变量；模型调用按需进入唯一模型入口。

### 3.4 前端

- `frontend/src/lib/contract.ts`：Agent wire + 五类 SSE 类型。
- `frontend/src/lib/api.ts`：
  - `runAgentStream` 使用 `fetch + ReadableStream`；
  - 支持跨 chunk 帧、CRLF、UTF-8 分割、尾帧无空行、流内 error；
  - `confirmPlan`／`rejectPlan`。
- `frontend/src/features/plans/PlansPage.tsx`：
  - active、draft、Evaluator 摘要、历史版本；
  - 生成/调整；
  - confirm/reject；
  - 五类 SSE 进度。
- `frontend/src/app/App.tsx`：新增 `/plans` 导航与路由。
- confirm/reject 后失效 `plans` 与 `calendar` Query；Run 结束刷新 plans。
- 前端不复制 Router 词表、不重算 PB/趋势/完成率、不实现 Stage 6 打卡。

## 4. 关键行为验收

### 4.1 Router / 非计划 intent

- 五类封闭规则均有单命中测试，单命中分类模型调用为 0。
- 零命中、多 intent 命中各调用一次模型。
- 严格枚举外输出报错。
- Router + Planner/Evaluator/修订最坏共 5 次模型调用。
- form/view 不写业务表；view 数值等于 `StatsService` 输出。
- natural-language record 不创建 draft。

### 4.2 激活 / 拒绝

- 首次激活：draft→active，旧 active→archived。
- 无旧 active：只激活新计划。
- `starts_on < business_day`、再校验失败：draft 保持不变。
- generate 与 adjustment 使用各自负荷校验规则。
- adjustment 的 increase/keep/regress/needs_calibration 均有通过/拒绝覆盖。
- 旧日程只取消 `scheduled_on >= business_day`；新 sessions 与 `training_days` 一致。
- 事务中途失败全回滚。
- 重复 confirm/reject 按 §2.2 幂等。
- 用户拒绝不会产生 `status='rejected'`。

### 4.3 checkpoint / fallback

- 等待 checkpoint + plan ID 相等：resume 成功。
- plan ID 不等：409/冲突，无写入。
- 无等待 checkpoint + 唯一 draft ID 相等：fallback 成功。
- 已完成 checkpoint 的重复请求仍进入领域幂等服务。
- SQLite checkpointer 重开后，同一 `conversation_id` 可继续等待中的确认。
- resume 与 fallback 均验证任意时刻只有一个 active。

### 4.4 adjustment / regenerate

- 无 active：Planner 前失败，0 模型调用、0 draft。
- 有 active：`source_plan_id` 正确，PB 只包含 active 动作。
- 普通 adjust 遇已有 draft 冲突。
- generate/adjust 不允许跨类型替换。
- `regenerate=true` 只替换同类型、同来源 draft。
- regenerate 失败保留原 draft。
- 固定案例验证未受证据影响的训练日、动作和处方保持不变。
- 安全词仍优先进入 `safety_stop`，一次修订上限保持。

### 4.5 HTTP / SSE

- 事件集合严格为五类；`waiting` 只含 `draft_plan_id`。
- 请求形状/UUID 错误在流前返回 JSON。
- 无 API Key、超时及 Run 错误在流内仅一个 SSE `error`。
- SSE 响应与 checkpoint 存档无 API Key / Provider 配置泄漏。
- 客户端断线不触发额外业务写入或状态迁移。
- confirm/reject REST 与领域服务结果一致。

### 4.6 前端

- build 通过。
- confirm/reject 调用正确端点。
- 历史列表包含 `rejected`。
- SSE parser 通过可执行验证脚本覆盖。
- 无新增前端依赖或测试框架。

## 5. 测试与 Gate

### 5.1 最终测试数量

| 测试                                            |                      结果 |
| ----------------------------------------------- | ------------------------: |
| `tests/test_stage5_router.py`                   |                 46 passed |
| `tests/test_stage5_activation.py`               |                 24 passed |
| `tests/test_stage5_adjust_and_confirm_graph.py` |                 29 passed |
| `tests/test_stage5_agent_api_sse.py`            |                 32 passed |
| Stage 5 四文件合计                              |            **131 passed** |
| 四文件清空 `MODEL_*`                            |            **131 passed** |
| 后端全量                                        | **453 passed, 1 warning** |
| 前端验证脚本                                    |         **10 组断言通过** |
| 前端 build                                      |                  **通过** |

唯一 warning 为既有 `starlette/testclient.py` 的 `anyio.abc.BlockingPortal` DeprecationWarning。

### 5.2 最终 Gate

```bash
cd backend && uv run pytest tests/test_stage5_router.py
cd backend && uv run pytest tests/test_stage5_activation.py
cd backend && uv run pytest tests/test_stage5_adjust_and_confirm_graph.py
cd backend && uv run pytest tests/test_stage5_agent_api_sse.py

cd backend && env -u MODEL_API_KEY -u MODEL_BASE_URL -u MODEL_MODEL uv run pytest \
  tests/test_stage5_router.py \
  tests/test_stage5_activation.py \
  tests/test_stage5_adjust_and_confirm_graph.py \
  tests/test_stage5_agent_api_sse.py

cd backend && uv run pytest
cd backend && uv run ruff check .
cd frontend && npm run build
cd frontend && node scripts/stage5-plans-verify.mjs
git diff --check
git diff --cached --name-only
```

最终结果：全部退出码 0；无暂存；HEAD 仍为 `03221f0`；Stage 1–4 日志未回改。

### 5.3 静态边界核对

- 无 PydanticAI 回流。
- 无 RIR／估算 1RM／训练容量／完成率实现回流。
- `WorkflowState` 仍 11 字段。
- 无新增数据库迁移。
- 计划状态仍为 `draft / active / archived / rejected`。
- 无新增计划时间字段。
- 只新增三个 `/api/agent/*` 端点，无 checkpoint 查询端点。
- `routes_agent.py` 无密钥/Provider 配置回显。
- `refactor-log/stage1.md`–`stage4.md` 与 HEAD 保持一致。
- 独立只读审查各轮均无 P0／P1。

## 6. 主要修改文件

```text
backend/domain/plans/repo.py
backend/domain/plans/rules.py
backend/domain/plans/service.py
backend/graph/router.py
backend/graph/nodes.py
backend/graph/workflow.py
backend/api/routes_agent.py
backend/api/app.py
backend/api/dto.py

backend/tests/test_stage5_router.py
backend/tests/test_stage5_activation.py
backend/tests/test_stage5_adjust_and_confirm_graph.py
backend/tests/test_stage5_agent_api_sse.py

frontend/src/lib/contract.ts
frontend/src/lib/api.ts
frontend/src/features/plans/PlansPage.tsx
frontend/src/app/App.tsx
frontend/scripts/stage5-plans-verify.mjs
```

## 7. Stage 6 交接边界

Stage 5 已保证：

- 生成 → 评估 → 一次修订 → draft → 确认激活 / 拒绝归档全链路；
- adjustment 基于 active 生成新 draft 并复用同一确认链；
- checkpoint 优先 + draft fallback 的幂等确认；
- Agent 三端点 + 五类 SSE + 计划页。

Stage 6 接手：

- 自然语言打卡提取与专用确认，复用既有表单写入；
- 清理剩余旧 Runtime／草稿／Provider／费用代码；
- README、架构图、演示收口。

Stage 6 必须保持：

- `rejected` 语义不变；
- 确认前不修改 active；
- 模型不进入事务；
- 不新增训练阈值或医学规则；
- SSE 不泄漏密钥。