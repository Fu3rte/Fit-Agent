-- 009：Stage 3 记录侧业务表 —— training_sessions / session_revisions / exercise_logs / training_sets
-- 依据：stage3.md §5 S3-09、architecture/05 5.1-5.5（训练身份、完整修订、逐组事实、同日多练）、
--       03 3.1（记录口径归目录）、07 7.2（编号迁移 + PRAGMA user_version）。
-- 边界：只建记录侧四表与最小索引；不建 reviews／pr_candidates（S3-13／S3-12）、不写任何记录草稿
--       载荷结构（S3-10）、不落统计口径（S3-12）。父表引用只指向已存在表（exercises 002、
--       business_drafts 004、arrangement_revisions 005），不新建第二套版本计数器。
-- 记录口径：`exercise_logs.record_type` 取**动作目录词表** reps_weight／reps_bodyweight／time
--       （2026-09-11 拍板 A：记录事实表不存第二套词汇；处方／统计措辞经既有映射
--       domain/plan/rules.CATALOG_RECORD_TYPE_TO_PRESCRIPTION 转换）。
-- 可空语义（05 5.2／5.5、报告 §4.4）：set_type（热身／工作组）、rir、assistance 与动作质量等
--       均可空；NULL 表示**未明确**，不得被读作热身组、无辅助或 RIR=0（不静默认定）。
-- 负重口径不混比（报告 §4.4）：原始十进制原文 + 单位保留在行上，换算键 load_kg_key 是派生列
--       （lb×0.45359237 → ×1000 ROUND_HALF_EVEN，见 domain/records/rules.load_kg_key）；
--       口径 load_notation 在 exercise_logs 上，不同口径结构上不可比（比较键见
--       domain/records/schema.LoadComparisonKey）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

-- 一次实际训练一个稳定身份：日期不唯一，同日多练各自身份（05 5.1／5.4）。
CREATE TABLE training_sessions (
    id TEXT PRIMARY KEY,
    -- 当前修订指针：决定统计所见版本（05 5.3）。复合外键把「指针必须指向属于本训练身份的
    -- 修订」落到库层（报告 §3.2：优先用复合外键表达），不由应用层各自校验。
    current_revision_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (id, current_revision_id) REFERENCES session_revisions (session_id, id)
);

-- 每次确认／更正／作废追加一笔**完整**修订，旧修订保留（05 5.3）。
CREATE TABLE session_revisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES training_sessions(id),
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    previous_revision_id TEXT REFERENCES session_revisions(id),
    -- 三态只此三种：待补全 / 有效 / 已作废（05 5.3；作废不回退采用旧有效版本）。
    status TEXT NOT NULL CHECK (status IN ('incomplete', 'valid', 'voided')),
    -- 实际发生日期必须明确（05 5.2 必填边界）；相对日期在草稿层解析，不在库层猜。
    occurred_on TEXT NOT NULL,
    -- 具体时刻与时间精度：均可空。精度词表未另有已拍依据（报告 §4.2 示例只出现
    -- timestamp），故库层不发明枚举，取值由领域层约定。
    started_at TEXT,
    time_precision TEXT,
    -- 关联可信的当次安排快照：可空（无对照安排的记录只作历史表现数据，05 5.1）。
    arrangement_revision_id TEXT REFERENCES arrangement_revisions(id),
    -- 用户申报「完成一次」与目标匹配是两件事，完成率仍用独立条件判定（05 5.2、04 4.4）。
    completion_declared INTEGER NOT NULL DEFAULT 0 CHECK (completion_declared IN (0, 1)),
    -- 回归期标记随修订保留，标记录错只能经历史更正确认修正（05 5.3）。
    is_return_phase INTEGER NOT NULL DEFAULT 0 CHECK (is_return_phase IN (0, 1)),
    -- 训练反馈可选，可空；结构校验归领域层（未报告疼痛在 JSON 内显式表达，不复用 NULL）。
    feedback_json TEXT CHECK (feedback_json IS NULL OR json_valid(feedback_json)),
    -- 来源草稿：正式事实只经确认事务写入（01 1.4、不变量 7）。
    source_draft_id TEXT NOT NULL REFERENCES business_drafts(id),
    confirmed_at TEXT NOT NULL,
    UNIQUE (session_id, revision_no),
    -- 供 training_sessions 的复合外键引用「同一训练身份内的修订」；SQLite 要求父键是唯一索引。
    UNIQUE (session_id, id)
);

-- 隶属具体修订的实际动作事实；完整修订含该修订全部动作，不只是差异（05 5.3）。
CREATE TABLE exercise_logs (
    id TEXT PRIMARY KEY,
    session_revision_id TEXT NOT NULL REFERENCES session_revisions(id),
    exercise_id TEXT NOT NULL REFERENCES exercises(id),
    position INTEGER NOT NULL CHECK (position >= 1),
    -- 对应当次安排目标项：可空（无安排或目标项未匹配时不得伪造对应关系，05 5.1）。
    target_item_key TEXT,
    -- 记录口径 = 目录词表（拍板 A）；目录外口径一律拒绝，不在此发明第四类。
    record_type TEXT NOT NULL CHECK (record_type IN (
        'reps_weight',
        'reps_bodyweight',
        'time'
    )),
    -- 负重口径语义（复用 002 目录已拍五种）：仅负重次数型需要，其余必须为 NULL。
    load_notation TEXT CHECK (load_notation IS NULL OR load_notation IN (
        'barbell_includes_bar_total',
        'dumbbell_per_hand',
        'machine_pin_displayed_value',
        'plate_loaded_total_excluding_empty',
        'unilateral_setting_per_side'
    )),
    -- 动作显示名与变式快照：可空；避免目录改名改变历史展示（报告 §3.2）。
    exercise_snapshot_json TEXT CHECK (
        exercise_snapshot_json IS NULL OR json_valid(exercise_snapshot_json)
    ),
    -- 热身摘要保留原文（如「递增至 60kg」）：可空，不展开为虚构组数据、不进精确组次统计（05 5.2）。
    warmup_summary_text TEXT,
    UNIQUE (session_revision_id, position),
    -- 与 002 exercises 同一口径：负重口径只属于负重次数型，不为其自重／计时虚构口径。
    CHECK (
        (record_type = 'reps_weight' AND load_notation IS NOT NULL)
        OR (record_type <> 'reps_weight' AND load_notation IS NULL)
    )
);

-- 隶属具体修订动作的逐组事实（05 5.3、报告 §3.2）。
CREATE TABLE training_sets (
    id TEXT PRIMARY KEY,
    exercise_log_id TEXT NOT NULL REFERENCES exercise_logs(id),
    set_no INTEGER NOT NULL CHECK (set_no >= 1),
    -- 组类型：热身组 / 工作组；可空 = 尚未明确（05 5.2：用户未说时不得静默认定）。
    set_type TEXT CHECK (set_type IS NULL OR set_type IN ('warmup', 'work')),
    -- 对应的当次目标组：可空（未匹配到目标组时不得编造对应关系）。
    target_set_key TEXT,
    -- 原始负重：十进制原文 + 单位（保留报告 §4.4 的原始输入，不做浮点判定）。
    load_value_text TEXT,
    load_unit TEXT CHECK (load_unit IS NULL OR load_unit IN ('kg', 'lb')),
    -- 换算整数键（kg×1000）：派生列，与原始值／单位必须同现，缺一即不可比。
    load_kg_key INTEGER,
    -- 次数与时长各自可空：计时型不强填次数，次数型不强填时长（05 5.2）。
    reps INTEGER CHECK (reps IS NULL OR reps >= 1),
    duration_seconds INTEGER CHECK (duration_seconds IS NULL OR duration_seconds >= 1),
    -- RIR：NULL 表示未报告（不等于 0）；非负有限校验的服务端防线在 domain/records/rules。
    rir REAL CHECK (rir IS NULL OR rir >= 0),
    -- 人工辅助：NULL = 尚未明确；不得默认 none（05 5.5 异常申报制，含糊辅助不自行归类）。
    assistance TEXT CHECK (assistance IS NULL OR assistance IN (
        'none',
        'spotter_only',
        'assisted'
    )),
    -- 辅助次数：知道则记录，不知道可空，不估算朋友承担的重量（05 5.5）。
    assisted_reps INTEGER CHECK (assisted_reps IS NULL OR assisted_reps >= 1),
    -- 动作质量反馈：可选，未提供保持为空（05 5.2）。
    quality_text TEXT,
    UNIQUE (exercise_log_id, set_no),
    CHECK (
        (load_value_text IS NULL AND load_unit IS NULL AND load_kg_key IS NULL)
        OR (
            load_value_text IS NOT NULL
            AND load_unit IS NOT NULL
            AND load_kg_key IS NOT NULL
        )
    )
);

-- 按动作取记录历史／PR 候选（报告 §6.1 以 exercise_id 分组，D3 校准按可信记录读动作）。
CREATE INDEX idx_exercise_logs_exercise ON exercise_logs (exercise_id);

-- 按所依据的安排快照取执行记录（05 5.1：记录绑定执行时依据的安排，不随后续计划变化）。
CREATE INDEX idx_session_revisions_arrangement
    ON session_revisions (arrangement_revision_id);

-- 其余按修订／动作的读取由既有唯一索引的最左前缀覆盖（UNIQUE(session_id, revision_no)、
-- UNIQUE(session_revision_id, position)、UNIQUE(exercise_log_id, set_no)），不再重复建索引。
