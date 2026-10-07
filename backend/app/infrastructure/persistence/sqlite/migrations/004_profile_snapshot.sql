-- Fit-Agent schema 版本 4：画像待确认快照与保存幂等记录。
-- 时间字段为 UTC 毫秒 INTEGER；全部表使用 STRICT；业务内容为 Pydantic 校验后的 JSON 文本。

-- 待确认快照：proposal_id 唯一标识固定内容，同时作为保存操作幂等标识。
-- display_entry_id 在准备结果持久化后绑定，confirmation_entry_id 在保存执行前绑定。
-- profile_id 固定为 1，不设指向 profile.id 的外键：首次建档时画像行尚不存在。
CREATE TABLE profile_snapshots (
    proposal_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    request_entry_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    profile_id INTEGER NOT NULL,
    base_profile_version INTEGER,
    payload TEXT NOT NULL,
    display_entry_id TEXT,
    confirmation_entry_id TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    CONSTRAINT profile_snapshots_target CHECK (profile_id = 1),
    CONSTRAINT profile_snapshots_base_version CHECK (
        base_profile_version IS NULL OR base_profile_version > 0
    ),
    CONSTRAINT profile_snapshots_created_at CHECK (created_at > 0),
    CONSTRAINT profile_snapshots_status CHECK (
        status IN ('pending', 'processing', 'saved', 'invalidated', 'conflicted')
    ),
    CONSTRAINT profile_snapshots_binding CHECK (
        confirmation_entry_id IS NULL OR display_entry_id IS NOT NULL
    ),
    CONSTRAINT profile_snapshots_saved_binding CHECK (
        status IN ('pending', 'invalidated', 'conflicted')
        OR (display_entry_id IS NOT NULL AND confirmation_entry_id IS NOT NULL)
    ),
    CONSTRAINT profile_snapshots_payload_shape CHECK (
        json_valid(payload) AND json_type(payload) IS 'object'
    ),
    CONSTRAINT profile_snapshots_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT profile_snapshots_request_entry
        FOREIGN KEY (session_id, request_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT profile_snapshots_source_entry
        FOREIGN KEY (session_id, source_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT profile_snapshots_display_entry
        FOREIGN KEY (session_id, display_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT profile_snapshots_confirmation_entry
        FOREIGN KEY (session_id, confirmation_entry_id)
        REFERENCES session_entries (session_id, id)
) STRICT;

-- 已完成保存的幂等记录：不引用会话及消息表，会话与消息删除后仍保留归属和固定结果。
CREATE TABLE profile_save_records (
    proposal_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    profile_id INTEGER NOT NULL,
    display_entry_id TEXT NOT NULL,
    confirmation_entry_id TEXT NOT NULL,
    result TEXT NOT NULL,
    saved_at INTEGER NOT NULL,
    CONSTRAINT profile_save_records_target CHECK (profile_id = 1),
    CONSTRAINT profile_save_records_saved_at CHECK (saved_at > 0),
    CONSTRAINT profile_save_records_result_shape CHECK (
        json_valid(result) AND json_type(result) IS 'object'
    ),
    -- 固定保存结果与记录自身的标识、原保存时间保持一致。
    CONSTRAINT profile_save_records_result_identity CHECK (
        json_type(result, '$.proposal_id') IS 'text'
        AND json_extract(result, '$.proposal_id') = proposal_id
        AND json_extract(result, '$.saved_at') = saved_at
    )
) STRICT;

CREATE INDEX idx_profile_snapshots_pending
    ON profile_snapshots (session_id, profile_id, status);
-- 一条确认消息只授权保存一个指定快照：重复绑定原 proposal_id 幂等，绑定其他快照被拒绝。
CREATE UNIQUE INDEX idx_profile_snapshots_confirmation
    ON profile_snapshots (session_id, confirmation_entry_id);
CREATE INDEX idx_profile_save_records_confirmation
    ON profile_save_records (session_id, confirmation_entry_id);

-- 请求节点必须引用同会话用户消息节点。
CREATE TRIGGER profile_snapshots_request_entry_user_insert
BEFORE INSERT ON profile_snapshots
FOR EACH ROW
WHEN (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.request_entry_id
      AND json_extract(messages, '$[0].role') IS 'user'
) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'profile_snapshots.request_entry_id 必须引用同会话用户消息节点');
END;

-- 工具调用来源节点必须引用同会话助手消息节点。
CREATE TRIGGER profile_snapshots_source_entry_assistant_insert
BEFORE INSERT ON profile_snapshots
FOR EACH ROW
WHEN (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.source_entry_id
      AND json_extract(messages, '$[0].role') IS 'assistant'
) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'profile_snapshots.source_entry_id 必须引用同会话助手消息节点');
END;

-- 展示绑定只能指向同会话工具结果节点。
CREATE TRIGGER profile_snapshots_display_entry_tool_result
BEFORE UPDATE OF display_entry_id ON profile_snapshots
FOR EACH ROW
WHEN NEW.display_entry_id IS NOT NULL
  AND (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.display_entry_id
      AND json_extract(messages, '$[0].role') IS 'toolResult'
  ) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'profile_snapshots.display_entry_id 必须引用同会话工具结果节点');
END;

-- 确认绑定只能指向同会话用户消息节点。
CREATE TRIGGER profile_snapshots_confirmation_entry_user
BEFORE UPDATE OF confirmation_entry_id ON profile_snapshots
FOR EACH ROW
WHEN NEW.confirmation_entry_id IS NOT NULL
  AND (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.confirmation_entry_id
      AND json_extract(messages, '$[0].role') IS 'user'
  ) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'profile_snapshots.confirmation_entry_id 必须引用同会话用户消息节点');
END;

-- 快照归属、依据版本与内容创建后不可修改；修改内容只能创建新快照。
CREATE TRIGGER profile_snapshots_immutable_update
BEFORE UPDATE OF
    session_id, request_entry_id, source_entry_id, profile_id,
    base_profile_version, payload, created_at ON profile_snapshots
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'profile_snapshots 的归属、依据版本与内容不可修改');
END;
