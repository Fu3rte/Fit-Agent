-- 版本 11：会话消息节点新增模型投影省略标记 model_omitted。
-- 明确上下文溢出的失败助手消息持久化该标记，重启后投影继续跳过它。
-- 重建 session_entries 以把新列并入节点形状约束；现有行置 0。
-- 由 database.py 在外键关闭、legacy_alter_table 开启状态下应用。

ALTER TABLE session_entries RENAME TO session_entries_old;

CREATE TABLE session_entries (
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    parent_id TEXT,
    run_id TEXT,
    type TEXT NOT NULL,
    messages TEXT,
    summary TEXT,
    first_kept_entry_id TEXT,
    tokens_before INTEGER,
    usage TEXT,
    system_message TEXT,
    details TEXT,
    created_at INTEGER NOT NULL,
    model_omitted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, id),
    CONSTRAINT session_entries_type CHECK (type IN ('message', 'compaction')),
    CONSTRAINT session_entries_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id),
    CONSTRAINT session_entries_node_shape CHECK (
        CASE
            WHEN type = 'message' THEN
                CASE
                    WHEN json_valid(messages) = 0 THEN 0
                    WHEN json_type(messages) IS NOT 'array' THEN 0
                    WHEN json_array_length(messages) <> 1 THEN 0
                    WHEN json_type(messages, '$[0]') IS NOT 'object' THEN 0
                    WHEN json_extract(messages, '$[0].role') IS NULL THEN 0
                    WHEN json_extract(messages, '$[0].role') NOT IN
                         ('system', 'user', 'assistant', 'toolResult') THEN 0
                    WHEN json_extract(messages, '$[0].role') = 'assistant'
                         AND json_extract(messages, '$[0].stop_reason') = 'pending' THEN 0
                    WHEN model_omitted NOT IN (0, 1) THEN 0
                    WHEN model_omitted = 1
                         AND json_extract(messages, '$[0].role') <> 'assistant' THEN 0
                    WHEN model_omitted = 1
                         AND json_extract(messages, '$[0].stop_reason') <> 'error' THEN 0
                    WHEN summary IS NOT NULL
                         OR first_kept_entry_id IS NOT NULL
                         OR tokens_before IS NOT NULL
                         OR usage IS NOT NULL
                         OR system_message IS NOT NULL
                         OR details IS NOT NULL THEN 0
                    ELSE 1
                END
            WHEN type = 'compaction' THEN
                CASE
                    WHEN messages IS NOT NULL THEN 0
                    WHEN model_omitted <> 0 THEN 0
                    WHEN summary IS NULL OR length(summary) = 0 THEN 0
                    WHEN first_kept_entry_id IS NULL
                         OR length(first_kept_entry_id) = 0 THEN 0
                    WHEN tokens_before IS NULL OR tokens_before < 0 THEN 0
                    WHEN json_valid(usage) = 0 THEN 0
                    WHEN json_type(usage) IS NOT 'object' THEN 0
                    WHEN json_valid(system_message) = 0 THEN 0
                    WHEN json_type(system_message) IS NOT 'object' THEN 0
                    WHEN details IS NOT NULL AND json_valid(details) = 0 THEN 0
                    WHEN details IS NOT NULL
                         AND json_type(details) IS NOT 'object' THEN 0
                    ELSE 1
                END
            ELSE 0
        END
    ),
    CONSTRAINT session_entries_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT session_entries_parent
        FOREIGN KEY (session_id, parent_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT session_entries_first_kept
        FOREIGN KEY (session_id, first_kept_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT session_entries_run
        FOREIGN KEY (session_id, run_id)
        REFERENCES session_runs (session_id, id)
) STRICT;

INSERT INTO session_entries (
    session_id, id, parent_id, run_id, type, messages,
    summary, first_kept_entry_id, tokens_before, usage, system_message, details,
    created_at, model_omitted
)
SELECT
    session_id, id, parent_id, run_id, type, messages,
    summary, first_kept_entry_id, tokens_before, usage, system_message, details,
    created_at, 0
FROM session_entries_old;

DROP TABLE session_entries_old;

CREATE INDEX idx_session_entries_parent ON session_entries (session_id, parent_id);
CREATE INDEX idx_session_entries_run ON session_entries (session_id, run_id);

-- 父节点必须已存在于同一会话：先于插入检查，拒绝自引用并防止多行插入形成环。
CREATE TRIGGER session_entries_parent_exists_insert
BEFORE INSERT ON session_entries
FOR EACH ROW
WHEN NEW.parent_id IS NOT NULL
 AND (SELECT 1 FROM session_entries
      WHERE session_id = NEW.session_id AND id = NEW.parent_id) IS NULL
BEGIN
    SELECT RAISE(ABORT, '父节点必须已存在于同一会话');
END;

-- 不可变历史节点：禁止更新全部字段。
CREATE TRIGGER session_entries_immutable_update
BEFORE UPDATE ON session_entries
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'session_entries 为不可变历史节点，禁止更新');
END;
