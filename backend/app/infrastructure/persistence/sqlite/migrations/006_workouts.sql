-- 版本 6：实际训练记录、不可修改快照与固定保存结果。
CREATE TABLE workouts (
    id TEXT NOT NULL PRIMARY KEY,
    performed_on TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL CHECK (version > 0),
    content TEXT NOT NULL CHECK (json_valid(content) AND json_type(content) IS 'object'),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    updated_at INTEGER NOT NULL CHECK (updated_at > 0),
    CHECK (length(performed_on) = 10 AND performed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')
) STRICT;

CREATE TABLE workout_snapshots (
    proposal_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    request_entry_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    performed_on TEXT NOT NULL,
    base_workout_id TEXT,
    base_workout_version INTEGER,
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) IS 'object'),
    display_entry_id TEXT,
    confirmation_entry_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'saved', 'invalidated', 'conflicted')),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    CHECK ((base_workout_id IS NULL AND base_workout_version IS NULL)
        OR (base_workout_id IS NOT NULL AND base_workout_version IS NOT NULL AND base_workout_version > 0)),
    CHECK (length(performed_on) = 10 AND performed_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    CHECK (confirmation_entry_id IS NULL OR display_entry_id IS NOT NULL),
    CHECK (status IN ('pending', 'invalidated', 'conflicted')
        OR (display_entry_id IS NOT NULL AND confirmation_entry_id IS NOT NULL)),
    FOREIGN KEY (session_id, request_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, source_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, display_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, confirmation_entry_id) REFERENCES session_entries(session_id, id)
) STRICT;

-- 保存结果在会话与消息删除后保留，不设置物理外键。
CREATE TABLE workout_save_records (
    proposal_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    display_entry_id TEXT NOT NULL,
    confirmation_entry_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (json_valid(result) AND json_type(result) IS 'object'),
    saved_at INTEGER NOT NULL CHECK (saved_at > 0),
    CHECK (json_type(result, '$.proposal_id') IS 'text'
        AND json_extract(result, '$.proposal_id') = proposal_id
        AND json_type(result, '$.saved_at') IS 'integer'
        AND json_extract(result, '$.saved_at') = saved_at
        AND json_type(result, '$.updated_at') IS 'integer'
        AND json_extract(result, '$.updated_at') = saved_at)
) STRICT;

CREATE INDEX idx_workouts_performed_on ON workouts(performed_on DESC, id DESC);
CREATE INDEX idx_workout_snapshots_pending ON workout_snapshots(session_id, performed_on, status);
CREATE UNIQUE INDEX idx_workout_snapshots_confirmation ON workout_snapshots(session_id, confirmation_entry_id);
CREATE UNIQUE INDEX idx_workout_save_records_confirmation ON workout_save_records(session_id, confirmation_entry_id);

CREATE TRIGGER workout_snapshots_request_entry_user_insert
BEFORE INSERT ON workout_snapshots
WHEN (SELECT 1 FROM session_entries WHERE session_id = NEW.session_id AND id = NEW.request_entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots.request_entry_id 必须引用同会话用户消息节点');
END;

CREATE TRIGGER workout_snapshots_source_entry_assistant_insert
BEFORE INSERT ON workout_snapshots
WHEN (SELECT 1 FROM session_entries WHERE session_id = NEW.session_id AND id = NEW.source_entry_id
    AND json_extract(messages, '$[0].role') IS 'assistant') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots.source_entry_id 必须引用同会话助手消息节点');
END;

CREATE TRIGGER workout_snapshots_display_entry_tool_result_insert
BEFORE INSERT ON workout_snapshots
WHEN NEW.display_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.display_entry_id
    AND json_extract(messages, '$[0].role') IS 'toolResult') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots.display_entry_id 必须引用同会话工具结果节点');
END;

CREATE TRIGGER workout_snapshots_display_entry_tool_result
BEFORE UPDATE OF display_entry_id ON workout_snapshots
WHEN NEW.display_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.display_entry_id
    AND json_extract(messages, '$[0].role') IS 'toolResult') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots.display_entry_id 必须引用同会话工具结果节点');
END;

CREATE TRIGGER workout_snapshots_confirmation_entry_user_insert
BEFORE INSERT ON workout_snapshots
WHEN NEW.confirmation_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.confirmation_entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots.confirmation_entry_id 必须引用同会话用户消息节点');
END;

CREATE TRIGGER workout_snapshots_confirmation_entry_user
BEFORE UPDATE OF confirmation_entry_id ON workout_snapshots
WHEN NEW.confirmation_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.confirmation_entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots.confirmation_entry_id 必须引用同会话用户消息节点');
END;

CREATE TRIGGER workout_snapshots_immutable_update
BEFORE UPDATE OF proposal_id, session_id, request_entry_id, source_entry_id, performed_on,
    base_workout_id, base_workout_version, payload, created_at ON workout_snapshots
BEGIN
    SELECT RAISE(ABORT, 'workout_snapshots 的归属、依据版本与内容不可修改');
END;

CREATE TRIGGER workout_save_records_immutable_update
BEFORE UPDATE ON workout_save_records
BEGIN
    SELECT RAISE(ABORT, 'workout_save_records 固定保存结果不可修改');
END;
