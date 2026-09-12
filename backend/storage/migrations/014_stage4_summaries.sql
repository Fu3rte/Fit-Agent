-- 014：Stage 4 摘要持久化两表（S4-06a）
-- 依据：07 7.4「Stage 4 已拍：摘要持久化」——成功摘要保存覆盖范围与来源关联、不删除原消息；
--       新摘要提交成功才启用，提交时核对 Run 状态，取消先发生则不启用；08「压缩 A 与失败 B」。
-- 边界：只新增摘要两表；不建第二套消息存储、不改 messages/runs/run_events、不写业务事实；
--       本迁移不含摘要生成（模型调用归 S4-06 后续）与上下文投影（读取路径归实现细节）。
-- 覆盖范围：summaries.covered_from_seq/covered_to_seq 精确记录该摘要取代的历史消息 seq 区间
--       （messages 的会话内全序，UNIQUE(conversation_id, seq)）；范围为会话前缀且新摘要只扩展
--       旧覆盖，故最新有效摘要 = covered_to_seq 最大的一条（提交时由 repo 条件校验）。
-- 来源关联：summary_sources 逐条指向作为摘要输入的 messages 行；外键默认 NO ACTION 即拒绝
--       删除仍被来源引用的原消息，悬空来源不可能存在。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

CREATE TABLE summaries (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    -- 生成该摘要的 Run；提交时条件校验它仍是 running（取消先发生则不落库）
    run_id TEXT NOT NULL REFERENCES runs(id),
    content TEXT NOT NULL CHECK (content <> ''),
    -- 精确覆盖区间（会话内消息 seq）：首条摘要从会话最早消息起，后续摘要包含旧覆盖并向前扩展
    covered_from_seq INTEGER NOT NULL CHECK (covered_from_seq > 0),
    covered_to_seq INTEGER NOT NULL CHECK (covered_to_seq >= covered_from_seq),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_summaries_conversation_coverage
    ON summaries (conversation_id, covered_to_seq);

CREATE TABLE summary_sources (
    summary_id TEXT NOT NULL REFERENCES summaries(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    PRIMARY KEY (summary_id, message_id)
);
