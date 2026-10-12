-- 版本 13：结构化保存确认操作账本。
-- 独立确认操作记录四字段请求（operation_id / business_kind / proposal_id /
-- display_entry_id）、后端生成的确认节点与执行终态，供任务 2 的确认 HTTP 读写。
-- 不设会话或节点外键：已完成保存结果在会话与消息删除后仍可追溯；删除会话只清除未完成操作。
CREATE TABLE save_confirmation_operations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    business_kind TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    display_entry_id TEXT NOT NULL,
    confirmation_entry_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CONSTRAINT save_confirmation_operations_kind CHECK (
        business_kind IN ('profile', 'plan', 'workout')
    ),
    CONSTRAINT save_confirmation_operations_status CHECK (
        status IN ('processing', 'saved', 'failed')
    ),
    CONSTRAINT save_confirmation_operations_created_at CHECK (created_at > 0),
    CONSTRAINT save_confirmation_operations_updated_at CHECK (updated_at > 0),
    -- 状态与 result / error 严格配对：processing 两者皆空，saved 只带 result，failed 只带 error。
    CONSTRAINT save_confirmation_operations_outcome CHECK (
        CASE status
            WHEN 'processing' THEN result IS NULL AND error IS NULL
            WHEN 'saved' THEN result IS NOT NULL AND error IS NULL
            ELSE result IS NULL AND error IS NOT NULL
        END
    ),
    CONSTRAINT save_confirmation_operations_result_shape CHECK (
        result IS NULL OR (json_valid(result) AND json_type(result) IS 'object')
    ),
    CONSTRAINT save_confirmation_operations_error_shape CHECK (
        error IS NULL OR (json_valid(error) AND json_type(error) IS 'object')
    ),
    -- 固定保存结果必须归属同一快照。
    CONSTRAINT save_confirmation_operations_result_proposal CHECK (
        result IS NULL OR json_extract(result, '$.proposal_id') = proposal_id
    )
) STRICT;

-- 按会话与状态支持历史与恢复查询；按会话与确认节点定位事件。
CREATE INDEX idx_save_confirmation_operations_session_status
    ON save_confirmation_operations (session_id, status);
CREATE INDEX idx_save_confirmation_operations_confirmation
    ON save_confirmation_operations (session_id, confirmation_entry_id);

-- 操作四字段请求与目标关联创建后不可改换；只允许更新状态、结果、错误与时间。
CREATE TRIGGER save_confirmation_operations_immutable_update
BEFORE UPDATE OF operation_id, session_id, business_kind, proposal_id, display_entry_id,
    created_at ON save_confirmation_operations
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'save_confirmation_operations 的请求归属与目标不可修改');
END;
