-- 001：Fit-Agent LangGraph 新版最小业务 Schema（重构后从 001 重新开始）
-- 依据：Fit-Agent-LangGraph-重构讨论总结.md §5（记忆分层）／§7（PB 口径）／§9（数据模型建议）、
--       LANGGRAPH_REFACTOR_PLAN.md §5（新版数据模型实施）、Stage 1 子任务 01（口径与数据基座）。
-- 已拍口径（本阶段）：1A 训练记录物理删除；3A athlete_profile 三态 JSON；4A plans.structured_content
--       复用旧 PlanPayload 基本形状（去除 RIR／固定 PPL 模板）；5A 保留现有 24 项动作种子。
-- 数值范围：体重 20–400kg；体脂 0–100%；外加重量 0–1000kg（整数或最多一位小数）；
--       单组次数 1–100；一次训练总组数 1–50（外加重量精度为已拍口径，见 workout_sets CHECK）。
-- 边界：只建总结要求的 7 张业务表；不建 LangGraph Checkpoint 表（由 Checkpointer 自管，§5.6）；
--       不建 personal_bests／完成率／向量等表。RIR 不出现在任何列。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

-- ---------- 动作目录 ----------
CREATE TABLE exercises (
    -- 稳定动作身份：PB 分组与计划渐进的确定性来源（总结 §5.2、§9）。
    id TEXT PRIMARY KEY,
    standard_name_zh TEXT NOT NULL UNIQUE,
    -- 器械变式。
    equipment_variant TEXT NOT NULL,
    -- 记录口径：负重次数 / 自重次数 / 计时（总结 §9 只三类，不建辅助负重型）。
    record_type TEXT NOT NULL CHECK (record_type IN ('reps_weight', 'reps_bodyweight', 'time')),
    -- 负重口径：仅外加负重类型需要，自重／计时必须为 NULL（不虚构 0kg）。
    load_convention TEXT CHECK (load_convention IS NULL OR load_convention IN (
        'barbell_includes_bar_total',
        'dumbbell_per_hand',
        'machine_pin_displayed_value',
        'plate_loaded_total_excluding_empty',
        'unilateral_setting_per_side'
    )),
    -- 最小加重单位（kg）：仅外加负重类型需要；自重／计时为 NULL（禁止猜重量，§7.3）。
    min_load_increment_kg REAL CHECK (min_load_increment_kg IS NULL OR min_load_increment_kg > 0),
    -- 是否可用于计划：默认 0（新插入不自动置 1）；已核验的动作种子显式置 1（reviewer P1-1 决策 A）。
    recommendable INTEGER NOT NULL DEFAULT 0 CHECK (recommendable IN (0, 1)),
    -- 动作模式分类（13 项已拍词表的子集；元素级校验在领域层）。
    modes_json TEXT NOT NULL CHECK (json_valid(modes_json)),
    -- 来源与许可：核不上的条目不导入（保留数据集 copyright 声明）。
    source_ref TEXT NOT NULL,
    attribution TEXT NOT NULL,
    -- 负重口径与最小加重单位只属于外加负重类型，不为其自重／计时虚构。
    CHECK (
        (record_type = 'reps_weight'
            AND load_convention IS NOT NULL AND min_load_increment_kg IS NOT NULL)
        OR (record_type <> 'reps_weight'
            AND load_convention IS NULL AND min_load_increment_kg IS NULL)
    )
);

-- ---------- 长期画像（单用户单例行） ----------
CREATE TABLE athlete_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    -- 三态 JSON（每字段 {state: unknown|denied|known, value}）：未填写与明确为空可区分，
    -- 不静默混为默认值（总结 §5.1）。NULL = 未建档技术载体。
    profile_json TEXT CHECK (profile_json IS NULL OR json_valid(profile_json))
);

-- 只建立「未建档」技术载体：不预填目标／伤病／禁用动作。
INSERT INTO athlete_profile (id, profile_json) VALUES (1, NULL);

-- ---------- 身体指标 ----------
CREATE TABLE body_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 发生日期（业务时区自然日；格式与合法性由领域层校验）。
    measured_on TEXT NOT NULL,
    -- 体重必填：20–400kg。
    weight_kg REAL NOT NULL CHECK (weight_kg >= 20 AND weight_kg <= 400),
    -- 体脂可选：0–100%；无数据保持 NULL，不补 0。
    body_fat_pct REAL CHECK (body_fat_pct IS NULL OR (body_fat_pct >= 0 AND body_fat_pct <= 100))
);

CREATE INDEX idx_body_metrics_measured_on ON body_metrics (measured_on);

-- ---------- 计划 ----------
CREATE TABLE plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 单调版本号：只追加、恰好 +1（单调性由确认事务保证，UNIQUE 做兜底）。
    version INTEGER NOT NULL UNIQUE CHECK (version >= 1),
    -- draft → active → archived（总结 §3.3）。
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'archived')),
    -- 可追溯来源计划：调整基于旧计划生成，不脱离旧计划重生成（总结 §3.4）。
    source_plan_id INTEGER REFERENCES plans(id),
    -- 结构化计划内容（Stage 1 复用旧 PlanPayload 基本形状；形状校验在领域层）。
    structured_content TEXT NOT NULL CHECK (json_valid(structured_content)),
    -- Evaluator 结果或失败原因所需的最小持久字段（可空）。
    evaluator_result TEXT CHECK (evaluator_result IS NULL OR json_valid(evaluator_result)),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    archived_at TEXT
);

-- 任意时刻最多一条 active 计划（总结 §9、§14）。
CREATE UNIQUE INDEX idx_plans_single_active ON plans (status) WHERE status = 'active';

-- 计划日程：只保留单次完成需要的事实（不建完成率分子／分母）。
CREATE TABLE plan_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    -- 应训练日（业务时区自然日）。
    scheduled_on TEXT NOT NULL,
    -- 取消状态：激活新计划时取消旧计划尚未到期日程（总结 §9）。
    cancelled_at TEXT,
    -- 同一计划同一日历日至多一个名额。
    UNIQUE (plan_id, scheduled_on)
);

-- ---------- 训练记录 ----------
CREATE TABLE workout_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    performed_on TEXT NOT NULL,
    -- 可空外键：NULL 表示额外训练；关联有效训练即计为完成对应日程（总结 §9）。
    plan_session_id INTEGER REFERENCES plan_sessions(id),
    -- 同一计划日程最多关联一条有效训练（物理删除口径下用普通 UNIQUE；NULL 可重复）。
    UNIQUE (plan_session_id)
);

CREATE TABLE workout_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
    exercise_id TEXT NOT NULL REFERENCES exercises(id),
    -- 动作内组序号：1–50（一次训练总组数上限的逐动作子约束）。
    set_no INTEGER NOT NULL CHECK (set_no >= 1 AND set_no <= 50),
    -- 组类型固定 work / warmup / assisted（assisted 不计入 PB，总结 §7.1、§9）。
    set_type TEXT NOT NULL CHECK (set_type IN ('work', 'warmup', 'assisted')),
    -- 负重口径与重量同现同隐：外加负重组两者齐备（重量允许 0kg），自重／计数组均为 NULL。
    load_convention TEXT CHECK (load_convention IS NULL OR load_convention IN (
        'barbell_includes_bar_total',
        'dumbbell_per_hand',
        'machine_pin_displayed_value',
        'plate_loaded_total_excluding_empty',
        'unilateral_setting_per_side'
    )),
    weight_kg REAL CHECK (
        weight_kg IS NULL
        OR (
            weight_kg >= 0 AND weight_kg <= 1000
            -- 外加重量：整数或最多一位小数（62.5kg 合法，62.55kg 拒绝）；容差抵消二进制浮点误差。
            AND ABS(weight_kg - ROUND(weight_kg, 1)) < 1e-9
        )
    ),
    -- 单组次数必填：1–100。
    reps INTEGER NOT NULL CHECK (reps >= 1 AND reps <= 100),
    UNIQUE (workout_session_id, exercise_id, set_no),
    CHECK ((load_convention IS NULL) = (weight_kg IS NULL))
);

-- 按动作取有效记录现算 PB／趋势（总结 §7.1）。
CREATE INDEX idx_workout_sets_exercise ON workout_sets (exercise_id);
-- 按训练取组（外键无自动索引）。
CREATE INDEX idx_workout_sets_session ON workout_sets (workout_session_id);

-- 一次训练总组数上限 1–50（下限 1 由领域层在提交时保证）：CHECK 无法跨行计数，故用触发器。
CREATE TRIGGER trg_workout_sets_max_per_session
BEFORE INSERT ON workout_sets
WHEN (SELECT COUNT(*) FROM workout_sets WHERE workout_session_id = NEW.workout_session_id) >= 50
BEGIN
    SELECT RAISE(ABORT, '一次训练总组数不得超过 50');
END;

-- ---------- 动作种子（保留 5A 已确认的现有 24 项；新增 min_load_increment_kg） ----------
-- 来源：exercises-dataset（MIT，© Hasan Emir Yıldırım）。min_load_increment 按器械：
--       杠铃 2.5kg、哑铃 2.5kg/手、器械销 5kg、杠铃片总重 2.5kg、自重动作 NULL。
-- 24 项均为已核验动作，recommendable 全部显式置 1（reviewer P1-1 用户拍板 A）。
INSERT INTO exercises (
    id, standard_name_zh, equipment_variant, record_type, load_convention,
    min_load_increment_kg, recommendable, modes_json, source_ref, attribution
) VALUES
    ('barbell-back-squat', '杠铃背蹲', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 2.5, 1, '["深蹲"]', 'exercises-dataset:0043', '© Gym visual — https://gymvisual.com/'),
    ('barbell-deadlift', '杠铃传统硬拉', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 2.5, 1, '["髋铰链"]', 'exercises-dataset:0032', '© Gym visual — https://gymvisual.com/'),
    ('barbell-romanian-deadlift', '杠铃罗马尼亚硬拉', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 2.5, 1, '["髋铰链"]', 'exercises-dataset:0085', '© Gym visual — https://gymvisual.com/'),
    ('leg-press-45', '45°腿举', 'sled_machine', 'reps_weight', 'plate_loaded_total_excluding_empty', 2.5, 1, '["深蹲"]', 'exercises-dataset:0739', '© Gym visual — https://gymvisual.com/'),
    ('bulgarian-split-squat', '保加利亚分腿蹲', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["深蹲"]', 'exercises-dataset:0410', '© Gym visual — https://gymvisual.com/'),
    ('barbell-bench-press', '杠铃平板卧推', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 2.5, 1, '["水平推"]', 'exercises-dataset:0025', '© Gym visual — https://gymvisual.com/'),
    ('dumbbell-bench-press', '哑铃平板卧推', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["水平推"]', 'exercises-dataset:0289', '© Gym visual — https://gymvisual.com/'),
    ('dumbbell-incline-bench-press', '哑铃上斜卧推', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["水平推", "垂直推"]', 'exercises-dataset:0314', '© Gym visual — https://gymvisual.com/'),
    ('seated-dumbbell-shoulder-press', '坐姿哑铃肩推', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["垂直推"]', 'exercises-dataset:0405', '© Gym visual — https://gymvisual.com/'),
    ('dumbbell-lateral-raise', '哑铃侧平举', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["肩孤立"]', 'exercises-dataset:0334', '© Gym visual — https://gymvisual.com/'),
    ('dumbbell-reverse-fly', '哑铃反向飞鸟', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["肩孤立"]', 'exercises-dataset:0383', '© Gym visual — https://gymvisual.com/'),
    ('barbell-bent-over-row', '杠铃俯身划船', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 2.5, 1, '["水平拉"]', 'exercises-dataset:0027', '© Gym visual — https://gymvisual.com/'),
    ('seated-cable-row', '坐姿绳索划船', 'cable', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["水平拉"]', 'exercises-dataset:0861', '© Gym visual — https://gymvisual.com/'),
    ('one-arm-dumbbell-row', '单臂哑铃划船', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["水平拉"]', 'exercises-dataset:0292', '© Gym visual — https://gymvisual.com/'),
    ('lat-pulldown', '高位下拉', 'cable', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["垂直拉"]', 'exercises-dataset:0198', '© Gym visual — https://gymvisual.com/'),
    ('pull-up', '自重引体向上', 'bodyweight', 'reps_bodyweight', NULL, NULL, 1, '["垂直拉"]', 'exercises-dataset:0652', '© Gym visual — https://gymvisual.com/'),
    ('seated-leg-curl', '坐姿腿弯举', 'leverage_machine', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["膝屈"]', 'exercises-dataset:0599', '© Gym visual — https://gymvisual.com/'),
    ('leg-extension', '腿屈伸', 'leverage_machine', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["膝伸"]', 'exercises-dataset:0585', '© Gym visual — https://gymvisual.com/'),
    ('machine-standing-calf-raise', '器械站姿提踵', 'leverage_machine', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["小腿（踝跖屈）"]', 'exercises-dataset:0605', '© Gym visual — https://gymvisual.com/'),
    ('dumbbell-biceps-curl', '哑铃弯举', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 2.5, 1, '["肘屈"]', 'exercises-dataset:0294', '© Gym visual — https://gymvisual.com/'),
    ('cable-pushdown', '绳索下压', 'cable', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["肘伸"]', 'exercises-dataset:0201', '© Gym visual — https://gymvisual.com/'),
    ('cable-overhead-triceps-extension', '绳索过顶臂屈伸', 'cable', 'reps_weight', 'machine_pin_displayed_value', 5.0, 1, '["肘伸"]', 'exercises-dataset:0194', '© Gym visual — https://gymvisual.com/'),
    ('parallel-bar-dip', '自重双杠臂屈伸', 'bodyweight', 'reps_bodyweight', NULL, NULL, 1, '["垂直推", "肘伸"]', 'exercises-dataset:0251', '© Gym visual — https://gymvisual.com/'),
    ('hanging-leg-raise', '悬垂举腿', 'bodyweight', 'reps_bodyweight', NULL, NULL, 1, '["核心"]', 'exercises-dataset:0472', '© Gym visual — https://gymvisual.com/');
