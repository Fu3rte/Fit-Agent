# Stage 1 Windows 验收清单（方案 A · 同版本）

> Stage 1 结项门槛 = 同版本 Windows 全量自动化 + 隔离库人工实测。**当前未执行**；未逐项判定前不得视为通过。
> 背景与验收对照见 `S1-evidence-linux-2026-09-09.md`。

## 0. 前置

- [ ] 记录 `git rev-parse HEAD`（Stage 1 交付提交，整合前为 `d93fec3`）与 `git status --porcelain -- backend pre-prj/stage`
- [ ] Python 3.13+；依赖已装：`uv sync --group dev --locked`
- [ ] 环境：Windows `<build>`，PowerShell `<版本>`，Python `<版本>`，uv `<版本>`

## 1. 自动化测试（阻断项）

```powershell
cd <repo>\backend
uv sync --group dev --locked
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

- [ ] 预期 `243 passed`，`$LASTEXITCODE = 0`（出现 `PytestUnhandledThreadExceptionWarning` 即计失败，需回传用例名与日志）
- 实际：`______`

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

- [ ] 预期：`migrate=3`、`user_version=3`、`exercises=24`、第二行 `0`、哑铃分腿蹲 `0`、`user_profile=(1, None, 0)`
- 实际：`______`

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

- [ ] 预期：`before_version=1` → `after_migrate=3`；会话数 `1`、`business_timezone=Asia/Shanghai` 保留、种子 `24`、档案 `(1, None, 0)`
- 实际：`______`

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

- [ ] `/healthz`：`status=ok`、`database=open`、`business_timezone` 为 IANA 地区名、`provider_has_api_key=false`
- [ ] `host-bad=403`、`origin-bad=403`；监听仅 `127.0.0.1`
- [ ] 服务窗口 `Ctrl+C`（**勿用 `Stop-Process -Force`**）→ `Application shutdown complete` / `Finished server process`
- [ ] 用同一 `$env:FIT_AGENT_DATA_DIR` 重开 → 时区与首次完全相同（不重取样）；停服后目录只剩 `app.db`（WAL 已 checkpoint）
- 实际：`______`

### 2.4 种子清单人工只读核对

- [ ] 用 2.1 探针再跑一次；逐项名称可加 `SELECT standard_name_zh FROM exercises ORDER BY standard_name_zh;`
- [ ] 与 `S1-evidence-linux-2026-09-09.md` §5 的 24 项逐条一致
- 实际：`______`

## 3. 判定与回传

| # | 步骤 | 退出码 | 实际输出 | 判定 |
|---|---|---|---|---|
| 1 | 自动化 |  |  | □通过 □不通过 |
| 2.1 | 空库迁移/种子 |  |  | □通过 □不通过 |
| 2.2 | Stage 0 升级 |  |  | □通过 □不通过 |
| 2.3 | 启动/边界/重开 |  |  | □通过 □不通过 |
| 2.4 | 种子清单 |  |  | □通过 □不通过 |

- 结论：□ 通过（Stage 1 可结项）　□ 不通过（附失败项与日志）
- 回传：把本文件填好放回 `pre-prj/stage/evidence/`，或直接贴回会话。
- 本阶段不要求切换系统时区，故无需恢复步骤；Stage 0 已豁免的系统时区切换仍为豁免未验，不转写为通过。
