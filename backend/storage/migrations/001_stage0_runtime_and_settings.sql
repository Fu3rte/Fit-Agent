-- 001：Stage 0 运行时四表 + 固定业务时区与 Provider 配置最小结构
-- 依据 07 章 7.4（运行时表与事务规则）、7.3（固定业务时区）、10 章 10.3（Provider 同库密钥）。
-- 不建 11 张业务表（归 Stage 1-3，07 章责任边界）。

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    -- 全局唯一创建幂等键（07 7.4/7.5）
    client_request_id TEXT NOT NULL UNIQUE,
    -- 仅允许五种已拍状态（07 7.4）
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    -- 机器可读错误码；服务重启遗留 Run 由 Stage 4 标 interrupted_by_restart
    error_code TEXT,
    -- 手动重试指针：指向旧 Run，不复活旧记录（07 7.4）
    retry_of_run_id TEXT REFERENCES runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    -- 会话内全序（事务内 MAX(seq)+1 分配；锁串行化保证不重复）
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    -- user_request：随 pending Run 原子保存的用户请求应用事实
    -- framework：单条框架消息，payload 为 runtime 层经 PydanticAI
    --            ModelMessagesTypeAdapter 产出的 JSON（本层不依赖 PydanticAI）
    -- partial：流式合并文本分批落盘的部分回答，不冒充完整成功回答（07 7.4）
    kind TEXT NOT NULL CHECK (kind IN ('user_request', 'framework', 'partial')),
    payload_json TEXT NOT NULL,
    UNIQUE (conversation_id, seq)
);

CREATE TABLE run_events (
    -- 仅作数据库行身份（07 7.4）：不做前端恢复游标、无事件重放接口
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    -- 通用轨迹形态：event_type + payload_json，不为每种轨迹建独立表
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_messages_conversation_seq ON messages (conversation_id, seq);
CREATE INDEX idx_messages_run ON messages (run_id);
CREATE INDEX idx_runs_conversation ON runs (conversation_id, created_at);
CREATE INDEX idx_run_events_run ON run_events (run_id);

-- 固定业务时区（07 7.3：首次保存后不随系统时区变化）与后续固定配置的通用存储
CREATE TABLE app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Provider 配置与明文 API Key 同库（10.3）；查询默认只投影 has_api_key
CREATE TABLE provider_config (
    provider TEXT PRIMARY KEY,
    api_key TEXT,
    updated_at TEXT NOT NULL
);
