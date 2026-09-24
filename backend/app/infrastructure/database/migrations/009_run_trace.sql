-- 每次 Run 的受限诊断轨迹；不存储模型参数或工具载荷。
CREATE TABLE conversation_run_trace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES conversation_runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('planning_tools', 'evaluation_tools', 'general')),
    tool_call_id TEXT,
    tool_name TEXT,
    status TEXT NOT NULL CHECK (status IN ('success', 'failure')),
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_conversation_run_trace_run_sequence
    ON conversation_run_trace(run_id, id);
