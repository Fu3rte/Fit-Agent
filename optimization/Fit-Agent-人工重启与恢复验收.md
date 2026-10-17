# Fit-Agent 人工重启与恢复验收

本文件给出 Windows 下的停机、退出确认、启动与恢复核对步骤。适用于稳定性治理后的单应用进程、单 Uvicorn worker、单线程 Agent 执行器部署。

## 验收状态

当前开发环境为 WSL。任务五实现文件已落地，WSL/Linux 边界回归记录为 83 项通过；Windows 剩余测试延期至 Windows 专项验收时执行，任务五整体保持待验收状态。

Windows 独占副本已删除，相关测试进程已停止，8000、15173、18081 已无监听。本文件中的路径和命令用于后续重建独占环境，执行前须重新确认资源授权与端口空闲。

Windows 待验收项：
- 故障封闭下的 503 最终断言、原运行查询与停止诊断。
- 故障停机的完整进程残留与端口释放证据。
- 重启准入阻断、数据库不可用时保持不可受理。
- `interrupted`、已有终态及迟到提交结果核对。
- 已保存数据保留、确认保存对账及 `confirmation_interrupted` 恢复。
- 恢复后新运行、新确认及单实例检查。
- Windows 中未通过的 3 项短预算测试及驱动最终复跑。

WSL/Linux 已有回归证据的范围为对应测试套件；实际进程级重启恢复另需取证。Windows 验收完成须记录 Windows 实际命令、退出码与结果。稳定性治理方案与前后端契约中的最终恢复要求保持有效。

## 1. 独占资源与前提

必须在明确授权的独占副本中执行。后续 Windows 补测预留配置如下，使用前重新核对：

- 仓库副本：`C:\Users\Ericcc\fit-agent-task5`。
- 后端：`127.0.0.1:8000`。
- 前端：`http://localhost:15173`。
- 可控模型端点：`127.0.0.1:18081`。
- 数据库：`C:\Users\Ericcc\fit-agent-task5\backend\data\fit-agent.db`。
- 附件与工作文件：`C:\Users\Ericcc\fit-agent-task5\tmp\sessions\`。
- 证据：`C:\Users\Ericcc\fit-agent-task5\tmp\stability-task5\`。

禁止操作 `D:\Repository\Fit-Agent` 及其运行资源、共享服务、共享数据库和 V2 量化测试。副本的 `.env` 只使用无凭据的本地测试模型配置。

前置检查：

```powershell
cd C:\Users\Ericcc\fit-agent-task5
git status --short
Get-NetTCPConnection -State Listen -LocalPort 8000,15173,18081 -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*fit-agent-task5*' } |
  Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine
```

端口必须为空，且没有属于该副本的旧进程。`backend\data` 与 `tmp` 必须是本次验收的独立副本，不能指向真实用户数据。

验收用 `backend\.env`：

```text
FIT_AGENT_RUN_BUDGET_SECONDS=6
FIT_AGENT_SQLITE_TIMEOUT_SECONDS=30
```

`FIT_AGENT_SQLITE_TIMEOUT_SECONDS` 只用于延长真实 SQLite 锁等待，默认值为 5 秒。模型配置通过应用内 `PUT /api/provider` 写入本地端点，API Key 使用无凭据的测试值，不写入日志或报告。

### 1.1 后续 Windows 完整驱动入口

重新建立独占副本并同步当前源码，安装根目录与 frontend 的 npm 依赖，以及 `uv sync --project backend --frozen`。完成上述配置后，在独占副本根目录执行：

```powershell
cd C:\Users\Ericcc\fit-agent-task5
& .\backend\.venv\Scripts\python.exe .\backend\test\check_stability_task5_windows.py `
  --root (Get-Location).Path `
  --out .\tmp\stability-task5
```

每次测试脚本超时上限为 600 秒，实际测试派发给测试 subagent。驱动的两项调整尚待最终复跑确认，完成全部阶段后核对退出码与完整证据。Windows 短预算测试需测量请求时延并核对原断言语义，全部未通过项保持待验证。

## 2. 实际启动链与 PID 取证

启动：

```powershell
$env:FIT_AGENT_FRONTEND_PORT = "15173"
cd C:\Users\Ericcc\fit-agent-task5
npm run dev
```

进程关系：

1. `npm` 启动 `node scripts/dev.mjs`。
2. `dev.mjs` 启动 `concurrently`，分别启动 `npm run dev:backend` 与 `npm run dev:frontend`。
3. 后端命令为 `uv run --env-file .env python -m uvicorn app.interfaces.http:app --reload --host 127.0.0.1 --port 8000`。
4. `uvicorn --reload` 包含 reloader 监督进程与应用进程；reloader 会重新拉起应用。
5. 前端为 Vite，`strictPort`，端口由 `FIT_AGENT_FRONTEND_PORT` 注入。

记录根 PID、创建时间、父 PID、可执行路径和命令行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\Users\Ericcc\fit-agent-task5\scripts\stability-task5\process.ps1 `
  snapshot -RootPid <根PID> -Ports 8000,15173 `
  -OutFile C:\Users\Ericcc\fit-agent-task5\tmp\stability-task5\process-start.json
```

证据只保留 `pid`、`ppid`、`creation_date`、`executable_path` 与脱敏后的 `command_line`。不得记录 API Key、完整提示词或原始异常正文。本次已执行的文件工具运行于 Agent 执行线程，未观察到独立工具子进程；后续验收须按实际工具行为追踪子进程。进程树中应出现且仅出现一个 Uvicorn 应用 worker、一个 reloader、一个 Vite 进程及其构建子进程。

## 3. 人工受控停机与退出确认

### 3.1 协作停机

优先使用应用现有取消链：前端停止或关闭运行，等待运行进入持久化终态。确认 SSE 已关闭，且没有伪造的 `done/error`。

### 3.2 卡死停机

`worker_stuck` 或 `terminal_persist_failed` 时，应用保持不可受理，必须人工终止。步骤：

1. 记录故障运行 `run_id`、`operation_id`、`stop_diagnostic`、故障发生时间。
2. 记录当前应用根 PID 和完整进程树。
3. 先停止 reloader 监督进程，防止退出核对期间自动生成新实例。
4. 再停止应用 worker、Vite、npm/concurrently 与根进程。
5. 每一次终止前按 PID、创建时间和可执行路径核对目标。禁止按进程名批量终止。
6. 终止后再次取证，旧应用、执行线程所属进程和已记录工具子进程必须全部退出。
7. 端口 8000 与 15173 必须无监听。

使用脚本按快照逐 PID 核验并终止：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\Users\Ericcc\fit-agent-task5\scripts\stability-task5\process.ps1 `
  stop -SnapshotFile C:\Users\Ericcc\fit-agent-task5\tmp\stability-task5\process-fault.json `
  -Ports 8000,15173 `
  -OutFile C:\Users\Ericcc\fit-agent-task5\tmp\stability-task5\process-stop.json
```

判定：

- `remaining_pids` 必须为空。
- `listeners` 必须为空。
- 任一旧执行进程仍存活时，保持停止状态，禁止启动新实例。

## 4. 启动恢复与准入

旧进程退出证据完整后，重新执行第 2 节启动命令。恢复顺序由 `backend/app/interfaces/http.py` 的 lifespan 固定：

1. `reset_fault_closure()`。
2. `open_database()`。
3. `SessionService.recover_interrupted()`：仍为 `running` 的运行改为 `interrupted`，保留 `stop_diagnostic`，丢弃未结算 Steering。
4. 业务保存恢复：`profile_snapshots`、`workout_snapshots`、`plan_snapshots` 中未提交的 `processing` 回到 `pending`。
5. `ConfirmationService.recover_interrupted()`：`processing` 确认按已有保存记录恢复 `saved`，否则记录 `failed`、`confirmation_interrupted` 并解除快照绑定与占用。
6. 完成上述步骤后才开始监听并受理 HTTP。

准入核对：

- 恢复完成前，HTTP 连接被拒绝或连接建立后无响应。
- 恢复完成后，`GET /api/sessions` 返回 200。
- 恢复前不得受理新运行或新确认。
- 数据库不可用时应用启动失败，不得先开放服务。

数据库不可用与准入阻断可直接用路径故障验证：

```powershell
Rename-Item C:\Users\Ericcc\fit-agent-task5\backend\data data-ok
New-Item -ItemType File -Path C:\Users\Ericcc\fit-agent-task5\backend\data
npm run dev
# 观察启动异常、8000 无监听、HTTP 连接拒绝
Remove-Item C:\Users\Ericcc\fit-agent-task5\backend\data
Rename-Item C:\Users\Ericcc\fit-agent-task5\backend\data-ok data
# 清除故障后重新人工启动
```

启动失败、连接拒绝与 HTTP 拒绝分别记录实际观察值，不得用同一结论替代。

## 5. 恢复结果核对

### 5.1 运行终态

```sql
SELECT id, status, stop_reason, stop_diagnostic, finished_at
FROM session_runs
WHERE id = '<run_id>';
```

核对：

- 仍未终态的运行进入 `interrupted`，`stop_diagnostic` 保留。
- 已提交终态保持原值。
- `terminal_persist_failed` 的迟到 `finish` 若已成功，以实际持久化结果为准；等待超时不证明事务已回滚。
- 同一运行只有一条记录，不得重复提交终态或重复释放名额。

HTTP 核对：

```text
GET /api/sessions/{session_id}/runs/{run_id}
GET /api/sessions/{session_id}/operations/{operation_id}
```

### 5.2 业务保存与确认

```sql
SELECT proposal_id, status, confirmation_entry_id FROM profile_snapshots WHERE proposal_id = '<proposal_id>';
SELECT proposal_id, session_id, display_entry_id, confirmation_entry_id, saved_at
FROM profile_save_records WHERE proposal_id = '<proposal_id>';
SELECT operation_id, status, result, error FROM save_confirmation_operations WHERE operation_id = '<operation_id>';
SELECT version, content FROM profile WHERE id = 1;
```

核对：

- 已提交业务快照、版本与保存记录保持有效。
- 同一 `proposal_id` 不重复增加版本或保存记录。
- 未提交事务按 SQLite 回滚语义处理。
- `processing` 确认有保存记录时恢复 `saved`；无保存记录时恢复 `failed`、`confirmation_interrupted`，快照回到 `pending`，解除确认绑定。
- 恢复不调用模型，不自动重放保存或工具请求。

前端使用原 `run_id`、`operation_id` 与确认 `operation_id` 查询，展示 `interrupted` 与已保存结果。

### 5.3 新实例与残留

- 新实例启动后只有一个 Uvicorn 应用 worker 和一个监听 8000 的应用。
- 旧工具进程、旧应用进程和旧 reloader 不得残留。
- 重启只恢复持久化状态，不自动续跑未完成工具。
- 恢复后新运行可正常受理并提交终态；新确认可正常保存。

## 6. 前端与回归入口

在隔离副本中执行：

```powershell
cd C:\Users\Ericcc\fit-agent-task5\frontend
npm run verify:stability
npm run verify:runs
npm run verify:confirm
npm run build
```

后端回归：

```powershell
cd C:\Users\Ericcc\fit-agent-task5\backend
.venv\Scripts\python.exe -m pytest test\test_governance_contract.py -q
.venv\Scripts\python.exe -m pytest test\test_stability_task2.py -q
.venv\Scripts\python.exe -m pytest test\test_stability_task3.py -q
```

任务三套件包含真实 SQLite 锁下的 `terminal_persist_failed`、迟到 `finish` 成功与运行占用保留场景。Windows 进程启停与 `worker_stuck` 的完整恢复流程待按本文件第 2–5 节执行 `npm run dev` 验收。

## 7. 资源清理

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\Users\Ericcc\fit-agent-task5\scripts\stability-task5\process.ps1 `
  stop -SnapshotFile C:\Users\Ericcc\fit-agent-task5\tmp\stability-task5\process-final.json `
  -Ports 8000,15173 `
  -OutFile C:\Users\Ericcc\fit-agent-task5\tmp\stability-task5\process-final-stop.json
```

确认：

- 8000、15173、18081 均无监听。
- 没有命令行包含 `fit-agent-task5` 的残留进程。
- 可控模型端点、SQLite 锁辅助进程已退出。
- 证据目录位于 `tmp`，不提交数据库、附件、日志或凭据。
