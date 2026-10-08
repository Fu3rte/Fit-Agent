-- 版本 7：计划历史版本、不可修改快照与固定保存结果。
CREATE TABLE plans (
    id TEXT NOT NULL PRIMARY KEY,
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    content TEXT NOT NULL CHECK (json_valid(content) AND json_type(content) IS 'object'),
    created_at INTEGER NOT NULL CHECK (created_at > 0)
) STRICT;
CREATE UNIQUE INDEX idx_plans_current ON plans(is_current) WHERE is_current = 1;
CREATE INDEX idx_plans_created_at ON plans(created_at DESC, id DESC);

CREATE TABLE plan_snapshots (
    proposal_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    request_entry_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    base_profile_version INTEGER NOT NULL CHECK (base_profile_version > 0),
    base_plan_id TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) IS 'object'),
    display_entry_id TEXT,
    confirmation_entry_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'saved', 'invalidated', 'conflicted')),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    CHECK (confirmation_entry_id IS NULL OR display_entry_id IS NOT NULL),
    CHECK (status IN ('pending', 'invalidated', 'conflicted')
        OR (display_entry_id IS NOT NULL AND confirmation_entry_id IS NOT NULL)),
    FOREIGN KEY (session_id, request_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, source_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, display_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, confirmation_entry_id) REFERENCES session_entries(session_id, id)
) STRICT;

-- 已完成保存结果随原会话删除继续保留。
CREATE TABLE plan_save_records (
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
        AND json_type(result, '$.created_at') IS 'integer'
        AND json_extract(result, '$.created_at') = saved_at)
) STRICT;
CREATE INDEX idx_plan_snapshots_pending ON plan_snapshots(session_id, status);
CREATE UNIQUE INDEX idx_plan_snapshots_confirmation ON plan_snapshots(session_id, confirmation_entry_id);
CREATE UNIQUE INDEX idx_plan_save_records_confirmation ON plan_save_records(session_id, confirmation_entry_id);

CREATE TRIGGER plans_immutable_update
BEFORE UPDATE OF id, content, created_at ON plans
BEGIN
    SELECT RAISE(ABORT, 'plans 历史内容不可修改');
END;
CREATE TRIGGER plan_snapshots_immutable_update
BEFORE UPDATE OF proposal_id, session_id, request_entry_id, source_entry_id,
    base_profile_version, base_plan_id, payload, created_at ON plan_snapshots
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots 归属、依据与内容不可修改');
END;
CREATE TRIGGER plan_save_records_immutable_update
BEFORE UPDATE ON plan_save_records
BEGIN
    SELECT RAISE(ABORT, 'plan_save_records 固定结果不可修改');
END;
CREATE TRIGGER plan_snapshots_request_entry_user_insert
BEFORE INSERT ON plan_snapshots
WHEN (SELECT 1 FROM session_entries WHERE session_id = NEW.session_id AND id = NEW.request_entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots.request_entry_id 必须引用同会话用户节点');
END;
CREATE TRIGGER plan_snapshots_source_entry_assistant_insert
BEFORE INSERT ON plan_snapshots
WHEN (SELECT 1 FROM session_entries WHERE session_id = NEW.session_id AND id = NEW.source_entry_id
    AND json_extract(messages, '$[0].role') IS 'assistant') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots.source_entry_id 必须引用同会话助手节点');
END;
CREATE TRIGGER plan_snapshots_display_entry_tool_result_insert
BEFORE INSERT ON plan_snapshots
WHEN NEW.display_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.display_entry_id
    AND json_extract(messages, '$[0].role') IS 'toolResult') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots.display_entry_id 必须引用同会话工具结果节点');
END;
CREATE TRIGGER plan_snapshots_display_entry_tool_result
BEFORE UPDATE OF display_entry_id ON plan_snapshots
WHEN NEW.display_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.display_entry_id
    AND json_extract(messages, '$[0].role') IS 'toolResult') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots.display_entry_id 必须引用同会话工具结果节点');
END;
CREATE TRIGGER plan_snapshots_confirmation_entry_user_insert
BEFORE INSERT ON plan_snapshots
WHEN NEW.confirmation_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.confirmation_entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots.confirmation_entry_id 必须引用同会话用户节点');
END;
CREATE TRIGGER plan_snapshots_confirmation_entry_user
BEFORE UPDATE OF confirmation_entry_id ON plan_snapshots
WHEN NEW.confirmation_entry_id IS NOT NULL AND (SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id AND id = NEW.confirmation_entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots.confirmation_entry_id 必须引用同会话用户节点');
END;
