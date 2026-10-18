# Fit-Agent 人工重启与恢复验收

本文件给出 Windows 下的停机、退出确认、启动与恢复核对步骤。适用于稳定性治理后的单应用进程、单 Uvicorn worker、单线程 Agent 执行器部署。

## 验收状态

Windows 进程级验收完成。独占副本 `C:\Users\Ericcc\fit-agent-task5`、独占端口 8000/15173/18081、证据目录 `D:\Repository\Fit-Agent\tmp\stability-task5\`。全部取证命令退出码为 0，读数登记在 `tmp/stability-task-5-report.md`。

已实测通过：

- `terminal_persist_failed` 与 `worker_stuck` 两种形态的故障封闭：SSE 5 个事件、无 `done/error` 终态、运行行保持 `running`、名额占用保留。
- 故障封闭下的准入：新运行返回 503 且错误码 `service_unavailable`；会话、原运行、历史、运行操作、画像五路只读查询全部 200。
- 停机与退出确认：按快照逐 PID 核验身份后终止，`remaining_pids` 为空、监听为空；Ctrl+Break 通道下 19 个进程 0.52 秒内自然全部退出、监听清零、根退出码 `0xC000013A`。
- 重启准入：外部排他锁构造恢复窗口期间 8000 监听数 0、探测全部连接超时；锁释放后 0.88 秒受理。
- 恢复核对：`running` 转 `interrupted`；`completed` 终态原值不变、运行行数不变；已保存画像与确认记录保持有效且未重复增加；未提交事务按 SQLite 语义回滚，确认操作记 `failed` 与 `confirmation_interrupted`、快照回 `pending`、绑定清空。
- 数据库不可用：路径故障下应用启动失败并打印 `Application startup failed. Exiting.`，8000 无监听、HTTP 连接超时，故障清除后重新人工启动恢复服务。
- 恢复后新运行与新确认均可正常受理；新实例内 spawn 应用 worker 恰 1 个、其父进程持有唯一 8000 监听、旧工具与应用进程无残留。

两条既有语义作为运维前提：

- 停机诊断写入只有 3 次 1 秒的重试窗口，数据库不可用超过约 3.1 秒时 `stop_diagnostic` 不再写入，封闭原因以应用日志与内存诊断为准，重启后运行行进入 `interrupted`。
- 故障封闭期间数据库仍被外部持锁时，运行请求在幂等解析阶段读库失败返回 500；数据库可用时返回 503。

前端完整浏览器故障矩阵与跨任务集成归任务六验收。本文件路径与命令可在新独占副本上直接复用，执行前须重新确认资源授权与端口空闲。

## 1. 独占资源与前提

必须在明确授权的独占副本中执行。后续 Windows 补测预留配置如下，使用前重新核对：

- 仓库副本：`C:\Users\Ericcc\fit-agent-task5`。
- 后端：`127.0.0.1:8000`。
- 前端：`http://localhost:15173`。
- 可控模型端点：`127.0.0.1:18081`。
- 数据库：`C:\Users\Ericcc\fit-agent-task5\backend\data\fit-agent.db`。
- 附件与工作文件：`C:\Users\Ericcc\fit-agent-task5\tmp\sessions\`。
- 证据：`D:\Repository\Fit-Agent\tmp\stability-task5\`（仓库 `.gitignore` 覆盖 `tmp/`，副本删除后证据仍可复核）。

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

验收用 `backend\.env`（按实例分段，驱动自动写入）：

```text
# 第一个实例：含 worker_stuck 注入
FIT_AGENT_RUN_BUDGET_SECONDS=20
FIT_AGENT_SQLITE_TIMEOUT_SECONDS=2

# 重启实例：恢复门与后续场景
FIT_AGENT_RUN_BUDGET_SECONDS=20
FIT_AGENT_SQLITE_TIMEOUT_SECONDS=30
```

分段取值依据：停机诊断的写入只有 3 次 1 秒的重试窗口（`app/interfaces/http.py:1696-1707`）。第一个实例把 SQLite 超时压到 2 秒，被外部持锁阻塞的终态写入在故障封闭前就已失败并放弃，锁释放后不会迟到提交终态，`stop_diagnostic` 的落库窗口才可核对；重启实例用 30 秒，让恢复流程的首笔读取在持锁上等待而非立刻报错，观察窗口才落在「应用已启动、恢复未完成」这段时间内。SQLite 连接超时默认 5 秒，运行预算默认 300 秒，两者均通过既有治理配置入口注入。模型配置通过应用内 `PUT /api/provider` 写入本地端点，API Key 使用无凭据的测试值，不写入日志或报告。

### 1.1 驱动入口

重建独占副本：`git clone file:///D:/Repository/Fit-Agent C:\Users\Ericcc\fit-agent-task5`，根目录与 `frontend` 各执行 `npm install --no-audit --no-fund`，再 `uv sync --project backend --frozen`。副本目录名须为 `fit-agent-task5`，驱动按该名称判定可清理的数据范围。四条命令必须串行，每条结束后确认端口释放，单条超时上限 600 秒，实际执行派发给测试 subagent。

```powershell
cd C:\Users\Ericcc\fit-agent-task5
$out = "D:\Repository\Fit-Agent\tmp\stability-task5"

# 1 全链：预热、基线保存、正常运行、terminal_persist_failed 封闭、503 与只读、
#   按 PID 停机、恢复门、重启恢复、恢复后运行与确认、中断确认对账
& .\backend\.venv\Scripts\python.exe .\backend\test\check_stability_task5_windows.py `
  --root (Get-Location).Path --out $out --frontend-port 15173

# 2 worker_stuck 链与单实例判据（重建全新 backend\data）
& .\backend\.venv\Scripts\python.exe .\backend\test\check_stability_task5_worker_stuck.py `
  --root (Get-Location).Path --out "$out\worker-stuck" --frontend-port 15173

# 3 Ctrl+Break 优雅退出：进程树自然退出与端口释放
& .\backend\.venv\Scripts\python.exe .\backend\test\check_stability_task5_graceful_stop.py `
  --root (Get-Location).Path --out "$out\graceful" --frontend-port 15173

# 4 数据库路径故障：启动失败语义与不可受理
& .\backend\.venv\Scripts\python.exe .\backend\test\check_stability_task5_db_down.py `
  --root (Get-Location).Path --out $out
```

判据取值口径见第 3 节与第 5 节；`tmp/stability-task-5-report.md` 登记了本轮全部实测读数。

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

首轮模型调用包含客户端构造与系统提示装配开销，副本冷启动到双就绪实测 61.3 秒，同一副本温启动实测 12.4 秒。驱动的 `wait_backend`、`wait_frontend` 上限取 150 秒，并在进入计时场景前执行一次 `final` 模式预热运行；预热运行自身有独立 phase 记录，与被验收运行分开计数。

记录根 PID、创建时间、父 PID、可执行路径和命令行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\Users\Ericcc\fit-agent-task5\scripts\stability-task5\process.ps1 `
  snapshot -RootPid <根PID> -Ports 8000,15173 `
  -OutFile D:\Repository\Fit-Agent\tmp\stability-task5\process-start.json
```

证据只保留 `pid`、`ppid`、`creation_date`、`executable_path` 与脱敏后的 `command_line`，不得记录 API Key、完整提示词或原始异常正文。满树 19 个进程：`cmd.exe` → `node.exe` → npm → `uv.exe` → venv 启动器 → reloader（持有 8000 监听 socket）→ spawn 应用 worker（命令行含 `--multiprocessing-fork`），前端为 Vite 与 esbuild。单实例判据按「spawn worker 恰 1 个、其 PID 与启动日志 `Started server process [PID]` 一致、8000 监听记录恰 1 条且属主等于该 worker 的父进程」，按命令行统计 reloader 无法唯一化（含 `uvicorn` 与 `--reload` 字样的进程有 4 个）。本轮工具调用全部运行在 Agent 执行线程内，未派生独立工具子进程；带外部子进程的工具按第 3.3 节的逐 PID 核验追加取证。

## 3. 人工受控停机与退出确认

### 3.1 正常停止

在交互控制台按 Ctrl+C，`concurrently` 与 uvicorn 走既有收尾路径。核对项：已记录 PID 全部自然退出、8000 与 15173 无监听、根进程退出码为 `0xC000013A`。实测 `check_stability_task5_graceful_stop.py` 下 19 个进程在 0.52 秒内全部退出、`survivors=[]`、`listeners_after=0`。

无控制台的执行宿主里 `CTRL_C_EVENT` 没有送达通道（实测 19 个进程 60 秒零退出）；需先 `FreeConsole` 加 `AllocConsole` 取得控制台句柄，再按进程组发送 `CTRL_BREAK_EVENT`。该通道下进程树整体即时退出，uvicorn 的 `Shutting down` 与 `Finished server process` 日志不会落盘；收尾日志的观测在交互控制台执行本节步骤时取得。

### 3.2 协作停机

优先使用应用现有取消链：前端停止或关闭运行，等待运行进入持久化终态。确认 SSE 已关闭，且没有伪造的 `done/error`。

### 3.3 卡死停机

`worker_stuck` 或 `terminal_persist_failed` 时，应用保持不可受理，必须人工终止。步骤：

1. 记录故障运行 `run_id`、`operation_id`、故障发生时间；`stop_diagnostic` 按实际读数记录，数据库不可用超过约 3.1 秒时该列为 NULL，封闭原因以应用日志中的 `{run_id}/{reason}` 与内存诊断为准。
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
  stop -SnapshotFile D:\Repository\Fit-Agent\tmp\stability-task5\process-fault.json `
  -Ports 8000,15173 `
  -OutFile D:\Repository\Fit-Agent\tmp\stability-task5\process-stop.json
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
- 故障封闭且数据库可用时，新运行返回 503 且错误码为 `service_unavailable`；数据库被外部持锁时，运行请求在幂等解析阶段读库失败，沿用既有 500 语义。

恢复门用外部 `BEGIN EXCLUSIVE` 持锁构造：

```powershell
python C:\Users\Ericcc\fit-agent-task5\scripts\stability-task5\lock_helper.py `
  --db C:\Users\Ericcc\fit-agent-task5\backend\data\fit-agent.db `
  --mode exclusive --hold-seconds 90 --ready-file <ready> --release-file <release>
# 持锁后启动 npm run dev，等日志出现 "Waiting for application startup"
# 连续探测 GET /api/sessions 8 秒，记录结果分布与 8000 监听数
# 写 release 文件释放锁，再等待服务就绪
```

uvicorn 在 lifespan 启动成功后才绑定端口，因此恢复未完成期间探测结果表现为连接超时、8000 监听数为 0；锁释放后同一探测点返回 200。实测窗口内 5 次探测全部 `ConnectTimeout`（1314 至 1331 毫秒）、`listeners_during_gate=0`、锁释放后 0.88 秒完成受理。

数据库不可用用路径故障验证，`backend\test\check_stability_task5_db_down.py` 已把该流程与取证固化（把 `backend\data` 改名为 `data-held`，在同一路径放一个普通文件，启动真实 `npm run dev`，探测 HTTP、统计 8000 监听数、按快照停机并恢复目录）：

```powershell
cd C:\Users\Ericcc\fit-agent-task5
& .\backend\.venv\Scripts\python.exe .\backend\test\check_stability_task5_db_down.py `
  --root (Get-Location).Path --out D:\Repository\Fit-Agent\tmp\stability-task5
```

等价手工步骤：

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

任务三套件包含真实 SQLite 锁下的 `terminal_persist_failed`、迟到 `finish` 成功与运行占用保留场景。Windows 副本内实测：`test_governance_contract.py` 17 项通过、`test_stability_task2.py` 41 项通过、`test_stability_task3.py` 23 项通过 2 项未通过（`test_http_noncooperative_tool_keeps_slot_and_records_worker_stuck` 与 `test_http_cooperative_tool_exits_on_cancel_and_releases_slot`，均因注入预算 0.2 与 0.3 秒低于本机工具线程启动耗时，`assert started.is_set()` 未成立）。进程启停与两种故障形态的恢复流程已按第 2 至第 5 节以 `npm run dev` 实跑取证。

## 7. 资源清理

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File C:\Users\Ericcc\fit-agent-task5\scripts\stability-task5\process.ps1 `
  stop -SnapshotFile D:\Repository\Fit-Agent\tmp\stability-task5\process-final.json `
  -Ports 8000,15173 `
  -OutFile D:\Repository\Fit-Agent\tmp\stability-task5\process-final-stop.json
```

确认：

- 8000、15173、18081 均无监听。
- 没有命令行包含 `fit-agent-task5` 的残留进程。
- 可控模型端点、SQLite 锁辅助进程已退出。
- 证据目录位于 `tmp`，不提交数据库、附件、日志或凭据。
