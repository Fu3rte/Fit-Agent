-- 010：Stage 3 统计侧 PR 候选视图 —— pr_candidates
-- 依据：stage3.md §5 S3-12、architecture/06 6.3（PR 只经视图筛选后现算，不存当前 PR）、
--       07 7.2（编号迁移 + PRAGMA user_version）。
-- 边界：只建视图，不建统计结果表、不建 reviews（S3-13）、不复制完成率或三桶口径。
-- 记录口径（S3-09 拍板 A）：`exercise_logs.record_type` 取**动作目录词表** `reps_weight`，
--       不是已验证基准报告 §6.1 的处方词表 `external_load_reps`（两者经
--       domain/plan/rules.CATALOG_RECORD_TYPE_TO_PRESCRIPTION 映射）；照抄报告原词会得到空结果。
-- 现算口径（06 6.3）：
--   * 只取**当前**修订（`training_sessions.current_revision_id`），旧修订天然不参与；
--   * 只取正式有效修订（`status='valid'`）：草稿不在这些表里，待补全／已作废当前修订被排除；
--   * 排除回归期（`is_return_phase=1`）与热身组（`set_type='work'` 才算工作组）；
--   * 排除人工辅助组：只认 `assisted`（全程未接触的旁边保护与未申报辅助按独立完成处理，
--     05 5.5 的「未提辅助」不物化 `none`，S3-11 交接①把该解释放在本任务）；
--   * 外加负重次数型且次数明确（`load_kg_key`／`reps` 非空）：自重／计时型不套用重量 PR。
--   * 同一动作、器械变式（口径）与换算整数键分别比较：同重量最高次数由查询侧按单组
--     `MAX(reps)` 聚合，不累计多组（06 验收 7）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

CREATE VIEW pr_candidates AS
SELECT s.id AS session_id,
       r.id AS revision_id,
       r.occurred_on AS occurred_on,
       e.exercise_id AS exercise_id,
       e.load_notation AS load_notation,
       t.load_kg_key AS load_kg_key,
       t.reps AS reps
FROM training_sessions AS s
JOIN session_revisions AS r ON r.id = s.current_revision_id
JOIN exercise_logs AS e ON e.session_revision_id = r.id
JOIN training_sets AS t ON t.exercise_log_id = e.id
WHERE r.status = 'valid'
  AND r.is_return_phase = 0
  AND e.record_type = 'reps_weight'
  AND t.set_type = 'work'
  AND t.assistance IS NOT 'assisted'
  AND t.load_kg_key IS NOT NULL
  AND t.reps >= 1;
