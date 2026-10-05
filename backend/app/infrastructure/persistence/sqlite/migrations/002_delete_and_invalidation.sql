-- Fit-Agent schema 版本 2：允许编辑/重新生成在受理事务内实际删除会话内容，
-- 并记录被删除的旧操作编号，防止失效编号被重新执行。
-- 由 database.py 在单一事务内应用并写入 PRAGMA user_version。

-- 编辑/重新生成需要真实删除被替换的会话路径，放开不可变节点的禁止删除守卫。
-- 父节点、运行、Steering、操作及外键完整性仍由现有外键与触发器保证。
DROP TRIGGER session_entries_no_delete;

-- 已失效操作编号。仅保留 operation_id 与 session_id，不保留旧请求正文或运行结果。
CREATE TABLE session_operation_invalidations (
    operation_id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL,
    CONSTRAINT session_operation_invalidations_session
        FOREIGN KEY (session_id) REFERENCES sessions (id)
) STRICT;
