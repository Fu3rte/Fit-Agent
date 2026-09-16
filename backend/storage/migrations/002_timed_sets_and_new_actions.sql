-- 002：计时组字段与新增动作种子（负重引体／平板支撑／前水平）
-- 依据：Fit-Agent-LangGraph-重构讨论总结.md §3.1（计时动作时长统一秒、Domain 唯一校验、库不加范围 CHECK）、
--       §7.1（有效工作组：计时动作时长完整且大于 0）、§9（动作目录承载记录类型与负重口径）；
--       LANGGRAPH_REFACTOR_PLAN.md §5.4（workout_sets 记录计时时长；时长范围只由 Domain 唯一校验规则实施）、
--       §11 阶段 2（002 增加计时组字段与三个动作）；
--       refactor-log/stage2.md §3、refactor-log/stage2-subTasks/02-migration-and-records.md §A。
-- 已拍口径（Subtask 01 冻结）：duration_seconds 为不小于 1 的整数（秒），无业务上限；
--       范围只由 Domain 唯一规则实施、DTO 复用该规则，**本迁移不加时长范围 CHECK**。
-- 重建范围与原因：SQLite 无法 ALTER 既有 CHECK，`load_convention` 取值域新增 `external_added_weight`
--       必须重建 `exercises` 与 `workout_sets`；`reps` 改为可空、新增 `duration_seconds` 随同一次重建完成。
-- 数据安全（顺序即约束）：旧训练组行先搬进无约束暂存表 `workout_sets_backup`，旧 `workout_sets`
--       随即删除；此时引用 `exercises` 的子表为空，`DROP TABLE exercises` 的隐式删除才不触发
--       `PRAGMA foreign_keys=ON`（连接在事务内无法关闭外键）下的 FK 违规；动作目录重建完成后，
--       才把暂存行回写进新 `workout_sets`（id 原样，AUTOINCREMENT 序号随最大 id 抬升，不重发身份）。
-- 边界：不建 View、不建 PB／统计表、不改 `001_initial.sql`；本文件不含 BEGIN/COMMIT/PRAGMA user_version，
--       由 storage/migrations.py 统一包事务并推进到 2。

-- ---------- 1. 训练组：旧行暂存 + 新结构建表 + 移除旧表 ----------
CREATE TABLE workout_sets_backup AS SELECT * FROM workout_sets;

CREATE TABLE workout_sets_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    exercise_id TEXT NOT NULL REFERENCES exercises(id),
    -- 动作内组序号：1–50（一次训练总组数上限的逐动作子约束）。
    set_no INTEGER NOT NULL CHECK (set_no >= 1 AND set_no <= 50),
    -- 组类型固定 work / warmup / assisted（assisted 不计入 PB）。
    set_type TEXT NOT NULL CHECK (set_type IN ('work', 'warmup', 'assisted')),
    -- 负重口径与重量同现同隐：外加负重组两者齐备（重量允许 0kg），自重／计时组均为 NULL。
    -- `external_added_weight` 为独立负重引体的「外加重量（不含体重）」口径。
    load_convention TEXT CHECK (load_convention IS NULL OR load_convention IN (
        'barbell_includes_bar_total',
        'dumbbell_per_hand',
        'machine_pin_displayed_value',
        'plate_loaded_total_excluding_empty',
        'unilateral_setting_per_side',
        'external_added_weight'
    )),
    weight_kg REAL CHECK (
        weight_kg IS NULL
        OR (
            weight_kg >= 0 AND weight_kg <= 1000
            -- 外加重量：整数或最多一位小数（62.5kg 合法，62.55kg 拒绝）；容差抵消二进制浮点误差。
            AND ABS(weight_kg - ROUND(weight_kg, 1)) < 1e-9
        )
    ),
    -- 单组次数：计时组必须为空，故本列可空；有值时仍是 1–100（001 原有值域不变）。
    reps INTEGER CHECK (reps IS NULL OR (reps >= 1 AND reps <= 100)),
    -- 计时动作的单组持续秒数：可空整数。范围（不小于 1、无业务上限）只由 Domain 唯一规则实施，
    -- 本迁移刻意不加 CHECK（Subtask 01 冻结）。
    duration_seconds INTEGER,
    UNIQUE (workout_session_id, exercise_id, set_no),
    CHECK ((load_convention IS NULL) = (weight_kg IS NULL))
);

DROP TABLE workout_sets;

-- ---------- 2. 动作目录：负重口径取值域新增 external_added_weight + 三项新种子 ----------
CREATE TABLE exercises_new (
    -- 稳定动作身份：PB 分组与计划渐进的确定性来源。
    id TEXT PRIMARY KEY,
    standard_name_zh TEXT NOT NULL UNIQUE,
    equipment_variant TEXT NOT NULL,
    -- 记录口径：负重次数 / 自重次数 / 计时（只三类，不建辅助负重型）。
    record_type TEXT NOT NULL CHECK (record_type IN ('reps_weight', 'reps_bodyweight', 'time')),
    load_convention TEXT CHECK (load_convention IS NULL OR load_convention IN (
        'barbell_includes_bar_total',
        'dumbbell_per_hand',
        'machine_pin_displayed_value',
        'plate_loaded_total_excluding_empty',
        'unilateral_setting_per_side',
        'external_added_weight'
    )),
    min_load_increment_kg REAL CHECK (min_load_increment_kg IS NULL OR min_load_increment_kg > 0),
    recommendable INTEGER NOT NULL DEFAULT 0 CHECK (recommendable IN (0, 1)),
    modes_json TEXT NOT NULL CHECK (json_valid(modes_json)),
    source_ref TEXT NOT NULL,
    attribution TEXT NOT NULL,
    CHECK (
        (record_type = 'reps_weight'
            AND load_convention IS NOT NULL AND min_load_increment_kg IS NOT NULL)
        OR (record_type <> 'reps_weight'
            AND load_convention IS NULL AND min_load_increment_kg IS NULL)
    )
);

-- 完整复制既有 24 项种子（列名显式列出，顺序即 001 建表顺序）。
INSERT INTO exercises_new (
    id, standard_name_zh, equipment_variant, record_type, load_convention,
    min_load_increment_kg, recommendable, modes_json, source_ref, attribution
)
SELECT id, standard_name_zh, equipment_variant, record_type, load_convention,
       min_load_increment_kg, recommendable, modes_json, source_ref, attribution
FROM exercises;

DROP TABLE exercises;
ALTER TABLE exercises_new RENAME TO exercises;

-- 新增三项（用户拍板口径，source_ref / attribution 见下）：
--   weighted-pull-up：独立负重引体。外加重量口径（不含体重），最小加重 5kg；数据集 0841 weighted pull-up。
--   plank：平板支撑。计时动作，只记录秒数；exercises-dataset 无同名条目，故按 Stage 2 口径记为自建条目，
--          不引用任何数据集 ID（不得凭名称相似指向 side plank / front plank with twist 等变体）。
--   front-lever：前水平。计时动作；数据集 3296 front lever。
-- 负重口径与记录类型仍需跨列一致（表内 CHECK），自重／计时动作不虚构口径与加重单位。
INSERT INTO exercises (
    id, standard_name_zh, equipment_variant, record_type, load_convention,
    min_load_increment_kg, recommendable, modes_json, source_ref, attribution
) VALUES
    ('weighted-pull-up', '负重引体', 'weighted', 'reps_weight', 'external_added_weight', 5.0, 1,
     '["垂直拉"]', 'exercises-dataset:0841', '© Gym visual — https://gymvisual.com/'),
    ('plank', '平板支撑', 'bodyweight', 'time', NULL, NULL, 1,
     '["核心"]', 'refactor-log/stage2.md:§3', '无第三方媒体再分发 / Fit-Agent Stage 2 口径'),
    ('front-lever', '前水平', 'bodyweight', 'time', NULL, NULL, 1,
     '["核心"]', 'exercises-dataset:3296', '© Gym visual — https://gymvisual.com/');

-- `pull-up`（自重引体向上）保持 001 原样：reps_bodyweight、无负重口径、无加重单位，
-- 与 weighted-pull-up 是两个动作，PB 不得混算。

-- ---------- 3. 暂存行回写新训练组表（此时 exercises 已重建，外键可解析） ----------
-- 完整复制既有训练组（Stage 1 数据一行不丢）；计时列在旧数据上保持 NULL。
INSERT INTO workout_sets_new (
    id, workout_session_id, exercise_id, set_no, set_type, load_convention, weight_kg, reps,
    duration_seconds
)
SELECT id, workout_session_id, exercise_id, set_no, set_type, load_convention, weight_kg, reps, NULL
FROM workout_sets_backup;

DROP TABLE workout_sets_backup;
ALTER TABLE workout_sets_new RENAME TO workout_sets;

-- 索引与触发器随旧表一并删除，此处按 001 原名重建。
CREATE INDEX idx_workout_sets_exercise ON workout_sets (exercise_id);
CREATE INDEX idx_workout_sets_session ON workout_sets (workout_session_id);

-- 一次训练总组数上限 1–50（下限 1 由领域层在提交时保证）：CHECK 无法跨行计数，故用触发器。
CREATE TRIGGER trg_workout_sets_max_per_session
BEFORE INSERT ON workout_sets
WHEN (SELECT COUNT(*) FROM workout_sets WHERE workout_session_id = NEW.workout_session_id) >= 50
BEGIN
    SELECT RAISE(ABORT, '一次训练总组数不得超过 50');
END;
