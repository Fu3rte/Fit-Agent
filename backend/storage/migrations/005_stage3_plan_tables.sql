-- 005：Stage 3 计划侧业务表 —— plan_versions / scheduled_sessions / arrangement_revisions
-- 依据：stage3.md §5 S3-02、§4.4 D9（plan_versions 行字段与日程投影、安排接受）、
--       architecture/04 4.1-4.3（计划版本只追加、日程名额与取消、安排完整快照）、
--       07 7.2（编号迁移 + PRAGMA user_version）。
-- 边界：只建计划侧三表；记录／统计侧表归 S3-09 起（编号继续递增），不建完整计划编辑器、
--       通用组合草稿引擎，也不新增第二套业务版本计数器（统一业务版本仍是
--       user_profile.context_version，01 1.4）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

CREATE TABLE plan_versions (
    -- 版本身份（D9：id/version 是行字段，不进 payload_json）
    id TEXT PRIMARY KEY,
    -- 计划版本序号：只追加、恰好 +1（04 4.1）；单用户场景下全局唯一
    version INTEGER NOT NULL UNIQUE CHECK (version >= 1),
    -- 来源版本：恢复旧计划时基于旧版生成新版本并保留来源，不重新激活旧版本（04 4.1/4.6）
    source_plan_version_id TEXT REFERENCES plan_versions(id),
    -- 生效区间 [starts_on, review_on)，日期按固定业务时区解释（D9、07 7.3）
    starts_on TEXT NOT NULL,
    review_on TEXT NOT NULL,
    -- 常规 / 接回（D9、04 4.6）
    mode TEXT NOT NULL CHECK (mode IN ('regular', 'return')),
    -- 完整处方快照（schema_version=1；结构与目录引用校验归领域层 S3-03/S3-06，不在库层猜）
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    -- 来源草稿：正式事实只经确认事务写入（01 1.4、不变量 7）
    source_draft_id TEXT NOT NULL REFERENCES business_drafts(id),
    confirmed_at TEXT NOT NULL
);

CREATE TABLE scheduled_sessions (
    -- 一个应训练名额一行；没有打卡也存在，是完成率分母的事实基础（04 4.2）
    id TEXT PRIMARY KEY,
    -- 所属计划版本：替换计划时只取消旧版未来未锁定日程（04 4.2）
    plan_version_id TEXT NOT NULL REFERENCES plan_versions(id),
    -- 对应 plan_workouts[].workout_key（版本内唯一；D9 日程投影）
    plan_workout_key TEXT NOT NULL,
    -- 应训练日（固定业务时区日期；rest 槽不生成行，D9/04 4.2）
    scheduled_on TEXT NOT NULL,
    -- 存储状态：取消时间与存储锁定标记。到期锁定的唯一依据是日期规则，存储字段不是
    -- 唯一依据（04 4.2：停机跨过训练日仍禁改期／删除）
    cancelled_at TEXT,
    locked_at TEXT,
    -- D9 按日历日逐槽投影：同一版本每个日历日至多一个应训练名额，不重复造分母
    UNIQUE (plan_version_id, scheduled_on)
);

CREATE TABLE arrangement_revisions (
    -- 每次接受一笔完整目标快照（不只差异补丁），旧修订保留（04 4.3）
    id TEXT PRIMARY KEY,
    scheduled_session_id TEXT NOT NULL REFERENCES scheduled_sessions(id),
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    -- 接受时的完整目标快照（结构校验归领域层 S3-08，不在库层猜）
    target_snapshot_json TEXT NOT NULL CHECK (json_valid(target_snapshot_json)),
    -- 来源草稿：接受与草稿提交、业务版本 +1 同事务（01 1.4）
    source_draft_id TEXT NOT NULL REFERENCES business_drafts(id),
    -- 真实接受时间：接受即落盘，不得倒填（04 4.3）
    accepted_at TEXT NOT NULL,
    UNIQUE (scheduled_session_id, revision_no)
);
