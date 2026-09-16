# Stage 3：Memory、Checkpoint 与 Skill Loader 计划清单

> 状态：已完成——第 2 节口径已回写本清单（见 §2）；Subtask 01（冻结契约与最小 Graph State）已实施并留证（见 §3）；Subtask 02（MemoryAssembler）已实施并留证（见 §4；其中「复用修正后的 PB 算法」已随 `refactor-log/stage2.md` §14 修正完成）；Subtask 03（SQLite Checkpointer 与生命周期）已实施并留证（见 §5）；Subtask 04（两个核心 Skill 与渐进加载）已实施并留证（见 §6）；Subtask 05（行为测试与 Stage 3 Gate）已实施并留证（见 §7）。Stage 3 Gate 达成：`cd backend && uv run pytest` 236 passed、`cd backend && uv run ruff check .` All checks passed!、`cd frontend && npm run build` 通过、`git diff --check` 无输出、`git status --short` 已核对既有 dirty worktree 与本阶段文件、`git diff --cached --name-only` 无输出。Stage 4／5／6 仍未实施。
> 基线：`refactor/langgraph`，`ecb2399`（Stage 2）。
> 权威顺序：`Fit-Agent-LangGraph-重构讨论总结.md` > `LANGGRAPH_REFACTOR_PLAN.md` > 已合入源码。
> Stage 2 完成情况直接以 `refactor-log/stage2.md` 为依据；已删除的拆分子任务不作为本阶段证据。

## 1. 前置完成情况与本阶段边界

- Stage 2 文档记录：196 passed、Ruff 通过、前端构建通过、`git diff --check` 通过。原 scout 仅收集到 196 项测试并抽检源码，没有重新执行全量 Gate，不能将收集结果视为测试通过；`refactor-log/stage2.md` §14 前置修正完成后已重跑全量 Gate（`uv run pytest` 211 passed、`uv run ruff check .` All checks passed!、`npm run build` 通过、`git diff --check` 无输出），本阶段以该结果为依据。
- 三类 PB、确定性 `trend_summary`、月历与看板已有实现；用户后续取消的外加重量动作同重量次数 PB 已完成 `refactor-log/stage2.md` §14 的最小修正（外加重量动作只出重量 PB、纯自重只出次数 PB、计时只出时长 PB），Stage 3 可以消费 PB。
- 本文件成稿时没有 `backend/graph/`、`backend/skills/` 或 Agent 路由；Subtask 01 之后 `backend/graph/` 只有 State 契约（§3），Subtask 02 之后另有 MemoryAssembler（§4）与 records 的有界近期查询，Subtask 03 之后另有 Checkpointer 的独立存档与连接生命周期（§5），Subtask 04 之后另有 `backend/skills/` 的两个核心 Skill 与 `backend/graph/skills.py` 加载器（§6），仍无节点与 Agent 路由（按 §2.4 留 Stage 4）。LangGraph 与 SQLite Checkpointer 依赖已经声明，无需先引入新框架。
- 本阶段只交付 State、持久化工作记忆、固定范围 MemoryAssembler、两个核心 Skill 与渐进加载验证及其行为测试（§7）。

依据：总结 §5、§6、§9、§14；总计划 §8、§11「阶段 3」；`refactor-log/stage2.md` §12；`backend/pyproject.toml:18-21`。

## 2. 实施前需要用户确认的口径

以下为已回写的口径项，按勾选状态执行；未勾选项属于实施时选择，不是待确认口径。

### 2.1 相关动作 PB

- [x] 生成新计划时读取全部已有动作 PB。
- [x] 调整计划时只读取当前 active 计划涉及动作的 PB。
- [x] “相关”只过滤哪些动作进入 Agent 上下文，不改变动作适用的 PB 类型：外加重量动作只取最大重量 PB，纯自重动作取最大次数 PB，计时动作取最长时长 PB。

例如调整包含卧推与引体的 active 计划时，装配卧推重量 PB 与引体次数 PB，不把前水平等无关动作成绩一并塞入。当前只有全量 `StatsService.list_personal_bests()`，且 active 计划的 `structured_content` 在 Stage 4 统一 Schema 落地前仍是不透明 JSON（`backend/domain/plans/schema.py:8-11`）。因此 Stage 3 只提供按稳定 `exercise_id` 过滤的装配能力并测试；Stage 4 从统一计划 Schema 提取 active 计划动作 ID 后传入，不在 Stage 3 猜测 JSON 形状，也不按目标或伤病推导动作。

依据：总结 §5.2、§7.1；`backend/domain/stats/service.py:60`。

### 2.2 最近四次训练

- [x] 按 `performed_on DESC, id DESC` 取最近四个训练 session，同日训练各算一次。
- [x] 保留每次训练全部组与组类型，不把训练历史裁成 PB 有效工作组。
- [x] 不足四次时仅返回实际存在的记录，空库返回空列表。

当前全量读取按 `performed_on, id` 排序，并返回全部组；新增限量读取不改变原列表 API。最近四次仅用于 Agent 近期上下文，不限制 PB、趋势或后续渐进规则查询历史。

依据：总结 §5.2；`backend/domain/records/repo.py:66-86`（全量读取 `_read_all_sessions`）、`:256`（全量出口 `list_all`）；`backend/domain/records/schema.py:118-133`。

### 2.3 Checkpoint 存储与恢复验证

- [x] checkpoint 使用独立 SQLite 文件，与业务数据库分开（已同步 `Fit-Agent-LangGraph-重构讨论总结.md` §9、`LANGGRAPH_REFACTOR_PLAN.md` §5.6）。
- [x] Stage 3 验证 conversation/thread 的工作流 State 能落盘并在进程重启后恢复。
- [x] checkpoint 缺失后的业务 draft 兜底关联、确认/拒绝与幂等提交全部留 Stage 5 设计和实现。
- [x] 用测试内最小 interrupt 图验证真实暂停、关闭连接、重新打开后读取等待状态；不提前建设正式确认工作流。

Checkpoint 持久化的是 LangGraph 工作流执行位置和 State，例如“某 conversation 已生成 draft、当前停在等待确认节点”，不是画像、训练、PB 或计划的业务事实副本。`thread_id = conversation id`；独立文件允许工作流存档单独管理且不与业务写锁竞争。当前 plans 无 thread 字段，本阶段不得仅靠“取最新 draft”猜测对应关系。

依据：总结 §9；总计划 §5.6、§11 阶段 3/5；`backend/storage/migrations/001_initial.sql:72-90`；`backend/domain/plans/repo.py:57`。

### 2.4 Skill 内容与阶段交付

- [x] 两个 Skill 的硬边界与业务规则使用 Fit-Agent 总结已冻结的内容。
- [x] 可借用本地 `Lzheng-fitness` 专家知识库整理的具体训练知识与规划归纳，暂不增加专家 Skill、路由或其他基础设施。
- [x] 本阶段交付元数据清单与按名加载能力；实际 Router/模型提示词接线随 Stage 4 完成。
- [x] 实施时逐条选择纳入的 Lzheng 知识，保留来源边界；不得把其二次归纳写成 Fit-Agent 已独立核验的原始文献。→ 已随 Subtask 04 完成：`backend/skills/workout-planning/references/planning-rules.md` §7「来源登记」登记 5 个实际采用的本地来源文件（含内容与性质列），`backend/skills/plan-adjustment/SKILL.md`「训练知识来源」登记 4 个；两处均标注「本地二次归纳，非 Fit-Agent 独立核验的原始文献」，并逐条列出不采用范围。

Lzheng 可借鉴目录形态、渐进披露、固定契约与来源记录；其运行时加载器不存在，Fit-Agent 自行实现最小 loader。RIR、估算 1RM、训练容量、额外医学规则、主观疲劳判定、硬编码阈值、跨 Skill handoff、工作台与 HTML 交付仍禁止引入。统一计划 Pydantic Schema 属于 Stage 4，本阶段不另造一套输出 Schema。

依据：总结 §6、§7.3；总计划 §8.3、§11 阶段 4。

## 3. Subtask 01：冻结契约与最小 Graph State

- [x] 将第 2 节用户决定回写本清单；涉及需求补充时先同步最高权威总结，再同步总计划。→ §2.1–§2.4 已按勾选状态记录；涉及需求补充的 PB 范围口径已同步 `Fit-Agent-LangGraph-重构讨论总结.md` §5.2／§7.1／§14 与 `LANGGRAPH_REFACTOR_PLAN.md` §6.2／§8.2（工作区 diff）。
- [x] 在 `backend/graph/state.py` 定义当前工作流所需 State。→ `backend/graph/state.py`、`backend/graph/__init__.py`。
- [x] 覆盖请求、intent、装配上下文、已加载 Skill、draft 身份/内容、评估结果、修订次数、确认状态和终止原因所需字段；不预建额外运行状态机。→ 十一个字段，无编排/状态机字段；`test_stage3_graph_state.py::test_state_fields_are_frozen_to_the_authoritative_contract`。
- [x] conversation 身份与 checkpointer 的 `thread_id` 保持一致，不维护另一套线程映射。→ `WorkflowState.conversation_id` 即 Checkpointer 的 `thread_id`，无第二身份字段（字段集合断言）；`::test_langgraph_accepts_the_frozen_state_and_keeps_conversation_identity` 证明节点只回写自己的键、不改会话身份。
- [x] 不复制完整训练历史，不保存 API Key、Provider 配置或业务统计缓存。→ 字段集合断言不包含密钥、Provider、统计缓存与训练历史字段；装配结果只以 `context` 引用一次 MemoryAssembler 输出。
- [x] Stage 4 尚未定义的计划与评估契约不在本阶段伪装成已完成；只落实本阶段验证实际需要的字段。→ `draft_plan`／`evaluation`／`loaded_skill`／`context` 在 `backend/graph/state.py` 中只作不透明载荷，形状声明留子任务 02／04 与 Stage 4。

Subtask 01 验证：`cd backend && uv run pytest tests/test_stage3_graph_state.py` 4 passed；`cd backend && uv run pytest` 200 passed；`cd backend && uv run ruff check .` All checks passed!。前端未改动，§7 的 Gate 项（含 `npm run build`）仍归 Subtask 05。

依据：总结 §5.1、§9、§10；总计划 §8.1、§11 阶段 3/4。

## 4. Subtask 02：MemoryAssembler

目标文件：`backend/graph/context.py`；必要时扩展 records/stats 的既有只读接口。

- [x] 装配输出严格为六类：画像、active 计划、最近四次训练、相关动作最新 PB、`trend_summary`、当前请求。→ `backend/graph/context.py::MemoryContext` 六字段（字段顺序即 §5.2 读取清单顺序）；`test_stage3_memory_assembler.py::test_assembled_context_has_exactly_the_six_categories`、`::test_empty_database_still_assembles_the_six_categories_without_faking_facts`。
- [x] 复用 `ProfileService.read()`，未建档时保留现有缺失语义。→ `backend/graph/context.py` 只调 `ProfileService.read()`；`::test_profile_is_read_through_the_profile_service`（未建档为 `None`，建档后与 `ProfileService.read()` 三态事实一致，未填写字段保持 `unknown`）。
- [x] 复用 `PlanReadService.get_active()`，不得用 draft 或 archived 计划顶替 active。→ `::test_only_the_active_plan_enters_and_opaque_content_is_not_interpreted`、`::test_draft_plan_is_not_used_as_a_substitute_for_the_active_plan`。
- [x] 在 records repo/service 增加有界近期训练查询，按确认口径选 session 后读取其组；不先取完整训练历史再在 Graph 截断。→ `domain/records/repo.py::_read_recent_sessions`／`WorkoutRecordsRepo.list_recent`、`domain/records/service.py::WorkoutRecordsService.list_recent`（两次查询：先按 `performed_on DESC, id DESC` 取至多 `limit` 条训练行，再只取这些训练的组行；非正数 `limit` 直接失败 `ValueError`，不退化成 SQLite 的 `LIMIT -1` 全量读取）；`::test_recent_sessions_are_bounded_to_the_newest_four_with_their_own_sets`（六次训练只装最新四次，同日两次各算一次，被排除训练的组不进入结果）、`::test_recent_sessions_return_only_existing_records`、`::test_recent_sessions_reject_non_positive_limit`。
- [x] 生成新计划时装配全部已有动作 PB；提供按稳定 `exercise_id` 过滤的装配入口并测试，供 Stage 4 从统一计划 Schema 提取 active 计划动作后调用。复用修正后的 PB 算法：外加重量只取重量 PB、纯自重取次数 PB、计时动作取时长 PB，并保留来源与日期；不在 Graph 另算 PB或解析不透明计划 JSON。→ `MemoryAssembler.assemble(..., exercise_ids=None)` 装全部，给序列只装这些动作，空序列为空；`::test_personal_bests_cover_all_exercises_and_filter_by_the_caller_ids` 以「与 `StatsService.list_personal_bests()` 逐条相等」断言本层不重算 PB、来源与日期原样保留，并断言装配结果的（动作，PB 类型）组成为杠铃背蹲重量 PB、纯自重引体次数 PB、平板支撑时长 PB；不透明 JSON 未被解析由 `::test_only_the_active_plan_enters_and_opaque_content_is_not_interpreted` 证明。
- [x] 直接复用 `StatsService.trend_summary(business_day)`；业务日期由调用方注入。→ `backend/graph/context.py::MemoryAssembler.assemble(business_day=...)` 为必填关键字参数，本层不调 `date.today()`；`::test_trend_summary_matches_the_stats_service_at_the_injected_business_day`（与 `StatsService.trend_summary(day)` 相等，注入 6-30／7-10 得停训 10／20 天，体脂一条记录保持 `insufficient_data`）。
- [x] 每次装配重新读取业务事实；修改/删除训练或更新画像后下次装配立即反映新数据。→ `::test_assembly_rereads_facts_after_record_deletion_and_profile_update`（删除训练与更新画像后重新装配即得新事实）。
- [x] SQL 只在 repo；读取沿用 `Database.under_lock()`；不引入业务事实缓存或摘要表。→ `backend/graph/context.py` 无 SQL、无表写入，只调 `ProfileService`／`PlanReadService`／`WorkoutRecordsService.list_recent`／`StatsService`；新增读取经 `WorkoutRecordsRepo.list_recent` 的 `under_lock`。

源码复用索引：

| 输入 | 已有入口 |
| --- | --- |
| 画像 | `backend/domain/profile/service.py:25` |
| active 计划 | `backend/domain/plans/service.py:19` |
| 训练（全量与有界近期读取） | `backend/domain/records/service.py:91`（全量 `list_all`）、`repo.py:256`（全量 `list_all`）；有界近期读取见本子任务新增的 `service.py:95`／`repo.py:260`（`list_recent`） |
| PB | `backend/domain/stats/service.py:60`、`schema.py:236` |
| trend_summary | `backend/domain/stats/service.py:87-95`、`schema.py:159-167` |
| 业务日期 | `backend/api/deps.py:16-25` |

依据：总结 §5.2、§7；总计划 §4、§8.2。

Subtask 02 验证：`cd backend && uv run pytest tests/test_stage3_memory_assembler.py` 11 passed（Subtask 05 新增 1 项后现为 12 passed）；`cd backend && uv run pytest` 211 passed（Subtask 01 后基线 200 passed ＋ 本子任务 11 项；含随 `refactor-log/stage2.md` §14 修正同步的 PB 类型组成断言与非正数 `limit` 保护测试）；`cd backend && uv run ruff check .` All checks passed!。未完成边界：Checkpointer、Skill Loader、Router／节点接线与 §7 的 Gate（含 `npm run build`）仍归 Subtask 03–05。

## 5. Subtask 03：SQLite Checkpointer 与生命周期

- [x] 使用已安装的 LangGraph SQLite saver，不自行实现 checkpoint repo 或迁移表。→ `backend/graph/checkpointer.py::open_checkpointer` 直接包 `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver`（3.1.1）；本子任务未新增任何 checkpoint repo／表迁移（`backend/storage/migrations/` 未改）。
- [x] 在异步生命周期内创建并关闭 saver 连接；与业务数据库生命周期清晰分离，按第 2 节确定文件位置。→ `backend/api/app.py:89-97`（lifespan 内 `async with open_checkpointer(checkpoint_database_path(resolved))`，业务库连接在同一 finally 中关闭）；`test_stage3_checkpoint.py::test_lifespan_opens_and_closes_the_checkpoint_saver_on_a_separate_file`（生命周期内连接打开、停服后关闭，且路径 ≠ 业务库路径）、`::test_lifespan_cancellation_closes_the_checkpoint_connection`（运行期取消退出仍关闭存档连接）。
- [x] checkpoint 表由 saver 在独立 SQLite 文件中管理，不写进 `001`/`002` 业务迁移。→ `test_stage3_checkpoint.py::test_checkpointer_path_is_configured_independently_of_the_business_database`（独立文件内表集合恰为 `checkpoints`／`writes`，无业务表）、`::test_lifespan_opens_and_closes_the_checkpoint_saver_on_a_separate_file`（业务库不含 checkpoint 表）；`test_langgraph_stage0.py::test_startup_creates_new_database_with_business_schema` 的七张业务表断言未改动。
- [x] 用固定 conversation id 保存并重新读取 State。→ `test_stage3_checkpoint.py` 的 `CONVERSATION_ID`／`OTHER_CONVERSATION_ID` 为固定值；`graph/checkpointer.py::thread_config(conversation_id)` 生成的 `thread_id` 就是该 conversation id（无第二套映射），由 `::test_waiting_state_survives_restart_with_fixture_draft_identity` 与 `::test_checkpointed_threads_do_not_share_state` 使用。
- [x] 按第 2 节确定的验证方式证明重启后可定位等待确认的 thread 与 fixture draft 身份。→ `::test_waiting_state_survives_restart_with_fixture_draft_identity`：固定最小图经真实 `interrupt()` 暂停（返回含 `__interrupt__`）→ `aget_state().next == ("wait_for_confirmation",)` 且 `draft_plan_id` 为直接 SQL 写入的 fixture draft 行身份 → 停服（连接关闭）→ 新 app 实例重开同一存档文件仍按同一 thread 读回同一等待状态；`::test_checkpointed_threads_do_not_share_state` 另证未存档 thread 状态为空。
- [x] 不提供确认、激活或归档入口；不把恢复读取误写成业务提交。→ 生产代码只有打开／关闭与线程配置（`backend/graph/checkpointer.py`），无业务库调用、无 `plans` 写入；固定最小图只在测试文件内定义，不是生产确认工作流。`::test_waiting_state_survives_restart_with_fixture_draft_identity` 在恢复后断言 fixture draft 仍为 `draft`／`archived_at IS NULL`、原 active 计划仍为 `active` 且仍唯一，恢复链路自身未做任何业务提交。
- [x] 配置独立 checkpoint 路径并在测试中使用临时文件；业务数据库表集合断言保持不变。→ `backend/config.py:15`（`CHECKPOINT_DATABASE_FILENAME`）与 `checkpoint_database_path()`（`:37`）为独立于 `database_path()` 的路径入口；本子任务测试全部使用 pytest `tmp_path` 临时目录／临时文件（含任意嵌套路径）；`test_langgraph_stage0.py:41-51`、`test_stage1_data_base.py:68-73,125-128` 的表集合断言未改动。

接入索引：`backend/api/app.py:79-97`（lifespan 内开闭业务库与存档）；`backend/graph/checkpointer.py`（存档连接与线程配置）；`backend/config.py:15,37`（独立存档路径）；`backend/storage/db.py`（业务库连接，未改动）；`backend/tests/test_langgraph_stage0.py:41-51`、`backend/tests/test_stage1_data_base.py:68-73,125-128`（未改动的表集合断言）。

Subtask 03 验证：`cd backend && uv run pytest tests/test_stage3_checkpoint.py` 5 passed（Subtask 05 新增 2 项后现为 7 passed）；`cd backend && uv run pytest` 216 passed（Subtask 02 后基线 211 passed ＋ 本子任务 5 项）；`cd backend && uv run ruff check .` All checks passed!。本子任务测试不调真实模型、不需要 API Key（`env -u MODEL_API_KEY` 下 5 项照常通过），固定 State 断言也不含密钥／Provider 字段。未完成边界：计划子图节点、Skill Loader、Router 接线与 §7 的 Gate（含 `npm run build`）仍归 Subtask 04–05；本子任务未同步勾选 §7。

依据：总结 §5.1、§9；总计划 §5.6、§11 阶段 3。

## 6. Subtask 04：两个核心 Skill 与渐进加载

目标文件：

```text
backend/graph/skills.py
backend/skills/workout-planning/SKILL.md
backend/skills/workout-planning/references/planning-rules.md
backend/skills/plan-adjustment/SKILL.md
```

- [x] 启动扫描 `backend/skills/*/SKILL.md`，只解析并暴露 `name`、`description`；不预加载正文和 references 到模型上下文。→ `backend/graph/skills.py::SkillLoader.discover`／`::parse_skill_metadata`（流式读 frontmatter，停在闭合分隔符；返回的 `SkillMetadata` 只有两个字段）；`test_stage3_skill_loader.py::test_metadata_structure_is_frozen_to_name_and_description`（字段集合恰为 `name`／`description`）、`::test_startup_scan_exposes_only_metadata_of_the_two_real_skills`、`::test_startup_scan_reads_frontmatter_only_and_caches_the_scan`（启动只打开 `SKILL.md`、未打开 `references/` 下文件，且每个实例只扫描一次）。
- [x] 命中后按名加载对应 Skill 正文及其明确引用的 reference 文件，不全量加载两个 Skill。→ `SkillLoader.load` 只读命中 Skill 的正文与正文里 `references/…md` 形式的显式引用（`_referenced_paths`／`_load_references`）；`test_stage3_skill_loader.py::test_loading_one_skill_loads_only_its_body_and_referenced_files`（加载 alpha 时 beta 的 `SKILL.md` 与 references 都未被打开）、`::test_only_self_relative_reference_paths_are_loaded`（跨仓库 `…/references/x.md` 与目录名不算引用）、`::test_real_skills_locatable_sections_and_hard_boundaries`（planning 的 references 恰为 `references/planning-rules.md`，adjustment 无 references）。
- [x] 两个 Skill 都说明可用工具、所需记忆、禁止事项、结构化输出要求与知识来源。→ 两个 `SKILL.md` 各含「可用工具与数据边界」（能力＋既有入口表，工具绑定明确归属 Stage 4）、「所需记忆」、「禁止事项」、「结构化输出要求」、「训练知识来源」五节；`test_stage3_skill_loader.py::test_real_skills_locatable_sections_and_hard_boundaries`。
- [x] 从本地 `Lzheng-fitness/skills/lzheng-training-expert-library/` 及计划/复盘 references 中挑选与 Fit-Agent 边界兼容的训练知识归纳，记录实际采用的本地来源文件；不复制专家路由与原始材料声明。→ `backend/skills/workout-planning/references/planning-rules.md` §7 登记 `knowledge/00-lzheng-knowledge-map.md`、`knowledge/02-lzheng-program-design.md`、`knowledge/evidence-base.md`、`skills/lzheng-fitness-plan/references/program-design.md`、`skills/lzheng-training-expert-library/references/experts/greg-nuckols-strength-periodization/knowledge/跨文决策/02-变量改变必须是可验证的单次实验.md`（含采用内容与性质列）；`backend/skills/plan-adjustment/SKILL.md` 登记 `skills/lzheng-strength-training-review/references/rolling-review-rules.md`、`knowledge/05-lzheng-training-review.md`、`knowledge/00-lzheng-knowledge-map.md`、`knowledge/evidence-base.md`；未复制专家路由、登记表或原始材料声明（planning-rules.md §8 逐条列出不采用范围）。
- [x] planning 声明无历史待校准、起始负荷不从 PB 推算、一次修订及用户确认边界。→ `backend/skills/workout-planning/SKILL.md`「禁止事项」「结构化输出要求」＋`references/planning-rules.md` §2／§3／§5；`test_stage3_skill_loader.py::test_real_skills_locatable_sections_and_hard_boundaries` 断言「待校准」「不从 PB 反推」「一次修订」「用户确认」可定位。
- [x] adjustment 声明必须基于 active 计划与最新数据调整，不脱离原计划重新生成。→ `backend/skills/plan-adjustment/SKILL.md`「调整规则」／「禁止事项」；同测试断言「不脱离旧计划重新生成」「用户确认」可定位。
- [x] Skill 只是模型指令，不替代 Stage 4 确定性规则与统一 Schema。→ 两个 `SKILL.md` 开头与「结构化输出要求」均写明输出必须过 Stage 4 统一 Pydantic Schema 与确定性校验器，本 Skill 不定义字段、不新增第二套结构。
- [x] 不新增 onboarding/review Skill、插件注册中心或跨 Skill handoff 协议。→ `backend/skills/` 只有 `workout-planning`／`plan-adjustment` 两个目录；`backend/graph/skills.py` 的加载器按名只加载命中的那一个 Skill，无注册中心、无 Skill 间交接文件；`test_stage3_skill_loader.py::test_startup_scan_exposes_only_metadata_of_the_two_real_skills`。

Subtask 04 验证：`cd backend && uv run pytest tests/test_stage3_skill_loader.py` 16 passed（Subtask 05 新增 1 项后现为 17 passed）；`cd backend && uv run pytest` 232 passed（Subtask 03 后基线 216 passed ＋ 本子任务 16 项）；`cd backend && uv run ruff check .` All checks passed!。未完成边界（本次记录时）：Subtask 05 的行为测试与 Stage 3 Gate（含 `cd frontend && npm run build`、`git diff --check`）当时尚未执行、§7 当时未勾选，现已随 Subtask 05 完成（见 §7、§10）；Router／节点接线与实际工具绑定按 §2.4 留 Stage 4。

依据：总结 §6、§7.3、§14；总计划 §8.3。

## 7. Subtask 05：行为测试与 Stage 3 Gate

建议测试文件（可按实际实现合并，不为目录形式额外拆分）：

```text
backend/tests/test_stage3_memory_assembler.py
backend/tests/test_stage3_skill_loader.py
backend/tests/test_stage3_checkpoint.py
```

### Memory

- [x] 固定库输入下输出恰好六类信息。→ `test_stage3_memory_assembler.py::test_assembled_context_has_exactly_the_six_categories`（字段集合与顺序精确断言）、`::test_empty_database_still_assembles_the_six_categories_without_faking_facts`（空库同样六类）。
- [x] 超过四次、少于四次、空历史、同日多次训练均符合确认口径。→ `::test_recent_sessions_are_bounded_to_the_newest_four_with_their_own_sets`（六次只取最新四次；同日两次按身份取大、各算一次；被排除训练的组不进入结果）、`::test_recent_sessions_return_only_existing_records`（不足四次）、`::test_empty_database_still_assembles_the_six_categories_without_faking_facts`（空历史 `recent_sessions == ()`）。
- [x] 只有 active 计划进入上下文；画像/active 缺失不伪造。→ `::test_only_the_active_plan_enters_and_opaque_content_is_not_interpreted`、`::test_draft_plan_is_not_used_as_a_substitute_for_the_active_plan`、`::test_profile_is_read_through_the_profile_service`、`::test_empty_database_still_assembles_the_six_categories_without_faking_facts`。
- [x] PB 范围准确，来源完整；外加重量无次数 PB，纯自重有次数 PB，计时动作有时长 PB。→ `::test_personal_bests_cover_all_exercises_and_filter_by_the_caller_ids`（全量装配与 `StatsService.list_personal_bests()` 逐条相等；`(动作, PB 类型)` 组成恰为背蹲重量 PB、引体次数 PB、平板支撑时长 PB；来源训练身份／组序号／`performed_on` 逐条断言）。
- [x] 修改/删除记录后重新装配得到更新事实。→ `::test_assembly_reflects_modification_of_an_existing_workout`（Subtask 05 新增：改既有训练的日期与重量后，近期训练、PB 数值／来源、停训天数立即换成新事实）、`::test_assembly_rereads_facts_after_record_deletion_and_profile_update`（删除训练与更新画像）。
- [x] 固定业务日期下 `trend_summary` 与 Stats 服务相同，保留 no_data/insufficient_data。→ `::test_trend_summary_matches_the_stats_service_at_the_injected_business_day`（与 `StatsService.trend_summary(day)` 相等；注入两个业务日期得停训 10／20 天；体脂一条记录保持 `insufficient_data`）、`::test_empty_database_still_assembles_the_six_categories_without_faking_facts`（空库 `no_data`）。

### Skill

- [x] 启动元数据中不含正文和 reference 内容；references 未被提前读取。→ `test_stage3_skill_loader.py::test_metadata_structure_is_frozen_to_name_and_description`（结构只有两个字段）、`::test_startup_scan_reads_frontmatter_only_and_caches_the_scan`（临时 Skill：只打开 `SKILL.md`，引用文件未打开，每实例只扫描一次）、`::test_real_startup_scan_opens_no_body_or_reference_file`（Subtask 05 新增：真实 `backend/skills` 启动扫描只打开两个 `SKILL.md`，元数据不含正文措辞与 reference 文件名）。
- [x] 命中 planning/adjustment 只加载目标正文及明确引用内容。→ `::test_loading_one_skill_loads_only_its_body_and_referenced_files`（加载 alpha 时 beta 的 `SKILL.md` 与 references 都未被打开）、`::test_only_self_relative_reference_paths_are_loaded`（跨仓库路径不算引用）、`::test_real_skills_locatable_sections_and_hard_boundaries`（planning 的 references 恰为 `references/planning-rules.md`，adjustment 无 references）。
- [x] 未知 Skill、缺失的必需文件返回明确错误，不静默切换 Skill 或知识来源。→ `::test_unknown_skill_fails_explicitly`（列出已扫描名称）、`::test_missing_referenced_file_fails_explicitly`、`::test_skill_directory_without_skill_file_fails_explicitly`、`::test_invalid_metadata_fails_explicitly`（7 种非法 frontmatter）。
- [x] 两个 Skill 的来源、所需记忆、禁止事项和输出要求可定位。→ `::test_real_skills_locatable_sections_and_hard_boundaries`（`可用工具`／`所需记忆`／`禁止事项`／`结构化输出要求`／`训练知识来源` 五节，`来源登记`／`本地来源文件`，以及「待校准」「不从 PB 反推」「一次修订」「用户确认」「不脱离旧计划重新生成」硬边界措辞）。

### Checkpoint

- [x] 测试只使用固定节点/State，不调用真实模型。→ `test_stage3_checkpoint.py::test_fixed_minimal_graph_runs_fixed_nodes_on_the_frozen_state`（Subtask 05 新增：节点集合恰为 `__start__`／`stage_draft`／`wait_for_confirmation`／`__end__`，无模型节点；State 字段集合冻结为 `WorkflowState` 的 11 字段）、`::test_waiting_state_survives_restart_with_fixture_draft_identity`（真实 `interrupt()`，全程只在固定最小图上运行）。
- [x] 等待确认状态落盘，关闭后重开连接仍能按同一 thread 定位，并与 fixture draft 身份对应。→ `::test_waiting_state_survives_restart_with_fixture_draft_identity`、`::test_checkpointer_path_is_configured_independently_of_the_business_database`。
- [x] 不同 thread 的状态不混用。→ `::test_checkpointed_threads_do_not_share_state`（两个已存档 thread 各归各，未存档 thread 状态为空）。
- [x] 恢复测试不激活、不归档计划，不修改原 active。→ `::test_waiting_state_survives_restart_with_fixture_draft_identity`（重启后 fixture draft 仍为 `draft`／`archived_at IS NULL`，原 active 计划仍 `active` 且仍唯一）。
- [x] 无 API Key 时非模型业务与本阶段测试仍可运行；State/checkpoint 不携带密钥或 Provider 配置。→ `::test_no_api_key_and_no_provider_config_reach_state_or_checkpoint`（Subtask 05 新增：`MODEL_API_KEY` 未配置时服务照常启动（`provider_has_api_key is False`）、业务写入与 interrupt 落盘正常；存档文件字节不含 `MODEL_API_KEY`／`MODEL_BASE_URL`／`MODEL_MODEL` 名称与取值，State 字段不超出冻结集合）、`::test_waiting_state_survives_restart_with_fixture_draft_identity`（已配置的密钥不出现在存档字节里）；另有 `env -u MODEL_API_KEY uv run pytest tests/test_stage3_graph_state.py tests/test_stage3_memory_assembler.py tests/test_stage3_checkpoint.py tests/test_stage3_skill_loader.py` 40 passed。

### Gate

- [x] 第 2 节口径已确认，所有实际交付项有源码与测试索引。→ §2.1–§2.4 全部勾选；§3–§6 每项均带源码与测试索引，§7 各项见上。
- [x] `cd backend && uv run pytest`。→ 236 passed（Subtask 04 后基线 232 passed ＋ Subtask 05 新增 4 项：Memory 1、Skill 1、Checkpoint 2）。
- [x] `cd backend && uv run ruff check .`。→ All checks passed!。
- [x] `cd frontend && npm run build`（既有页面回归，本阶段不增加前端功能）。→ 通过（`tsc -b` ＋ `vite build`，本阶段未改前端源码）。
- [x] `git diff --check`。→ 无输出。
- [x] `git status --short`。→ 已核对：保留既有 dirty worktree 修改，本阶段新增／修改文件均在预期范围内；逐字输出见 §10。
- [x] `git diff --cached --name-only`。→ 无输出，没有暂存文件。
- [x] 记录实际通过数量、命令结果与未完成边界，不将计划勾选为已实施。→ 见本段与各子任务验证行；未完成边界：Stage 4／5／6 项（Router／Planner／Evaluator 正式接线、统一计划 Schema、负荷渐进校验、确认／拒绝／激活事务、checkpoint 优先加 draft 兜底的提交协议、SSE、自然语言打卡）保持未实施，本阶段只交付 State、MemoryAssembler、Checkpointer 生命周期与两个 Skill 的渐进加载。

## 8. 明确不做

- Stage 4 的 Router/Planner/Evaluator 正式接线、统一计划 Schema、负荷渐进校验、一次修订与评估结果持久化。
- Stage 5 的确认/拒绝/激活事务、完整 checkpoint 优先/业务 draft 兜底提交协议、计划页面和 SSE。
- Stage 6 的自然语言打卡确认。
- 聊天摘要、向量检索、后台记忆 Consolidation、通用草稿、旧 Run Harness。
- 新训练阈值、训练模板、PB 持久化、训练容量、估算 1RM、完成率或效果评价。
- README 历史内容清理及 Stage 2 已删除子任务文件重建。

阶段边界补充：总计划 §11 阶段 4 已写明“保存 draft 与评估结果”；不能将所有 draft 持久化笼统推迟到 Stage 5。Stage 3 仅用 fixture 建立计划事实，不提供生产写入口。

## 9. 实施顺序

```text
确认口径并同步依据
→ 最小 State 与 Memory 契约
→ 有界读取与 MemoryAssembler
→ Checkpointer 生命周期与重启测试
→ 两个 Skill 与渐进加载
→ 全量 Gate 与实际完成记录
```

## 10. Stage 3 收尾（Subtask 05）

§7 全部勾选；全量 Gate 实测：`cd backend && uv run pytest` 236 passed（Subtask 04 后 232 passed ＋ 本子任务新增 4 项）、`cd backend && uv run ruff check .` All checks passed!、`cd frontend && npm run build` 通过、`git diff --check` 无输出、`git status --short` 已核对既有 dirty worktree 与本阶段文件、`git diff --cached --name-only` 无输出；另以 `env -u MODEL_API_KEY` 重跑四个 Stage 3 测试文件 40 passed，证明无 API Key 时非模型业务与本阶段测试照常运行。本子任务新增测试索引：`test_stage3_memory_assembler.py::test_assembly_reflects_modification_of_an_existing_workout`、`test_stage3_skill_loader.py::test_real_startup_scan_opens_no_body_or_reference_file`、`test_stage3_checkpoint.py::test_fixed_minimal_graph_runs_fixed_nodes_on_the_frozen_state`／`::test_no_api_key_and_no_provider_config_reach_state_or_checkpoint`。

`git status --short` 逐字输出：

```text
 M "Fit-Agent-LangGraph-\351\207\215\346\236\204\350\256\250\350\256\272\346\200\273\347\273\223.md"
 M LANGGRAPH_REFACTOR_PLAN.md
 M backend/api/app.py
 M backend/config.py
 M backend/domain/records/repo.py
 M backend/domain/records/service.py
 M backend/domain/stats/schema.py
 M backend/domain/stats/service.py
 M backend/storage/migrations/002_timed_sets_and_new_actions.sql
 M backend/tests/test_stage2_api_stats.py
 M backend/tests/test_stage2_domain_calendar.py
 M backend/tests/test_stage2_domain_stats_pb.py
 M backend/tests/test_stage2_domain_stats_trend.py
 M backend/tests/test_stage2_migration.py
 M backend/tests/test_stage2_records_duration.py
 M frontend/src/features/dashboard/DashboardPage.tsx
 M frontend/src/lib/contract.ts
 M refactor-log/stage2.md
?? backend/graph/
?? backend/skills/
?? backend/tests/test_stage3_checkpoint.py
?? backend/tests/test_stage3_graph_state.py
?? backend/tests/test_stage3_memory_assembler.py
?? backend/tests/test_stage3_skill_loader.py
?? refactor-log/stage3.md
```

Stage 3 完成边界：交付最小 Graph State、固定范围 MemoryAssembler、独立 SQLite Checkpointer 生命周期与重启验证、两个核心 Skill 与渐进加载及其行为测试；未实施 Router／Planner／Evaluator 接线、统一计划 Schema、负荷渐进校验、确认／拒绝／激活事务、checkpoint 优先加业务 draft 兜底的提交协议、SSE 与自然语言打卡（Stage 4／5／6）。
