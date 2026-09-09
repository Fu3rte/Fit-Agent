# Stage 1 Windows 验收清单（方案 A · 同版本）

> 用途：Stage 1 结项门槛 = 同版本 Windows 全量自动化 + 隔离库人工实测。
> **本清单当前未执行**；未逐项填写并判定前，不得视为通过，Stage 1 不得结项。
> 详解与背景见 `S1-07-handover-linux-2026-09-09.md` §6；本文件只做「照抄 + 记录位」。

## 0. 前置

- [ ] 同一代码版本：`git rev-parse HEAD` = `dd168bba79cbcec7e93a23ccbdec4e1945e08e5e`（或记录实际 HEAD 并逐项说明差异）
- [ ] 工作区差异与 handover §1 清单一致（或逐项说明）
- [ ] Python 3.13+；依赖已装：`uv sync --group dev --locked`
- [ ] 记录环境：Windows `<build>`，PowerShell `<版本>`，Python `<版本>`，uv `<版本>`

## 1. 自动化测试（阻断项）

```powershell
cd <repo>\backend
uv sync --group dev --locked
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

- 预期：`243 passed`，`$LASTEXITCODE = 0`
- 若出现 `PytestUnhandledThreadExceptionWarning` 计为失败（非警告），需回传用例名与日志原文
- 实际：`______________________________`

## 2. 隔离库人工清单（不碰 `%LOCALAPPDATA%\Fit-Agent`）

```powershell
$env:FIT_AGENT_DATA_DIR = Join-Path $env:TEMP ("fitagent-s1-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $env:FIT_AGENT_DATA_DIR | Out-Null
```

### 2.1 空库迁移 + 种子/档案基线

把 handover §6.2.1 的探针脚本存为 `$env:TEMP\s107_probe.py` 后运行：

```powershell
cd <repo>\backend
.\.venv\Scripts\python.exe (Join-Path $env:TEMP "s107_probe.py"); $LASTEXITCODE
```

- [ ] 预期：`migrate=3`、`user_version=3`、`exercises=24`、`recommendable=1` 计数 `0`、`active=0` 计数 `0`、哑铃分腿蹲 `0`、`user_profile=(1, None, 0)`
- 实际：`______________________________`

### 2.2 Stage 0 库升级（只含 001 的旧库 → 生产迁移目录）

把 handover §6.2.2 的探针脚本存为 `$env:TEMP\s107_upgrade.py` 后运行：

```powershell
cd <repo>\backend
.\.venv\Scripts\python.exe (Join-Path $env:TEMP "s107_upgrade.py"); $LASTEXITCODE
```

- [ ] 预期：`before_version=1` → `after_migrate=3`；会话数 `1`、`business_timezone=Asia/Shanghai` 保留；种子 `24`；档案 `(1, None, 0)`
- 实际：`______________________________`

### 2.3 启动 / 停服 / 重开（回环与 Host/Origin 边界）

```powershell
cd <repo>\backend
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000   # 前台窗口，保持运行
```

另开终端：

```powershell
curl.exe -s http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "host-bad=%{http_code}\n"   -H "Host: 198.51.100.7:8000"      http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "origin-bad=%{http_code}\n" -H "Origin: http://evil.example"  http://127.0.0.1:8000/healthz
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 | Select-Object LocalAddress,LocalPort,OwningProcess
```

- [ ] `/healthz`：`status=ok`、`database=open`、`business_timezone` 为 IANA 地区名、`provider_has_api_key=false`
- [ ] `host-bad=403`、`origin-bad=403`
- [ ] 监听只在 `127.0.0.1`
- [ ] 服务窗口 `Ctrl+C`（**勿用 `Stop-Process -Force`**）→ 看到 `Application shutdown complete` / `Finished server process`
- [ ] 端口释放后，用**同一** `$env:FIT_AGENT_DATA_DIR` 重开 → `/healthz` 的 `business_timezone` 与首次完全相同（不重取样）
- [ ] 停服后目录只剩 `app.db`（WAL 已 checkpoint）
- 实际：`______________________________`

### 2.4 种子清单人工只读核对

服务停止后，用 2.1 的探针再跑一次（同一目录）核对 24 项、`recommendable` 全 0、无哑铃分腿蹲；逐项名称可追加：

```powershell
SELECT standard_name_zh FROM exercises ORDER BY standard_name_zh;
```

- [ ] 与 `S1-03-actions-linux-2026-09-09.md` §3 的 24 项逐条对照一致
- 实际：`______________________________`

## 3. 记录表（逐条填）

| # | 步骤 | 命令/动作 | 退出码 | 实际输出 | 预期 vs 实际 | 判定 |
|---|---|---|---|---|---|---|
| 1 | 自动化 | `pytest tests -q -W error::...` |  |  | 243 passed | □通过 □不通过 |
| 2.1 | 空库迁移/种子 | `s107_probe.py` |  |  | migrate=3/24/全0 | □通过 □不通过 |
| 2.2 | Stage 0 升级 | `s107_upgrade.py` |  |  | before=1→after=3，数据保留 | □通过 □不通过 |
| 2.3 | 启动/边界/重开 | main.py + curl + Ctrl+C |  |  | 403×2，仅 127.0.0.1，时区不重取样 | □通过 □不通过 |
| 2.4 | 种子清单 | 只读查询 |  |  | 24 项一致 | □通过 □不通过 |

## 4. 判定与回传

- [ ] 第 1 节自动化通过
- [ ] 第 2 节人工清单全部通过
- 结论：□ 通过（Stage 1 可结项）　□ 不通过（附失败项与日志）
- 回传：把本文件填好放回 `pre-prj/stage/evidence/`，或直接贴回会话。
- 备注：本阶段**不要求**切换系统时区，故无需恢复步骤；Stage 0 已豁免的系统时区切换仍为豁免未验，不转写为通过。
