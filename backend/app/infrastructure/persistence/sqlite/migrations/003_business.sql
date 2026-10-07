-- Fit-Agent schema 版本 3：个人画像、只读动作目录、确认卡片与最小提交凭证。
-- 时间字段为 UTC 毫秒 INTEGER，ID 为 TEXT；全部表使用 STRICT。
-- 业务内容使用经过 Pydantic 校验的 JSON 文本保存。

-- 单用户个人画像，固定一行。version 首次建档为 1，每次成功更新递增 1。
CREATE TABLE profile (
    id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL CHECK (version > 0),
    content TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    CONSTRAINT profile_content_shape CHECK (
        json_valid(content) AND json_type(content) IS 'object'
    )
) STRICT;

-- 只读动作目录。load_convention 与数据集 Schema 的重量口径枚举一致。
CREATE TABLE exercises (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    body_part TEXT NOT NULL,
    equipment TEXT NOT NULL,
    target TEXT NOT NULL,
    muscle_group TEXT NOT NULL,
    secondary_muscles TEXT NOT NULL,
    load_convention TEXT,
    steps TEXT NOT NULL,
    CONSTRAINT exercises_load_convention CHECK (
        load_convention IS NULL OR load_convention IN (
            'per_implement', 'barbell_total', 'machine_display', 'plates_total',
            'per_side', 'added_weight', 'assistance_weight'
        )
    ),
    CONSTRAINT exercises_secondary_muscles_shape CHECK (
        json_valid(secondary_muscles) AND json_type(secondary_muscles) IS 'array'
    ),
    CONSTRAINT exercises_steps_shape CHECK (
        json_valid(steps) AND json_type(steps) IS 'object'
    )
) STRICT;

-- 确认卡片。payload 为按 kind 校验的完整待提交业务对象，result 为固定提交结果。
CREATE TABLE confirmations (
    id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    request_entry_id TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    replaces_id TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    base_profile_version INTEGER,
    base_plan_id TEXT,
    status TEXT NOT NULL,
    result TEXT,
    created_at INTEGER NOT NULL,
    committed_at INTEGER,
    CONSTRAINT confirmations_kind CHECK (kind IN ('profile', 'plan', 'workout')),
    CONSTRAINT confirmations_status CHECK (
        status IN ('pending', 'committed', 'invalidated')
    ),
    CONSTRAINT confirmations_result_pair CHECK (
        (status = 'committed')
        = (result IS NOT NULL AND committed_at IS NOT NULL)
    ),
    CONSTRAINT confirmations_payload_shape CHECK (
        json_valid(payload) AND json_type(payload) IS 'object'
    ),
    CONSTRAINT confirmations_session
        FOREIGN KEY (session_id) REFERENCES sessions (id),
    CONSTRAINT confirmations_request_entry
        FOREIGN KEY (session_id, request_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT confirmations_source_entry
        FOREIGN KEY (session_id, source_entry_id)
        REFERENCES session_entries (session_id, id),
    CONSTRAINT confirmations_replaces
        FOREIGN KEY (replaces_id) REFERENCES confirmations (id)
) STRICT;

-- 独立最小提交凭证：完整卡片删除后仍可返回原固定结果。
CREATE TABLE confirmation_receipts (
    confirmation_id TEXT NOT NULL PRIMARY KEY,
    result TEXT NOT NULL,
    committed_at INTEGER NOT NULL,
    CONSTRAINT confirmation_receipts_result_shape CHECK (
        json_valid(result) AND json_type(result) IS 'object'
    )
) STRICT;

CREATE INDEX idx_confirmations_session ON confirmations (session_id);
CREATE INDEX idx_confirmations_replaces ON confirmations (replaces_id);

-- 请求节点必须引用同会话用户消息节点。
CREATE TRIGGER confirmations_request_entry_user_insert
BEFORE INSERT ON confirmations
FOR EACH ROW
WHEN (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.request_entry_id
      AND json_extract(messages, '$[0].role') IS 'user'
) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'confirmations.request_entry_id 必须引用同会话用户消息节点');
END;

-- 来源节点必须引用同会话助手消息节点。
CREATE TRIGGER confirmations_source_entry_assistant_insert
BEFORE INSERT ON confirmations
FOR EACH ROW
WHEN (
    SELECT 1 FROM session_entries
    WHERE session_id = NEW.session_id
      AND id = NEW.source_entry_id
      AND json_extract(messages, '$[0].role') IS 'assistant'
) IS NULL
BEGIN
    SELECT RAISE(ABORT, 'confirmations.source_entry_id 必须引用同会话助手消息节点');
END;
