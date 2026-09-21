编号迁移 SQL 按序存放（`NNN_*.sql`），每个文件对应 `PRAGMA user_version` +1；`app/infrastructure/database/migrations.py` 逐个文件包在同一事务内执行，失败即整体回滚（版本不虚报成功）。

- `001_initial.sql`：七张业务表、24 项动作种子与 `idx_plans_single_active`。
- `002_timed_sets_and_new_actions.sql`：重建 `workout_sets`（`reps` 可空、新增 `duration_seconds`）与 `exercises`（负重口径新增 `external_added_weight`），追加计时与负重引体三个动作种子。
- `003_rejected_plan_status.sql`：重建 `plans`，`status` CHECK 加入 `rejected`；保留全部计划行、`source_plan_id` 自引用、`plan_sessions` 行、`workout_sessions.plan_session_id` 关联与 `idx_plans_single_active`；新增单 draft 部分唯一索引 `idx_plans_single_draft`；不新增 `rejected_at` 等任何计划状态时间列。
- `004_conversation_history.sql`：新建 `conversations`、`conversation_entries`、`conversation_runs`、`conversation_run_events` 四张对话历史表；Entry 在会话内按 `sequence` 单调追加（`UNIQUE(conversation_id, sequence)`）、`conversation_runs.thread_id`／`client_request_id` 全局唯一、`UNIQUE(run_id, sequence)` 事件保序、会话删除级联 entries／runs／events；不改动既有 7 张业务表。
