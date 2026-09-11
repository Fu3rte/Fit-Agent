# Stage 0 交接（S0-08）：运行说明、验证边界、数据库操作限制与 Windows 人工验收方案

> 本文件只做 S0-08 的交接汇总，不新增功能、不改变 PLAN.md / design-decisions.md / architecture 正本的任何约束。
> 本轮全部运行证据在 **Linux（WSL2，Ubuntu 26.04）** 产生；**Windows 未实际执行任何命令**，第 7 节为交付给用户执行的方案，
> 在同版本 Windows 实测结果回传前，Windows 验证与 Stage 0 正式结项保持**待验**。

## 1. 本阶段实际交付的可运行入口（核对结果）

| 入口 | 实际位置 | 状态 |
|---|---|---|
| 进程入口（单 Uvicorn Worker，仅回环） | `backend/main.py`（`--host` 只接受 `127.0.0.1`/`localhost`/`::1`，默认 `127.0.0.1:8000`） | 已实现 |
| 应用工厂 + lifespan（唯一连接、迁移、固定时区初始化、Provider 状态、Host/Origin 校验） | `backend/api/app.py:create_app` | 已实现 |
| 唯一 HTTP 端点 | `GET /healthz`（返回 `status`、`database`、`business_timezone`、`provider_has_api_key`） | 已实现 |
| 数据目录解析与隔离接缝 | `backend/config.py:resolve_data_dir`，环境变量 `FIT_AGENT_DATA_DIR`（优先于默认 `platformdirs.user_data_dir("Fit-Agent", appauthor=False)`） | 已实现 |
| 数据库底座 | `backend/storage/db.py`（单连接 + 单 `asyncio.Lock` + `transaction()`）、`storage/migrations.py`、`storage/migrations/001_stage0_runtime_and_settings.sql` | 已实现 |
| 存储层 repo | `storage/run_repo.py`（四张运行时表）、`storage/setting_repo.py`（业务时区 + Provider 配置/`has_api_key` 投影 + 内部取 Key） | 已实现 |
| 测试入口 | `backend/tests/`（69 个用例，全部临时目录/临时文件库；含首轮三项 P1 修复补的 6 个取消/日志回归用例，逐项清单见证据文件第 6.4 节） | 已实现 |

尚未实现、不得当作可用入口（属后续阶段）：Provider 设置页与任何凭据 HTTP 接口、SSE/聊天/草稿/只读看板路由、业务表与领域服务、
Agent 运行时、前端静态托管、"打开数据目录"按钮、数据库在线下载、备份入口、自动备份任务、发布打包（`[project.scripts]` 未注册）。
`backend/app/`、`backend/runtime/`、`backend/domain/`、`backend/api/routes_*.py` 目前是单行占位 docstring（目录骨架），无实现。

## 2. 安装前提（可复制）

- Python **3.13+**（`backend/pyproject.toml` `requires-python = ">=3.13"`；本次实测解释器 3.13.15）。
- [uv](https://docs.astral.sh/uv/)（锁定文件 `backend/uv.lock` 存在，同步命令按 uv 写）。
- 项目依赖（全部为 PLAN.md「技术栈」已拍依赖，Stage 0 授权加入）：`fastapi`、`uvicorn`、`aiosqlite`、`platformdirs`、
  `pydantic-ai-slim`、`tzlocal`、`tzdata`；开发组：`pytest`。
- 开发期工具 **不在项目依赖内**（本阶段未擅自加入）：`ruff`、`pyright`（pyright 需要 Node.js）。
  本机为 uv 工具层全局安装（`~/.local/bin/ruff`、`~/.local/bin/pyright`）。
- 运行时不需要 Node.js；不制作 exe/桌面程序（10.1）。
- **本阶段不是可安装的 Python 包**：`backend/pyproject.toml` 无 `[build-system]`，只有 `[tool.uv] package = false`，
  源码以 `backend/` 下的顶层模块/包（`config.py`、`main.py`、`storage/`、`domain/` …）直接导入，不产出发行物。
  发布打包（`[build-system]` 与 `[project.scripts]` 入口）待 Stage 5 补，届时删掉 `package = false`（见第 9 节）。
  该声明是 `uv sync` 可复制的前提：若恢复 `[build-system] = uv_build` 而没有 `src/backend/__init__.py`，同步会失败。

Linux / macOS（本轮实际执行，退出码 `0`，见 `evidence/S0-08-evidence-2026-09-09.md`）：

```bash
cd backend
uv sync --group dev --locked   # 按 uv.lock 生成 .venv 并安装依赖 + pytest
```

Windows PowerShell（已实测 exit `0`，与上面同一条命令、同一 `uv.lock`，见 `evidence/S0-08-evidence-2026-09-09.md` Windows 行）：

```powershell
cd backend
uv sync --group dev --locked   # 生成 .venv\Scripts\python.exe
```

## 3. 测试命令

Linux（本轮实际执行，见 `evidence/S0-08-evidence-2026-09-09.md`）：

```bash
cd backend
# 全量：69 passed，退出码 0（`-W` 门把 aiosqlite 工作线程告警升级为错误）
timeout 120s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
timeout 120s .venv/bin/python -m pytest tests/test_provider_settings.py -q   # S0-07 凭据底座复跑：12 passed
```

**必带 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 门**：首轮全量运行出现过 1 条间歇性的
`PytestUnhandledThreadExceptionWarning`（aiosqlite 工作线程 `RuntimeError: Event loop is closed`，teardown 噪声），
该门把这类“测试通过但收尾漏关闭连接”的噪声变成硬失败。Linux 已实测：加门后 `69 passed`、退出码 `0`；
同代码不加门复跑亦 `69 passed`、无告警。

Windows（已实测：`69 passed`、`$LASTEXITCODE=0`，与上面同一套测试，命令只差解释器路径，见 `evidence/S0-08-evidence-2026-09-09.md`）：

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
```

隔离性（stage0.md 第 6 节）：用例只写 pytest `tmp_path` 下的临时文件库与临时数据目录，不触碰 `%LOCALAPPDATA%\Fit-Agent\app.db`，
不读取真实 API Key，不访问模型或其他外部网络。

## 4. 最小启动命令（仅回环）

Linux（本轮实际执行过，命令与证据见证据文件）：

```bash
cd backend
.venv/bin/python main.py --host 127.0.0.1 --port 8000
# 另开终端：
curl -i http://127.0.0.1:8000/healthz
```

隔离数据目录（推荐验收时使用，不写真实用户目录）：

```bash
FIT_AGENT_DATA_DIR=$(mktemp -d) .venv/bin/python main.py --host 127.0.0.1 --port 8000
```

Windows（**未实测**）：

```powershell
cd backend
$env:FIT_AGENT_DATA_DIR = Join-Path $env:TEMP "fit-agent-s008"   # 隔离；不设则为 %LOCALAPPDATA%\Fit-Agent
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8000/healthz
```

硬边界（10.1，已实测）：`--host 0.0.0.0` 被 argparse 拒绝（`invalid choice`）；`Host` 非回环或 `Origin` 非回环 → `403`；
`ss -ltnH` 只见 `127.0.0.1:<port>`。停服用前台 `Ctrl+C`（Linux 亦可用 `kill -TERM <pid>`），观察 `Application shutdown complete` /
`Finished server process`。

## 5. 数据库操作限制（10.2、10.3）

- 唯一数据库文件：平台用户数据目录内的 `app.db`（Windows `%LOCALAPPDATA%\Fit-Agent\app.db`，不多出厂商/版本子目录）；
  运行期另有 `app.db-wal`、`app.db-shm`，正常停服收尾后不应遗留。
- **服务停止后**才允许手动复制或删除数据目录（PowerShell `Copy-Item` / `Remove-Item`，Linux `cp -r` / `rm -rf`）。
- **运行中**唯一合规备份方式是 SQLite Backup API（`sqlite3.Connection.backup()` / `aiosqlite` 同底接口）；
  本阶段**没有**任何备份入口、下载入口或自动备份任务，也没有实现"打开数据目录"按钮 —— 该缺口保留在后续阶段，
  不得把"停止后再复制"说成运行中可备份。
- 不提供数据库在线下载（10.2）；数据文件权限继承操作系统用户目录 ACL，不自建权限系统。
- 明文 API Key 与业务数据同库：拥有同一操作系统用户权限者，或拿到数据库副本者，都能读取 Key。
  上述限制只是应用层约束，**不能禁止操作系统用户自行复制文件**。
- 默认查询投影只返回 `has_api_key`（无掩码字段）；完整 Key 不得进入响应、日志、Trace、`run_events` 或异常详情；
  进程内取 Key 只经 `SettingRepo.get_provider_api_key_internal`，真实模型调用接线归后续阶段。

## 6. 未覆盖的验证项（不得当作已通过）

1. Windows 底座自动化测试与人工实测（实际数据目录、启动/停服/重开、时区切换与恢复）：**未执行**。
2. Stage 4 的 Run 重启恢复（遗留 `pending`/`running` 改 `failed` + `interrupted_by_restart`）、全局单 Run 互斥、
   `409 conversation_busy`、实际取消中断、SSE 断线/补读：本阶段只做存储层条件写入与竞争测试，未验证。
3. 实际模型上下文投影、工具执行、真实 Provider 调用与凭据端到端（设置页 → 调用取 Key）：未接线、未验证。
4. Stage 3 的日程到期、计划切换、完成率分母消费固定时区：仅有 `business_date()` 的时间解释基础检查。
5. 备份/下载入口、"打开数据目录"按钮、Python 发布包、最终成品发布验收清单（PLAN.md 仍为待收口）：未实现。
6. 11 张业务表与领域/应用层逻辑：未创建、未实现（属 Stage 1–2）。

## 7. Windows 人工验收方案（PowerShell，与真实入口一一对应）

前提：同一代码版本（记录 `git rev-parse HEAD` 与 `git status --porcelain` 工作区差异，须与第 8 节 Linux 证据同版本或有明确差异说明）、
Python 3.13+、`uv sync --group dev --locked` 已完成（见第 2 节）。**每一步都记录：命令/步骤、退出码、实际输出、是否偏离预期。**

### 7.1 自动化测试（同版本）

```powershell
cd <repo>\backend
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

预期：`69 passed`（允许平台相关的时区样例通过形态一致），退出码 `0`。
`-W` 门与第 3 节一致（Linux 已实测通过，本条 Windows 命令仍未实测）：若出现
`PytestUnhandledThreadExceptionWarning` 则计为失败（而非警告），需连同该用例名与日志原文回传。

### 7.2 隔离数据目录下的启动 → 停服 → 重开

```powershell
cd <repo>\backend
$env:FIT_AGENT_DATA_DIR = Join-Path $env:TEMP ("fitagent-s008-" + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000      # 前台窗口，保持运行
```

另开终端：

```powershell
curl.exe -s http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "host-bad=%{http_code}\n" -H "Host: 198.51.100.7:8000" http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "origin-bad=%{http_code}\n" -H "Origin: http://evil.example" http://127.0.0.1:8000/healthz
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 | Select-Object LocalAddress,LocalPort,OwningProcess
Get-ChildItem $env:FIT_AGENT_DATA_DIR
```

预期：`/healthz` 返回 `{"status":"ok","database":"open","business_timezone":"<IANA 地区名>","provider_has_api_key":false}`；
两次校验请求均 `403`；监听只出现在 `127.0.0.1`；目录内有 `app.db`、`app.db-wal`、`app.db-shm`。

回到服务窗口按 `Ctrl+C`（正常停服路径；**勿用 `Stop-Process -Force`**，那是强杀，不等价于收尾，不能作为停服证据），
然后只检查收尾现象与文件形态：

```powershell
Get-ChildItem $env:FIT_AGENT_DATA_DIR
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000   # 应报 "No matching" 之类，端口已释放
```

预期：服务窗口出现 `Shutting down` → `Application shutdown complete` → `Finished server process`；
停服后目录只剩 `app.db`（WAL 已 checkpoint）。Ctrl+C 后的进程退出码由控制台决定，不作为判据，判据是上述收尾日志。

重开（同一 `$env:FIT_AGENT_DATA_DIR`）再次启动并 `curl.exe -s http://127.0.0.1:8000/healthz`：
`business_timezone` 必须与首次完全相同（07 7.3：不重取样），`database` 为 `open`。停服后再离库读取版本与结构
（PowerShell 里 `python -c "...` 的嵌套引号易被参数解析打碎，统一落成临时脚本文件跑）：

```powershell
cd <repo>\backend
@'
import os, sqlite3
path = os.path.join(os.environ["FIT_AGENT_DATA_DIR"], "app.db")
conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
print("user_version=", conn.execute("PRAGMA user_version").fetchone()[0])
print("journal_mode=", conn.execute("PRAGMA journal_mode").fetchone()[0])
print("tables=", sorted(r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")))
print("tz=", conn.execute(
    "SELECT value FROM app_config WHERE key='business_timezone'").fetchone())
print("providers=", conn.execute(
    "SELECT provider, (api_key IS NOT NULL) FROM provider_config").fetchall())
'@ | Set-Content -Encoding utf8 (Join-Path $env:TEMP "s008_read_db.py")
.\.venv\Scripts\python.exe (Join-Path $env:TEMP "s008_read_db.py")
$LASTEXITCODE
```

预期：`user_version= 1`；六张表 `app_config, conversations, messages, provider_config, run_events, runs`
（与 07 7.4 四张运行时表 + `app_config` + `provider_config` 一致；Linux 侧实测原文见证据文件第 4 节）；
`tz` 等于两次启动看到的值。

### 7.3 真实默认数据目录（覆盖 `%LOCALAPPDATA%\Fit-Agent\app.db`）

```powershell
Remove-Item Env:\FIT_AGENT_DATA_DIR
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000
# 另开终端
curl.exe -s http://127.0.0.1:8000/healthz
Test-Path "$env:LOCALAPPDATA\Fit-Agent\app.db"
Get-ChildItem "$env:LOCALAPPDATA\Fit-Agent"
```

预期：`True`；目录里只有 `app.db`（运行中另有 `-wal`/`-shm`），**没有**厂商或版本号子目录（`%LOCALAPPDATA%\Fit-Agent\app.db`，10.2）。
服务窗口 `Ctrl+C` 停服后，做一次"停服后手动复制"验证（不是备份入口，只是操作限制说明）：

```powershell
Copy-Item "$env:LOCALAPPDATA\Fit-Agent" "$env:TEMP\Fit-Agent-copy" -Recurse
Get-ChildItem "$env:TEMP\Fit-Agent-copy"
Remove-Item "$env:TEMP\Fit-Agent-copy" -Recurse
```

并记录事实：本阶段无"打开数据目录"按钮、无数据库下载、无备份入口、无运行中备份能力（10.2 只允许 SQLite Backup API，尚未接线）。

### 7.4 固定业务时区：切换系统时区 → 重启服务 → 恢复系统设置

先用管理员 PowerShell 记录并恢复原始时区（**必须执行恢复步骤**）：

```powershell
Get-TimeZone                                   # 记录原值，例如 China Standard Time
$OriginalTz = (Get-TimeZone).Id
Set-TimeZone -Id "Eastern Standard Time"       # 切换系统时区（夏令时地区）
Get-TimeZone
```

首次采样（写库）必须在切换时区**之前**完成，所以本步骤直接沿用 7.2 已有保存值的数据目录；按 7.2 的方式再启动一次：

```powershell
cd <repo>\backend
# 确认 $env:FIT_AGENT_DATA_DIR 仍指向 7.2 那个已写入 business_timezone 的目录（同一会话内未清除即可）
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000     # 前台服务窗口
```

另开终端：

```powershell
curl.exe -s http://127.0.0.1:8000/healthz      # 记录 business_timezone（应为切换前已保存的值）
```

预期：系统时区已变为 Eastern，但 `/healthz` 的 `business_timezone` 仍等于首次采样保存值（不被改写、不降级为 UTC/固定偏移）。
若不小心用了全新数据目录，首次值会随切换后的系统时区采样——此时第二次重开必须仍保持该首次值不变；两种情况都要写清实际现象。

恢复系统设置并验证：

```powershell
Set-TimeZone -Id $OriginalTz
Get-TimeZone                                   # 必须回到 7.4 开头记录的原值
Remove-Item Env:\FIT_AGENT_DATA_DIR            # 清理隔离变量
```

### 7.5 凭据边界的底座人工检查（无 HTTP 入口，只能走内部 repo）

设置页与凭据接口未实现，因此 Windows 侧只能用一次性脚本走 `SettingRepo` 内部路径（假 Key，辨识度明确，绝不用真实 Key）：

```powershell
cd <repo>\backend
$env:FIT_AGENT_DATA_DIR = Join-Path $env:TEMP "fitagent-s008-key"
New-Item -ItemType Directory -Force -Path $env:FIT_AGENT_DATA_DIR | Out-Null
@'
import asyncio, os
from storage.db import Database
from storage.setting_repo import SettingRepo, DEFAULT_PROVIDER

path = os.path.join(os.environ["FIT_AGENT_DATA_DIR"], "app.db")

async def main():
    db = Database(path)
    await db.open()
    await db.migrate()
    repo = SettingRepo(db)
    print("set:", await repo.set_provider_api_key(DEFAULT_PROVIDER, "FAKE-S008-ONLY-NOT-REAL"))
    print("projection:", await repo.get_provider_status(DEFAULT_PROVIDER))
    await db.close()

asyncio.run(main())
'@ | Set-Content -Encoding utf8 (Join-Path $env:TEMP "s008_key_check.py")
.\.venv\Scripts\python.exe (Join-Path $env:TEMP "s008_key_check.py")
$LASTEXITCODE
```

启动服务后确认 `curl.exe -s http://127.0.0.1:8000/healthz` 只报 `provider_has_api_key: true`（服务日志用
`.\.venv\Scripts\python.exe main.py ... *>&1 | Tee-Object $env:TEMP\s008-server.log` 采集），日志中检索假 Key 应无命中
（`Select-String -Path $env:TEMP\s008-server.log -Pattern "FAKE-S008-ONLY-NOT-REAL"` 无输出）；停服后清 Key
（把脚本里的 `set_provider_api_key` 换成 `delete_provider_api_key(DEFAULT_PROVIDER)` 再跑一次）并确认投影 `has_api_key=False`。

同样记录事实：这一步只是底座内部路径的人工复核，**不代替**后续设置页 + HTTP 查询接口的端到端凭据验收。

### 7.6 证据记录模板（Windows 回传时逐条填写）

```
任务编号：S0-08 / Windows 底座人工实测
平台：Windows <版本 build>，PowerShell <版本>，Python 3.13.x，uv <版本>
代码版本：git rev-parse HEAD = <sha>
工作区差异：git status --porcelain = <清单，或与 Linux 证据同版本的说明>
步骤/命令：<逐条粘贴实际执行的命令或人工动作>
退出码：<每条命令的 $LASTEXITCODE / 服务 Ctrl+C 后状态>
实际结果：<关键输出原文：/healthz 体、403 计数、ss/Get-NetTCPConnection 行、目录清单、user_version、business_timezone、Get-TimeZone 前后值>
判定：预期 vs 实际，逐项 通过/不通过/偏离说明
证据路径：<截图或日志保存位置>
未覆盖部分：<列出仍未执行/不适用的项>
系统设置恢复：Get-TimeZone = <原值>（必须与 7.4 开头记录一致）
```

## 8. 版本标识与工作区差异隔离

见 `pre-prj/stage/evidence/S0-08-evidence-2026-09-09.md`：Linux 侧记录的代码版本为 HEAD `16165572e8339e79b8115a72def01936b1570b26`
（当时 Stage 0 文件尚未提交）；Windows 实测取 Stage 0 入库后 HEAD `60a30c9fe088535a7b687ebe998db4af9e31df84`（`f82517d` 含 Stage 0，
其后 commit 仅 frontend），`git status --porcelain` 为空。两侧全量均为修复后 **69 用例** + `-W` 门 exit 0；
不得拿首轮 63 用例的快照比对。

## 9. 后续接入点（交接给 Stage 1–5）

| 后续要做的事 | 必须接的现有接缝 | 边界提醒 |
|---|---|---|
| Stage 1–2 业务表与领域服务 | `storage/migrations/` 追加 `002_*.sql`（连续编号）；`storage/<模块>/repo.py` 经 `Database.under_lock` / `transaction()` | 迁移失败即拒绝对外服务；不得以删库代替迁移；SQL 只在 repo |
| 草稿确认事务（01 章） | `app/confirm.py`（现为占位）→ `Database.transaction()` | 事务内禁止模型请求/工具执行/SSE |
| Stage 4 Agent 运行时 | `runtime/run_service.py` 用 `RunRepo.create_run_with_user_message`（`client_request_id` 幂等）、`start_run`、`complete_run`、`cancel_run`、`save_partial_answer`、`append_run_events`；重启恢复在 `migrate()` 之后扫描遗留 `pending`/`running` | 单锁不等于全局 Run 互斥；取消后不得补写晚到快照或覆盖终态 |
| 凭据端到端 | `api/routes_settings.py`（占位）→ `SettingRepo.set_provider_api_key` / `delete_provider_api_key` / `get_provider_status`；真实调用取 Key 用 `get_provider_api_key_internal` | 响应/日志/Trace/`run_events`/异常详情不得出现完整 Key |
| 前端静态托管与业务路由 | `api/app.py:create_app`（lifespan 已提供 `app.state.db`、`app.state.business_timezone`、`app.state.provider_has_api_key`） | 仍须保持单 Worker、仅回环 + Host/Origin 校验 |
| 发布与运维入口 | `backend/pyproject.toml`：当前 `[tool.uv] package = false` 且无 `[build-system]`/`[project.scripts]`；Stage 5 打包时补 build-system 与脚本入口并删除该开关 | 发布产物为 Python 项目，不含 exe；运行时不需 Node.js；改回打包前必须补 `src/backend/` 布局，否则 `uv sync` 失败 |
