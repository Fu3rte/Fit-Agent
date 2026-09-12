-- 013：动作目录补充主要肌群字段与首批 24 项取值（S4-04 修复；03 3.1 已拍补充）
-- 依据：architecture/03 §3.1「本章已拍结论」的肌群补充条目、architecture/04 §4.7（同等刺激
--       替换要求主要肌群交集）、design-decisions「同等刺激替换的确定性等价」。
-- 来源：exercises-dataset 上游 ``data/exercises.json``（pinned commit
--       7455efae41b330c265e7cd4b78dfa848e7ce5ebd）的 ``target`` 字段**逐行原样**写入，
--       24/24 行按精确 id 核对、无缺失、无歧义、无猜测、无模糊匹配；``muscle_group`` 与
--       ``secondary_muscles`` 是协力肌来源，不入本列。项目内 action id ↔ 上游 id 的对应
--       关系沿用 003 的 ``source_ref``，本迁移不重复记录。
-- 边界：只新增一列并写 24 行取值；不新增表、不改记录口径／负重口径／动作模式、不动
--       aliases_json、不改写入路径、不导入媒体与说明文本。003 已应用于既有库，故按编号
--       迁移增量补充、不回改 003（07 7.2 编号迁移）。
-- 保守后果（已接受、非缺陷）：上游 ``target`` 为 glutes 与 quads 的深蹲族在主要肌群上无交集，
--       故按 §4.7 判定为不等价；这是来源数据的既有口径，不做医学修正、不扩词表、不放宽规则。
-- 未收录动作保持 NULL：缺失或歧义数据不得猜测，替换等价在 NULL 上 fail-closed。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

ALTER TABLE exercises ADD COLUMN muscle TEXT;

UPDATE exercises
SET muscle = CASE id
    WHEN 'barbell-back-squat' THEN 'glutes'
    WHEN 'barbell-deadlift' THEN 'glutes'
    WHEN 'barbell-romanian-deadlift' THEN 'glutes'
    WHEN 'leg-press-45' THEN 'glutes'
    WHEN 'bulgarian-split-squat' THEN 'quads'
    WHEN 'barbell-bench-press' THEN 'pectorals'
    WHEN 'dumbbell-bench-press' THEN 'pectorals'
    WHEN 'dumbbell-incline-bench-press' THEN 'pectorals'
    WHEN 'seated-dumbbell-shoulder-press' THEN 'delts'
    WHEN 'dumbbell-lateral-raise' THEN 'delts'
    WHEN 'dumbbell-reverse-fly' THEN 'delts'
    WHEN 'barbell-bent-over-row' THEN 'upper back'
    WHEN 'seated-cable-row' THEN 'upper back'
    WHEN 'one-arm-dumbbell-row' THEN 'upper back'
    WHEN 'lat-pulldown' THEN 'lats'
    WHEN 'pull-up' THEN 'lats'
    WHEN 'seated-leg-curl' THEN 'hamstrings'
    WHEN 'leg-extension' THEN 'quads'
    WHEN 'machine-standing-calf-raise' THEN 'calves'
    WHEN 'dumbbell-biceps-curl' THEN 'biceps'
    WHEN 'cable-pushdown' THEN 'triceps'
    WHEN 'cable-overhead-triceps-extension' THEN 'triceps'
    WHEN 'parallel-bar-dip' THEN 'pectorals'
    WHEN 'hanging-leg-raise' THEN 'abs'
END
WHERE id IN (
    'barbell-back-squat', 'barbell-deadlift', 'barbell-romanian-deadlift',
    'leg-press-45', 'bulgarian-split-squat', 'barbell-bench-press',
    'dumbbell-bench-press', 'dumbbell-incline-bench-press',
    'seated-dumbbell-shoulder-press', 'dumbbell-lateral-raise',
    'dumbbell-reverse-fly', 'barbell-bent-over-row', 'seated-cable-row',
    'one-arm-dumbbell-row', 'lat-pulldown', 'pull-up', 'seated-leg-curl',
    'leg-extension', 'machine-standing-calf-raise', 'dumbbell-biceps-curl',
    'cable-pushdown', 'cable-overhead-triceps-extension', 'parallel-bar-dip',
    'hanging-leg-raise'
);
