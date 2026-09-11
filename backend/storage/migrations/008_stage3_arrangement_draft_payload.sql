-- 008：Stage 3 安排草稿载荷列 —— business_drafts.proposed_arrangement_json
-- 依据：stage3.md §5 S3-08、§4.1（草稿 kind 含 arrangement）、04 4.3（当次安排绑定计划版本
--       与训练日、接受时写完整目标快照）；S3-02 证据残留「安排草稿载荷列归 S3-08」。
-- 边界：只用 ALTER TABLE ADD COLUMN 增量扩展；不改既有列与既有 CHECK；不建记录侧表
--       （S3-09 起，编号继续递增）。
-- 与 006 的计划／记录载荷列同口径：列只存合法 JSON 文本，结构校验归领域层
-- （domain/plan/schema.arrangement_target_from_json + rules.validate_arrangement_target）；
-- 「该列只出现在 arrangement 草稿上」由应用层写入方法固定列选择保证。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

-- 安排草稿拟议载荷：当次目标完整快照（绑定 + 完整动作目标，不只差异补丁）。
ALTER TABLE business_drafts ADD COLUMN proposed_arrangement_json TEXT
    CHECK (proposed_arrangement_json IS NULL OR json_valid(proposed_arrangement_json));
