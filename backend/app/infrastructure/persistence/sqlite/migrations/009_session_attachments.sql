-- 版本 9：会话原附件元数据与有序用户节点、Steering 输入关联。
CREATE TABLE session_attachments (
    attachment_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL CHECK (length(file_name) > 0),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0 AND size_bytes <= 100000),
    storage_ref TEXT NOT NULL CHECK (
        storage_ref = 'tmp/sessions/' || session_id || '/attachments/' || attachment_id ||
            CASE lower(substr(file_name, -3)) WHEN '.md' THEN '.md' ELSE '.txt' END
        AND (lower(substr(file_name, -3)) = '.md' OR lower(substr(file_name, -4)) = '.txt')
    ),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    UNIQUE (session_id, attachment_id)
) STRICT;
CREATE INDEX idx_session_attachments_session ON session_attachments(session_id);

CREATE TABLE session_entry_attachments (
    session_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (session_id, entry_id, attachment_id),
    UNIQUE (session_id, entry_id, position),
    FOREIGN KEY (session_id, entry_id) REFERENCES session_entries(session_id, id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, attachment_id) REFERENCES session_attachments(session_id, attachment_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE steering_input_attachments (
    session_id TEXT NOT NULL,
    steering_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (session_id, steering_id, attachment_id),
    UNIQUE (session_id, steering_id, position),
    FOREIGN KEY (session_id, steering_id) REFERENCES steering_inputs(session_id, id) ON DELETE CASCADE,
    FOREIGN KEY (session_id, attachment_id) REFERENCES session_attachments(session_id, attachment_id) ON DELETE CASCADE
) STRICT;

CREATE TRIGGER session_attachments_immutable_update
BEFORE UPDATE ON session_attachments
BEGIN
    SELECT RAISE(ABORT, 'session_attachments 原文元数据不可修改');
END;

CREATE TRIGGER session_entry_attachments_user_insert
BEFORE INSERT ON session_entry_attachments
WHEN (SELECT 1 FROM session_entries WHERE session_id = NEW.session_id AND id = NEW.entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, '附件必须关联同会话用户节点');
END;

CREATE TRIGGER session_entry_attachments_user_update
BEFORE UPDATE ON session_entry_attachments
WHEN (SELECT 1 FROM session_entries WHERE session_id = NEW.session_id AND id = NEW.entry_id
    AND json_extract(messages, '$[0].role') IS 'user') IS NULL
BEGIN
    SELECT RAISE(ABORT, '附件必须关联同会话用户节点');
END;
