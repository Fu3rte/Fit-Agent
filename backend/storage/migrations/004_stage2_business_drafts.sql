-- 004：Stage 2 草稿最小持久化结构 —— business_drafts（正本 architecture/01 1.3）
-- 依据：stage2.md §5 S2-02（连续编号迁移、草稿 repo、最小事务内访问能力）与 01 章 1.3
--       （草稿数据与生命周期）、07 章 7.2（编号迁移 + PRAGMA user_version）。
-- 边界：只新增 business_drafts 一张表；不建计划／日程／训练记录／统计表，不建通用组合
--       草稿引擎、独立版本服务或第二套业务版本计数器。不预建 parent_draft_id 与计划草稿
--       补丁字段：重算关联与计划组合按 01 1.3／1.5、stage2.md §4.1 属 Stage 3/4 接入契约，
--       由对应阶段的增量迁移补充（stage2.md §4.1：不预建业务内容）。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

-- 草稿来源组合外键的父键：Run 必须属于草稿所关联的同一会话（stage2.md §4.1「来源关联
-- 现有会话／Run 身份」、S2-03「不同来源关联不混淆」）。runs.id 本就是主键，该索引只是把
-- 既有唯一性组合化以充当父键：不改变既有行数据与约束面，也不是新增业务表。
CREATE UNIQUE INDEX idx_runs_conversation_id ON runs (conversation_id, id);

CREATE TABLE business_drafts (
    -- 草稿身份（01 1.2：草稿纠错／确认／丢弃走业务接口，通知引用身份与修订版本）
    id TEXT PRIMARY KEY,
    -- 来源关联现有会话身份；Run 可空：本阶段没有真实模型执行，不得凭空伪造 Run。
    -- 单列外键保证会话存在；表级组合外键保证 Run 与该会话同属——跨会话来源在库层即
    -- 拒绝，不只靠应用层校验。run_id 为 NULL 时 SQLite 缺省 MATCH 语义跳过组合约束，
    -- 会话存在性仍由单列外键保证。
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    run_id TEXT,
    -- 生成基线：生成输入读取时的正式档案快照（与 base_business_version 同一业务快照）；
    -- NULL = 生成时未建档（不得把「未建档」当成「无限制／无症状」）
    base_profile_json TEXT CHECK (
        base_profile_json IS NULL OR json_valid(base_profile_json)
    ),
    -- 拟议结果快照 = 最终草稿内容：确认事务按此复查领域规则，不信任客户端传入内容
    proposed_profile_json TEXT NOT NULL CHECK (json_valid(proposed_profile_json)),
    -- 草稿实际生成依据的 user_profile.context_version；保存与纠错都不得改取最新版本
    -- （01 1.3、stage2.md §4.1：过期在首次确认时拦截，不在这里重基）
    base_business_version INTEGER NOT NULL CHECK (base_business_version >= 0),
    -- 用户所见并准备确认的草稿修订版本（01 1.4：防止提交已被修改的草稿）
    revision INTEGER NOT NULL CHECK (revision >= 1),
    -- 生命周期恰三态（01 1.3）；过期是业务基线冲突，不新建持久化状态
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed', 'discarded')),
    -- 提交凭据：至少识别草稿、已提交 revision 与该次提交后的业务版本；提交后不可改写
    committed_revision INTEGER CHECK (
        committed_revision IS NULL OR committed_revision >= 1
    ),
    committed_business_version INTEGER CHECK (
        committed_business_version IS NULL OR committed_business_version >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    -- 凭据与生命周期同进同退：Committed 必有凭据，Pending／Discarded 必无凭据
    CHECK (
        (
            status = 'committed'
            AND committed_revision IS NOT NULL
            AND committed_business_version IS NOT NULL
        )
        OR (
            status <> 'committed'
            AND committed_revision IS NULL
            AND committed_business_version IS NULL
        )
    ),
    -- 已提交凭据只能记录真正提交过的那一版内容
    CHECK (status <> 'committed' OR committed_revision = revision),
    -- 来源配对不变量：run_id 非空时必须指向 conversation_id 名下的 Run
    FOREIGN KEY (conversation_id, run_id) REFERENCES runs (conversation_id, id)
);

-- 会话维度查询当前草稿（01 1.2：不依赖历史通知，按会话身份读当前状态）
CREATE INDEX idx_business_drafts_conversation
    ON business_drafts (conversation_id, created_at);
