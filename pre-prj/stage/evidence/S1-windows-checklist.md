# Stage 1 Windows 验收清单（方案 A · 同版本）

> Stage 1 结项门槛 = 同版本 Windows 全量自动化 + 隔离库人工实测。**已执行（2026-09-09，Agent 实测）**；判定见下文 §3，全部通过。
> 背景与验收对照见 `S1-evidence-linux-2026-09-09.md`。

## 0. 前置

- [x] 记录 `git rev-parse HEAD`：`1a3187620868e7ea85e77826046ca4440b5d8c32`（Stage 1 已整合：`d93fec3` + `1a31876` 均在历史内）；`git status --porcelain -- backend pre-prj/stage` 为空
- [x] Python 3.13+；依赖已装：`uv sync --group dev --locked`
- [x] 环境：Windows 11 Pro build 26200，PowerShell 5.1.26100.8328，Python 3.13.13（backend/.venv），uv 0.11.28

## 1. 自动化测试（阻断项）

```powershell
cd <repo>\backend
uv sync --group dev --locked
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

- [x] 预期 `243 passed`，`$LASTEXITCODE = 0`（出现 `PytestUnhandledThreadExceptionWarning` 即计失败，需回传用例名与日志）
- 实际：`243 passed in 10.61s`，`$LASTEXITCODE = 0`，`-W` 门无触发

## 2. 隔离库人工清单（不碰 `%LOCALAPPDATA%\Fit-Agent`）

```powershell
$env:FIT_AGENT_DATA_DIR = Join-Path $env:TEMP ("fitagent-s1-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $env:FIT_AGENT_DATA_DIR | Out-Null
```

### 2.1 空库迁移 + 种子/档案基线

```powershell
cd <repo>\backend
@'
import asyncio, os
from storage.db import Database
async def main():
    db = Database(os.path.join(os.environ["FIT_AGENT_DATA_DIR"], "app.db")); await db.open()
    print("migrate=", await db.migrate(), "user_version=", await db.pragma_value("user_version"))
    async with db.transaction() as conn:
        for sql in ["SELECT COUNT(*) FROM exercises",
                    "SELECT COUNT(*) FROM exercises WHERE recommendable=1 OR active=0",
                    "SELECT COUNT(*) FROM exercises WHERE standard_name_zh LIKE '%哑铃分腿蹲%'",
                    "SELECT id, profile_json, context_version FROM user_profile"]:
            print(sql, "->", await (await conn.execute(sql)).fetchall())
    await db.close()
asyncio.run(main())
'@ | Set-Content -Encoding utf8 $env:TEMP\s1_probe.py
.\.venv\Scripts\python.exe $env:TEMP\s1_probe.py; $LASTEXITCODE
```

- [x] 预期：`migrate=3`、`user_version=3`、`exercises=24`、第二行 `0`、哑铃分腿蹲 `0`、`user_profile=(1, None, 0)`
- 实际：`migrate= 3 user_version= 3`；`total: [(24,)]`；`bad-flag: [(0,)]`；`dumbbell-split-squat: [(0,)]`；`user_profile: [(1, None, 0)]`（隔离目录，未触碰 `%LOCALAPPDATA%\Fit-Agent`）

### 2.2 Stage 0 库升级（只含 001 的旧库 → 生产迁移目录）

```powershell
cd <repo>\backend
@'
import asyncio, os, shutil
from pathlib import Path
from storage.db import Database
from storage.migrations import DEFAULT_MIGRATIONS_DIR
root = Path(os.environ["FIT_AGENT_DATA_DIR"]); d = root / "stage0_migrations"; d.mkdir(exist_ok=True)
shutil.copy(DEFAULT_MIGRATIONS_DIR / "001_stage0_runtime_and_settings.sql", d)
async def main():
    db = Database(root / "legacy.db", migrations_dir=d); await db.open(); await db.migrate()
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO conversations (id, created_at) VALUES ('c1','2026-09-09T00:00:00Z')")
        await conn.execute("INSERT INTO app_config (key, value, updated_at) VALUES ('business_timezone','Asia/Shanghai','2026-09-09T00:00:00Z')")
    print("before_version=", await db.pragma_value("user_version")); await db.close()
    db = Database(root / "legacy.db"); await db.open(); print("after_migrate=", await db.migrate())
    async with db.transaction() as conn:
        for sql in ["SELECT COUNT(*) FROM conversations",
                    "SELECT value FROM app_config WHERE key='business_timezone'",
                    "SELECT COUNT(*) FROM exercises",
                    "SELECT id, profile_json, context_version FROM user_profile"]:
            print(sql, "->", await (await conn.execute(sql)).fetchall())
    await db.close()
asyncio.run(main())
'@ | Set-Content -Encoding utf8 $env:TEMP\s1_upgrade.py
.\.venv\Scripts\python.exe $env:TEMP\s1_upgrade.py; $LASTEXITCODE
```

- [x] 预期：`before_version=1` → `after_migrate=3`；会话数 `1`、`business_timezone=Asia/Shanghai` 保留、种子 `24`、档案 `(1, None, 0)`
- 实际：`before_version= 1`；`after_migrate= 3`；`conversations: [(1,)]`；`tz: [('Asia/Shanghai',)]`；`exercises: [(24,)]`；`user_profile: [(1, None, 0)]`

### 2.3 启动 / 停服 / 重开（回环与 Host/Origin 边界）

```powershell
cd <repo>\backend
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000   # 前台窗口，保持运行
```

另开终端：

```powershell
curl.exe -s http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "host-bad=%{http_code}\n"   -H "Host: 198.51.100.7:8000"     http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "origin-bad=%{http_code}\n" -H "Origin: http://evil.example" http://127.0.0.1:8000/healthz
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 | Select-Object LocalAddress,LocalPort,OwningProcess
```

- [x] `/healthz`：`{"status":"ok","database":"open","business_timezone":"Asia/Shanghai","provider_has_api_key":false}`
- [x] `host-bad=403`、`origin-bad=403`；监听仅 `127.0.0.1`
- [x] 停服（Agent 注入 `CTRL_C_EVENT`，等价 Ctrl+C，非强杀）日志：`Shutting down` → `Application shutdown complete` → `Finished server process [25656]`
- [x] 用同一 `$env:FIT_AGENT_DATA_DIR` 重开 → `business_timezone` 仍为 `Asia/Shanghai`（不重取样）；停服后目录只剩 `app.db`（另有探针产物 `legacy.db`/`stage0_migrations/`，无 `-wal`/`-shm` 遗留，WAL 已 checkpoint）
- 实际：见上四项，均符合预期

### 2.4 种子清单人工只读核对

- [x] 用 2.1 探针再跑一次；逐项名称可加 `SELECT standard_name_zh FROM exercises ORDER BY standard_name_zh;`
- [x] 与 `S1-evidence-linux-2026-09-09.md` §5 的 24 项逐条一致
- 实际：名称集 24=24，`missing=[] extra=[]`；属性级对比（equipment_variant/record_type/load_convention/unilateral/modes/source_ref）逐行一致，无差异。说明：文档 §5 的 `equipment` 列用数据集可读词（`leverage machine`/`body weight`/`sled`），库内规范 token 为 `leverage_machine`/`bodyweight`/`sled_machine`（与 `003_stage1_action_seed.sql` 种子原文逐字一致）；`自重双杠臂屈伸` modes 语义序 `["垂直推","肘伸"]` 与文档「垂直推＋肘伸」一致。全部 `recommendable=0`、`active=1`

## 3. 判定与回传

| # | 步骤 | 退出码 | 实际输出 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 自动化 | 0 | `243 passed in 10.61s` | ☑通过 |
| 2.1 | 空库迁移/种子 | 0 | `migrate=3`/24 项/`user_profile=(1,None,0)` | ☑通过 |
| 2.2 | Stage 0 升级 | 0 | `1→3`，数据保留+种子 24 | ☑通过 |
| 2.3 | 启动/边界/重开 | 0 | healthz ok/双 403/停服收尾日志/时区不重取样 | ☑通过 |
| 2.4 | 种子清单 | 0 | 24 项名称+属性逐条一致 | ☑通过 |

- 结论：☑ 通过（Stage 1 可结项）
- 回传：本文件已填好，证据为隔离目录探针实测输出；系统时区未改动，无需恢复步骤；Stage 0 已豁免的系统时区切换仍为豁免未验，不转写为通过。
