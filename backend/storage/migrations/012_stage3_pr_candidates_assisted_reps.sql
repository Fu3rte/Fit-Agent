-- 012：PR 候选视图修正 —— 排除携带 assisted_reps 的组（S3-12 残留⑦、06 验收 9）
-- 依据：architecture/06 6.3「排除人工辅助组」；010 只按 `assistance IS NOT 'assisted'` 过滤，
--       而 `assisted_reps` 有值、`assistance` 未标记的组合（历史／损坏数据；生产校验已拒，
--       见 domain/records/rules._validate_set）仍会进 PR。
-- 边界：只重建视图（DROP + CREATE），不改表、不新增结果表、不改写入路径、不发明统计口径。
--       010 已应用于既有库，故按编号迁移增量修正、不回改 010（07 7.2 编号迁移）。
-- 口径与 010 一致：记录词表（拍板 A）`reps_weight`、只取当前有效修订、排除回归期／热身组、
--       外加负重次数型且负重与次数明确；本迁移只追加 `assisted_reps IS NULL` 一条过滤。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

DROP VIEW IF EXISTS pr_candidates;
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
  AND t.assisted_reps IS NULL
  AND t.load_kg_key IS NOT NULL
  AND t.reps >= 1;
