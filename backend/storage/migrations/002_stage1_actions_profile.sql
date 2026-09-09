-- 002：Stage 1 业务域最小结构 —— 动作目录（exercises）与单用户档案（user_profile）
-- 依据：stage1.md §5 S1-02（增量迁移与最小持久化结构）、architecture 03 章 3.1-3.2、
--       02 章 2.1-2.4、07 章 7.2（编号迁移 + PRAGMA user_version）。
-- 边界：只建本阶段两项业务存储；不建 drafts/plans/records/stats 任何表；
--       不导入动作种子（种子属 S1-03，用后续编号迁移写入）；不预填用户事实。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

CREATE TABLE exercises (
    -- 稳定动作身份：停用后仍按 ID 可读，不随展示名变化（03 3.1、停用不删除）。
    id TEXT PRIMARY KEY,
    -- 中文标准名 = 已拍首批清单原词（stage1.md §5 S1-03）；一个身份一个标准名。
    standard_name_zh TEXT NOT NULL UNIQUE,
    -- 器械变式：与别名、负重方式必须可区分（03 3.1）。
    equipment_variant TEXT NOT NULL,
    -- 记录口径恰三类（03「本章已拍结论」；不新增辅助负重型、不建第四类）：
    -- reps_weight=负重次数、reps_bodyweight=自重次数、time=计时。
    record_type TEXT NOT NULL CHECK (record_type IN ('reps_weight', 'reps_bodyweight', 'time')),
    -- 负重口径语义保留（stage1.md §5 S1-03「已确认的负重口径」）：仅负重次数型需要，
    -- 自重次数型与计时型必须为 NULL（不得为它们虚构负重口径）。
    load_convention TEXT CHECK (load_convention IS NULL OR load_convention IN (
        'barbell_includes_bar_total',
        'dumbbell_per_hand',
        'machine_pin_displayed_value',
        'plate_loaded_total_excluding_empty',
        'unilateral_setting_per_side'
    )),
    -- 单侧动作：区分左右、次数按每侧（口径保留；输入、保存与展示归后续阶段）。
    unilateral INTEGER NOT NULL DEFAULT 0 CHECK (unilateral IN (0, 1)),
    -- 可推荐标记：默认 0；只有经过检查才标记为 1（03 3.2），不得自动置 1。
    recommendable INTEGER NOT NULL DEFAULT 0 CHECK (recommendable IN (0, 1)),
    -- 停用不删除：默认 1；停用只置 0，不删行、不覆盖已检查标记（03「本章已拍结论」）。
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    -- 别名（含数据集英文 name；中文口语别名待人工确认前不入库）：JSON 数组；
    -- 别名命中多个身份时保留候选，不静默取第一项（03 3.1）。
    aliases_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(aliases_json)),
    -- 动作模式：13 项已拍词表的子集，多归属仅两处；JSON 数组。
    -- 词表与归属的校验在 rules/repo 层（SQLite CHECK 不能遍历 JSON 数组元素）。
    modes_json TEXT NOT NULL CHECK (json_valid(modes_json)),
    -- 来源与许可：核不上的条目不导入（stage1.md §5 S1-03）；文字数据保留 attribution。
    source_ref TEXT NOT NULL,
    attribution TEXT NOT NULL,
    -- 动作说明（文字数据；不含任何媒体字段：媒体整体移出本阶段）。
    instructions_zh TEXT,
    -- 表级约束必须置于全部列定义之后（SQLite 语法）：负重口径只属于负重次数型。
    CHECK (
        (record_type = 'reps_weight' AND load_convention IS NOT NULL)
        OR (record_type <> 'reps_weight' AND load_convention IS NULL)
    )
);

-- 推荐候选筛选：未停用且已检查可推荐（03 3.2）。
CREATE INDEX idx_exercises_active_recommendable ON exercises (active, recommendable);

CREATE TABLE user_profile (
    -- 单用户单例行：id 恒为 1（02「责任边界」user_profile 单用户档案）。
    id INTEGER PRIMARY KEY CHECK (id = 1),
    -- 档案 JSON 载体；NULL = 未建档技术载体（不预填目标/经验/限制/身体状态/红旗，
    -- 也不得默认「无伤病」「无红旗」「已完成安全筛查」）。
    profile_json TEXT CHECK (profile_json IS NULL OR json_valid(profile_json)),
    -- 统一业务版本载体（02「责任边界」+ 01 1.4）：与档案同一快照、同一事务读写；
    -- 本阶段只建载体，不提供推进入口（推进归 Stage 2 确认事务）。
    context_version INTEGER NOT NULL DEFAULT 0 CHECK (context_version >= 0)
);

-- 初始化只建立「未建档」技术载体：无档案事实、版本 0；重复启动不会重跑本迁移。
INSERT INTO user_profile (id, profile_json, context_version) VALUES (1, NULL, 0);
