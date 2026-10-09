-- 版本 8：准备类型与可空画像依据，保留完整旧快照及固定保存事实。
CREATE TABLE plan_snapshots_v8 (
    proposal_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    request_entry_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    preparation_kind TEXT NOT NULL CHECK (preparation_kind IN ('generation', 'import', 'adjustment')),
    base_profile_version INTEGER CHECK (base_profile_version IS NULL OR base_profile_version > 0),
    base_plan_id TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) IS 'object'),
    display_entry_id TEXT,
    confirmation_entry_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'saved', 'invalidated', 'conflicted')),
    created_at INTEGER NOT NULL CHECK (created_at > 0),
    CHECK (preparation_kind <> 'generation' OR base_profile_version IS NOT NULL),
    CHECK (confirmation_entry_id IS NULL OR display_entry_id IS NOT NULL),
    CHECK (status IN ('pending', 'invalidated', 'conflicted')
        OR (display_entry_id IS NOT NULL AND confirmation_entry_id IS NOT NULL)),
    FOREIGN KEY (session_id, request_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, source_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, display_entry_id) REFERENCES session_entries(session_id, id),
    FOREIGN KEY (session_id, confirmation_entry_id) REFERENCES session_entries(session_id, id)
) STRICT;
INSERT INTO plan_snapshots_v8
SELECT proposal_id, session_id, request_entry_id, source_entry_id, 'generation',
    base_profile_version, base_plan_id, payload, display_entry_id, confirmation_entry_id,
    status, created_at FROM plan_snapshots;
DROP TABLE plan_snapshots;
ALTER TABLE plan_snapshots_v8 RENAME TO plan_snapshots;
CREATE INDEX idx_plan_snapshots_pending ON plan_snapshots(session_id, status);
CREATE UNIQUE INDEX idx_plan_snapshots_confirmation ON plan_snapshots(session_id, confirmation_entry_id);
CREATE TRIGGER plan_snapshots_immutable_update
BEFORE UPDATE OF proposal_id, session_id, request_entry_id, source_entry_id,
    preparation_kind, base_profile_version, base_plan_id, payload, created_at ON plan_snapshots
BEGIN
    SELECT RAISE(ABORT, 'plan_snapshots 归属、类型、依据与内容不可修改');
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
