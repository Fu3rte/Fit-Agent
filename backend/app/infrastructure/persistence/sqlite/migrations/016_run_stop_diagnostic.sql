-- 版本 16：运行记录新增可空诊断原因列。
-- 存放需要在重启后核对的执行诊断：worker_stuck（执行尚未退出）、
-- terminal_persist_failed（终态持久化等待超时或失败）。与停止原因分开管理，
-- 故障监控与触发接线由后续任务接入。
ALTER TABLE session_runs ADD COLUMN stop_diagnostic TEXT;
