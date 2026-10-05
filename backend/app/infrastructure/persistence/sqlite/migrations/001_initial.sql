-- Fit-Agent 应用数据库初始结构（schema 版本 1）。
-- 时间字段为 UTC 毫秒 INTEGER，ID 为 TEXT；全部表使用 STRICT。
-- 由 database.py 统一在单一事务内建表、建索引、建触发器并写入 PRAGMA user_version。

-- 会话头。active_leaf_id 通过同会话复合外键指向会话节点，可为空。
CREATE TABLE sessions (
    id TEXT NOT NULL PRIMARY KEY,
    title TEXT NOT NULL,
    active_leaf_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CONSTRAINT sessions_active_leaf_same_session
        FOREIGN KEY (id, active_leaf_id)
        REFERENCES session_entries (session_id, id)
) STRICT;

-- 不可变会话树节点。messages 为长度为 1 的 AgentMessage JSON 数组文本。
CREATE TABLE session_entries (
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    parent_id TEXT,
    run_id TEXT,
    type TEXT NOT NULL,
    messages TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, id),
    CONSTRAINT session_entries_type CHECK (type = 'message'),
    CONSTRAINT session_entries_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id),
    CONSTRAINT session_entries_messages_shape CHECK (
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
            ELSE 1
        END
    ),
    CONSTRAINT session_entries_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT session_entries_parent
        FOREIGN KEY (session_id, parent_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT session_entries_run
        FOREIGN KEY (session_id, run_id)
        REFERENCES session_runs (session_id, id)
) STRICT;

-- 运行记录。request_entry_id 指向发起运行的用户节点，last_entry_id 指向本运行最后新增节点。
CREATE TABLE session_runs (
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    request_entry_id TEXT NOT NULL,
    last_entry_id TEXT,
    status TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    error_code TEXT,
    error_message TEXT,
    PRIMARY KEY (session_id, id),
    CONSTRAINT session_runs_status CHECK (
        status IN ('running', 'completed', 'failed', 'cancelled', 'interrupted')
    ),
    CONSTRAINT session_runs_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT session_runs_request_entry
        FOREIGN KEY (session_id, request_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT session_runs_last_entry
        FOREIGN KEY (session_id, last_entry_id)
        REFERENCES session_entries (session_id, id)
) STRICT;

-- Steering 输入。message 为完整 UserMessage 文本；entry_id 仅 consumed 状态非空。
CREATE TABLE steering_inputs (
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_id TEXT,
    reason TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, id),
    CONSTRAINT steering_inputs_status CHECK (
        status IN ('pending', 'consumed', 'withdrawn', 'discarded')
    ),
    CONSTRAINT steering_inputs_entry_state CHECK (
        (status = 'consumed') = (entry_id IS NOT NULL)
    ),
    CONSTRAINT steering_inputs_message_shape CHECK (
        CASE
            WHEN json_valid(message) = 0 THEN 0
            WHEN json_type(message) IS NOT 'object' THEN 0
            WHEN json_extract(message, '$.role') IS NOT 'user' THEN 0
            ELSE 1
        END
    ),
    CONSTRAINT steering_inputs_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT steering_inputs_run
        FOREIGN KEY (session_id, run_id)
        REFERENCES session_runs (session_id, id),
    CONSTRAINT steering_inputs_entry
        FOREIGN KEY (session_id, entry_id)
        REFERENCES session_entries (session_id, id)
) STRICT;

-- 已受理操作。operation_id 为客户端幂等键，全局唯一。
CREATE TABLE session_operations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    request TEXT NOT NULL,
    run_id TEXT NOT NULL,
    steering_id TEXT,
    created_at INTEGER NOT NULL,
    CONSTRAINT session_operations_kind CHECK (
        kind IN ('send', 'edit', 'regenerate', 'steering')
    ),
    CONSTRAINT session_operations_steering_state CHECK (
        (kind = 'steering') = (steering_id IS NOT NULL)
    ),
    CONSTRAINT session_operations_request_shape CHECK (
        CASE
            WHEN json_valid(request) = 0 THEN 0
            WHEN json_type(request) IS NOT 'object' THEN 0
            ELSE 1
        END
    ),
    CONSTRAINT session_operations_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT session_operations_run
        FOREIGN KEY (session_id, run_id)
        REFERENCES session_runs (session_id, id),
    CONSTRAINT session_operations_steering
        FOREIGN KEY (session_id, steering_id)
        REFERENCES steering_inputs (session_id, id)
) STRICT;

-- 外键与查询所需索引。
CREATE INDEX idx_session_entries_parent ON session_entries (session_id, parent_id);
CREATE INDEX idx_session_entries_run ON session_entries (session_id, run_id);
CREATE INDEX idx_steering_inputs_run ON steering_inputs (session_id, run_id);
CREATE UNIQUE INDEX idx_steering_inputs_entry ON steering_inputs (session_id, entry_id);
CREATE INDEX idx_session_operations_session ON session_operations (session_id);

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

-- 跨表约束：运行请求节点必须是同会话用户消息节点。
CREATE TRIGGER session_runs_request_entry_user_insert
BEFORE INSERT ON session_runs
FOR EACH ROW
WHEN (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.request_entry_id
      AND json_extract(messages, '$[0].role') IS 'user'
) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'session_runs.request_entry_id 必须引用同会话用户消息节点');
END;

CREATE TRIGGER session_runs_request_entry_user_update
BEFORE UPDATE OF request_entry_id ON session_runs
FOR EACH ROW
WHEN (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.request_entry_id
      AND json_extract(messages, '$[0].role') IS 'user'
) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'session_runs.request_entry_id 必须引用同会话用户消息节点');
END;

-- 跨表约束：非空的 last_entry_id 必须属于该运行。
CREATE TRIGGER session_runs_last_entry_in_run_insert
BEFORE INSERT ON session_runs
FOR EACH ROW
WHEN NEW.last_entry_id IS NOT NULL
 AND (SELECT run_id FROM session_entries
      WHERE session_id = NEW.session_id AND id = NEW.last_entry_id) IS NOT NEW.id
BEGIN
    SELECT RAISE(ABORT, 'session_runs.last_entry_id 必须属于该运行');
END;

CREATE TRIGGER session_runs_last_entry_in_run_update
BEFORE UPDATE OF last_entry_id ON session_runs
FOR EACH ROW
WHEN NEW.last_entry_id IS NOT NULL
 AND (SELECT run_id FROM session_entries
      WHERE session_id = NEW.session_id AND id = NEW.last_entry_id) IS NOT NEW.id
BEGIN
    SELECT RAISE(ABORT, 'session_runs.last_entry_id 必须属于该运行');
END;

-- 跨表约束：已消费 Steering 必须关联该运行的用户消息节点。
CREATE TRIGGER steering_inputs_consumed_entry_insert
BEFORE INSERT ON steering_inputs
FOR EACH ROW
WHEN NEW.entry_id IS NOT NULL
 AND (
    (SELECT run_id FROM session_entries
     WHERE session_id = NEW.session_id AND id = NEW.entry_id) IS NOT NEW.run_id
    OR (SELECT 1 FROM session_entries
        WHERE session_id = NEW.session_id AND id = NEW.entry_id
          AND json_extract(messages, '$[0].role') IS 'user') IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, '已消费 Steering 必须关联该运行的用户消息节点');
END;

CREATE TRIGGER steering_inputs_consumed_entry_update
BEFORE UPDATE OF status, entry_id, run_id ON steering_inputs
FOR EACH ROW
WHEN NEW.entry_id IS NOT NULL
 AND (
    (SELECT run_id FROM session_entries
     WHERE session_id = NEW.session_id AND id = NEW.entry_id) IS NOT NEW.run_id
    OR (SELECT 1 FROM session_entries
        WHERE session_id = NEW.session_id AND id = NEW.entry_id
          AND json_extract(messages, '$[0].role') IS 'user') IS NULL
 )
BEGIN
    SELECT RAISE(ABORT, '已消费 Steering 必须关联该运行的用户消息节点');
END;

-- 跨表约束：操作关联的 Steering 输入必须属于操作指定的运行。
CREATE TRIGGER session_operations_steering_run_insert
BEFORE INSERT ON session_operations
FOR EACH ROW
WHEN NEW.steering_id IS NOT NULL
 AND (SELECT run_id FROM steering_inputs
      WHERE session_id = NEW.session_id AND id = NEW.steering_id) IS NOT NEW.run_id
BEGIN
    SELECT RAISE(ABORT, '操作关联的 Steering 输入必须属于指定运行');
END;

CREATE TRIGGER session_operations_steering_run_update
BEFORE UPDATE OF kind, steering_id, run_id ON session_operations
FOR EACH ROW
WHEN NEW.steering_id IS NOT NULL
 AND (SELECT run_id FROM steering_inputs
      WHERE session_id = NEW.session_id AND id = NEW.steering_id) IS NOT NEW.run_id
BEGIN
    SELECT RAISE(ABORT, '操作关联的 Steering 输入必须属于指定运行');
END;

-- 不可变历史节点：禁止更新全部字段，禁止直接删除。
CREATE TRIGGER session_entries_immutable_update
BEFORE UPDATE ON session_entries
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'session_entries 为不可变历史节点，禁止更新');
END;

CREATE TRIGGER session_entries_no_delete
BEFORE DELETE ON session_entries
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'session_entries 禁止直接删除');
END;
