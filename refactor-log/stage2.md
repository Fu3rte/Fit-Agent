# Stage 2：PB、单次日程状态、趋势与看板计划清单

> 状态：已完成——Subtask 01（口径冻结与契约）、02（`002` 迁移与训练记录链路）、03（Stats 基础与三类 PB）、04（趋势／月历／Stats API）、05（前端看板与缓存失效）、06（集成验证与 Stage 2 Gate）全部完成；Stage 2 Gate 通过（`cd backend && uv run pytest` 196 passed、`cd backend && uv run ruff check .` All checks passed!、`cd frontend && npm run build` 通过、`git diff --check` 无输出）  
> 前置阶段：Stage 1 Gate 已通过（134 passed、Ruff 通过、前端构建通过）  
> 权威顺序：`Fit-Agent-LangGraph-重构讨论总结.md` > `LANGGRAPH_REFACTOR_PLAN.md` > 已合入源码  
> 本清单已纳入本轮确认：PB 只保留最大重量、最大次数、最长时长；完全删除训练容量与估算 1RM；新增平板支撑、前水平和独立负重引体。

## 0. 已确认口径

- [x] PB 只包含 `weight_pb`、`reps_pb`、`duration_pb`。
- [x] 完全删除 `volume_pb`、训练容量趋势和训练总重量展示。
- [x] 完全删除 `estimated_1rm_pb` 和 Epley 公式。
- [x] PB 不持久化，从当前有效训练记录确定性现算。
- [x] PB 刷新日期使用来源训练的 `performed_on`。
- [x] 并列 PB 的来源取最早达成者（用户拍板口径 A）：依次取更小的 `performed_on`、来源训练身份和组序号；重复完成等值结果不刷新 PB 来源与日期。
- [x] 写入训练后由后端重算 PB；若刷新 PB，只显示提示，不询问用户是否更新。
- [x] 纯自重动作的 PB 是最大次数。
- [x] 计时动作的 PB 是最长持续秒数。
- [x] 新增计时动作：平板支撑、前水平。
- [x] 负重引体作为独立动作，不与纯自重引体混用。
- [x] 负重引体的重量只表示外加重量，不包含体重；最小加重单位为 5kg。
- [x] 哑铃重量沿用记录值，即单手重量，不乘 2。
- [x] 趋势图默认展示最近 30 天。
- [x] 体重和体脂变化取最近两条有效记录之差，不评价进步、退步或停滞。
- [x] 数据不足时返回明确状态，不补 0、不伪造变化值。
- [x] 月历请求参数使用 `YYYY-MM`，条目日期使用 `YYYY-MM-DD`。
- [x] 空白日期不显示“休息日”。
- [x] 只读取 active 计划的日程；额外训练显示为普通训练日。
- [x] `scheduled_on` 显示计划及完成状态，`performed_on` 显示实际训练；跨日时分别展示。
- [x] 不计算或展示完成率。
- [x] 统一有效工作组入口采用 repo 内共享 SQL，不新增数据库 View。
- [x] Stats API：`/api/stats/personal-bests`、`/api/stats/trends`、`/api/stats/calendar?month=YYYY-MM`。
- [x] `duration_seconds` 为不小于 1 的整数（秒），不设业务上限；该范围只由 Domain 唯一校验规则实施，DTO 复用同一规则，`002` 迁移不加时长范围 CHECK。
- [x] 力量趋势采用截至各日期的累计 PB（历史最好成绩，曲线不下降），默认沿用趋势图最近 30 天窗口。
- [x] 力量趋势由后端计算并通过 Stats API 暴露；Stage 2 前端看板不展示该曲线，也不新增动作选择 UI。

## 1. 已冻结的剩余两项口径（用户确认）

- [x] `duration_seconds` 的允许范围：不小于 1 的整数（秒），不设业务上限。该范围只由 Domain 唯一校验规则实施，DTO 复用同一规则，`002` 迁移不加时长范围 CHECK。
- [x] “力量趋势”的曲线口径：截至各日期的累计 PB（历史最好成绩，曲线不下降）；按动作计算——外部负重取截至该日期的最大重量、同重量次数取单组最大次数、纯自重取单组最大次数、计时动作取单组最长秒数。
- [x] 前后端边界：后端计算并通过 Stats API 暴露力量趋势；Stage 2 前端看板不展示力量趋势，也不新增动作选择 UI。

本节口径已全部冻结，无剩余待猜测项。

## 2. 权威文档同步

- [x] `Fit-Agent-LangGraph-重构讨论总结.md` 已改为三类 PB。
- [x] `Fit-Agent-LangGraph-重构讨论总结.md` 已删除训练容量与估算 1RM 要求。
- [x] `Fit-Agent-LangGraph-重构讨论总结.md` 已加入计时时长、30 天趋势和新动作口径。
- [x] `LANGGRAPH_REFACTOR_PLAN.md` 已同步三类 PB、`trend_summary`、看板、测试和 Stage 2 Gate。
- [x] `LANGGRAPH_REFACTOR_PLAN.md` 已明确 repo 共享 SQL、跨日月历展示和 `002` 迁移范围。

## 3. `002` 数据库迁移

- [x] 新建连续编号的 `backend/storage/migrations/002_*.sql`，不得回改已发布的 `001_initial.sql`。
- [x] 重建 `workout_sets`，将 `reps` 改为可空。
- [x] 新增可空整数列 `duration_seconds`（允许范围不小于 1 的整数，无业务上限；迁移不加范围 CHECK）。
- [x] 保留训练组外键、唯一约束、组类型约束和每次训练最多 50 组的约束/触发器。
- [x] 数据迁移时完整复制现有 Stage 1 训练组。
- [x] 增加外加重量口径 `external_added_weight`，用于独立负重引体。
- [x] 新增 `weighted-pull-up`：负重引体，外加重量动作，`min_load_increment_kg=5`。
- [x] 新增 `plank`：平板支撑，计时动作。
- [x] 新增 `front-lever`：前水平，计时动作。
- [x] 纯自重 `pull-up` 保持次数动作，不改变既有语义。
- [x] 迁移后 `user_version=2`。
- [x] 测试首次升级、重复启动和旧训练组数据保留。

## 4. 训练记录 Domain 与 API 适配

涉及：

- `backend/domain/actions/`
- `backend/domain/records/`
- `backend/api/dto.py`
- `backend/api/routes_records.py`

任务：

- [x] 动作目录支持 `external_added_weight`。
- [x] 训练组模型增加 `duration_seconds: int | None`，并由 Domain 唯一规则校验不小于 1 的整数，DTO 复用该规则。
- [x] 外加重量动作要求 `weight_kg` 与 `reps`，禁止 `duration_seconds`。
- [x] 纯自重动作要求 `reps`，禁止重量和时长。
- [x] 计时动作要求 `duration_seconds`（不小于 1 的整数，无业务上限），禁止重量和次数。
- [x] `work / warmup / assisted` 组类型保持不变。
- [x] 新增、读取、整条覆盖修改和删除均支持计时组。
- [x] API DTO 与领域对象转换保持单一实现，不建立第二套规则。
- [x] 错误继续映射为现有 4xx 响应结构。
- [x] 表单写入仍不经过 Agent 或通用草稿。

## 5. 前端记录页适配

涉及：

- `frontend/src/lib/contract.ts`
- `frontend/src/lib/api.ts`
- `frontend/src/features/records/RecordsPage.tsx`

任务：

- [x] 传输类型增加 `duration_seconds`。
- [x] 外加重量动作显示重量和次数输入。
- [x] 纯自重动作只显示次数输入。
- [x] 计时动作只显示秒数输入。
- [x] 负重引体与纯自重引体作为两个动作选项。
- [x] 编辑时正确回填对应输入，不保留不适用字段。
- [x] 成功写入后失效记录、PB、趋势、月历和计划日程状态 Query。

## 6. Stats Domain

新建：

```text
backend/domain/stats/
├── __init__.py
├── schema.py
├── repo.py
└── service.py
```

约束：

- [x] SQL 只放在 repo；service 只编排查询和确定性计算。
- [x] 只读查询使用 `Database.under_lock()`。
- [x] 统一有效工作组使用 repo 内共享 SQL，不建立 View。
- [x] 不新增 `personal_bests` 表或统计结果表。
- [x] 不调用模型。
- [x] 不在 repo/service 中调用 `date.today()`。

### 6.1 有效工作组

- [x] 训练组行仍存在，等价于训练记录未删除。
- [x] 仅包含 `set_type='work'`。
- [x] 排除 `warmup` 和 `assisted`。
- [x] 外加重量动作要求重量完整、次数不少于 1。
- [x] 纯自重动作要求次数不少于 1。
- [x] 计时动作要求时长完整且大于 0。
- [x] 查询返回动作、记录类型、负重口径、训练 ID、组序号和 `performed_on`。

### 6.2 三类 PB

#### `weight_pb`

- [x] 同动作、同负重口径取至少完成 1 次的单组最大实际重量。
- [x] 次数只作为来源组事实，不参与 PB 数值计算。
- [x] 负重引体只比较外加重量。

#### `reps_pb`

- [x] 外加重量动作按相同重量分别取单组最大次数。
- [x] 纯自重动作直接取单组最大次数。
- [x] 多组次数不得累加为次数 PB。

#### `duration_pb`

- [x] 仅用于计时动作。
- [x] 取单组最大 `duration_seconds`。

#### 公共返回信息

- [x] 返回 `exercise_id`、动作名称、PB 类型和值。
- [x] 返回适用的重量/负重口径。
- [x] 返回来源训练 ID、来源组序号和 `performed_on`。
- [x] 并列值使用稳定、可测试的来源排序：取最早达成者（`performed_on` → 来源训练身份 → 组序号，取更小者）；重复完成等值结果不刷新 PB 来源与日期（口径 A，见第 0 节）。
- [x] 修改或删除记录后查询结果自然更新。

## 7. 趋势与 `trend_summary`

- [x] 体重趋势默认查询最近 30 天原始点。
- [x] 体脂趋势默认查询最近 30 天非空原始点。
- [x] 力量趋势实现为截至各日期的累计 PB（曲线不下降）：外部负重取截至该日期的最大重量、同重量次数取单组最大次数、纯自重取单组最大次数、计时动作取单组最长秒数；默认窗口沿用最近 30 天。
- [x] 体重变化取最近两条有效体重记录之差。
- [x] 体脂变化取最近两条非空体脂记录之差。
- [x] 距上次训练天数使用注入的业务日期和最近 `performed_on`。
- [x] 数据足够时返回当前值、前值、差值及两次日期。
- [x] 无记录返回 `no_data`。
- [x] 只有一条记录返回 `insufficient_data`。
- [x] `trend_summary` 不包含训练容量或计划完成率。
- [x] 后端不输出进步、退步、停滞等效果评价。
- [x] 同一统计服务供看板和 Stage 3 MemoryAssembler 复用。

## 8. 月历与单次日程状态

- [x] 接收严格的 `month=YYYY-MM`。
- [x] 返回条目日期 `YYYY-MM-DD`。
- [x] 读取当前 active 计划及其日程。
- [x] `scheduled_on` 返回计划、取消和完成状态。
- [x] 完成由是否存在关联训练现算，不新增完成字段。
- [x] `performed_on` 返回实际训练标记。
- [x] 跨日关联时，计划状态和实际训练分别落在各自日期。
- [x] `plan_session_id IS NULL` 的额外训练仍显示为训练日，但不完成任何日程。
- [x] 无训练且无计划的日期不返回“休息日”文案。
- [x] 同一日程最多一个有效训练，继续由现有唯一约束兜底。
- [x] 不计算周/月完成率，不返回分子、分母或百分比。
- [x] Stage 2 测试使用 fixture/直接 SQL 写入 active 计划和日程；正式计划写入口仍留 Stage 5。

## 9. Stats API

新建 `backend/api/routes_stats.py`，并在静态前端兜底前注册。

- [x] `GET /api/stats/personal-bests`
- [x] `GET /api/stats/trends`
- [x] `GET /api/stats/calendar?month=YYYY-MM`
- [x] 在 `backend/api/dto.py` 增加唯一响应 DTO 与传输映射。
- [x] 延续 snake_case 字段和对象包裹响应。
- [x] 月份参数由 Pydantic 校验；非法月份返回明确 4xx。
- [x] 路由只调用 service，不写 SQL 或复制统计规则。
- [x] 业务日期通过 `current_business_date` 注入。

## 10. 数据看板

- [x] 图表只新增 Recharts，不新增状态管理或其他图表依赖；按用户决定增加 Radix 基础 shadcn Tooltip 展示窄列完整状态。
- [x] 在 `frontend/src/lib/contract.ts` 增加 PB、趋势和月历类型。
- [x] 在 `frontend/src/lib/api.ts` 增加三个 Stats 请求函数。
- [x] 新建 `frontend/src/features/dashboard/DashboardPage.tsx`。
- [x] 在 `frontend/src/app/App.tsx` 增加看板导航和路由。
- [x] 月历显示计划状态、单次完成状态和实际训练标记。
- [x] 跨日时分别显示计划事实与训练事实；跨月完成通过日程 `actual_performed_on` 显示真实训练日期。
- [x] 空白日期保持空白，不显示“休息日”。
- [x] 展示最近 30 天体重折线图。
- [x] 展示最近 30 天体脂折线图。
- [x] Stage 2 前端不展示力量趋势（后端已计算并经 Stats API 暴露）；不新增动作选择 UI。
- [x] 展示最大重量、最大次数、最长时长 PB 和来源日期。
- [x] 无数据时显示空态，不用 0 补线。
- [x] 前端不重算 PB、趋势或日程状态。
- [x] 不展示训练容量、估算 1RM 或完成率。

实现与验证索引：`refactor-log/stage2-subTasks/05-frontend-dashboard.md`（契约字段映射、Query key、写入后全量失效与验证记录）。

## 11. 测试清单

建议新增：

```text
backend/tests/test_stage2_migration.py
backend/tests/test_stage2_records_duration.py
backend/tests/test_stage2_domain_stats_pb.py
backend/tests/test_stage2_domain_stats_trend.py
backend/tests/test_stage2_domain_calendar.py
backend/tests/test_stage2_api_stats.py
```

### 11.1 迁移与记录

- [x] `001 → 002` 保留既有训练数据。
- [x] 重复启动不重复迁移或覆盖数据。
- [x] 三种动作记录类型的必填/互斥字段正确。
- [x] `duration_seconds` 为 0、负数或非整数时被拒绝；无业务上限（超大整数仍可写入）。
- [x] 平板支撑和前水平种子正确。
- [x] 负重引体外加重量口径与 5kg 最小增量正确。

### 11.2 PB

- [x] `work` 组可刷新 PB。
- [x] `warmup`、`assisted` 和不完整组不能刷新 PB。
- [x] 最大重量只比较重量，不使用次数计算。
- [x] 外加重量动作按同重量计算最大次数。
- [x] 纯自重动作计算单组最大次数。
- [x] 平板支撑和前水平计算最长秒数。
- [x] 纯自重引体与负重引体不混算。
- [x] 修改和删除记录后 PB 立即重算。
- [x] PB 来源训练、组序号和日期正确。
- [x] 不存在容量 PB、估算 1RM 或 PB 结果表。

### 11.3 趋势

- [x] 最近 30 天边界正确。
- [x] 累计 PB 力量趋势按日期单调不下降，且可由固定数据库输入复算；后端暴露该趋势。
- [x] 体重和体脂取最近两条有效记录之差。
- [x] 无数据返回 `no_data`。
- [x] 一条记录返回 `insufficient_data`。
- [x] 距上次训练天数由固定业务日期复算。
- [x] 输出不含容量、完成率或效果评价。

### 11.4 月历与 API

- [x] active 计划日程可见，非 active 计划不进入首版月历。
- [x] 取消、未完成和已完成状态正确。
- [x] 实际训练按 `performed_on` 显示。
- [x] 跨日关联在两个日期分别展示。
- [x] 额外训练不完成日程。
- [x] 同一日程最多完成一次。
- [x] `YYYY-MM` 合法/非法输入正确。
- [x] Stats Router 不被静态兜底吞掉。
- [x] API 响应与前端契约一致（Subtask 05 已在 `frontend/src/lib/contract.ts` 增加类型，并逐字段核对下述字段集合：`personal_bests` 九字段；`trends` 的 `window_days`／`from`／`to`／`weight`／`body_fat`／`strength`／`trend_summary`；`calendar` 的 `month`／`from`／`to`／`days` 与条目内 `plan_sessions`／`workouts` 字段，核对依据为本文件中的 API 测试断言）。

## 12. Stage 2 Gate

- [x] `002` 迁移安全升级并保留 Stage 1 数据。→ `test_stage2_migration.py::test_upgrade_from_001_keeps_stage1_data_and_sets_user_version_two`／`::test_upgrade_is_repeatable_and_does_not_overwrite_data`／`::test_rebuilt_table_keeps_constraints_indexes_and_trigger`
- [x] 表单可记录外加重量、纯自重和计时动作。→ `test_stage2_records_duration.py`（领域 CRUD、传输 CRUD、三类字段互斥、时长边界）
- [x] 最大重量、最大次数、最长时长三类 PB 可演示。→ `test_stage2_domain_stats_pb.py`、`test_stage2_api_stats.py`、`06-stage2-gate.md` 端到端 DOM 核对
- [x] 修改或删除记录后 PB 与趋势立即一致。→ `test_stage2_api_stats.py::test_pb_and_trends_recompute_after_edit_through_the_form_api`／`::test_personal_bests_and_trends_recompute_after_record_deletion`
- [x] 热身组、辅助组和不完整组不得刷新 PB。→ `test_stage2_domain_stats_pb.py::test_personal_bests_only_count_valid_work_sets`／`::test_incomplete_sets_do_not_refresh_pb`、`test_stage2_domain_stats_trend.py::test_strength_trend_excludes_warmup_assisted_and_incomplete_sets`
- [x] 纯自重引体和负重引体不混算。→ `test_stage2_domain_stats_pb.py::test_bodyweight_reps_pb_is_single_set_max_and_separate_from_weighted_pull_up`、`test_stage2_api_stats.py::test_three_record_types_flow_from_the_form_api_into_three_pb_types`
- [x] 最近 30 天体重与体脂趋势可演示；力量趋势由后端按累计 PB 计算并经 Stats API 暴露（Stage 2 前端不展示）。→ `test_stage2_domain_stats_trend.py::test_strength_trend_*`、`test_stage2_api_stats.py::test_trends_endpoint_uses_the_injected_business_date`；前端禁显核对（DOM 中 `力量趋势` 命中 0）见 `06-stage2-gate.md`
- [x] `trend_summary` 的体重变化、体脂变化和停训天数可由固定输入复算。→ `test_stage2_domain_stats_trend.py::test_trend_summary_uses_the_latest_two_weight_and_body_fat_records`／`::test_trend_summary_reports_no_data_and_insufficient_data`／`::test_trend_summary_days_since_last_workout_uses_injected_business_date`
- [x] 月历正确区分计划日、完成状态、实际训练日和额外训练。→ `test_stage2_domain_calendar.py`、`test_stage2_api_stats.py::test_calendar_endpoint_returns_month_facts`、`06-stage2-gate.md` 端到端 DOM 核对
- [x] 系统不计算或展示训练容量、估算 1RM 或完成率。→ `test_stage2_migration.py::test_schema_has_no_volume_or_estimated_1rm_columns`、`test_stage2_domain_stats_pb.py::test_personal_best_output_has_only_three_types_and_source_facts`、`test_stage2_domain_stats_trend.py::test_trend_output_has_no_volume_completion_rate_or_evaluation`、`test_stage2_domain_calendar.py::test_calendar_output_has_no_completion_rate_or_rest_day_fields`
- [x] `cd backend && uv run pytest` 通过。→ 196 passed
- [x] `cd backend && uv run ruff check .` 通过。→ All checks passed!
- [x] `cd frontend && npm run build` 通过。→ 通过（`tsc -b` + `vite build`）

## 13. 明确不做

- 训练总重量、训练容量和容量 PB。
- 估算 1RM。
- 完成率、计划次数与实际次数对比。
- PB 持久化表。
- 数据库有效工作组 View。
- 进步、退步、停滞或疲劳判断。
- 计划创建、确认或激活入口（Stage 5）。
- 前水平难度变式、最快时间、距离和功率 PB。
- Stage 3 MemoryAssembler、Checkpoint 和 Skill Loader。

## 14. 建议实施顺序

```text
确认剩余两项口径
→ 002 迁移与动作种子
→ records domain/API/frontend 适配
→ stats schema/repo/service
→ 三类 PB
→ trend_summary
→ 月历状态
→ Stats API
→ 后端测试与 Gate
→ Recharts 看板
→ 缓存失效
→ 全量 Gate
```

Stage 2 收尾（Subtask 06）：§12 Gate 全部通过；`uv run pytest` 196 passed、`uv run ruff check .` All checks passed、`npm run build` 通过、`git diff --check` 无输出；跨层回归新增 3 项（`test_stage2_api_stats.py` 11→13、`test_stage2_migration.py` 5→6），看板闭环以真实后端 + 无头 Chromium DOM 核对留档（`06-stage2-gate.md`）。Stage 3 MemoryAssembler／Checkpoint／Skill Loader 与 Stage 5 计划写入口仍未实施。
