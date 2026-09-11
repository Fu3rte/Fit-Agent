-- 011：Stage 3 复盘侧表 —— reviews / review_source_revisions
-- 依据：stage3.md §5 S3-13、§8 D6（仅显式请求时生成并保存）、architecture/06 6.4（保存 Markdown
--       正文、生成时统计依据快照与来源修订引用；重生成追加不覆盖；依据变化标记已变更）、
--       07 7.2（编号迁移 + PRAGMA user_version）。
-- 边界：只建复盘两表；不建统计结果表（当前统计一律现算，06 6.3／6.4），不改记录／计划侧表，
--       不新增业务版本计数器（唯一版本仍是 user_profile.context_version）。
-- 生成侧：Stage 3 不生成模型正文（D6）；写入只经内部显式保存 seam（app/review_store.py），
--       不在本迁移里建任何 HTTP 面（是否暴露查询归 S3-14）。
-- 不可变：正文与快照写入后没有 UPDATE 路径；重生成是**追加新行**，旧行保留（06 6.4）。
-- 依据变更：只经「读时现算 stale」表达——所引修订不再是该训练身份的当前修订即为已变更；
--       不回写旧行的正文／快照（06 6.4「不静默改写」）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

CREATE TABLE reviews (
    id TEXT PRIMARY KEY,
    -- 复盘正文（Markdown）：生成时冻结，入库后不改写。
    body_markdown TEXT NOT NULL CHECK (length(trim(body_markdown)) > 0),
    -- 生成时统计依据快照：冻结当时确定性数值（计划周完成率、现算 PR 值）。
    -- **不参与当前统计**：当前统计始终按最新有效事实现算（06 6.3／6.4），快照只作历史解释证据。
    basis_json TEXT NOT NULL CHECK (json_valid(basis_json)),
    -- 生成时刻（UTC ISO 文本）：重生成追加时按它排序，不覆盖旧行。
    generated_at TEXT NOT NULL
);

-- 精确来源修订引用：生成该快照时所用的每一笔训练修订 id（存「当时那一笔」，不存「最新」、
-- 不在读时重解析）。外键把「引用必须存在」落到库层，非法引用写不进。
CREATE TABLE review_source_revisions (
    review_id TEXT NOT NULL REFERENCES reviews(id),
    session_revision_id TEXT NOT NULL REFERENCES session_revisions(id),
    PRIMARY KEY (review_id, session_revision_id)
);

-- 读取只按 review_id 取全部来源：主键索引 (review_id, session_revision_id) 的最左前缀已覆盖，
-- 当前也不需要「按修订反查复盘」的查询，故不另建索引。
