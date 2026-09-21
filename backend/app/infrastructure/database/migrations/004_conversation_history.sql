-- 004：对话历史四张表（conversations／conversation_entries／conversation_runs／conversation_run_events）
-- 依据：CHAT_HISTORY_IMPLEMENTATION_PLAN.md §3.1／§3.2（存储层与 Repository 层）；
--       Pi 源码 packages/coding-agent/src/core/session-manager.ts:25-168（Entry 基类：id／timestamp）、
--       :1041-1067（append-only 追加）；对应测试 session-manager/save-entry.test.ts。
-- 口径（已拍）：SQLite 是会话事实源；Entry 在会话内按 sequence 单调追加（append-only，不分支）；
--       Run Event 由 UNIQUE(run_id, sequence) 保序；删除会话级联清理 entries／runs／events；
--       事件类型与状态取值域为闭集。
-- 边界：只建对话历史四张表与一个 entries 检索索引；不改动 001／002／003 的既有业务表；不建压缩缓存表、
--       全文搜索表与分页游标；本文件不含 BEGIN/COMMIT/PRAGMA user_version，由 storage/migrations.py
--       统一包事务并推进到 4。

-- ---------- 会话头部（对齐 Pi SessionHeader：稳定身份 + 时间戳） ----------
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ---------- 会话条目（对齐 Pi SessionEntry：id／type／timestamp 三要素 + 会话内序号） ----------
CREATE TABLE conversation_entries (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    -- 同一会话内单调递增的追加序号（从 1 开始）：append-only 顺序的唯一事实源。
    sequence INTEGER NOT NULL,
    -- 首批闭集三类：普通消息／压缩摘要／确认动作。
    entry_type TEXT NOT NULL CHECK (entry_type IN ('message', 'compaction', 'confirmation')),
    -- 领域层 payload 形状校验后序列化；数据库兜底要求是合法 JSON，损坏即报错。
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, sequence)
);

-- 时间序读取（plan §9：按会话全量重建）。
CREATE INDEX idx_conversation_entries_conversation_created
    ON conversation_entries (conversation_id, created_at, id);

-- ---------- 一轮 Run（用户 Entry + pending／running／waiting／终态） ----------
CREATE TABLE conversation_runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    -- LangGraph 恢复用的 thread 身份：全局唯一，同一 thread 只属于一个 Run。
    thread_id TEXT NOT NULL UNIQUE,
    -- 本轮幂等键：全局唯一，重复请求不得产生第二个 Run 或第二条用户 Entry。
    client_request_id TEXT NOT NULL UNIQUE,
    user_entry_id TEXT NOT NULL,
    assistant_entry_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'waiting', 'completed', 'failed', 'cancelled')
    ),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ---------- Run 事件流（UI 重建／等待卡恢复／诊断；不进入模型上下文） ----------
CREATE TABLE conversation_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES conversation_runs(id) ON DELETE CASCADE,
    -- 同一 Run 内单调序号：重复序号由 UNIQUE 拒绝，不静默覆盖。
    sequence INTEGER NOT NULL,
    -- 与现有 AgentEvent 五类事件同名（graph/workflow.py AgentEventName）。
    event_type TEXT NOT NULL CHECK (
        event_type IN ('node', 'message', 'waiting', 'done', 'error')
    ),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence)
);
