# Fit-Agent LangGraph 重构实施计划

> 状态：已完成  
> 编写日期：2026-09-15  
> 目标岗位：AI Agent 工程师  
> 主需求正本：`../Fit-Agent-LangGraph-重构讨论总结.md`  
> 已确认实施决策：1A（新版使用全新数据库，不迁移旧数据）、2A（继续本地自部署、单用户、仅回环访问）、3C（新建分支后在当前仓库原位重写，以 Git 历史保留旧实现）

## 1. 文档权威与边界

### 1.1 权威顺序

发生冲突时按以下顺序处理：

1. `../Fit-Agent-LangGraph-重构讨论总结.md`；
2. 本轮用户确认的 1A、2A、3C；
3. 当前仓库源码中可复用的业务事实和实现；
4. 旧版 `PLAN.md`、`pre-prj/` 与 `frontend/plans/` 仅供历史溯源，不再约束新版。

本计划不继续执行旧版 PydanticAI 计划。旧版已实现功能不自动算作 LangGraph 新版成果。

### 1.2 重构目标

把当前“PydanticAI + 通用草稿/Run Harness”应用重写为基于 LangGraph 的个性化健身训练助手，跑通以下闭环：

```text
训练与身体数据记录
  → 日历、趋势与 PB 展示
  → 个性化计划生成
  → Evaluator 评估
  → Planner 最多修订一次
  → 用户确认启用
  → 结合后续训练数据调整当前计划
```

### 1.3 首版边界

首版保留：

- 本地自部署、单用户、FastAPI 单体、SQLite；
- 仅监听回环地址，保留 Host/Origin 回环校验；
- 表单打卡直接写入；
- 自然语言打卡解析后确认再写入；
- PB、趋势和单次计划日程状态由确定性代码计算；完成率不在首版范围；
- LangGraph State、SQLite Checkpointer；
- Planner / Evaluator 协作与最多一次修订；
- 计划确认后启用；
- OpenAI 兼容模型端点，API Key 只从环境变量读取；
- SSE 用于 Graph Run 的可见进度和结果传输。

首版删除或不建设：

- PydanticAI 及其适配层；
- RIR 的数据库字段、类型、页面、Prompt、Skill 和测试；
- 通用业务草稿与 `context_version`；
- 旧 Run 状态机、全局单 Run 互斥、Provider Profile、费用预留/账本、多层重试；
- 复杂红旗状态、通用安全 Harness；
- 向量数据库、通用事件总线、后台记忆 Consolidation；
- Agent Swarm、动态角色、A2A、跨 Skill handoff 文件；
- 登录、RBAC、多租户和公网部署；
- 首版 MCP。

## 2. 当前仓库基线

基线 commit：`b54a13fe8933e10d8ae41039ab486a27775c11c8`。编写计划前仓库干净；当前 `git status --short` 仅显示本计划文件 `LANGGRAPH_REFACTOR_PLAN.md` 为未跟踪文件。

### 2.1 已核对的源码事实

| 当前实现 | 源码证据 | 新版处理 |
| --- | --- | --- |
| FastAPI 生命周期、回环校验、静态前端托管 | `backend/api/app.py` | 保留骨架，移除旧 Run、草稿和数据库 Provider 设置装配 |
| 单连接 SQLite、WAL、外键、事务锁 | `backend/storage/db.py` | 保留并精简；继续作为业务事务入口 |
| `PRAGMA user_version` 编号迁移 | `backend/storage/migrations.py` | 保留执行器；新数据库从新版 `001` 开始 |
| 本地数据目录 | `backend/config.py` | 保留 `platformdirs` 和测试覆盖入口；移除 Harness TOML 与旧模型目录依赖 |
| PydanticAI Agent 与通用工具装配 | `backend/runtime/agent_factory.py` | 删除，用 LangGraph 图替换 |
| 费用、预算、压缩、Provider、Run 状态 | `backend/runtime/`、`backend/storage/run_repo.py`、`backend/storage/fee_repo.py` | 不迁移到新版 |
| 时区、Provider 与旧摘要存储 | `backend/storage/setting_repo.py`、`summary_repo.py` | 只迁移 `business_date` 与 IANA 时区校验语义；删除 Provider 数据库存储和旧摘要存储 |
| 通用草稿确认 | `backend/app/*draft*`、`backend/app/confirm.py`、`backend/api/routes_drafts.py` | 删除；只在 `plans` 中保留计划 draft 状态 |
| 训练记录修订模型 | `backend/domain/records/`、迁移 `009` | 业务口径参考；按新版最小表重写并彻底删除 RIR |
| PR 现算 | `backend/domain/stats/service.py`、`repo.py`、迁移 `010/012` | 复用“从有效记录现算”的思想；重写为最大重量、最大次数、最长时长三类 PB；不计算训练容量 PB 或估算 1RM；旧完成率查询不复用 |
| 计划结构与“待校准”类型 | `backend/domain/plan/schema.py`、`service.py` | 可参考负荷互斥表达；删除 RIR、固定 PPL 模板和旧复杂安排语义后重写 |
| 旧计划读取与复盘保存 | `backend/app/plan_reads.py`、`review_store.py` | 前者只参考固定业务日期注入和只读投影分层；后者不属于新版首版表结构，删除 |
| 旧聚合路由与媒体占位 | `backend/api/routes_readonly.py`、`routes_media.py` | 聚合路由由新版 profile/records/stats/plans 路由替换；空媒体路由删除 |
| 旧联调脚本 | `backend/scripts/` | 绑定旧 Runtime/PydanticAI 的脚本删除；新版不重建旧联调脚本 |
| 动作目录与负重口径 | `backend/domain/actions/`、迁移 `002/003/007/013` | 复核后保留稳定动作 ID、负重口径与可用种子；新增 `min_load_increment` |
| React、Router、React Query、UI 组件 | `frontend/src/`、`frontend/package.json` | 保留应用壳和通用 UI；重写 records/review，新增日历和趋势页面 |
| 当前无图表依赖 | `frontend/package.json` | 按需求加入 Recharts，除此之外不新增状态管理或图表依赖 |

### 2.2 不直接复用的历史设计

以下旧能力虽然已有代码和测试，但与新版总结冲突，不能为了减少改动而保留：

- 对话是唯一业务变更入口；
- 所有写入都经通用草稿确认；
- `user_profile.context_version` 全局过期控制；
- `training_sessions` + 完整修订链 + `voided` 终态作为首版必要架构；
- `target_rir`、`rir` 和相关判定；
- 固定 PPL 模板作为计划生成主体；
- Run 费用账本、Provider 数据库存储、Harness 配置与自研历史压缩；
- 服务重启把旧 Run 标记失败的恢复方式。

新版是否保留某段代码，以新版验收条件为准，而不是以旧版测试数量为准。

## 3. 分支与数据库策略

### 3.1 分支策略（3C）

实施开始时：

```bash
git switch -c refactor/langgraph
```

在该分支原位修改 `backend/`、`frontend/` 和测试；不复制 `backend-v2/`，也不把旧代码搬进仓库内的 archive 目录。旧实现由 Git 历史和基线 commit 保留。

每个阶段独立提交，阶段 Gate 未通过不进入下一阶段。建议提交顺序：

```text
refactor: reset dependencies and schema
feat: add workout records and deterministic stats
feat: add calendar and trend dashboard
feat: add langgraph memory and skill loader
feat: add planner evaluator workflow
feat: add plan confirmation and recovery
feat: add natural-language workout confirmation
chore: remove legacy runtime and update docs
```

### 3.2 新数据库策略（1A）

- 新版不读取、不转换旧 `app.db`；
- 新版迁移目录重置为从 `001` 开始的最小 Schema；
- 使用新的数据库文件名，避免新版程序误开旧版 `user_version=16` 数据库；
- 不写旧数据导入脚本；
- README 明确说明旧数据库不兼容，新版首次启动创建新库；
- 测试继续使用临时目录和独立数据库。

建议新版文件名为 `fit_agent_langgraph.db`。这是 1A 的隔离实现，不代表数据迁移承诺。

### 3.3 部署策略（2A）

- 保留 FastAPI 单体、一个 Uvicorn worker 和本地 SQLite；
- 保留 `main.py` 的 `127.0.0.1 / localhost / ::1` 监听白名单；
- 保留 `LoopbackGuardMiddleware`；
- 不新增用户表、登录、权限和租户字段；
- `athlete_profile` 继续用单例行表达当前用户；
- 保留 `business_date(instant, timezone_name)` 和 IANA 时区校验语义；应用 lifespan 启动时用现有 `local_timezone_name()` 采样一次本机时区并注入各用例，不新增 `app_config` 表，也不在领域服务中直接取系统“今天”；
- 删除数据库中的 Provider 设置，API Key、Base URL 和模型名改从环境变量读取；API Key 缺失时允许非模型业务启动，仅在进入模型节点时返回明确的未配置错误；
- 环境变量值不进入日志、响应、Checkpoint 或错误详情。

## 4. 目标代码结构

以下结构只建立当前需求需要的模块，不为未来能力预建接口或工厂：

```text
backend/
├── api/
│   ├── app.py
│   ├── deps.py
│   ├── dto.py
│   ├── routes_profile.py
│   ├── routes_records.py
│   ├── routes_stats.py
│   ├── routes_plans.py
│   └── routes_agent.py
├── domain/
│   ├── actions/
│   ├── profile/
│   ├── records/
│   ├── stats/
│   └── plans/
├── graph/
│   ├── state.py
│   ├── context.py
│   ├── skills.py
│   ├── nodes.py
│   └── workflow.py
├── skills/
│   ├── workout-planning/
│   │   ├── SKILL.md
│   │   └── references/planning-rules.md
│   └── plan-adjustment/
│       └── SKILL.md
├── storage/
│   ├── db.py
│   ├── migrations.py
│   └── migrations/001_initial.sql
├── business_time.py
├── tests/
├── config.py
├── main.py
└── pyproject.toml
```

约束：

- SQL 继续只放在 repo 或迁移文件中；
- 领域规则不依赖 FastAPI、LangGraph 或模型 SDK；
- Graph 节点只编排服务和工具，不复制统计或计划校验规则；
- 模型调用不能出现在数据库事务内部；
- Planner 和 Evaluator 共用一个计划 Pydantic Schema，但使用不同提示词和节点；
- 首版不建立 Planner/Evaluator 基类、Agent 工厂或插件注册中心。

## 5. 新版数据模型实施

总结只冻结了必要表和关键业务关系，没有冻结全部列名。本阶段先用一份 `001_initial.sql` 落实下列已明确事实；未被需求使用的字段不添加。

### 5.1 `athlete_profile`

承载：

- 用户目标；
- 每周可训练次数；
- 可用器械；
- 明确偏好；
- 当前水平；
- 已知伤病；
- 禁用动作。

实现要求：

- 单用户单例行；
- 未填写与明确为空不能被静默混为默认值；
- 禁用动作保存稳定 `exercise_id`，计划前确定性过滤；
- 不再携带 `context_version`。

### 5.2 `exercises`

至少承载：

- 稳定 ID 与名称；
- 负重口径：外加重量或自重；
- 可用器械/动作分类；
- `min_load_increment`；
- 是否可用于计划。

动作种子可从现有目录中筛选复用，但迁移前必须验证每条种子的负重口径和最小加重单位；不能沿用旧数据中的 RIR 或旧计划模板假设。

### 5.3 `body_metrics`

承载体重、体脂及发生日期。写入时由 Pydantic 和领域代码校验日期、数值类型与允许范围；趋势按发生日期查询，不让 LLM参与计算。

### 5.4 `workout_sessions` 与 `workout_sets`

- `workout_sessions` 表示一次训练；
- `workout_sessions.plan_session_id` 为可空外键；
- `NULL` 表示额外训练；首版不聚合计划完成率；
- `workout_sets` 记录动作、负重口径、重量、次数、计时时长和组类型；
- 外加重量动作记录重量与次数，纯自重动作记录次数，计时动作记录 `duration_seconds`；`duration_seconds` 为可空整数，允许范围为不小于 1 的整数（秒），不设业务上限，该范围只由 Domain 唯一校验规则实施、DTO 复用同一规则，迁移不加时长范围 CHECK；
- 组类型固定为 `work / warmup / assisted`；
- RIR 不出现在任何列或 DTO；
- 修改或删除记录后，PB 与趋势从当前有效记录重新查询，不更新统计结果表。

表单关联日程规则：

1. 用户可直接选择未完成的计划日程；
2. 当天恰有一个未完成日程时可以自动关联；
3. 零个或多个候选时不得猜测，要求用户选择或保持额外训练；
4. 同一日程最多被一个有效训练结果计为完成。

删除采用何种物理表达由本阶段 DDL 与用例一起确定，但必须满足“删除后 PB/趋势立即不再读取，且 PB 来源记录可解释”。本计划不额外承诺历史审计系统。

### 5.5 `plans` 与 `plan_sessions`

新版自然日统一由 `business_time.business_date()` 按 lifespan 冻结的本机 IANA 时区计算并由调用方注入；测试直接注入固定日期。不得在 repo、领域服务或 Graph 节点中各自调用 `date.today()`，也不得静默退回 UTC。

`plans` 必须包含：

- `draft / active / archived / rejected` 状态；其中 `rejected` 是 Evaluator 二次评估失败后的终态，不可激活、不得改回 draft，且不新增 rejected 时间字段，`confirmed_at` 与 `archived_at` 保持 `NULL`；
- 单调版本号；
- 可追溯的来源计划，用于调整而不是脱离旧计划重生成；
- 结构化计划内容；
- Evaluator 结果或失败原因所需的最小持久字段；
- 创建、确认或归档时间。

数据库增加部分唯一索引：仅允许一条 `status='active'` 的计划。

`plan_sessions` 必须满足：

- 关联计划版本和计划训练日；
- 保留日程日期、取消状态和单次完成状态；
- 关联有效训练即完成；
- 同一日程最多完成一次；
- 不建立完成率分子、分母、百分比查询或趋势字段；
- 激活新计划时取消旧 active 计划尚未到期的日程，已到期历史保留。

### 5.6 Checkpoint 表

Checkpoint 表由采用的 LangGraph SQLite Checkpointer 管理，使用与业务数据库分开的独立 SQLite 文件（不与业务库同库、不写进任何业务迁移），也不在业务 `001_initial.sql` 中复制一套；路径独立配置，测试用临时文件。`thread_id` 直接使用 conversation id。

确认恢复只有一个提交入口：

1. 优先从 checkpoint 恢复等待确认的 Graph；
2. checkpoint 不存在时，读取数据库中对应的 `plans.status='draft'`；
3. 两条读取路径最终调用同一个事务服务；
4. 服务依据计划当前状态实现幂等，禁止重复激活。

## 6. 确定性业务内核

在接入 LangGraph 前先完成并测试业务内核，确保模型不能改变统计与安全边界。

### 6.1 数据写入

实现普通业务接口：

- 身体指标新增、修改、删除；
- 训练及工作组新增、修改、删除；
- 训练与计划日程关联；
- 用户画像读取与更新。

校验责任：

- DTO/Pydantic：JSON 形状、必填字段、基础类型；
- domain rules：日期、数值范围、动作与负重口径匹配、组类型、日程关联规则；
- SQLite：外键、唯一索引、状态枚举和 active 计划唯一性。

表单路径不经过 Agent、不创建草稿。

### 6.2 PB 查询

建立统一有效工作组查询或数据库 View，只允许：

- 训练记录未删除；
- `set_type='work'`；
- 外加重量动作的重量和次数完整且次数不少于 1；
- 纯自重动作的次数完整且不少于 1；
- 计时动作的 `duration_seconds` 完整且大于 0。

热身、辅助完成和不完整记录必须在统一入口排除，避免三类 PB 各写一套过滤条件。统一入口采用 repo 内共享 SQL，不新增数据库 View。

三类 PB：

| 类型 | 计算口径 |
| --- | --- |
| `weight_pb` | 同动作、同负重口径至少完成 1 次的单组最大实际重量，次数不参与 PB 数值计算 |
| `reps_pb` | 仅用于纯自重动作，取单组最大次数；外加重量动作不记录次数 PB |
| `duration_pb` | 计时动作的单组最长持续秒数 |

每个结果返回来源训练、来源组序号和 `performed_on`；数值并列时来源固定取最早达成者（用户拍板口径 A）——依次比较 `performed_on`、来源训练身份和来源组序号，取更小者，使并列来源排序确定且可测试；重复完成一个等值结果不刷新 PB 的来源与日期。外加重量、纯自重和计时动作不混算。PB 不建结果表，不计算训练容量 PB 或估算 1RM。

### 6.3 单次计划日程状态

首版只保留日历需要的单次状态：计划日程是否取消、是否关联一条有效训练。`workout_sessions.plan_session_id IS NULL` 表示额外训练；同一计划日程最多关联一个有效完成结果。

不实现周/月完成率，不建立分子、分母、百分比 API 或趋势字段。现有 `StatsService.weekly_completion` 不迁移到新版。

### 6.4 `trend_summary`

确定性输出：

- 最近两条有效记录的体重变化；
- 最近两条有效记录的体脂变化；
- 距上次训练天数。

`trend_summary` 不包含训练容量或计划完成率，也不评价进步、退步或停滞。趋势图默认展示最近 30 天；数据不足时返回 `no_data` 或 `insufficient_data` 等明确状态，不补 0、不伪造变化值。

力量趋势不属于 `trend_summary`：按动作返回截至各日期的累计 PB（历史最好成绩，因此曲线不下降），默认窗口沿用趋势图最近 30 天口径；外部负重只取截至该日期的最大重量，不生成次数 PB 趋势；纯自重取单组最大次数，计时动作取单组最长秒数。后端计算并通过 Stats API 暴露，Stage 2 前端不展示该曲线。

同一底层查询同时服务看板和 MemoryAssembler。每个指标用固定数据库样本测试可复算，不让模型估算。

### 6.5 计划渐进规则

实现为领域函数和 Evaluator 的确定性校验器：

- 起始负荷只取最近一次有效工作组；
- 没有有效历史时为“待校准”，不得出现具体重量；
- 同一负荷连续两次完成全部目标组且达到次数上限，下一次只按 `min_load_increment` 递增；
- 连续两次未达到次数下限，回退到最近一次完整完成的负荷；
- 没有可回退负荷时重新变为“待校准”；
- 禁用动作不得进入计划。

不从 PB 反推日常训练重量，不把 PB 当疲劳指标，不声明能判断主观疲劳。

### 6.6 急性关键词与禁用动作

急性关键词沿用旧源码 `backend/domain/profile/safety.py::MESSAGE_RED_FLAG_TERMS` 的 10 项封闭词表：

```text
胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛、
疼痛持续加重、明显肿胀、卡锁、关节失稳
```

只对当前请求做精确子串检查。任一命中即进入 `safety_stop`，不得调用 Planner 或生成计划，并提示咨询专业人员。该检查不做医学诊断、同义词扩展或否定语义分析，因此“没有麻木”会被保守拦截。

禁用动作只来自用户长期画像中明确保存的稳定 `exercise_id`：

1. Planner 前从候选动作中确定性排除禁用 ID；
2. Evaluator 对计划输出再次检查，若仍包含禁用 ID则评估失败；
3. 系统不得根据伤病名称自动推导禁用动作，也不得让 LLM 决定硬过滤结果。

## 7. 前端重构

### 7.1 可直接保留的基础

- Vite、React、TypeScript、Tailwind；
- React Router 与 TanStack Query；
- `components/ui/` 通用组件；
- 主题与侧边栏基础；
- API 错误处理模式；
- FastAPI 静态托管构建产物。

### 7.2 必须重写或新增的页面

1. **记录页（重写）**：从只读列表改为表单新增、修改和删除；移除 RIR、旧修订链和“通过对话更正”的文案。
2. **数据看板（新增，替换旧 ReviewPage 的统计职责）**：展示月历、单次计划日程状态、最近 30 天体重趋势、体脂趋势和三类 PB；不展示训练容量、估算 1RM 或完成率；引入 Recharts。力量趋势由后端按截至各日期的累计 PB 计算并通过 Stats API 暴露，Stage 2 前端不展示该曲线，也不新增动作选择交互。
3. **计划页（新增）**：展示 active 计划、draft 评估结果、确认/拒绝按钮和历史版本。
4. **对话页**：保留自然语言记录、趋势解释、计划生成/调整入口；删除通用 DraftCard，改为计划确认或训练解析确认的专用 UI。
5. **设置页**：删除数据库 API Key 编辑能力；改为只展示环境变量是否已配置，绝不回显值，或在首版直接移除该页面。

### 7.3 日历和图表边界

- 前端只展示后端返回的统计，不重算 PB 或 `trend_summary`；
- 力量趋势由后端计算并暴露，Stage 2 看板不展示该曲线，也不新增动作选择交互；
- 月历区分训练日、休息日和单次计划完成状态；该状态重构完成后再评估是否删除；
- 图表无数据时显示空态，不用零值补线；
- 修改或删除记录成功后，失效记录、PB、趋势、日历和计划日程状态相关 Query；
- 首版不开发拖拽排程和完整计划编辑器。

## 8. Memory 与 Skill 实施

### 8.1 Graph State

State 只承载当前工作流需要的数据：

- conversation/thread id；
- 当前请求和已判定 intent；
- MemoryAssembler 输出；
- 已加载 Skill 正文；
- 计划草稿；
- Evaluator 结果；
- 修订次数；
- 确认状态；
- 终止原因。

State 不复制完整数据库历史，不保存 API Key，不把 PB 或趋势重新计算一遍。

### 8.2 MemoryAssembler

每次严格读取：

```text
用户长期画像
当前 active 计划
最近 4 次训练
相关动作最新 PB（生成新计划时读取全部已有动作 PB；调整计划时只读取当前 active 计划涉及动作的 PB）
确定性 trend_summary
当前请求
```

实现要求：

- 单个服务负责装配，Planner 和调整流程共用；
- 业务事实每次从 SQLite 读取；
- 历史聊天超过阈值后可由 Graph 摘要节点压缩；
- 聊天摘要只保存对话语义，不代替业务表；
- 用固定数据库输入对装配字段和数量做精确断言。

首版先完成固定范围装配；聊天摘要只在上下文确实达到阈值后实现，不移植旧 `runtime/compression.py`。

### 8.3 Skill Loader

启动时扫描 `backend/skills/*/SKILL.md`，只解析 frontmatter 中的 `name` 和 `description`。Router 命中任务后，`load_skill` 再读取完整正文和明确引用的 reference 文件。

首版只实现：

- `workout-planning`；
- `plan-adjustment`。

每个 Skill 明确：

- 可使用的工具；
- 需要的记忆字段；
- 禁止事项；
- 结构化输出 Schema；
- 训练知识来源。

不复制 `Lzheng-fitness-ref.md` 中的七 Skill 交接体系，只参考目录格式、渐进披露、来源限定和固定输出契约。

## 9. LangGraph 工作流实施

### 9.1 Router

先用确定性规则识别：

- 表单记录；
- 自然语言记录；
- 查看进步；
- 生成计划；
- 调整计划。

只有无法判断时才调用模型分类。Router 是节点/函数，不包装成第三个 Agent。

### 9.2 计划子图

节点及责任：

| 节点 | 责任 | 禁止事项 |
| --- | --- | --- |
| `load_context` | 调用 MemoryAssembler | 不生成计划、不修改数据库 |
| `load_skill` | 按 intent 加载完整 Skill | 不加载全部 Skills |
| `safety_check` | 按 §6.6 的 10 项词表检查当前请求；按稳定 ID 过滤画像中的禁用动作 | 不诊断、不扩展词表、不让 LLM 决定硬过滤 |
| `planner` | 按统一 Schema 生成或基于当前计划调整 draft | 不写 active、不猜无历史负荷 |
| `evaluator` | 调用确定性校验器，再按模型 Rubric 检查目标、频率和解释 | 不自行修改计划 |
| `revise_once` | 把失败理由交回 Planner | `revision_count >= 1` 时不得再循环 |
| `wait_for_confirmation` | 保存 draft 并 checkpoint，等待用户确认/拒绝 | 不自动激活 |
| `activate_plan` | 调用唯一计划激活事务 | 不在 Graph 节点内散写多张表 |
| `archive_draft` | 用户拒绝时归档 draft | 不修改原 active |
| `reject_draft` | 二次评估失败，写入 `rejected` 终态记录并结束 | 不产生可激活计划；用户拒绝仍走 `archive_draft` |
| `safety_stop` | 返回停止计划生成及专业咨询提示 | 不输出训练计划 |

Evaluator 分两层：

1. 确定性检查负荷来源、次数区间、递增条件、训练频率和禁用动作；
2. 模型 Rubric 只检查目标匹配、安排合理性和解释质量。

### 9.3 确认与事务

激活事务按以下顺序执行：

1. 读取 draft，并确认仍为 `draft`；
2. 再跑一次确定性计划校验；
3. 归档当前 active 计划；
4. 取消旧计划尚未到期的 `plan_sessions`；
5. 激活新计划并建立其日程；
6. 提交事务。

任一步失败整体回滚。数据库部分唯一索引作为最后防线。重复确认读取到已 active 的同一计划时返回既有结果；读取到 archived/rejected draft 时拒绝重新激活。

### 9.4 SSE

复用 FastAPI `StreamingResponse` 的 SSE 方式，但事件来源改为 LangGraph stream。只发送产品事件：

- 节点/阶段状态；
- 可见文本；
- 等待确认；
- 完成或失败。

不发送隐藏推理、API Key、完整系统提示词或原始模型事件。SSE 断线不作为取消或提交信号；客户端通过 thread id 查询当前计划 draft/checkpoint 状态恢复页面。

## 10. 自然语言打卡

自然语言路径单独使用“解析确认”，不复活通用草稿系统：

1. 模型提取日期、动作、负重口径、重量、次数、计时时长、组数和候选计划日程；
2. Pydantic 校验结构；
3. 领域规则校验动作、日期、数值和关联歧义；
4. 前端回显结构化摘要；
5. 用户确认后调用与表单相同的训练写入服务；
6. 返回写入结果和重新查询的 PB。

用户修改解析结果时重新走同一校验。未确认、解析失败或日程存在歧义时不写库。

## 11. 实施阶段与 Gate

> 阶段 0–6 全部完成；各阶段结果与 Gate 取证见 `refactor-log/stage-0.md`–`refactor-log/stage6.md`。

### 阶段 0：建立重构基线

任务：

- 从基线 commit 创建 `refactor/langgraph`；
- 保存旧版可运行命令和基线测试结果；
- 更新 `pyproject.toml`：删除 PydanticAI，加入 LangChain/LangGraph、SQLite Checkpointer 所需依赖；
- 清理旧 Harness 配置入口，但暂不边删边改业务逻辑；
- 确认新版数据库文件名与环境变量名写入 README。

Gate：应用可以在没有旧 `app.db` 的临时目录启动并创建空新版数据库；API Key 不在日志中出现。

### 阶段 1：最小 Schema 与表单写入

任务：

- 重置迁移为 `001_initial.sql`，其中一次建立 `plans` 和 `plan_sessions`；
- 实现 profile、exercise、body metric、workout session/set 的 repo、rules、service；
- 实现 plans/plan_sessions 的 repo 和只读服务；计划创建、确认与激活写入留到阶段 5；
- 新建 `backend/business_time.py`，从 `setting_repo.py` 迁出 `business_date` 与 IANA 时区校验，由 lifespan 冻结本机时区并注入各用例；
- 新增表单 API；
- 删除所有 RIR 类型、列和 DTO；
- 改造记录页面支持新增、修改、删除。

Gate：固定输入可完成训练和身体数据 CRUD；非法日期、重量、次数、时长、组数和缺字段被拒绝；全仓搜索无 RIR 业务字段残留。

### 阶段 2：PB、单次日程状态、趋势与看板

任务：

- 通过 `002` 迁移增加计时组字段、新增平板支撑和前水平计时动作，并新增独立的负重引体动作；负重引体记录外加重量且 `min_load_increment=5kg`；
- 实现 repo 内共享 SQL 形式的统一有效工作组查询，不新增数据库 View；
- 实现最大重量、最大次数、最长时长三类 PB 和来源信息；
- 实现日历需要的单次计划日程状态，不实现完成率；计划状态落在 `scheduled_on`，实际训练标记落在 `performed_on`，跨日时分别展示；
- 实现不含训练容量和完成率的 `trend_summary`；
- 新增月历及最近 30 天体重、体脂 Recharts 趋势图；力量趋势只由后端按截至各日期的累计 PB 计算并通过 Stats API 暴露，Stage 2 前端不展示该曲线、不新增动作选择交互。

Gate：修改或删除记录后查询立即一致；热身/辅助组不刷新 PB；额外训练不关联计划日程，同一计划日程最多完成一次。此阶段测试用 fixture 直接写入 active 计划和日程，正式创建/激活入口留到阶段 5。

### 阶段 3：Memory、Checkpoint 与 Skill Loader

任务：

- 定义最小 Graph State；
- 接入 SQLite Checkpointer；
- 实现 MemoryAssembler；
- 建立两个核心 Skill；
- 测试启动只加载元数据、命中后加载正文。

Gate：固定数据库输入下，MemoryAssembler 只输出总结规定的六类内容；active 计划由 fixture 直接写入；重启后可定位等待确认的 thread/draft。正式创建/激活入口留到阶段 5。

### 阶段 4：Planner / Evaluator 计划链路

任务：

- 定义统一计划 Pydantic Schema；
- 实现 §6.6 已确认的 10 项急性关键词精确子串检查；
- 实现负荷来源和渐进规则校验；
- 完成 Planner、Evaluator、一次修订与失败终止；
- 保存 draft、`rejected` 终态与评估结果（`rejected` 不新增时间字段）。

Gate：急性伤病命中不进入 Planner；禁用动作不出现；无历史不生成具体重量；二次评估失败没有可激活计划。

### 阶段 5：确认启用与计划调整

任务：

- 实现确认、拒绝和激活事务；
- 新增计划页面；
- 调整流程强制读取当前 active 计划和最新数据；
- 实现 Checkpoint 主恢复、数据库 draft 兜底和统一提交入口；
- 接入 LangGraph stream → SSE。

Gate：任意时刻最多一个 active 计划；拒绝、重复确认、并发确认或事务失败均不破坏原计划；调整结果保留未受影响的旧计划内容。

### 阶段 6：自然语言记录与收口

任务：

- 实现自然语言结构化提取与专用确认；
- 复用表单写入服务；
- 删除剩余旧 Runtime、草稿、Provider 设置和费用代码；
- 更新 README、架构图、启动方式和真实完成边界。

Gate：未确认不写库；解析歧义不自动关联计划；后端测试、前端构建和人工闭环演示全部通过。

### 投递截止时的降级顺序

如时间不足，按以下顺序延后，不增加基础设施：

1. `fitness-onboarding`、`workout-review`；
2. 聊天历史摘要；
3. 自然语言打卡；
4. 非核心图表和页面美化。

必须保留：表单打卡、PB、日历/至少三类趋势、MemoryAssembler、两个核心 Skill、Planner/Evaluator、一次修订和确认启用。

## 12. 测试计划

### 12.1 确定性单元/集成测试

最小必测：

- 工作组、热身组、辅助组和不完整组的 PB 过滤；
- 最大重量、纯自重最大次数、最长时长三类 PB 的数值、适用动作、来源与日期；
- 累计 PB 力量趋势按日期单调不下降，且可由固定数据库输入复算；
- 纯自重引体与独立负重引体不混算，负重引体只把外加重量计为重量 PB；
- 记录修改/删除后的 PB 与趋势重算；
- 单次计划日程与有效训练的关联、额外训练分离和最多完成一次；
- 最近负荷、连续两次达标加重、连续两次未达标回退、无基线待校准；
- `min_load_increment` 精确递增；
- active 计划部分唯一索引；
- 激活事务成功、回滚、拒绝和重复提交；
- API 输入边界和 API Key 不泄漏。

### 12.2 Graph 行为测试

使用固定模型或节点替身，不依赖真实模型随机性验证硬规则：

- Router 确定性命中不调用模型；
- Skill 启动只加载元数据；
- MemoryAssembler 上下文范围；
- 急性伤病命中直接 `safety_stop`；
- Evaluator 失败只修订一次；
- 二次失败进入 `reject_draft`；
- 用户拒绝归档 draft 且原 active 不变；
- 用户确认后新计划 active、旧计划 archived；
- Checkpoint 缺失时 draft 兜底不会重复提交。

### 12.3 人工功能验收

真实模型检查不作为本计划的 Gate；由项目负责人在投递前逐功能人工验收以下固定案例：

- 有历史的计划生成；
- 无历史的“待校准”；
- 禁用动作过滤；
- 一次评估修订；
- 基于当前计划的调整；
- 自然语言打卡解析和确认。

人工验收只确认结构化输出与业务不变量，不恢复旧版费用账本或正式评测平台。

### 12.4 执行命令

实际命令以更新后的 `pyproject.toml` 和 `package.json` 为准，至少保留：

```bash
cd backend && uv run pytest
cd backend && uv run ruff check .
cd frontend && npm run build
```

## 13. 删除清单

对应替代实现通过阶段 Gate 后删除，禁止长期保留双路径：

- `backend/agent_core/`；
- `backend/runtime/`；
- `backend/app/draft_repo.py`、`drafts.py`、`confirm.py` 与各类 `*_drafts.py`；
- `backend/api/routes_drafts.py`、旧 `routes_chat.py`、`routes_settings.py`、`routes_readonly.py`、空的 `routes_media.py`；
- `backend/app/plan_reads.py`、`review_store.py`；新版计划读取在 `domain/plans` 重建，不保留旧 arrangement/context_version 语义；
- `backend/storage/run_repo.py`、`fee_repo.py`、`summary_repo.py`；删除 `setting_repo.py` 的 Provider/数据库设置职责，只把纯函数 `business_date` 的时区语义迁入 `business_time.py`；
- `backend/scripts/` 中绑定旧 Runtime/PydanticAI 的脚本；
- 旧 migrations `001`–`016`，由新版 `001_initial.sql` 替换；
- 与旧 Run、通用草稿、费用、RIR、复杂安全和 PydanticAI 绑定的测试；
- 前端 `DraftCard.tsx`、通用草稿字段组件、旧 Provider 设置交互及只读更正文案；`frontend/src/features/review/ReviewPage.tsx` 在新版数据看板接管统计职责后删除，不保留旧复盘双路径。

删除前必须确保新版对应 Gate 已通过；不保留兼容层。

## 14. 最终验收

### 14.1 业务验收

- [x] 表单可新增、修改和删除训练/身体数据；
- [x] 自然语言打卡先回显日期、动作、重量、次数、计时时长、组数和关联日程，确认前不写入；
- [x] 月历、最近 30 天体重、体脂、力量趋势和最大重量/最大次数/最长时长 PB 可演示；
- [x] 修改或删除训练记录后，PB 与趋势查询立即一致；热身组、辅助组不得刷新 PB；
- [x] 无有效训练历史或无可回退负荷时，计划不得生成具体重量；
- [x] 同一负荷连续两次完成全部目标组且达到次数上限后，才能按 `min_load_increment` 递增；未满足条件不得加重；连续两次未达到次数下限时按规则回退；
- [x] 额外训练不关联计划日程；同一计划日程最多完成一次；系统不计算或展示完成率；
- [x] 计划可以生成、评估、至多修订一次、确认或拒绝；
- [x] 数据库任意时刻最多一条 active 计划；
- [x] 后续调整读取当前计划和最新数据，不脱离旧计划重新生成；
- [x] 用户拒绝或二次评估失败时，原 active 计划保持不变。

### 14.2 技术验收

- [x] 使用 LangGraph State 与 SQLite Checkpointer；
- [x] Planner / Evaluator 是两个独立节点和提示词职责；
- [x] 固定输入下，MemoryAssembler 只输出用户画像、当前 active 计划、最近 4 次训练、相关动作最新 PB、确定性 `trend_summary` 和当前请求；生成新计划时读取全部已有动作 PB，调整计划时只读取当前 active 计划涉及动作的 PB；
- [x] `trend_summary` 的最近两条体重/体脂记录变化和距上次训练天数均可由固定数据库输入复算；数据不足时返回明确状态，且不包含训练容量或完成率；
- [x] 启动时只注入 Skill 的 `name` 和 `description`，任务命中后才加载正文；
- [x] PB、趋势和单次计划日程状态全部由确定性代码计算，LLM 只解释结果；
- [x] 当前请求精确命中 §6.6 任一关键词时不得进入 Planner；画像中的禁用动作 ID 不得出现在计划中；
- [x] Checkpoint 恢复与数据库 draft 兜底统一进入同一幂等提交服务，不得双路径提交；
- [x] API Key 只来自环境变量且不回显、不记录；缺失时不阻塞非模型业务启动；
- [x] 每个 Graph Run 有请求次数和超时上限，但无费用账本；
- [x] 全仓无 PydanticAI 与 RIR 运行时代码；
- [x] 后端测试、lint 和前端 build 通过。

### 14.3 简历口径验收

只有代码、测试和演示均存在的模块才能从“重构方案”改写为“已实现”。README 必须明确：

- 实际完成范围；
- 未完成项；
- 架构图和启动方式；
- 固定行为测试结果。

## 15. 已知风险与止损

| 风险 | 最小处理 |
| --- | --- |
| 边改旧库边保留双路径导致语义混乱 | 新分支原位重写；新版 Gate 通过即删除旧路径，不做兼容层 |
| 旧数据库被新版误开 | 新数据库文件名；检测到旧库不自动迁移 |
| 模型输出绕过业务规则 | 所有计划输出先过统一 Pydantic Schema 和确定性校验器 |
| Planner/Evaluator 无限循环 | State 中只允许一次修订，第二次失败终止 |
| 确认恢复重复激活 | Checkpoint/DB 只负责定位，最终统一进入幂等事务服务和唯一索引 |
| 统计过滤规则分散 | 建立一个有效工作组查询/View，所有 PB 和趋势复用 |
| 工期不足 | 先保留表单、PB、图表和一条计划链路，按第 11 节顺序延后 |
| 健身安全范围被夸大 | 只做封闭急性关键词拦截和已知禁用动作过滤，不进行诊断或疲劳推断 |

## 16. 实施前检查

开始编码前只需确认以下执行事实，不再扩写设计文档：

- [x] 当前位于基线 commit 创建的 `refactor/langgraph` 分支；
- [x] 旧版测试结果已留档；
- [x] 新数据库文件名和环境变量名已写入 README；
- [x] `001_initial.sql` 只包含总结要求的业务表；
- [x] 第一阶段测试能够在临时数据库独立运行。

满足后按第 11 节逐阶段实施。任何与总结冲突的新需求先回写重构讨论总结，再修改本计划，避免出现两个需求正本。
