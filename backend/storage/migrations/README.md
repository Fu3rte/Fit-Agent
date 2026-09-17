编号迁移 SQL 按序存放（`NNN_*.sql`），每个文件对应 `PRAGMA user_version` +1；`storage/migrations.py` 逐个文件包在同一事务内执行，失败即整体回滚（版本不虚报成功）。

- `001_initial.sql`：七张业务表、24 项动作种子与 `idx_plans_single_active`。
- `002_timed_sets_and_new_actions.sql`：重建 `workout_sets`（`reps` 可空、新增 `duration_seconds`）与 `exercises`（负重口径新增 `external_added_weight`），追加计时与负重引体三个动作种子。
- `003_rejected_plan_status.sql`：重建 `plans`，`status` CHECK 加入 `rejected`；保留全部计划行、`source_plan_id` 自引用、`plan_sessions` 行、`workout_sessions.plan_session_id` 关联与 `idx_plans_single_active`；新增单 draft 部分唯一索引 `idx_plans_single_draft`；不新增 `rejected_at` 等任何计划状态时间列。
