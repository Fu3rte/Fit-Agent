# Stage 4：Planner / Evaluator 计划链路开发计划

> 状态：**已完成**——Subtask 01–05 全部实施并按 §11 逐项留证（见 §6）；权威文档已同步加入 `rejected`；生产源码、迁移、Graph、测试与前端共享契约（`frontend/src/lib/contract.ts:179` 补 `rejected`）均已改动但未提交（无暂存、无提交）。Gate 全通过，见 §10。
> 当前分支：`refactor/langgraph`；HEAD：`ef97055 feat: complete stage 3 memory checkpoint and skills`；计划成稿时工作区基线干净。
> 权威顺序：`Fit-Agent-LangGraph-重构讨论总结.md` > `LANGGRAPH_REFACTOR_PLAN.md` > 已合入源码。Stage 3 完成证据只以 `refactor-log/stage3.md` 为基线（其 Gate：236 passed、Ruff、构建、`git diff --check` 通过；本轮制定计划未复跑）。
> §6 逐 Subtask 先列任务与验收，再列实施证据（源码／测试／验证）；本文件是 Stage 4 的唯一正本（原先的 `refactor-log/stage4子任务/subtask-0N-*.md` 分解文件已删除，其任务与验收已并入 §6）。

## 1. 当前基线与权威依据

### 1.1 已完成基线（Stage 3 交付，Stage 4 直接复用）

- `backend/graph/state.py::WorkflowState`：11 个冻结字段；`conversation_id` 即 Checkpointer `thread_id`；`draft_plan`／`evaluation` 留给 Stage 4 定形。
- `backend/graph/context.py::MemoryAssembler`：只装配画像、当前 active 计划、最近 4 次训练、相关 PB、确定性 `trend_summary`、当前请求六类信息。
- `backend/graph/skills.py::SkillLoader`：启动只读 Skill 的 `name`/`description`，命中后才加载正文与显式 reference。
- `backend/graph/checkpointer.py`：独立 SQLite `AsyncSqliteSaver`，与业务数据库分离。
- `backend/skills/workout-planning/`、`backend/skills/plan-adjustment/`：两个核心 Skill 已存在；Skill 不是领域规则实现者。
- `backend/domain/profile/safety.py`：10 项急性关键词封闭词表与精确子串纯函数。
- `backend/domain/profile/`：画像已有三态 `weekly_frequency` 与稳定 `forbidden_exercise_ids`。
- `backend/domain/actions/`：动作目录已有稳定 ID、`record_type`、负重口径、`min_load_increment_kg`、`recommendable`。
- `backend/domain/records/`、`backend/domain/stats/`：训练记录、有效工作组、PB、趋势与计划日程关联事实已存在。
- `backend/domain/plans/`：目前只有只读计划行/日程行能力，`structured_content` 与 `evaluator_result` 仍是不透明 JSON。

依据：讨论总结 §4–§10、§14；总计划 §4、§6、§8、§9、§11「阶段 4」；`refactor-log/stage3.md` §3–§10。

### 1.2 关键源码事实

| 事实 | 源码依据 |
| --- | --- |
| 计划状态只有 `draft / active / archived`（成稿时） | `backend/storage/migrations/001_initial.sql:72-90`；`backend/domain/plans/schema.py:19-21` |
| `plans` 已有 `version/source_plan_id/structured_content/evaluator_result` | `backend/storage/migrations/001_initial.sql:72-88` |
| plans repo/service 全部只读 | `backend/domain/plans/repo.py::PlanRepo`；`backend/domain/plans/service.py::PlanReadService` |
| State 已有 `draft_plan_id/draft_plan/evaluation/revision_count/termination_reason` | `backend/graph/state.py::WorkflowState` |
| 终止原因已有 `safety_stop/archive_draft/reject_draft` | `backend/graph/state.py::TerminationReason` |
| 10 项词表与精确子串检查已实现 | `backend/domain/profile/safety.py::MESSAGE_RED_FLAG_TERMS/message_red_flag_hits` |
| 周频率值域是 1–7 | `backend/domain/profile/rules.py::WEEKLY_FREQUENCY_MIN/MAX` |
| 记录口径是负重次数/自重次数/计时 | `backend/domain/actions/schema.py::RecordType` |
| 有效工作组全量查询已存在 | `backend/domain/stats/repo.py::StatsRepo.list_valid_work_sets` |
| Checkpoint 用独立文件，thread ID 直接用 conversation ID | `backend/graph/checkpointer.py::open_checkpointer/thread_config` |
| 模型环境变量名已存在，生产源码尚无模型调用 | `backend/config.py:17-19`；`backend/pyproject.toml` |

## 2. Stage 4 范围及明确不做

### 2.1 本阶段交付

1. Planner 与 Evaluator 共用的统一 Pydantic 计划 Schema。2. Evaluator 结果 Pydantic Schema。3. 与动作目录记录口径一致的三类处方判别联合。4. 当前请求的 10 项急性关键词安全分流。5. Planner 前按画像稳定 `exercise_id` 过滤禁用动作，Evaluator 再检查输出。6. 独立 Planner 节点与独立 Evaluator 节点、不同提示词职责。7. 负荷来源、次数/时长区间、频率、禁用动作、渐进与回退的确定性领域校验器。8. 首次失败最多交回 Planner 修订一次，二次阻断失败终止。9. `draft`、`rejected` 与 Evaluator 结果的最小业务持久化。10. 通过后的 `wait_for_confirmation`：先提交业务 draft，再 checkpoint/interrupt，interrupt 只携带 `draft_plan_id`。11. 复用 Stage 3 `WorkflowState`、MemoryAssembler、SkillLoader 与 SQLite Checkpointer。12. 固定模型/节点替身行为测试，不依赖真实模型或 API Key。13. 更新最高权威总结与总计划，使新增 `rejected` 先成为正式依据。

### 2.2 本阶段明确不做

- 用户确认后的激活事务；归档旧 active 计划；正式启用后的 `plan_sessions`。
- 用户拒绝后的 `archive_draft` 事务；checkpoint 缺失时业务 draft 的兜底提交；幂等确认接口。
- 完整 Intent Router（Stage 4 直接以 `intent='generate_plan'` 驱动计划子图）；调整计划执行链路（属 Stage 5；本阶段只保证统一 Schema、领域规则与 MemoryAssembler 接口可被复用）。
- Agent HTTP 路由、SSE、前端确认页面；自然语言打卡；onboarding/review Skill。
- Planner/Evaluator 基类、Agent 工厂、插件注册中心、Agent Swarm、复杂 A2A。
- RIR、估算 1RM、训练容量、完成率、主观疲劳、医学诊断、新训练阈值。
- 旧 Run Harness、通用草稿系统、旧 README 历史设计。

## 3. 已确定契约

### 3.1 统一计划 Schema

Pydantic 只用于结构化数据校验；已删除的是 PydanticAI，不是 Pydantic。

```text
PlanDraft: goal: str; starts_on: date; explanation: str; weekly_frequency: int; training_days: tuple[TrainingDay, ...]
TrainingDay: scheduled_on: date; exercises: tuple[PlannedExercise, ...]
PlannedExercise: exercise_id: str; sets: int; prescription: WeightedReps | BodyweightReps | Timed
WeightedRepsPrescription: type='weighted_reps'; reps_min/reps_max: int; load: KnownLoad | NeedsCalibration; progression_note: str | None
BodyweightRepsPrescription: type='bodyweight_reps'; reps_min/reps_max: int; progression_note: str | None
TimedPrescription: type='timed'; duration_seconds_min/duration_seconds_max: int; progression_note: str | None
KnownLoad: status='known'; weight_kg: float; source_workout_session_id: int; source_set_no: int
NeedsCalibration: status='needs_calibration'
```

共同结构要求：

- 未声明字段一律拒绝，不保留模型自由扩展字段；`goal`、`explanation`、`exercise_id` 必须是非空文本。
- `starts_on` 是计划七天窗口的第一天；`weekly_frequency` 必须复用画像值（1–7）；`training_days` 数量必须恰好等于 `weekly_frequency`；每个 `scheduled_on` 唯一且位于 `[starts_on, starts_on+6]`。
- 每个训练日至少一个动作；同一训练日不得重复同一 `exercise_id`；`sets` 是正整数，不新增权威依据之外的上限。
- `source_plan_id` 不进入计划内容，只保存在 `plans.source_plan_id`；首次生成为 `NULL`。
- 次数沿用已合入记录规则的 1–100（`min <= max`）；计时时长沿用已合入规则（不小于 1 秒、无业务上限，`min <= max`）。
- 判别联合必须与目录动作 `record_type` 一致：`reps_weight ↔ weighted_reps`、`reps_bodyweight ↔ bodyweight_reps`、`time ↔ timed`；自重和计时处方不得携带重量或负荷来源。
- `progression_note` 只是可选解释，不代替确定性渐进规则。
- `KnownLoad` 的重量与来源必须同现，来源指向该动作最近一次有效工作组；`NeedsCalibration` 不得携带具体重量或伪造来源；重量复用现有记录值域与一位小数精度，不从 PB 反推日常训练重量。

### 3.2 画像缺失

`weekly_frequency` 为 `unknown` 或 `denied` 时：Planner 前明确失败并要求补充；不调用 Planner；不生成 draft/rejected 业务记录；不用默认频率或模型猜测。同理，统一 Schema 必需的事实无法从画像/当前请求获得时，不得由模型补默认值。

### 3.3 安全与禁用动作

安全检查只复用 `message_red_flag_hits(current_request)`：

- 词表固定 10 项：胸部异常不适、晕厥、异常气短、锐痛、麻木、放射痛、疼痛持续加重、明显肿胀、卡锁、关节失稳。
- 只做精确子串（「没有麻木」仍保守命中）；不扩词表、不做同义词、否定语义、医学诊断或多层安全状态。
- 命中后直接 `safety_stop`，不进入 Skill、Planner、计划 Schema、持久化或模型调用。

禁用动作：只取画像 `forbidden_exercise_ids` 的 `known` 值（`unknown/denied` 不补造 ID）；Planner 前从 `recommendable` 候选中确定性删除；Evaluator 确定性层再次检查全部计划动作 ID，出现即阻断失败；不根据 `known_injuries` 文本推导新禁用动作。

### 3.4 负荷、渐进与回退

- **起始负荷**：最近一次任意有效工作组（不要求关联计划日程），排序沿用确定性业务日期、训练身份、组序号，不使用 PB。
- **渐进历史（10B）**：只使用关联当前 active 计划 `plan_sessions` 的训练；`plan_session_id IS NULL` 的额外训练既不计入，也不打断连续性。
- Stage 4 只在领域函数与固定 fixture 中完成渐进/回退规则；调整计划的 Graph 接线留 Stage 5。

负重动作规则：

1. 没有有效起始历史时只能 `NeedsCalibration`。
2. 最近两次关联计划训练在同一目标负荷完整完成全部目标组，且每个目标组均达次数上限 → 下一次只增加一次目录 `min_load_increment_kg`。
3. 最近两次关联计划训练均存在「目标组数不足，或任一目标组低于次数下限」（10B-1）→ 回退到当前 active 计划关联历史中最近一次完整完成的负荷。
4. 没有可回退的完整完成负荷时变为 `NeedsCalibration`。
5. 同一次训练按 `set_no` 取计划要求数量的目标 work 组；额外 work 组不改变该次计划目标是否完成。
6. 目标 work 组负荷不一致时，不得算作在同一负荷完整达标。
7. 热身、assisted、不完整组不参与。
8. 自重和计时动作本阶段不发明次数/时长递增阈值，只校验处方结构和记录口径。

### 3.5 Evaluator 分层与结果 Schema

执行顺序固定：统一 Pydantic Schema → 确定性领域校验 → 确定性通过后才调用模型 Rubric。

确定性层检查：Schema 与字段互斥；计划日期窗口与训练频率；动作存在、`recommendable`、记录口径与处方类型；禁用动作；负荷来源与待校准；次数/时长区间；`min_load_increment_kg`；连续两次达标加重、连续两次未达标回退、无可回退待校准。

模型 Rubric：`goal_alignment`（阻断项，检查计划与已知目标是否匹配，不重算业务事实）；`schedule_reasonableness`（阻断项，检查七天内安排是否合理，不替代代码的频率/日期检查，不引入新硬阈值）；`explanation_quality`（建议项，失败只进入 warning，不触发修订、不阻止进入等待确认）。

```text
EvaluationResult: passed: bool; deterministic{passed: bool; failures: tuple[RuleFailure, ...]};
  rubric{goal_alignment, schedule_reasonableness, explanation_quality: RubricVerdict};
  blocking_failures: tuple[str, ...]; warnings: tuple[str, ...]; revision_count: int
RuleFailure: code: str; message: str; exercise_id: str | None
RubricVerdict: passed: bool; reason: str
```

总体通过条件：`deterministic.passed AND rubric.goal_alignment.passed AND rubric.schedule_reasonableness.passed`。不使用数值评分、维度权重或总分阈值——没有文献提供本项目三项 Rubric 的通用权重/阈值，运动处方与 LLM Rubric 研究更支持分解式布尔判据（调研索引见 §11）。

### 3.6 一次修订闭环

```text
Planner 首次候选 → Evaluator
  ├─ 通过 → 持久化 draft → wait_for_confirmation
  └─ 阻断失败且 revision_count == 0 → 结构化失败理由交回 Planner → revision_count = 1 → Evaluator
       ├─ 通过 → 持久化 draft → wait_for_confirmation
       └─ 阻断失败 → reject_draft
```

`revision_count >= 1` 时不得再回 Planner；`explanation_quality` 单独失败不算阻断失败、不触发修订；模型配置缺失、超时、调用失败或非法结构是运行基础设施错误，不消耗修订次数、不创建 rejected 业务记录；一个新用户请求可以启动新的 Run，但每个 Run 仍只允许一次修订。

### 3.7 模型调用契约

- 节点构造时注入 callable/model；生产传 OpenAI 兼容 `ChatOpenAI`，测试传固定替身；不建立 Planner/Evaluator 基类、Agent 工厂或 Provider 注册中心。
- `MODEL_API_KEY`、`MODEL_BASE_URL`、`MODEL_MODEL` 进入模型节点前必须均存在，缺失返回明确配置错误；API Key／Base URL／模型名不得进入日志、响应、State、业务库或 checkpoint。
- 单次模型请求超时 60 秒；单次 Graph Run 超时 180 秒；单次 Run 最大模型请求数 5（当前无 Router 的拓扑最多实际调用 4 次：Planner、Evaluator、Planner 修订、Evaluator）。
- 模型调用不在数据库事务中发生；请求预算放在每次 workflow invocation 的运行上下文，不新增 WorkflowState 字段、不持久化 Provider 配置。

### 3.8 draft、rejected 与版本

状态扩为 `draft → active → archived` 与 `draft候选 → rejected`；必须先同步最高权威总结和总计划，再实现源码。

- 任意时刻最多一个 `active`（既有部分唯一索引继续保证）；任意时刻最多一个可确认 `draft`（`003` 增加 draft 部分唯一索引作为最后防线）。
- 新业务记录在事务内取 `max(version)+1`；`version` 仍为非空唯一整数。
- `rejected` 是终态：不得激活、不得改回 draft；不新增 `rejected_at`，也不复用 `archived_at`（`confirmed_at=NULL`、`archived_at=NULL`，终态由 `status='rejected'` 和失败 `evaluator_result` 表达）。
- rejected 进入计划版本列表与按 ID 查询，但不进入 draft 列表、active 查询或日历 active 计划；原 active 在 Stage 4 全程不修改。

### 3.9 首次生成与已有 draft 的区别

候选内容先保存在 State，终态确定后才写业务库；评估期间不暴露未经通过的可确认 draft。

| Run 开始时 | 结果 | 业务写入 |
| --- | --- | --- |
| 无已有 draft（`draft_plan_id=None`） | 通过 | 插入一条 draft，保存最终 Evaluator 结果 |
| 无已有 draft | 二次阻断失败 | 插入一条 rejected，保存最终候选和失败结果 |
| 已有 draft，普通生成请求 | — | 直接返回已有 draft，不调用模型 |
| 已有 draft，显式重新生成 | 新候选通过 | 在同一 draft ID/version 上原子替换内容和 Evaluator 结果 |
| 已有 draft，显式重新生成 | 新候选二次失败 | 原 draft 不变；失败候选只留本次 State/checkpoint |

安全替换要求：生成和评估均在事务外完成；只有新候选完全通过后，才以 `WHERE id=? AND status='draft'` 在一个短事务内替换 `structured_content/evaluator_result`；条件更新失败时明确报冲突，不覆盖已变化状态；Stage 4 只提供并测试显式「重新生成」运行模式，自然语言识别和 SSE 驱动留后续阶段。

### 3.10 wait_for_confirmation 边界

顺序固定：最终候选通过 → 业务事务创建或安全替换 draft 并保存 `evaluator_result` → State 写入 `draft_plan_id` → `interrupt({'draft_plan_id': id})` → Checkpointer 保存等待位置。

- interrupt 载荷只携带 `draft_plan_id`，不复制完整计划、评估结果或业务事实。
- checkpoint 写入失败时业务 draft 保留，供 Stage 5 的数据库 fallback 使用；Stage 4 不实现该 fallback。
- 不自动激活、不归档原 active、不创建计划日程；只通过内部 workflow 与行为测试驱动，不增加临时 HTTP/SSE。

## 4. 已确认决策

成稿时本阶段所需产品/API/Schema/持久化/模型调用决策均已确认，无剩余阻塞项。

| 决策 | 结果 |
| --- | --- |
| 计划结构 | Pydantic 判别联合；顶层目标、开始日期、解释、周频率、具体日期训练日 |
| 计时处方 | 时长下限/上限区间（1B-1） |
| 来源计划 | 只使用 `plans.source_plan_id` |
| 负荷 | `KnownLoad / NeedsCalibration` 判别联合 |
| Evaluator | 分层结构化结果（4A） |
| draft | 单用户最多一个；已有时先返回；显式重新生成安全替换（5A/5A-1） |
| 二次失败 | 新增 `rejected` 状态，不新增时间字段（6C/6C-1） |
| Router | Stage 4 不实现，直接运行生成计划子图（7A） |
| 模型限制 | 60 秒/请求、180 秒/Run、最多 5 次（8B） |
| 计划周期 | 从 `starts_on` 起连续 7 天（9A） |
| 渐进历史 | 只用关联当前 active 计划日程的训练（10B）；任一目标组低于下限或组数不足即未达标（10B-1） |
| 持久化顺序 | 业务 draft 先提交，再 interrupt；只携带 draft ID（11A） |
| Rubric | 目标/安排为硬门槛，解释质量为 warning（12A-2） |
| 模型注入 | 节点注入 callable/model（1A） |
| 模型错误 | 基础设施错误终止，不消耗修订、不创建 rejected（2A） |
| 周频率缺失 | Planner 前终止并要求补充（3A） |

实施期另裁定三项（详见 §6）：**Q1=A** checkpoint 内容按冻结 State 设计解释，不调整 `WorkflowState`；**Q2=A** 不新增「普通请求 vs 显式重新生成」的冻结信号；**Q3=(b)** 不同 thread 测试用例取 A=draft／B=`reject_draft`／C=未使用。

## 5. 拟修改文件及职责

### 5.1 权威文档

| 文件 | 计划职责 |
| --- | --- |
| `Fit-Agent-LangGraph-重构讨论总结.md` | 将计划状态扩为含 `rejected`；同步二次失败、数据模型、验收与「不产生可激活计划」的精确表达 |
| `LANGGRAPH_REFACTOR_PLAN.md` | 同步 `rejected`、Stage 4 持久化、测试和阶段边界；不得改变其余阶段范围 |
| `refactor-log/stage4.md` | 实施时逐项回写源码/测试/Gate 证据；不把计划勾选冒充已完成 |

历史 `refactor-log/stage1.md`–`stage3.md` 不回改；它们保留当时真实实施边界。

### 5.2 数据与领域层

| 文件 | 计划职责 |
| --- | --- |
| `backend/storage/migrations/003_rejected_plan_status.sql` | 安全重建 plans 状态 CHECK；保留数据/FK/索引；增加单 draft 部分唯一索引；不增加 `rejected_at` |
| `backend/storage/migrations/README.md` | 登记 `003` 的状态和索引变化 |
| `backend/domain/plans/schema.py` | 定义统一计划 Schema、Evaluator Schema、`rejected` 状态与 JSON 编解码；读库后不再把结构化内容当任意 `Any` |
| `backend/domain/plans/rules.py`（新增） | 纯领域确定性校验：目录匹配、日期/频率、禁用动作、负荷来源、渐进与回退；不得依赖 FastAPI/LangGraph/模型 SDK |
| `backend/domain/plans/repo.py` | 参数化 SQL：唯一 draft 读取、终态插入、安全替换、rejected 持久化；SQL 只在 repo/迁移 |
| `backend/domain/plans/service.py` | 编排目录/画像/训练/计划事实与短事务；模型调用不进入本服务事务 |

可直接复用而非复制：`backend/domain/profile/safety.py`、`backend/domain/profile/service.py`、`backend/domain/actions/service.py`、`backend/domain/records/service.py`、`backend/domain/stats/repo.py::list_valid_work_sets`、`backend/domain/plans/PlanReadService`。需要读取计划关联训练时，优先以现有 `WorkoutRecordsService.list_all()` 在领域服务内有界连接 `workout_session_id → plan_session_id`；不得复制有效工作组 SQL，也不为首版新增统计 View。

### 5.3 Graph 与模型层

| 文件 | 计划职责 |
| --- | --- |
| `backend/graph/nodes.py`（新增） | `safety_check/safety_stop/validate_required_profile/load_context/load_skill/planner/evaluator/revise_once/persist_draft/wait_for_confirmation/reject_draft` 节点；只编排领域服务与注入模型 |
| `backend/graph/workflow.py`（新增） | 编译生成计划子图、条件边、一次修订上限、Run limits、Checkpointer；不包装 Router/第三 Agent |
| `backend/graph/model.py`（新增，最小） | 读取并验证三个模型环境变量，创建一个具体 OpenAI 兼容模型调用入口；不建 Provider/Agent 工厂或注册中心 |
| `backend/config.py` | 只补充非敏感的 60/180/5 固定限制或读取入口；不把密钥写入响应/日志/State |
| `backend/graph/state.py` | 原则上不改；11 字段已足够。若实施发现必须加字段，停止并重新询问用户 |
| `backend/graph/context.py` | 原则上不改；生成计划继续 `exercise_ids=None`；调整计划接线留 Stage 5 |
| `backend/graph/skills.py` | 原则上不改，只由节点调用既有 `load` |
| `backend/api/app.py` | 原则上不增加 Agent 路由；只复用既有 checkpointer 生命周期 |

### 5.4 传输和前端共享契约

| 文件 | 计划职责 |
| --- | --- |
| `backend/api/dto.py` | 继续透传计划状态和结构化 Schema；不增加 rejected 时间字段 |
| `frontend/src/lib/contract.ts` | 将状态联合扩为 `draft | active | archived | rejected`；同步统一计划/Evaluator 只读类型所需最小字段，保证 build。**已实施（审计发现漏项后补齐，owner 拍 A）**：`contract.ts:179` 补 `rejected`，见 §6 Subtask 05 证据末条 |

Stage 4 不新增或修改计划页面。

### 5.5 测试

```text
backend/tests/test_stage4_plan_schema_and_rules.py
backend/tests/test_stage4_plan_persistence.py
backend/tests/test_stage4_workflow.py
backend/tests/test_stage4_migration.py
```

按职责合并为四个文件，不为形式制造更多拆分；必要时只定点更新已有 plans/API/frontend 契约测试；不得删除 Stage 1–3 仍有效的回归测试。

## 6. 子任务与实施证据

所有 Subtask 单写者、顺序实施；前一项验收未通过不得进入下一项。每个 Subtask 先列**任务**与**验收**（编号即为任务/验收条号），再列实施证据。

### Subtask 01：同步权威与冻结 Stage 4 契约

交付：两份权威文档正式加入 `rejected`；§3 已确认契约冻结为代码前断言；历史 stage 日志不回写；未接触生产模型或 Graph 编排。

任务：① 先更新最高权威总结、再更新总计划，正式加入 `rejected`；② 把 §3 已确认字段／状态／一次修订／10B-1／12A-2／模型限制／持久化顺序转录为代码前契约；③ 确认历史 `refactor-log/stage1.md`–`stage3.md` 不回改；④ 在 Stage 4 测试中先写字段集合、状态集合与禁止字段断言。
验收：① 两份权威文档对状态与 Stage 4/5 边界无冲突；② `rejected` 只表示 Evaluator 二次阻断失败，用户拒绝仍在 Stage 5 走 `archive_draft`；③ 无 `rejected_at`／RIR／1RM／容量／完成率等未授权字段；④ 未接触生产模型或 Graph 编排。

- [x] 计划状态集合冻结为四态，且在 `Fit-Agent-LangGraph-重构讨论总结.md`（§3.3／§9／§12.2）与 `LANGGRAPH_REFACTOR_PLAN.md`（§5.5／§9.2／§11「阶段 4」）一致、无三状态残留 → `tests/test_stage4_plan_schema_and_rules.py::test_plan_status_enumeration_is_four_states_across_both_authorities`
- [x] `rejected` 为终态：不可激活、不得改回 draft、不新增 `rejected_at`、不进 draft 列表／active 查询／日历 active 计划（讨论总结 §3.4；总计划 §5.5）→ `::test_rejected_status_is_terminal_and_carries_no_timestamp_field`（`backend/tests/test_stage4_plan_schema_and_rules.py:1059`；原名 `test_rejected_is_terminal_without_timestamp_and_hidden_from_draft_queries`，其「不进 draft 列表／active 查询／日历 active 计划」半部分已移交 Subtask 03 的 repo／service 测试，见 §6 Subtask 02 末条）
- [x] `rejected` 只表示 Evaluator 二次阻断失败；用户拒绝留在 Stage 5 走 `archive_draft`（讨论总结 §3.3／§3.4；总计划 §9.2）→ `::test_user_refusal_routes_to_archive_draft_not_rejected`
- [x] 未授权字段与口径（`rejected_at`／RIR／估算 1RM／训练容量／完成率）只出现在否定语境（讨论总结 §7／§7.1／§9；总计划 §5.5；本文件 §2.2／§8.1）→ 当时为 `::test_unauthorized_fields_only_appear_in_prohibitions`；该测试已在 Subtask 02 按 A+D 决定删除，由 `backend/tests/test_stage4_plan_schema_and_rules.py:389::test_unauthorized_field_payloads_are_rejected_by_schema` 取代（见 §6 Subtask 02 末条）
- [x] §3 已确认决策（5A 唯一可确认 draft／一次修订闭环／10B-1／12A-2／8B 60·180·5／11A 先提交再 interrupt）在本文件保持原文可查（本文件 §2.1／§3.6／§3.7／§3.8／§3.10／§4）→ `::test_stage4_confirmed_decisions_are_present_in_the_stage_plan`
- [x] 本 Subtask 未接触生产模型／Graph 编排，历史 stage1–stage3 日志不回改 → `git status --short`（当时仅两份权威文档 ` M` 与新增 `backend/tests/test_stage4_plan_schema_and_rules.py`、本文件、`refactor-log/stage4子任务/` 为 `??`；该目录后已删除，任务与验收并入本文件 §6）

收尾 Gate（真实结果）：`cd backend && uv run pytest` 241 passed；`uv run ruff check .` All checks passed；`git diff --check` 无输出；`tests/test_stage4_plan_schema_and_rules.py` 当时为 5 passed。

### Subtask 02：统一 Schema、确定性规则与 `003` 迁移

交付：统一 Pydantic Schema（§3.1）、Evaluator 结果 Schema（§3.5）、纯领域 `rules.py`、`003` 迁移、迁移 README 登记与三个测试文件对应断言；未接触节点、Graph、持久化服务与确认入口。

任务：① 定义 §3.1／§3.5 的计划与 Evaluator Pydantic Schema；② 新建纯领域 `rules.py`（目录匹配、日期／频率、禁用动作、负荷来源、渐进回退）；③ 建立 `003_rejected_plan_status.sql`（保留数据／FK／single-active，CHECK 加入 `rejected`，加 single-draft 部分唯一索引，不加 `rejected_at`）；④ 完成 10B／10B-1 纯函数规则，不接调整 Graph；⑤ 补 Schema／规则／迁移测试。
验收：① 三类处方字段严格互斥且与目录 `record_type` 一致；② 计时是区间且下限 ≥1、无业务上限；③ 七天窗口／唯一日期／频率精确校验；④ 无历史时只能待校准、有历史时来源精确指向最近有效工作组；⑤ PB 数值即使更大也不能作为负荷来源；⑥ 10B-1 的连续两次达标／未达标／回退／无基线路径均由固定事实复算；⑦ 2→3 迁移保留数据、FK、索引，非法状态仍失败，active/draft 唯一索引生效；⑧ 领域模块不导入 FastAPI／LangGraph／LangChain／OpenAI SDK。

- [x] 统一计划 Schema：未声明字段一律拒绝、非空文本、七天窗口、唯一训练日期、训练日数量等于每周训练次数 → `backend/domain/plans/schema.py::PlanSchemaModel`（`extra="forbid"` + `strict=True`，`:101-109`）、`::PlanDraft`（`:223-256`）、`::PLAN_WINDOW_DAYS`（`:60`）、`::NonEmptyText`／`::BusinessDate`（`:78/98`）；`tests/test_stage4_plan_schema_and_rules.py::test_seven_day_window_unique_dates_and_frequency_are_exact`
- [x] 三类处方字段严格互斥，判别联合与目录 `record_type` 精确匹配 → `schema.py::Prescription`（判别键 `type`，`:190-193`）、`::WeightedRepsPrescription`／`::BodyweightRepsPrescription`／`::TimedPrescription`（`:157/164/170`，只有外加负重处方携带 `load`）、`backend/domain/plans/rules.py::RECORD_TYPE_BY_PRESCRIPTION`（`:45-49`）、`::validate_plan_draft`（`:223`）；`::test_three_prescription_kinds_are_mutually_exclusive`、`::test_catalog_record_type_and_recommendable_matching`
- [x] 次数区间复用已合入 1–100；计时时长区间下限至少 1 秒且无业务上限 → `schema.py::RepsPrescription`（`REPS_MIN`/`REPS_MAX`，`:140-155`）、`::TimedPrescription`（只设 `ge=DURATION_SECONDS_MIN`、无上限，`:170-185`），复用 `backend/domain/records/rules.py` 既有口径；`::test_reps_and_duration_ranges_reuse_recorded_facts`
- [x] 负荷结构：重量与来源同现、`NeedsCalibration` 不携带重量、重量复用记录值域与一位小数精度 → `schema.py::KnownLoad`（`:118-131`）、`::NeedsCalibration`（`:112-115`）、`::Load`（`:137`）；`::test_three_prescription_kinds_are_mutually_exclusive`
- [x] 起始负荷只取最近一次有效工作组（不读 PB），无有效历史时只能待校准 → `rules.py::resolve_starting_load`（`:81-110`，按 `performed_on`→`workout_session_id`→`set_no` 取最近）、`::_load_source_failures`（`:303`）；`::test_starting_load_is_the_most_recent_valid_work_set`、`::test_larger_personal_best_never_becomes_the_starting_load`、`::test_without_valid_history_only_needs_calibration`
- [x] 10B／10B-1 纯函数渐进与回退（两次达标只加一档、两次未达标回退、无基线待校准、额外训练不计数、混合负荷不算达标）→ `rules.py::resolve_progression`（`:152-219`）、`::_assess_training`（`:122-150`）、`::ProgressionDecision`（`:57`）、`::InvalidPlanRule`（`:52`）；`::test_two_completed_trainings_at_target_load_increment_once`、`::test_single_completed_training_does_not_increment`、`::test_extra_unlinked_training_neither_counts_nor_breaks_continuity`、`::test_insufficient_target_sets_or_below_min_counts_as_failed`、`::test_two_failed_trainings_regress_to_last_completed_load`、`::test_failed_trainings_without_completed_history_become_needs_calibration`、`::test_extra_work_sets_do_not_change_target_judgement`、`::test_bodyweight_and_timed_prescriptions_carry_no_load_progression`
- [x] 禁用动作只取画像 `known` 值并在 Planner 前确定性删除，Evaluator 确定性层复验输出；`known_injuries` 不推导新禁用 ID → `rules.py::known_forbidden_exercise_ids`（`:64-71`）、`::filter_forbidden_exercises`（`:73-79`）、`::validate_plan_draft`（`:223`），安全词表复用 `backend/domain/profile/safety.py`；`::test_forbidden_exercises_are_removed_before_planning_and_rejected_in_output`、`::test_known_injuries_never_derive_forbidden_ids`、`::test_acute_red_flag_vocabulary_is_reused_without_extension`
- [x] Evaluator 分层结果 Schema：`passed` 恰为「确定性层通过且两个硬门槛通过」的合取，解释质量只进 warnings → `schema.py::EvaluationResult`（`:290-318`）、`::DeterministicResult`／`::RuleFailure`／`::RubricResult`／`::RubricVerdict`（`:260/268/275/282`）；`::test_evaluation_result_passed_matches_the_two_hard_gates`、`::test_explanation_quality_failure_stays_a_warning`、`::test_plan_and_evaluation_json_codecs_round_trip`
- [x] rejected 状态迁移（2 → 3）→ `backend/storage/migrations/003_rejected_plan_status.sql`：`status CHECK IN ('draft','active','archived','rejected')`（`:39`）、`source_plan_id` 自引用（`:41`）、`idx_plans_single_active` 重建（`:52`）、新增 `idx_plans_single_draft`（`:54`）、`plan_sessions` 行与 `workout_sessions.plan_session_id` 关联原样回填（`:56-79`）、无 `rejected_at`、`PRAGMA defer_foreign_keys=ON` 在迁移执行器事务内生效；`tests/test_stage4_migration.py::test_upgrade_from_002_keeps_plan_data_and_sets_user_version_three`、`::test_rebuilt_plans_keeps_foreign_keys_indexes_and_identity_sequence`、`::test_rejected_status_is_accepted_and_other_statuses_are_rejected`、`::test_single_active_and_single_draft_partial_indexes_are_enforced`、`::test_plans_has_no_rejected_timestamp_and_no_new_tables`、`::test_upgrade_is_repeatable_and_does_not_overwrite_data`
- [x] 迁移状态与索引变化已登记在迁移说明 → `backend/storage/migrations/README.md:5`；`tests/test_stage4_migration.py::test_plans_has_no_rejected_timestamp_and_no_new_tables`
- [x] 用真实 Schema 断言接手 Subtask 01 的文档文本扫描（A+D 决定）→ `schema.py::PlanSchemaModel`（`extra="forbid"` + `strict=True`）与各模型字段集合；`::test_unauthorized_field_payloads_are_rejected_by_schema`（取代已删除的 `test_unauthorized_fields_only_appear_in_prohibitions`；`UNAUTHORIZED_TERMS`／`NEGATION_MARKERS`／否定语境启发式一并移除、全仓无引用）、`::test_frozen_model_field_sets_reject_undeclared_fields`、`::test_plan_status_enumeration_is_four_states_across_both_authorities`（四态 literal + 两份权威一致性断言保留）、`::test_rejected_status_is_terminal_and_carries_no_timestamp_field`（「不进 draft 列表／active 查询／日历 active 计划」的查询可见性已移交 Subtask 03 repo/service 测试）、`::test_user_refusal_routes_to_archive_draft_not_rejected`、`::test_stage4_confirmed_decisions_are_present_in_the_stage_plan`（无代码等价物，保留）
- [x] 领域模块不导入 FastAPI／LangGraph／LangChain／OpenAI SDK → `schema.py`、`rules.py`（仅依赖 Stdlib、Pydantic 与既有领域模块）；`::test_domain_modules_import_no_agent_or_web_frameworks`（AST 遍历 `backend/domain/**.py`）
- [x] 既有 Stage 1–3 回归测试按新状态集合定点更新（语义不变）→ `backend/tests/test_langgraph_stage0.py:51-52`、`backend/tests/test_stage1_data_base.py:117`（`user_version=3`）、`backend/tests/test_stage2_migration.py:110-116`（只含 001／002 的临时迁移目录，仍验证 001 → 002 与 `user_version=2`）、`backend/tests/test_stage1_api_records.py:36-45`、`backend/tests/test_stage1_domain_records.py:85-92`、`backend/tests/test_stage3_checkpoint.py:285-287`

收尾 Gate（真实结果）：`tests/test_stage4_plan_schema_and_rules.py` 28 passed；`tests/test_stage4_migration.py` 6 passed；`cd backend && uv run pytest` 270 passed；Ruff All checks passed；`git diff --check` 无输出；`git status --short` 仅四个新增测试/源码文件与迁移（`??`）及本阶段预期改动（` M`）；`git diff --cached --name-only` 无输出。

### Subtask 03：计划持久化与原 active 保护

交付：业务库侧最小持久化——`PlanRepo` 的事务内版本分配、draft／rejected 终态插入、draft 条件替换与原 active 快照；`PlanPersistenceService` 按 §3.9 编排四种写入并守护原 active。未接 Graph／节点／模型／确认／激活入口，未改迁移、统一 Schema、前端与 API。

任务：① 事务内版本分配与 draft／rejected 写入；② 无现有 draft：通过写 draft、二次失败写 rejected；③ 有现有 draft：普通请求直接返回、不调模型；④ 显式重新生成：候选只留 State，通过后条件更新同一 draft，失败不改原 draft；⑤ 保存统一 Evaluator JSON，rejected 的两个时间字段保持 NULL；⑥ 所有写路径验证原 active 的 ID／状态／内容不变。
验收：① 任意时刻最多一个 draft、一个 active；② 条件更新失败时明确冲突、不覆盖数据；③ 二次失败写入 rejected 且不可被 draft 查询读出；④ 重新生成失败不建第二 draft／rejected、不破坏原可确认 draft；⑤ 模型调用不在业务事务内；⑥ 失败事务完整回滚。

- [x] 事务内分配 `max(version)+1` 并插入 draft／rejected → `backend/domain/plans/repo.py::PlanRepo._append_new_version_in_transaction`（`COALESCE(MAX(version),0)+1` 与 INSERT 同一事务）、`::write_draft_in_transaction`、`::write_rejected_in_transaction`；`tests/test_stage4_plan_persistence.py::test_passing_first_generation_writes_draft_with_next_version`、`::test_second_blocking_failure_without_draft_writes_rejected_with_null_timestamps`
- [x] 无现有 draft：通过写 draft；二次阻断失败写 rejected（`confirmed_at`／`archived_at` 为 NULL）→ `backend/domain/plans/service.py::PlanPersistenceService._persist_passing`、`::_persist_blocking_failure`；`repo.py::_INSERT_NEW_VERSION`（两个时间字段写死 NULL）；同上两个测试
- [x] 已有 draft 的普通请求直接返回该 draft，不调用模型 → `service.py::PlanPersistenceService.get_unique_draft`；`repo.py::PlanRepo.read_draft`；`::test_existing_draft_normal_request_returns_it_without_new_rows`；本层无模型依赖另由 `::test_persistence_service_depends_on_no_model_or_graph_sdk` 的静态导入断言覆盖
- [x] 显式重新生成通过后同 ID/version 安全替换；二次失败不改原 draft、不写 rejected → `repo.py::PlanRepo.replace_draft_in_transaction`（`WHERE id=? AND status='draft'`，只改内容与评估结果）、`service.py::PlanPersistenceService._persist_passing`／`::_persist_blocking_failure`；`::test_explicit_regeneration_replaces_same_id_and_version`、`::test_regeneration_blocking_failure_keeps_original_draft_and_writes_nothing`
- [x] 条件更新未命中时明确冲突、不覆盖数据 → `service.py::PlanDraftConflict`、`::PlanPersistenceService._persist_passing`（`replace_draft_in_transaction` 返回 None 即抛）；`::test_replace_miss_reports_explicit_conflict`
- [x] 保存统一计划 JSON 与 Evaluator JSON；rejected 不进 draft 列表／active 查询 → `service.py::PlanPersistenceService.persist_plan_result`；`backend/domain/plans/schema.py::plan_draft_to_json`／`::evaluation_result_to_json`；`repo.py::PlanRepo.read_draft`／`read_active`；`::test_passing_first_generation_writes_draft_with_next_version`、`::test_second_blocking_failure_without_draft_writes_rejected_with_null_timestamps`、`::test_rejected_row_is_hidden_from_draft_and_active_reads`
- [x] 所有写路径验证原 active 的 id／状态／版本／内容／确认时间与行数不变 → `repo.py::ActivePlanSnapshot`、`::PlanRepo.read_active_snapshot_in_transaction`；`service.py::PlanPersistenceService._require_active_unchanged`；本文件全部 8 个 DB 持久化测试均调 `_assert_active_unchanged`（`::test_failed_write_rolls_back_completely` 等）
- [x] 写失败完整回滚 → `service.py::PlanPersistenceService._persist_passing`（`Database.transaction()` 包裹）；single-draft 唯一索引兜底；`::test_failed_write_rolls_back_completely`
- [x] 模型调用不在业务事务内（本层无模型依赖）→ `service.py`（只依赖 `PlanRepo` 与 `Database.transaction()`，不导入任何模型 SDK）；`::test_persistence_service_depends_on_no_model_or_graph_sdk`

收尾 Gate（真实结果）：`tests/test_stage4_plan_persistence.py` 9 passed；`cd backend && uv run pytest` 279 passed, 1 warning（唯一 warning 为 `starlette/testclient.py:53` 的 `anyio.abc.BlockingPortal` DeprecationWarning，与本案新增文件无关）；Ruff All checks passed；`git diff --check` 无输出；`git status --short` 为 ` M backend/domain/plans/repo.py`、` M backend/domain/plans/service.py`、`?? backend/tests/test_stage4_plan_persistence.py`（另含前序无提交改动与并发出现的 `?? refactor-log/stage3-architecture-visualizer.html`）；`git diff --cached --name-only` 无输出。

### Subtask 04：Planner/Evaluator 节点与一次修订图

交付：生成计划子图的三个模块（`backend/graph/nodes.py`、`backend/graph/workflow.py`、`backend/graph/model.py`）、`backend/config.py` 的 60／180／5 固定上限，与主测文件 `backend/tests/test_stage4_workflow.py`。拓扑固定为 `safety_check`（命中→`safety_stop`）→ `validate_required_profile` → `load_context` → `load_skill` → `planner` → `evaluator`（通过→`persist_draft`→`wait_for_confirmation`；阻断且 `revision_count=0`→`revise_once`→`evaluator`；阻断且 `revision_count>=1`→`reject_draft`）。

任务：① 建立直接以 `intent='generate_plan'` 驱动的计划子图，不实现 Router；② 按上列节点顺序接线；③ `safety_check` 必须在 Skill／MemoryAssembler／Planner 前；④ Planner 只接收确定性过滤后的候选动作与六类上下文；⑤ Evaluator 先运行领域校验、只有通过才调用模型 Rubric；⑥ Planner／Evaluator 使用不同系统提示词，Evaluator 不修改计划；⑦ 用依赖注入的固定 callable/model 测试，真实模型入口只做环境与边界接线；⑧ 配置／超时／非法响应作为运行错误终止，不走 revise／reject。
验收：① 10 项关键词逐项与「没有麻木」均直接 safety stop，普通酸痛等非词表文本不误命中；② 安全命中时 Planner／Evaluator／Skill／持久化调用计数均为 0；③ 禁用动作 Planner 前不可见、伪造输出仍被 Evaluator 拒绝；④ 确定性失败不调用模型 Rubric；⑤ 阻断失败最多修订一次、第二次进入 rejected；⑥ 仅 explanation warning 时不修订、进入等待确认；⑦ 整个固定测试不需要 API Key。节点只编排既有领域能力（安全复用 `backend/domain/profile/safety.py`；禁用过滤／起始负荷／确定性校验复用 `backend/domain/plans/rules.py`；结构与 Evaluator 结果复用 `backend/domain/plans/schema.py`；写入复用 `backend/domain/plans/service.py`）。未实现 Router、调整计划链路、确认／拒绝／激活、`archive_draft`、checkpoint 兜底与幂等确认；`backend/graph/state.py`、`context.py`、`skills.py`、`checkpointer.py` 未改动。本块源码行号已在 parent-audit 修复后重新核实，`tests/test_stage4_workflow.py` 的结果均为修复后复跑的当前结果（新增 2 个参数化用例：37 passed）。

- [x] 安全分流在 Skill／MemoryAssembler／Planner 前：10 项封闭词表逐项与「没有麻木」都直接 `safety_stop`，命中时 Planner／Evaluator／Skill／持久化调用计数均为 0，也不写业务库 → `nodes.py::GeneratePlanNodes.safety_check`（`message_red_flag_hits` 唯一调用点，`:198`）、`::GeneratePlanNodes.safety_stop`（`:212`）；`workflow.py::_route_after_safety_check`（`:56`）、`::build_generate_plan_graph`（`:76`，`START → safety_check`）；`::test_red_flag_hits_stop_before_skill_memory_planner_or_persistence`（11 个参数化用例）、`::test_multiple_hits_stop_and_keep_the_closed_vocabulary_order`
- [x] 非词表文本不误命中（普通肌肉酸痛／关节异响），继续走计划链路 → 同 `safety_check`／`safety_stop`；`::test_non_vocabulary_soreness_does_not_stop_the_plan_path`（Planner／Evaluator 各 1 次、`termination_reason` 显式为 None，终态进入等待确认）
- [x] 同一 thread 的多次 Run 各自决定路由与修订预算：上一次 Run 存档里的 `safety_stop`／`reject_draft` 与已用掉的修订不参与本次 Run（§3.6）→ `nodes.py::GeneratePlanNodes.safety_check`（未命中分支显式写 `termination_reason=None`）、`::GeneratePlanNodes.planner`（首个候选写常量 `revision_count=0`，`revise_once` 是唯一写 1 的地方）；`workflow.py::_route_after_safety_check`、`::_route_after_evaluator`；`::test_second_run_on_the_same_thread_is_not_terminated_by_the_previous_run`、`::test_revision_budget_is_per_run_on_the_same_thread`（Planner 共 4 次、`revision_count=1`、rejected 1 条 ＋ draft 1 条）；两用例在修复前实测断言失败
- [x] 不包装 Router／第三个 Agent：直接以 `intent='generate_plan'` 驱动，节点集合就是本 Subtask 拓扑 → `workflow.py::NODE_NAMES`（`:41`）、`::build_generate_plan_graph`（`:76`）；`::test_graph_is_the_direct_generate_plan_subgraph_without_a_router`（节点集合精确相等，`safety_check` 为入口）
- [x] 禁用动作只取画像 `known` 值并在 Planner 前确定性删除；`known_injuries` 文本不推导新禁用 ID → `nodes.py::GeneratePlanNodes._candidate_actions`（`:350`，复用 `backend/domain/plans/rules.py` 的 `filter_forbidden_exercises`／`known_forbidden_exercise_ids`）；`::test_planner_payload_carries_six_context_categories_and_filtered_candidates`、`::test_known_injuries_never_derive_new_forbidden_ids`
- [x] Planner 只接收确定性过滤后的候选动作与六类上下文（六类字段名即读取清单），Skill 只加载命中的那一个 → `nodes.py::GeneratePlanNodes._planner_payload`（`:341`）、`::load_context`（`:221`，`exercise_ids=None`）、`::load_skill`（`:232`）；`::test_planner_payload_carries_six_context_categories_and_filtered_candidates`（载荷键集合、候选 ID 集合、起始负荷全部 `needs_calibration`）
- [x] 伪造输出里的禁用 ID 仍被 Evaluator 确定性层阻断，且不调用模型 Rubric → `nodes.py::GeneratePlanNodes.evaluator`（`:253`，复用 `rules.validate_plan_draft`）；`::test_forged_forbidden_exercise_is_blocked_by_the_deterministic_layer`（`forbidden_exercise:` 阻断项、`evaluator_calls == []`、终态 `rejected`）
- [x] Evaluator 分层：确定性校验先跑，只有通过才调用模型 Rubric；确定性失败不调用 Rubric → `nodes.py::GeneratePlanNodes.evaluator`（`:253`）、`::_rubric_not_run`（`:421`）；`::test_blocking_deterministic_failure_revises_once_then_persists_draft`（首轮 Planner 1 次／Evaluator 0 次）、`::test_forged_forbidden_exercise_is_blocked_by_the_deterministic_layer`
- [x] 两个硬门槛（目标匹配、安排合理性）是阻断项；解释质量失败只进 warnings，总体仍通过 → `nodes.py::_evaluation_result`（`:435`，`passed` 恰为确定性层与两个硬门槛的合取）、`::PLANNER_SYSTEM_PROMPT`／`::EVALUATOR_SYSTEM_PROMPT`（`:79/92`）；`::test_second_blocking_failure_rejects_without_waiting`（`goal_alignment`／`schedule_reasonableness` 两个参数）、`::test_explanation_warning_only_does_not_revise_and_still_waits`
- [x] 确定性校验失败时不伪造 Rubric 结论：阻断项与 warning 只来自真实确定性失败，`RubricResult` 的三个「未运行」判定仍显式保留（§3.5 分层）→ `nodes.py::_evaluation_result`（`:435`，`rubric_ran=False` 时不追加未运行判定）、`::_rubric_not_run`（`:421`）、`::GeneratePlanNodes.evaluator`（`:270`，`rubric_ran = deterministic.passed`）；`::test_forged_forbidden_exercise_is_blocked_by_the_deterministic_layer`（`blocking_failures` 逐项等于确定性失败投影、无两个 Rubric 前缀、`warnings == ()`、三个未运行判定仍 `passed=False`、理由含「未调用模型 Rubric」、落库 `evaluator_result` 同口径）、`::test_blocking_deterministic_failure_revises_once_then_persists_draft`（交回 Planner 的 `revision.evaluation` 同口径）；修复前逻辑实测失败（逐字照抄旧公式替换运行期 `graph.nodes._evaluation_result` 后同一断言抛 `AssertionError`）
- [x] Planner／Evaluator 使用不同系统提示词，且提示词携带统一 Schema 字段名 → `nodes.py::PLANNER_SYSTEM_PROMPT`（`:79`）、`::EVALUATOR_SYSTEM_PROMPT`（`:92`）；`::test_planner_and_evaluator_use_distinct_system_prompts`（记录的系统提示词集合恰为两者）
- [x] 通过路径：先提交业务 draft，再 `interrupt({'draft_plan_id': id})` 等待确认，不自动激活、不归档原 active → `nodes.py::GeneratePlanNodes.persist_draft`（`:307`）、`::wait_for_confirmation`（`:324`）；`workflow.py::_route_after_evaluator`（`:66`）；`backend/domain/plans/service.py::PlanPersistenceService.persist_plan_result`；`::test_first_pass_persists_draft_then_waits_and_models_run_outside_transactions`（interrupt 载荷恰为 `{'draft_plan_id': id}`、`snapshot.next == ('wait_for_confirmation',)`、业务 draft 已提交、State 只有冻结字段、原 active 不变）
- [x] 阻断失败最多修订一次：`revision_count == 0` 才回 Planner（结构化理由交回），`revision_count >= 1` 直接终态 → `nodes.py::GeneratePlanNodes.revise_once`（`:289`，`revision` 段携带上一候选与完整评估结果）；`workflow.py::_route_after_evaluator`（`:66`）；`::test_blocking_deterministic_failure_revises_once_then_persists_draft`（Planner 恰 2 次、Evaluator 1 次、`revision_count == 1`、修订后 draft + wait）、`::test_second_blocking_failure_rejects_without_waiting`（Planner 恰 2 次不动）
- [x] 二次阻断失败写 `rejected` 终态并结束：不进入等待确认、不产生可激活计划、原 active 不变 → `nodes.py::GeneratePlanNodes.reject_draft`（`:329`）；`workflow.py::_route_after_evaluator`（`:66`）；`service.py::PlanPersistenceService.persist_plan_result`（`evaluation.passed is False` → rejected）；`::test_second_blocking_failure_rejects_without_waiting`（1 条 rejected、0 条 draft、无 `__interrupt__`、`snapshot.next == ()`）、`::test_forged_forbidden_exercise_is_blocked_by_the_deterministic_layer`
- [x] 模型配置／请求超时／调用失败／非法响应，以及持久化前的完整 Run 超时，均作为运行错误：不消耗修订、不创建 draft／rejected；业务 draft 已提交后才发生的超时遵守 §3.10／§9.1，不伪装跨库回滚 → `backend/graph/model.py::require_model_env`（`:45`，`ModelConfigurationError`）、`::parse_model_json`（`:87`，`InvalidModelResponse`）、`nodes.py::GeneratePlanNodes._request_model`（`:387`，`asyncio.timeout` 与预算先扣后调）、`workflow.py::invoke_generate_plan`（完整 Run 超时边界）；`::test_invalid_rubric_response_is_a_run_error_without_revision_or_rejected`、`::test_model_call_failure_is_a_run_error_without_records`、`::test_model_request_timeout_is_a_run_error_without_records`、`::test_whole_run_deadline_bounds_non_model_nodes_and_writes_nothing`、`::test_production_model_entry_validates_environment_and_fixes_request_timeout`
- [x] 画像 `weekly_frequency` 为 `unknown`／`denied`（含未建档）时在 Planner 前明确失败：不调 Planner、不装配、不写记录、不用默认频率或模型猜测 → `nodes.py::GeneratePlanNodes.validate_required_profile`（`:216`）、`::require_profile`（`:172`）、`::require_weekly_frequency`（`:181`，`RequiredProfileMissing`）；`::test_missing_weekly_frequency_fails_before_the_planner`（`unknown`／`denied` 两个参数）、`::test_unregistered_profile_fails_before_the_planner`
- [x] 模型调用不在数据库事务内（写入只在持久化服务自己的短事务）→ `nodes.py::GeneratePlanNodes._request_model`（`:387`，节点不经 `Database.transaction`）；`::test_first_pass_persists_draft_then_waits_and_models_run_outside_transactions`（固定替身在每次模型调用里读业务库：持锁即超时失败）
- [x] 固定 60／180／5：单次请求 60 秒、单 Run 180 秒、最多 5 次模型请求；请求预算放在每次 invocation 的运行上下文（不新增 State 字段、不持久化 Provider 配置）→ `backend/config.py::MODEL_REQUEST_TIMEOUT_SECONDS`（`:24`）／`::GRAPH_RUN_TIMEOUT_SECONDS`（`:26`）／`::MAX_MODEL_REQUESTS_PER_RUN`（`:28`）、`model.py::build_chat_model`（`:53`，`timeout=60`）、`nodes.py::ModelRequestBudget.begin_request`（`:135`）、`::GeneratePlanRun`（`:151`）；`::test_run_limits_are_fixed_and_the_request_budget_is_per_invocation`、`::test_request_timeout_is_capped_by_the_remaining_run_time`、`::test_model_request_timeout_is_a_run_error_without_records`（`budget.used == 1` 证明预算来自本次 invocation）、`::test_whole_run_deadline_bounds_non_model_nodes_and_writes_nothing`（`active_plan` 两个参数）
- [x] 180 秒 Run 时限界定**完整一次 invocation**（含非模型节点），单次请求 60 秒与最多 5 次请求的上限不变 → `workflow.py::invoke_generate_plan`（`:129`，唯一 invocation 入口：`asyncio.timeout(run.budget.remaining_run_seconds())` 包住完整一次 `graph.ainvoke`）、`nodes.py::ModelRequestBudget.remaining_run_seconds`（`:130`，Run 剩余时限的唯一算法，`begin_request` 复用）；`::test_whole_run_deadline_bounds_non_model_nodes_and_writes_nothing`（`load_context` 非模型节点 0.5 秒 vs Run 时限 0.05 秒 → `TimeoutError`、`budget.used == 0`、模型调用 0 次、draft／rejected 0 条；`active_plan` 两个参数覆盖有／无原 active 行）；修复前实测（同场景直接 `graph.ainvoke`）：Run 时限 0.05 秒下 `load_context` 的 0.5 秒仍跑完，只在 Planner 的模型资格检查处以 `ModelRequestBudgetExceeded` 结束；`persist_draft` 0.5 秒时整个 Run 直接完成并写入 1 条 draft（无 `TimeoutError`）
- [x] 三个模型环境变量进入模型节点前必须存在；缺失即明确配置错误，且错误文本不含已配置取值；生产入口只做环境与边界接线 → `model.py::require_model_env`（`:45`）、`::build_chat_model`（`:53`）、`::openai_compatible_model_call`（`:63`）；`::test_production_model_entry_validates_environment_and_fixes_request_timeout`（缺任一变量抛 `ModelConfigurationError`；`request_timeout == 60`；密钥值不出现在错误消息里）
- [x] 固定替身驱动整条链路，不需要 API Key（依赖注入 callable，不建基类／工厂／注册中心）→ `nodes.py::GeneratePlanDeps`（`:159`，`model: ModelCall` 构造期注入）；`::test_fixed_double_workflow_needs_no_api_key`（清空三个环境变量后仍 1 条 draft + 等待确认）
- [x] §9.2：每个 Graph 行为测试都断言原 active 的 id／状态／版本／结构内容／确认时间与行数不变 → `tests/test_stage4_workflow.py::_harness`（`:401`，测试退出时自动调用 `::_Harness.assert_active_unchanged`（`:389`）；快照在测试操作前采集，`finally` 使 invocation 抛错的测试同样覆盖，`active_plan=False` 覆盖没有 active 行的测试）；覆盖本文件当时 24 个测试函数（37 个参数化用例）

收尾 Gate（真实结果，parent-audit 修复后复跑）：`tests/test_stage4_workflow.py` 37 passed；`env -u MODEL_API_KEY -u MODEL_BASE_URL -u MODEL_MODEL uv run pytest tests/test_stage4_workflow.py` 37 passed；`cd backend && uv run pytest` 316 passed, 1 warning；Ruff All checks passed；`git diff --check` 无输出；`git status --short` 为 `?? backend/graph/model.py`、`?? backend/graph/nodes.py`、`?? backend/graph/workflow.py`、`?? backend/tests/test_stage4_workflow.py`、` M backend/config.py`（另含前序改动与并发出现的 `?? refactor-log/stage3-architecture-visualizer.html`）；`git diff --cached --name-only` 无输出。

边界说明：本 Subtask 未实现 Router、调整计划链路、HTTP／SSE／前端、确认／拒绝／激活、`archive_draft`、checkpoint 缺失兜底与幂等确认；`backend/graph/state.py`、`context.py`、`skills.py`、`checkpointer.py` 与迁移、领域 Schema／rules／持久化服务均未改动。§8.6「另测」中「已有 draft 的普通请求直接返回、不调用模型」的 Graph 行为未在本 Subtask 实现（该路径不由本 Subtask 的 11 个节点之一表达），已在 Subtask 05 §6.1 矩阵中登记，交 Subtask 05 处理。

### Subtask 05：持久化等待边界、行为矩阵与完整 Gate

交付：`backend/tests/test_stage4_workflow.py` §8.7 一节 6 个 checkpoint 测试（既有 24 个测试函数未改动），复用该文件既有 `_harness`。两个决策点按用户裁定执行：**Q1=A**（不调整冻结 State 用法）、**Q2=A**（不新增「普通请求 vs 显式重新生成」信号）。唯一越出本 Subtask 原声明范围的改动是本块末条「§5.4 前端共享契约补齐」——属 Stage 4 计划内漏项（parent audit 发现，owner 拍 A），不涉后端。

任务：① 用真实 SQLite Checkpointer 编译 Stage 4 图；② 证明业务 draft 先持久化、再 interrupt，载荷只含 ID；③ 关闭／重开存档后同一 thread 恢复到等待位置，并能按 ID 读回同一 draft／Evaluator 结果；④ 不实现确认／拒绝／激活，恢复读取不得产生业务提交；⑤ 跑完整测试、Ruff、前端构建与 diff/status 检查；⑥ 按 §11 回写每项真实证据与未完成边界。
验收：① checkpoint 内容不含 API Key／Base URL／模型名／完整业务计划副本或完整 Evaluator 结果（Q1=A 口径见本块末条）；② checkpoint 写入模拟失败后业务 draft 仍存在且原 active 不变；③ Stage 4 无 routes_agent／SSE／确认入口；④ 全量 Gate 通过后才把 Stage 4 标为完成。

- [x] 用真实 SQLite Checkpointer 编译 Stage 4 图（存档不是替身）→ `backend/graph/checkpointer.py::open_checkpointer`（`:23`，独立 SQLite `AsyncSqliteSaver`）、`::thread_config`（`:34`）；`workflow.py::build_generate_plan_graph`（`:76`，`checkpointer=` 注入）；`tests/test_stage4_workflow.py::_observing_boundary_saver`（`:430`，只重绑真实 `aput`、其余调用原样委托）／`::_harness`（`:547`，`saver_wrapper` 默认 None）
- [x] §3.10 顺序：业务事务先提交 draft，再写等待位置（interrupt）→ `nodes.py::GeneratePlanNodes.persist_draft`（`:307`，先 `PlanPersistenceService.persist_plan_result`）→ `::wait_for_confirmation`（`:324`，`interrupt({'draft_plan_id': ...})`）；`::test_business_draft_is_committed_before_the_checkpoint_records_the_waiting_boundary`（在真实 `aput` 的 `confirmation == 'pending'` 提交点内读业务库；`assert observations` 保证非空；draft 若延后提交该点即 None 而失败）
- [x] interrupt 载荷恰为 `{'draft_plan_id': id}`，不复制完整计划／评估／业务事实 → `nodes.py::GeneratePlanNodes.wait_for_confirmation`（`:324`）；`::test_first_pass_persists_draft_then_waits_and_models_run_outside_transactions`、`::test_waiting_boundary_survives_checkpointer_restart_on_the_same_thread`、`::test_checkpointed_threads_do_not_mix`
- [x] 关闭并重开真实存档后，同一 thread 恢复到 `wait_for_confirmation`；按 ID 从业务库读回同一统一 `PlanDraft` 与 `EvaluationResult` → `checkpointer.py::open_checkpointer`（`:23`，退出即关闭连接）、`workflow.py::invoke_generate_plan`（`:129`）、`backend/domain/plans/service.py::PlanReadService`；`::test_waiting_boundary_survives_checkpointer_restart_on_the_same_thread`（`snapshot.next == ('wait_for_confirmation',)`、interrupt 恰一条且值为 `{'draft_plan_id': id}`、经 `plan_draft_from_json`／`evaluation_result_from_json` 回读）
- [x] 恢复读取不产生业务提交（不含确认/拒绝/激活）→ `::test_waiting_boundary_survives_checkpointer_restart_on_the_same_thread`（以 `None` 恢复只重入 interrupt：`persistence.writes == 1`、`_row_counts`（`:366`）计划与日程行数不变、draft 行与原 active 逐字段不变）
- [x] 不同 thread 不混用（Q3=(b)：A=draft／B=`reject_draft`／C=未使用；受 §3.8 单可确认 draft 约束）→ `backend/storage/migrations/003_rejected_plan_status.sql:54`（`idx_plans_single_draft`）；`::test_checkpointed_threads_do_not_mix`（B 运行后 A 的 `StateSnapshot` 与 draft 行逐字段不变；B 无 `draft_plan_id` 泄漏且 `next == ()`；C `values == {}`、`next == ()`；`planner_calls == 3` 排除「提前失败」假象）
- [x] checkpoint 写入失败：已提交业务 draft 保留、原 active 不变、不伪装跨库回滚（§9.1／§9.3）→ `service.py::PlanPersistenceService.persist_plan_result`（`:76`）、`nodes.py::GeneratePlanNodes.persist_draft`（`:307`）；`::test_checkpoint_write_failure_keeps_the_committed_business_draft`（`_failing_boundary_writes`（`:469`）只在 `confirmation == 'pending'` 抛错、其余委托真实 saver；draft 状态/内容/评估结果仍在，`rejected == 0`，无 interrupt 落盘；`_harness` 退出断言原 active 不变）
- [x] 序列化存档不含三个模型环境变量的名称与取值 → `backend/config.py::MODEL_API_KEY_ENV`／`::MODEL_BASE_URL_ENV`／`::MODEL_MODEL_ENV`（`:17-19`）、`backend/graph/state.py::WorkflowState`（`:44`，无 Provider 字段）；`::test_checkpoint_file_carries_no_provider_configuration`（哨兵值写入环境变量后读真实 `checkpoints.db` 字节；以 thread id 字节存在作非空锚点）；依据 `LANGGRAPH_REFACTOR_PLAN.md:149`、本文件 §3.7／§8.7
- [x] Stage 4 无 `routes_agent`／SSE／确认入口 → `backend/api/app.py::create_app`（`:75`；`include_router` 恰为 `routes_profile`／`routes_records`／`routes_plans`／`routes_stats`，`:120-123`）；`backend/api/routes_agent.py` 不存在；`::test_stage4_registers_no_agent_route_sse_or_confirmation_entry`
- [x] 决策 Q1＝A：checkpoint 内容边界按冻结 State 设计解释（用户已确认）→ 依据 `Fit-Agent-LangGraph-重构讨论总结.md:146`（工作记忆＝当前请求、Graph 节点、正在生成的计划、评估结果 → State + Checkpointer）与本文件 §3.9／§3.10／§8.7；源码 `backend/graph/state.py::WorkflowState`（`:44`；`draft_plan` `:58`、`evaluation` `:60` 为冻结字段）；**结论**：真实存档的 `channel_values` 按设计包含 `draft_plan`／`evaluation`／`context`／`loaded_skill`；测试只断言 interrupt 载荷恰为 ID 与秘密边界，**未**断言「存档不含完整计划/评估」；本块验收第①条的「完整业务计划副本或完整 Evaluator 结果」按 §3.10 读作 interrupt 载荷不复制完整计划/评估，不调整 WorkflowState 用法
- [x] 决策 Q2＝A：不新增「普通请求 vs 显式重新生成」的冻结信号（用户已确认）→ 依据 `backend/graph/state.py::WorkflowState`（`:44-61`，无该信号）与本文件 §3.9 表格、§12；源码 `backend/domain/plans/service.py::PlanPersistenceService.get_unique_draft`（`:72`）；`tests/test_stage4_plan_persistence.py::test_existing_draft_normal_request_returns_it_without_new_rows`（持久化层：普通请求只读返回既有 draft、不新增行）
- [x] §8.6「另测」行为矩阵按 Q2＝A 口径补齐（其余四项由既有 Subtask 03／04 测试覆盖）→ 无已有 draft 二次失败写 rejected：`::test_forged_forbidden_exercise_is_blocked_by_the_deterministic_layer`、`tests/test_stage4_plan_persistence.py::test_second_blocking_failure_without_draft_writes_rejected_with_null_timestamps`；重新生成通过后同 ID/version 安全替换：`tests/test_stage4_plan_persistence.py::test_explicit_regeneration_replaces_same_id_and_version`；重新生成二次失败原 draft 内容/评估结果不变：`tests/test_stage4_plan_persistence.py::test_regeneration_blocking_failure_keeps_original_draft_and_writes_nothing`；所有失败路径原 active 不变：`tests/test_stage4_workflow.py::_harness`（`:547`）／`::_Harness.assert_active_unchanged`（`:535`，逐测试退出自动断言）、`tests/test_stage4_plan_persistence.py::_assert_active_unchanged`
- [x] 无 API Key 的固定替身组合测试通过 → `tests/test_stage4_workflow.py::test_fixed_double_workflow_needs_no_api_key` 与四文件组合（§10 G5，86 passed）
- [x] §5.4 前端共享契约补齐（parent audit 发现的 Stage 4 漏项；owner 拍 A，最小改动）→ 改动 `frontend/src/lib/contract.ts:179` `PlanWire.status` 联合补 `rejected`（3 → 4 态），仅一处 hunk、1 加 1 删；依据本文件 §5.4、`LANGGRAPH_REFACTOR_PLAN.md:269`、`Fit-Agent-LangGraph-重构讨论总结.md:79-80／294／359`，后端本阶段已产出该状态（`backend/domain/plans/schema.py:56`、`003_rejected_plan_status.sql:39`、透传见 `backend/api/dto.py::plan_dto`（`:297`）），补齐前 `contract.ts` 声称的集合窄于后端可返回集合（漂移由本阶段 `schema.py` 四态化引入）；验证 `cd frontend && npm run build` — ✓ built in 4.83s（§10 G8）

收尾 Gate：见 §10（parent-audit 在 §5.4 前端补齐后的当前字节复跑，全部退出码 0）。测试函数 24 → 30、用例 37 → 43，既有 24 个测试函数未删除。

独立审查（fresh reviewer，只读）结论：无 P0/P1；§8.7 全部 8 条与本块验收第②／③条由上述测试覆盖，验收第①条按 Q1=A 口径覆盖；未发现范围内需修复的代码问题，故未进入修复轮（该轮审查未发现 §5.4 漏项，此后由 parent audit 发现并经 owner 拍 A 补齐，见本块末条）。

## 7. 每个 Subtask 的验收条件汇总

| Subtask | 可独立审查的交付 | 阻断进入下一项的条件 |
| --- | --- | --- |
| 01 | 权威同步、冻结契约测试 | 文档状态/阶段边界仍冲突 |
| 02 | Schema、纯规则、003 迁移及单测 | 任何字段/阈值未获依据，迁移丢数据/FK/索引 |
| 03 | 最小 draft/rejected 持久化服务 | 原 active 可能变化、失败候选可能覆盖旧 draft |
| 04 | 固定替身计划子图与一次修订 | 安全路径调用模型、循环超过一次、Rubric 越权计算规则 |
| 05 | Checkpoint 等待行为和完整 Gate | 恢复产生业务提交、秘密进入存档、任一 Gate 失败 |

## 8. 单元测试和 Graph 行为测试矩阵

### 8.1 Schema 与目录匹配

合法负重次数处方、合法自重次数处方（无 load）、合法计时时长区间（无 reps/load）均通过；计时动作携带 reps、自重动作携带 load、负重动作使用计时处方、`reps_min > reps_max`、`duration_min > duration_max` 或小于 1、未知字段/RIR/1RM/容量/完成率、训练日期重复／越过七天窗口／数量与频率不一致、重复动作／空训练日／非正数组数均失败（前者为 Schema 失败，负重用计时处方为目录匹配失败）。

### 8.2 安全与禁用动作

10 项词逐项命中；一个请求命中多词时按封闭词表顺序返回命中项；「没有麻木」仍命中；不在词表的普通酸痛/关节响不命中；命中后不加载 Skill、不装配 Memory、不进 Planner、不写库；画像禁用 ID 从候选动作确定性删除；模型伪造禁用 ID 被 Evaluator 拒绝；known injuries 不产生新禁用 ID。

### 8.3 负荷来源

最近一次有效 work 组可作为 `KnownLoad` 并保留 session/set 来源；同日多次训练按既有 session 身份顺序确定最近来源；warmup、assisted、不完整组不能作为来源；没有有效历史时只能 `NeedsCalibration`；`NeedsCalibration` 携带重量失败；`KnownLoad` 缺重量或来源失败；PB 大于最近工作重量时仍使用最近有效工作组。

### 8.4 渐进与回退

两次关联计划训练在同负荷、全部目标组达到次数上限 → 只加一个 `min_load_increment_kg`；只有一次达标不得加重；中间额外训练不计数、不打断连续性；目标组数不足算未达标；任一目标组低于下限算未达标；两次未达标回退到当前 active 关联历史最近一次完整完成负荷；没有可回退负荷变 `NeedsCalibration`；热身/assisted/额外 work 组不改变目标组判断；混合目标负荷不能算同负荷完整达标；自重/计时不套用重量加重规则。

### 8.5 Evaluator 分层

确定性失败不调用 Rubric 模型；目标匹配失败是 blocking；安排合理性失败是 blocking；解释质量失败只进入 warnings、总体仍通过；`EvaluationResult.passed` 与两个硬门槛精确一致；Rubric 非法结构/超时/配置缺失是运行错误，不增加 revision count。

### 8.6 一次修订与持久化

| 首次结果 | 修订结果 | 终态 |
| --- | --- | --- |
| 通过 | 不调用修订 | draft + wait |
| 阻断失败 | 通过 | draft + wait，revision_count=1 |
| 阻断失败 | 再失败 | rejected，不等待确认 |
| explanation warning | 不修订 | draft + wait + warning |
| 模型基础设施错误 | 不修订 | Run error，无 draft/rejected |

另测：无已有 draft 二次失败写 rejected；有已有 draft 的普通请求直接返回、不调用模型；重新生成通过后同 ID/version 安全替换；重新生成二次失败后原 draft 内容/Evaluator 结果不变；所有失败路径原 active 不变。

### 8.7 Checkpoint

draft 持久化后才发生 interrupt；interrupt 载荷恰为 `draft_plan_id`；重启恢复到 `wait_for_confirmation`；按 ID 读取业务库得到统一 Schema 和 Evaluator 结果；不同 thread 不混用；checkpoint 写失败不回滚已经提交的业务 draft；无 API Key 时所有固定替身测试可运行；序列化存档不含环境变量名称和值。

## 9. 失败、回滚与原 active 计划保护

### 9.1 失败分类

| 失败类型 | 处理 |
| --- | --- |
| 合法 `PlanDraft` 的确定性领域校验失败（统一 Schema 已通过、领域规则不通过） | 首次可修订一次；二次按 §3.9 写 rejected 或保留旧 draft |
| 模型 Rubric 两个硬项失败（确定性层已通过） | 同上 |
| 仅解释质量失败 | warning，不阻断 |
| 安全关键词命中 | safety_stop，不生成计划 |
| 画像必需事实缺失 | Planner 前明确失败，不生成记录 |
| 模型配置缺失／单次请求 60 秒或 Run 180 秒超时／传输失败／模型输出不是合法 JSON 或不符合统一 Schema（`InvalidModelResponse`） | Run error，不消耗修订、不创建 rejected |
| 数据库写入失败 | 当前短事务回滚；原 active/旧 draft 不变 |
| Checkpoint 写入失败 | 已提交业务 draft 保留；错误上报，fallback 留 Stage 5 |
| 状态条件更新冲突 | 不覆盖，返回明确冲突 |

### 9.2 active 保护断言

Stage 4 所有写服务都不得执行：`active → archived`；新 draft/rejected → active；旧 active 的 structured content 更新；旧 active 日程取消或新增；plan_sessions 创建。每个持久化和 Graph 行为测试在操作前后断言 `active_plan.id/status/version/structured_content/confirmed_at` 不变，且 active 行数量不变且至多为 1。

### 9.3 回滚边界

模型调用在事务外；版本分配与 insert 在同一业务事务；安全替换只含一条条件 UPDATE 和提交；`003` 的 DDL、数据复制、索引重建与 `user_version=3` 由现有迁移执行器放在同一事务（失败后保持版本 2 和原表可用）；业务数据库与 checkpoint 不伪装成跨库原子事务。

## 10. 完整 Gate

实施完成后按顺序执行并记录真实结果：

```bash
cd backend && uv run pytest tests/test_stage4_plan_schema_and_rules.py
cd backend && uv run pytest tests/test_stage4_plan_persistence.py
cd backend && uv run pytest tests/test_stage4_workflow.py
cd backend && uv run pytest tests/test_stage4_migration.py
cd backend && env -u MODEL_API_KEY -u MODEL_BASE_URL -u MODEL_MODEL uv run pytest \
  tests/test_stage4_plan_schema_and_rules.py \
  tests/test_stage4_plan_persistence.py \
  tests/test_stage4_workflow.py \
  tests/test_stage4_migration.py
cd backend && uv run pytest
cd backend && uv run ruff check .
cd frontend && npm run build
git diff --check
git status --short
git diff --cached --name-only
```

实测结果（当前字节＝子任务分解文件删除、docstring 悬空引用修复后复跑；全部退出码 0）：

本轮同时修复的悬空引用（仅 docstring／注释，不改行为）：3 个测试文件与 3 个生产模块（`backend/domain/plans/repo.py`／`service.py`、`backend/graph/nodes.py`）中指向 `refactor-log/stage4子任务/subtask-0N-*.md` 的路径与 `Subtask 0N §x` 引用，已改为 `stage4.md §x`／`§6 Subtask 0N`；该目录已删除，§6 为唯一正本。

| # | 命令 | 结果 |
| --- | --- | --- |
| G1 | `uv run pytest tests/test_stage4_plan_schema_and_rules.py` | 28 passed |
| G2 | `uv run pytest tests/test_stage4_plan_persistence.py` | 9 passed |
| G3 | `uv run pytest tests/test_stage4_workflow.py` | 43 passed |
| G4 | `uv run pytest tests/test_stage4_migration.py` | 6 passed |
| G5 | `env -u MODEL_API_KEY -u MODEL_BASE_URL -u MODEL_MODEL uv run pytest`（四文件） | 86 passed |
| G6 | `uv run pytest` | 322 passed, 1 warning（唯一 warning 为 `starlette/testclient.py:53` 既存 DeprecationWarning；本轮 9 次复跑均为 1 个，另有 1 次报 2 个 warning，未复现、未定位来源，不影响通过） |
| G7 | `uv run ruff check .` | All checks passed |
| G8 | `cd frontend && npm run build` | ✓ built（多次复跑 4.57–5.07s；前端字节自 `contract.ts` 补齐后未变） |
| G9 | `git diff --check` | 无输出 |
| G10 | `git status --short` | 已核对：本阶段文件与本 Subtask 改动（`?? backend/tests/test_stage4_workflow.py`、` M frontend/src/lib/contract.ts`）均在预期内；另含前序改动与并发出现的 `?? refactor-log/stage3-architecture-visualizer.html` |
| G11 | `git diff --cached --name-only` | 无输出（无暂存、无提交） |

历史全量 `uv run pytest`：Subtask 01 → 241 passed；02 → 270 passed；03 → 279 passed, 1 warning；04 → 316 passed, 1 warning；05 → 322 passed, 1 warning。`tests/test_stage4_workflow.py` 在 Subtask 04 收尾为 37 passed、05 收尾为 43 passed。

Gate 通过标准：所有命令退出码为 0；记录实际测试数量，不预写未来通过数；无 API Key 的固定替身测试通过；`git diff --check` 无输出；`git status --short` 只包含本阶段预期文件；无暂存或提交操作，除非用户另行明确要求；全仓运行时代码无 PydanticAI、RIR、估算 1RM、训练容量或完成率回流。

## 11. 源码/测试证据索引格式

实施时每个勾选项必须按以下格式回写，不能只写「已完成」：

```text
- [x] 行为或契约
  → 源码：`path/to/file.py::Symbol`（必要时补稳定行号）
  → 测试：`tests/test_file.py::test_exact_behavior`
  → 验证：`命令` — 实际结果
```

数据库项：

```text
- [x] rejected 状态迁移
  → 迁移：`backend/storage/migrations/003_rejected_plan_status.sql`
  → 约束：plans.status CHECK / single-active / single-draft / FK
  → 测试：升级、保留数据、非法状态、回滚的具体 test ID
```

Graph 项：

```text
- [x] 二次阻断失败终止
  → 节点：`backend/graph/nodes.py::...`
  → 条件边：`backend/graph/workflow.py::...`
  → 持久化：`backend/domain/plans/service.py::...`
  → 测试：调用计数、revision_count、DB 状态、active 前后快照
```

模型 Rubric 研究依据不得写成产品硬阈值；只记录为何采用布尔分解而不采用无依据权重：

- Lai et al., 2025, *An AI-Assisted Adaptive Boolean Rubric for exercise prescription evaluation*, DOI: https://doi.org/10.1016/j.ijmedinf.2025.106202
- Lee et al., 2025, *CheckEval*, https://aclanthology.org/2025.emnlp-main.796/
- Liu et al., 2023, *G-Eval*, https://aclanthology.org/2023.emnlp-main.153/
- OECD/JRC, *Handbook on Constructing Composite Indicators*, https://www.oecd.org/content/dam/oecd/en/publications/reports/2005/08/handbook-on-constructing-composite-indicators_g17a16e3/533411815016.pdf
- Guo et al., 2017, ACSM app scoring instrument, https://www.jmir.org/2017/3/e67

审查规则：文档依据引用章节；源码事实引用路径和符号；行为结论引用 test ID；外部文献只支持 Rubric 形式选择，不提升为新的训练业务规则；不以 scout/reviewer 收据代替源码、测试或命令结果；未实施项保持 `[ ]`，不得用计划文本冒充完成证据。

## 12. Stage 5 交接边界

Stage 4 完成后只保证：有统一、可解析的计划内容和 Evaluator 结果；生成计划可以安全停止、生成、评估、修订一次、持久化 draft/rejected；通过计划停在 checkpoint 的 `wait_for_confirmation`，State 和业务 draft 由同一 `draft_plan_id` 关联；原 active 未被修改；领域渐进函数可供 Stage 5 调整链路复用，但尚未接入调整 Graph。

Stage 5 接手：用户确认/拒绝输入和唯一激活事务；旧 active 归档和未到期日程取消；新计划日程创建；checkpoint 优先、业务 draft fallback 和幂等提交；基于当前 active 的调整计划执行链路；调整时从统一 Schema 提取动作 ID 传给 MemoryAssembler；用户明确要求重新生成已有 draft 的对话/SSE 驱动；Agent API、LangGraph stream → SSE、计划确认页面。

Stage 5 不得改变 Stage 4 已冻结的不变量：rejected 永不可激活；二次失败不破坏原 active/原可确认 draft；确认前不修改 active；业务事实从 SQLite 重读；模型调用不在数据库事务内；不增加新的训练阈值或医学规则。

## 13. 实施顺序总览

```text
同步最高权威总结 → 同步总计划 → 冻结计划/Evaluator Schema → 003 rejected + single-draft 迁移
→ 纯领域确定性校验器 → draft/rejected 最小持久化 → Planner/Evaluator 独立节点 → 一次修订条件图
→ 业务 draft 先提交 + checkpoint interrupt → 固定替身行为矩阵 → 全量 Gate 与证据回写
```

在实施期间若发现必须改变 WorkflowState、MemoryAssembler、SkillLoader、计划周期、Rubric 阻断规则、状态集合或持久化顺序，必须停止并询问用户；不得以实现便利自行修改本计划中的已确认契约。
