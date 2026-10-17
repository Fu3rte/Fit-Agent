-- 版本 15：运行记录新增可空停止原因列。
-- 停止原因作为独立字段管理，与终态状态分离：只记录首个有效停止原因，
-- 供重启后查询；单调时钟截止值属于进程内状态，不落库。
ALTER TABLE session_runs ADD COLUMN stop_reason TEXT;
