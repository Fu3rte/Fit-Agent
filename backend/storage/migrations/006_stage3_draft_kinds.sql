-- 006：Stage 3 业务草稿 kind 扩展 —— kind 列、计划载荷、记录载荷、档案补丁列
-- 依据：stage3.md §5 S3-02、§4.1（草稿扩展沿用 Stage 2 生命周期）、01 1.3（草稿数据）、
--       01 1.5（受限组合：补丁保存在计划草稿内，确认前不改正式档案）。
-- 边界：只用 ALTER TABLE ADD COLUMN 增量扩展——本迁移不改既有列与既有 CHECK
--       （status 仍恰三态，过期不是持久化状态）；不预建 parent_draft_id（重算关联归
--       Stage 4）；不加业务表外键（草稿属性列不引入新的写入顺序约束）。
-- 既有档案草稿行由 ADD COLUMN 的 DEFAULT 回填为 'profile_update'；读旧写新兼容由
-- app/draft_repo.py 的 kind 常量保证（应用层读旧行得到同一 kind）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

-- 草稿 kind：至少四项（§4.1）。DEFAULT 'profile_update' 让既有行与新写档案草稿同值。
ALTER TABLE business_drafts ADD COLUMN kind TEXT NOT NULL DEFAULT 'profile_update'
    CHECK (kind IN ('profile_update', 'plan', 'training_record', 'arrangement'));

-- 计划草稿拟议载荷（D9 payload schema_version=1；结构校验归领域层 S3-03/S3-04）。
-- 可空 = 尚未写入或非计划草稿；非空时必须是合法 JSON 文本。
ALTER TABLE business_drafts ADD COLUMN proposed_plan_json TEXT
    CHECK (proposed_plan_json IS NULL OR json_valid(proposed_plan_json));

-- 受限组合的拟议档案补丁（01 1.5：仅供该计划调整与校验，确认前绝不改正式档案）。
-- 列级 CHECK：NULL 或合法 JSON。SQLite 的 ALTER TABLE 不能追加表级 CHECK，因此
-- 「补丁只存在于计划草稿」由应用层在写入时保证（S3-04/S3-06），不在库层伪造约束。
ALTER TABLE business_drafts ADD COLUMN proposed_profile_patch_json TEXT
    CHECK (proposed_profile_patch_json IS NULL OR json_valid(proposed_profile_patch_json));

-- 记录草稿拟议载荷（S3-10 起写入；S3-02 只建列）。
ALTER TABLE business_drafts ADD COLUMN proposed_record_json TEXT
    CHECK (proposed_record_json IS NULL OR json_valid(proposed_record_json));
