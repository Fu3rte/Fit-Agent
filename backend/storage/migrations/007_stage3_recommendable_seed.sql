-- 007：Stage 3 系统迁移 —— 已核对 24 项动作置为可推荐（D2 已拍 A，2026-09-11）
-- 依据：stage3.md §8 D2 A、architecture/03 3.2（只有经过检查才标记 recommendable=1）、
--       01 1.2（动作目录种子等系统写入不走草稿，也不得借系统写入绕过用户业务确认）。
-- 边界：只把 Stage 1 已核对的 24 项（003 种子清单）的 recommendable 置 1；不新增目录行、
--       不改 active／别名／负重口径／任何用户事实、不推进 context_version、不建立计数器。
--       停用行的已检查标记不被覆盖：置 1 与 active 无关，生成侧只查
--       active = 1 AND recommendable = 1（D2）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

UPDATE exercises SET recommendable = 1
WHERE id IN (
    'barbell-back-squat',
    'barbell-deadlift',
    'barbell-romanian-deadlift',
    'leg-press-45',
    'bulgarian-split-squat',
    'barbell-bench-press',
    'dumbbell-bench-press',
    'dumbbell-incline-bench-press',
    'seated-dumbbell-shoulder-press',
    'dumbbell-lateral-raise',
    'dumbbell-reverse-fly',
    'barbell-bent-over-row',
    'seated-cable-row',
    'one-arm-dumbbell-row',
    'lat-pulldown',
    'pull-up',
    'seated-leg-curl',
    'leg-extension',
    'machine-standing-calf-raise',
    'dumbbell-biceps-curl',
    'cable-pushdown',
    'cable-overhead-triceps-extension',
    'parallel-bar-dip',
    'hanging-leg-raise'
);
