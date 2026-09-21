-- 003：plans 状态加入 rejected 与单 draft 部分唯一索引
-- 依据：Fit-Agent-LangGraph-重构讨论总结.md §3.3／§3.4／§9（rejected 只由 Evaluator 二次阻断失败产生、
--       是终态、不新增 rejected_at，confirmed_at／archived_at 保持 NULL；任意时刻最多一个 active 计划）；
--       LANGGRAPH_REFACTOR_PLAN.md §5.5（状态四态与部分唯一索引）；
--       refactor-log/stage4.md §3.8／§5.2／§6 Subtask 02（状态与索引变化）。
-- 重建范围与原因：SQLite 无法 ALTER 既有 CHECK，状态取值域加入 rejected 必须重建 plans。
-- 数据安全（顺序即约束；连接在事务内无法关闭外键，故沿用 002 的「先暂存、再重建、后回填」）：
--   1. plans 行、plan_sessions 行与 workout_sessions.plan_session_id 关联分别暂存；
--   2. 先解开 workout_sessions 对 plan_sessions 的引用，再清空 plan_sessions 行、删除旧 plans——
--      DROP TABLE 的隐式删除会触发 plan_sessions 的 ON DELETE CASCADE，不先解开引用会丢日程行与
--      训练关联；
--   3. 旧 plans 删除后按原 DDL 重建（CHECK 加入 rejected，其余列与 source_plan_id 自引用不变），
--      按 id 顺序回填全部行（id 原样，AUTOINCREMENT 序号随最大 id 抬升，不重发身份）；
--   4. plan_sessions 行与 workout_sessions.plan_session_id 关联原样回填，与原库逐行一致。
--   5. PRAGMA defer_foreign_keys=ON：自引用与回填的立即外键检查推迟到本迁移的 COMMIT（迁移执行器
--      在同一事务内推进 user_version，失败即整体回滚并保持版本 2）。
-- 边界：不新增 rejected_at 或任何计划状态时间列；不建 View／统计表；不改 001／002；本文件不含
--       BEGIN/COMMIT/PRAGMA user_version，由 storage/migrations.py 统一包事务并推进到 3。

PRAGMA defer_foreign_keys=ON;

-- ---------- 1. 暂存三类事实 ----------
CREATE TABLE plans_backup AS SELECT * FROM plans;
CREATE TABLE plan_sessions_backup AS SELECT * FROM plan_sessions;
CREATE TABLE workout_plan_links_backup AS
    SELECT id, plan_session_id FROM workout_sessions WHERE plan_session_id IS NOT NULL;

-- ---------- 2. 解开引用并移除旧 plans ----------
UPDATE workout_sessions SET plan_session_id = NULL;
DELETE FROM plan_sessions;
DROP TABLE plans;

-- ---------- 3. 重建 plans：状态 CHECK 加入 rejected，其余约束不变 ----------
CREATE TABLE plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 单调版本号：只追加、恰好 +1（单调性由确认事务保证，UNIQUE 做兜底）。
    version INTEGER NOT NULL UNIQUE CHECK (version >= 1),
    -- draft → active → archived 与 draft 候选 → rejected（rejected 是终态，不带时间字段）。
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'archived', 'rejected')),
    -- 可追溯来源计划：调整基于旧计划生成，不脱离旧计划重生成。
    source_plan_id INTEGER REFERENCES plans(id),
    -- 结构化计划内容（形状正本：domain/plans/schema.py::PlanDraft；形状校验在领域层）。
    structured_content TEXT NOT NULL CHECK (json_valid(structured_content)),
    -- Evaluator 结果或失败原因所需的最小持久字段（可空；形状正本：EvaluationResult）。
    evaluator_result TEXT CHECK (evaluator_result IS NULL OR json_valid(evaluator_result)),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    archived_at TEXT
);

-- 任意时刻最多一条 active 计划（001 原索引，随旧表删除后按原名重建）。
CREATE UNIQUE INDEX idx_plans_single_active ON plans (status) WHERE status = 'active';
-- 任意时刻最多一条可确认 draft：作为最后防线（唯一 draft 的保证仍在服务层事务）。
CREATE UNIQUE INDEX idx_plans_single_draft ON plans (status) WHERE status = 'draft';

INSERT INTO plans (
    id, version, status, source_plan_id, structured_content, evaluator_result,
    created_at, confirmed_at, archived_at
)
SELECT id, version, status, source_plan_id, structured_content, evaluator_result,
       created_at, confirmed_at, archived_at
  FROM plans_backup
 ORDER BY id;

DROP TABLE plans_backup;

-- ---------- 4. 回填计划日程与训练关联 ----------
INSERT INTO plan_sessions (id, plan_id, scheduled_on, cancelled_at)
SELECT id, plan_id, scheduled_on, cancelled_at FROM plan_sessions_backup;

DROP TABLE plan_sessions_backup;

UPDATE workout_sessions
   SET plan_session_id = (
       SELECT link.plan_session_id
         FROM workout_plan_links_backup AS link
        WHERE link.id = workout_sessions.id
   )
 WHERE id IN (SELECT id FROM workout_plan_links_backup);

DROP TABLE workout_plan_links_backup;
