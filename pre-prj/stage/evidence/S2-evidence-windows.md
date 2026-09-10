# Stage 2 S2-08：Windows 全量自动化证据

> 状态：**已执行（2026-09-10，Windows 本机，通过）**。§1、§3 已按 Windows 实测填写；§0、§2、§4 保持交付时预填。
> 阶段门槛（`stage2.md` §1、§6）：Windows 全量自动化**完整收集、全部通过、退出码 0**；
> 不以 Linux/WSL2 结果替代。本文件不改 `PLAN.md`、设计正本、Stage 1 与 `stage2.md`。

执行摘要（2026-09-10，Windows 11 Pro build 26200 本机）：`git diff 4c76737 -- backend` 为空、
`git status --porcelain` 为空；`--collect-only -q` → **396 tests collected**；
全量 `pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → **396 passed in 18.04s**，
`$LASTEXITCODE = 0`。与 §0 WSL2 基线（396 passed）逐项一致，无本机差异、无 skip／xfail／deselected。

## 0. 执行基线（本次 WSL2 交付实测，Windows 侧须一致）

| 项 | 值 |
|---|---|
| 仓库／分支 | `Fit-Agent`，`main` |
| Stage 2 交付形态 | **已提交**。代码正本 = Stage 2 交付提交 `4c76737`（`backend: Stage 2 档案草稿确认与共用事务（S2-01~S2-07）`）；其后的提交若只改文档，不改变本阶段代码正本。Windows 侧 clone 后工作树应为**干净** |
| 迁移 | `001`–`004`（`004_stage2_business_drafts.sql`）；不得改 001–003 |
| 新增依赖 | 无（`backend/pyproject.toml` / `uv.lock` 未改） |
| WSL2 实测（同命令） | **396 passed**，退出码 0 |
| WSL2 收集数 | `396 tests collected` |
| Stage 2 新增用例 | 152（Stage 0/1 既有 244） |

前置条件（Windows 侧开工前核对）：

- [x] Python 3.13+；`uv sync --group dev --locked` 成功；`.venv` 指向 `backend/`
- [x] `git status --porcelain` 输出为空（clone 后无本地改动；有输出说明基线已被改写，须先弄清来源）
- [x] `git log -1 --format=%H` 记入 §3.1；交付提交 `4c76737` 在历史内，且 `git diff 4c76737 -- backend` 无输出（后续提交未改本阶段代码）
- [x] 不触碰真实数据目录（自动化全部使用临时文件库；本阶段**无人工实测任务**，不额外加探针）

```powershell
cd <repo>
git log -1 --format="%H %s"
git status --porcelain
git diff 4c76737 -- backend
git log --oneline -6
```

交付提交应包含的文件（`git show --stat 4c76737` 应逐条对应）：

```text
backend/README.md
backend/api/app.py
backend/api/dto.py                          （新增）
backend/api/routes_drafts.py
backend/api/routes_readonly.py
backend/app/confirm.py
backend/app/draft_repo.py                   （新增）
backend/app/drafts.py
backend/domain/actions/repo.py
backend/domain/actions/schema.py
backend/domain/profile/repo.py
backend/domain/profile/rules.py
backend/domain/profile/schema.py
backend/storage/db.py
backend/storage/migrations/004_stage2_business_drafts.sql（新增，迁移 004）
backend/tests/test_migrations.py
backend/tests/test_provider_settings.py
backend/tests/test_stage1_actions_seed.py
backend/tests/test_stage1_profile_write.py
backend/tests/test_stage1_schema.py
backend/tests/test_stage2_business_api.py           （新增）
backend/tests/test_stage2_draft_create_diff.py      （新增）
backend/tests/test_stage2_draft_revise_discard.py   （新增）
backend/tests/test_stage2_draft_stale.py            （新增）
backend/tests/test_stage2_draft_storage.py          （新增）
backend/tests/test_stage2_profile_confirm.py        （新增）
backend/tests/test_stage2_profile_first_time_complete.py（新增）
pre-prj/stage/stage2.md                     （新增，阶段计划正本）
pre-prj/stage/evidence/S2-01-contract-mapping.md
pre-prj/stage/evidence/S2-07-api-contract-handoff.md
pre-prj/stage/evidence/S2-evidence-windows.md（本文件）
```

## 1. 全量自动化命令与预期（阻断项）

```powershell
cd <repo>\backend
uv sync --group dev --locked
.\.venv\Scripts\python.exe -m pytest tests --collect-only -q
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

- [x] 收集数 = **396**（先跑 `--collect-only`，防「只跑新增用例」即称全量通过）
- [x] 完整输出以 `396 passed` 结尾，`$LASTEXITCODE = 0`
- [x] 无 `skipped`／`xfailed`／`xfail`／`deselected`；`-W error::pytest.PytestUnhandledThreadExceptionWarning` 门未触发
- [x] 失败处理口径：记录**用例名 + 完整 traceback**；修复后重跑**全量**；不得删测试、放宽断言或新增 skip／xfail 以达到门槛（本轮无失败，无需修复）

## 2. 覆盖矩阵 → 测试文件（本次实际落点）

组名对应 `stage2.md` §6；用例数取自 WSL2 `--collect-only`（同一用例可覆盖多组，故各组计数不互斥）。

| §6 组 | 覆盖文件（新增用 ▶ 标出） | 用例数 |
|---|---|---|
| 升级兼容 | ▶ `test_stage2_draft_storage.py`（前 6 例：空库、Stage 1 升级、Stage 0 升级、重复启动、失败回滚、高版本拒绝）+ `test_migrations.py`、`test_stage1_schema.py` | 24 + 11 + 12 |
| 草稿隔离 | ▶ `test_stage2_draft_create_diff.py`、▶ `test_stage2_draft_revise_discard.py`、▶ `test_stage2_draft_storage.py` | 11 + 24 + 24 |
| 领域边界 | ▶ `test_stage2_profile_first_time_complete.py`（首次建档完整性）+ `test_stage1_profile_facts.py`、`test_stage1_profile_patch.py`、`test_stage1_profile_restrictions.py`、`test_stage1_profile_safety.py`、`test_stage1_profile_write.py` | 39 + 43 + 16 + 12 + 54 + 9 |
| 修订与状态 | ▶ `test_stage2_draft_revise_discard.py` | 24 |
| 正式确认 | ▶ `test_stage2_profile_confirm.py` | 18 |
| 幂等与恢复 | ▶ `test_stage2_profile_confirm.py`（重复确认／响应丢失重试／关闭重开／并发）+ ▶ `test_stage2_draft_storage.py`（凭据持久化、关闭重开） | 18 + 24 |
| 过期 | ▶ `test_stage2_draft_stale.py` | 9 |
| 并发与取消 | ▶ `test_stage2_draft_revise_discard.py`（纠错×确认、丢弃×确认、并发纠错／丢弃）+ ▶ `test_stage2_profile_confirm.py`（并发确认、提交前取消）+ ▶ `test_stage2_draft_storage.py`（插入后取消） | 24 + 18 + 24 |
| HTTP | ▶ `test_stage2_business_api.py`（全部端点、错误码、Host/Origin 边界、无堆栈／凭据泄漏）+ `test_app.py` | 27 + 5 |
| 旁路扫描 | ▶ `test_stage2_business_api.py::test_no_public_creation_recalc_write_or_fake_run_routes` + ▶ `test_stage2_draft_storage.py`（只多 `business_drafts` 一张表）+ `test_provider_settings.py`（凭据泄漏扫描扩展）、`test_stage1_actions_seed.py` | 27 + 24 + 12 + 10 |

## 3. Windows 执行记录（执行者填写）

### 3.1 环境与版本指纹

```powershell
[System.Environment]::OSVersion.VersionString
(Get-CimInstance Win32_OperatingSystem).BuildNumber
$PSVersionTable.PSVersion.ToString()
.\.venv\Scripts\python.exe -V
uv --version
git rev-parse HEAD
```

| 项 | 实测值 |
|---|---|
| 执行日期 | 2026-09-10 |
| Windows 版本 / build | Windows NT 10.0.26200.0（Windows 11 Pro）/ build 26200 |
| PowerShell / Python / uv / pytest | PowerShell 5.1.26100.8328；Python 3.13.13（`backend/.venv`，`uv sync --group dev --locked`）；uv 0.11.28；pytest 9.1.1 |
| `git log -1 --format=%H` | `e46c2c2add7ded131251d7cc34d05b7dd73e6b13`（`docs: S2-08 Windows 清单钉住交付提交 4c76737…`） |
| `git diff 4c76737 -- backend` 为空？ | 是（无输出；后续 3 个提交均为文档／前端，未改本阶段后端代码） |
| `git status --porcelain` 为空？ | 是（clone 后无本地改动） |
| 收集数 | 396 tests collected in 3.28s |
| 全量输出末行 | `396 passed in 18.04s` |
| `$LASTEXITCODE` | 0 |

逐文件收集数（与 §2 覆盖矩阵一致；Stage 2 新增 7 个文件合计 152 例）：

```text
39 tests/test_stage2_profile_first_time_complete.py   27 tests/test_stage2_business_api.py
24 tests/test_stage2_draft_storage.py                  24 tests/test_stage2_draft_revise_discard.py
18 tests/test_stage2_profile_confirm.py                11 tests/test_stage2_draft_create_diff.py
 9 tests/test_stage2_draft_stale.py
54 tests/test_stage1_profile_safety.py                 43 tests/test_stage1_profile_facts.py
17 tests/test_run_repo.py                              16 tests/test_stage1_profile_patch.py
12 tests/test_stage1_schema.py                         12 tests/test_stage1_profile_restrictions.py
12 tests/test_provider_settings.py                     11 tests/test_migrations.py
11 tests/test_db.py                                    10 tests/test_stage1_actions_seed.py
10 tests/test_stage1_actions_catalog.py                 9 tests/test_stage1_profile_write.py
 9 tests/test_stage1_actions_rules.py                   7 tests/test_timezone.py
 6 tests/test_config.py                                 5 tests/test_app.py
```

### 3.2 判定

| # | 步骤 | 退出码 | 实际输出 | 判定 |
|---|---|---|---|---|
| 1 | `--collect-only -q` | 0 | `396 tests collected in 3.28s` | ☑通过 ☐不通过 |
| 2 | 全量 `pytest tests -q -W error::...` | 0 | 6 行进度点（18/36/54/72/90/100%），末行 `396 passed in 18.04s` | ☑通过 ☐不通过 |

- 失败记录（用例名 / traceback 摘要 / 修复提交或改动 / 复跑结果）：无失败，无修复，无需复跑。
- 结论：☑ 通过（Stage 2 可结项） ☐ 不通过（记录阻断项，不以其他平台结果替代）
- 未覆盖范围：见 §5（Windows 侧如实转写，不改写成通过）

原始命令与实际输出（Windows PowerShell，`backend/` 目录）：

```powershell
PS> uv sync --group dev --locked
Resolved 34 packages in 32ms
Checked 32 packages in 14ms

PS> .\.venv\Scripts\python.exe -m pytest tests --collect-only -q
... (396 行 usecase id)
396 tests collected in 3.28s

PS> .\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
........................................................................ [ 18%]
........................................................................ [ 36%]
........................................................................ [ 54%]
........................................................................ [ 72%]
........................................................................ [ 90%]
....................................                                     [100%]
396 passed in 18.04s

PS> $LASTEXITCODE
0
```

## 4. Stage 3/4 接入说明（交接）

### 4.1 唯一正式写入入口

- 正式档案写入：`ProfileService.write_profile_in_transaction(conn, profile)`（仅外层事务内可用，不提交、不推进版本）。
- 业务版本推进：`ProfileRepo.bump_context_version_in_transaction(conn)`（载体上**唯一**一条递增语句）。
- 上述两者的**唯一调用者**都是 `app/confirm.py:ConfirmService._commit_pending`（经
  `confirm_profile_draft(draft_id, seen_revision)` 编排）；`api/`、`runtime/`、`agent_core/` 无任何正式写入路径。
- 草稿创建只在内部应用层：`DraftService.create_profile_draft(...)`（绑定 `prepare_generation_baseline()`
  读到的同一快照）；HTTP 面无创建草稿、`/recalc`、档案直写、假 Run 路由（`test_no_public_creation_recalc_write_or_fake_run_routes` 逐条断言 404/405）。
- 本阶段 HTTP 面只有 `S2-07` §0 的 5 个业务端点 + `/healthz`。

### 4.2 事务内调用约束（Stage 3 组合提交直接受影响）

- `Database.transaction()` 从 BEGIN 到 COMMIT/ROLLBACK 全程持唯一连接与唯一锁；**锁不可重入**。
- 事务内禁止调用自取锁的普通 Service 查询方法（如 `ProfileService.read_formal_profile` /
  `validate_patch` 等）；必须走 `*_in_transaction` / repo 的事务内路径，未在外层事务内即由
  `storage.db.require_outer_transaction` 显式拒绝（不死锁、不静默开新连接）。
- 事务内只做本地确定性计算与数据库操作；不调模型、工具执行、SSE，不增加第二把业务库锁。
- 版本推进、档案写入、草稿状态与提交凭据四者在同一事务内同成败；提交完成（COMMIT 后）才响应。

### 4.3 业务版本负责人

- `context_version` 只由确认编排推进，一次成功确认恰好 +1；草稿创建／纠错／丢弃与普通读取不推进。
- 领域 repo 只提供语句，不自行决定是否推进；Stage 3 的组合提交（档案＋计划）必须复用同一条路径，
  不新增第二套业务版本计数器（`PLAN.md` 架构不变量）。

### 4.4 DTO 差异（前端待改清单）

完整映射、真实样例与错误码表见 `S2-07-api-contract-handoff.md` §2–§4；要点：

- 档案事实改为三态 `{state, value}`（`unknown` / `denied` / `known`），不得压成空数组或默认值；
  限制按稳定身份 `{scope, target}` 传输，展示名由前端自行解析。
- `POST /api/drafts/{id}/revise` 请求体为 `{revision, payload}`（**新增所见 revision**），
  `reviseDraft(draftId, revision, payload)`；`payload.profile` 必须**九个字段全量**出现。
- `POST /api/drafts/{id}/confirm` 响应无 `newly_committed` / `summary`；版本字段为 `committed_business_version`
  （重复确认返回同一份凭据，无法区分「本次新提交」）。`status` 不出现 `stale`（过期以 409 `draft_stale` 表达）。
- `/api/drafts/{id}/recalc`、`recalcDraft` 本阶段不存在（404），不得接假成功。
- 纠错／确认按钮互斥规则（S2-04 已拍 3）与安全询问通俗文案见 `S2-07` §4。

### 4.5 未完成的第 01 章验收项（`stage2.md` §7）

| 第 01 章验收项 | Stage 2 已做 | 归属后续 |
|---|---|---|
| 5 档案＋计划组合提交 | 只交接事务约束，未用替身验收 | Stage 3（真实计划历史、日程联动、原子提交与一次 +1） |
| 6 过期提示与一键重算 | 只交付 `409 draft_stale` 与可核实字段变化；旧草稿身份／基线／内容可查 | Stage 4（最新上下文生成、`parent_draft_id` 关联、新旧 Diff、再次确认） |
| 7 安排接受立即落盘 | 未实现安排业务 | Stage 3 |
| 1 对话／Run 行为 | 验证草稿隔离与无正式写入旁路 | Stage 4（对话生成草稿即确认语义） |
| 2–4 revision／幂等／重开 | 用真实档案草稿完整验收 | Stage 3 扩展计划／记录字段与跨业务版本影响 |

## 5. 未覆盖范围与残留风险（执行者如实转写）

- **无真实对话生成草稿入口**：草稿由测试内部应用层准备；**不得**据此声称聊天闭环已打通。
- 前端未切换到真实后端（另行执行）；`frontend/**` 改动不在本阶段验收范围。
- `/recalc`、计划／日程／记录／统计、Agent 运行时均未接入（Stage 3/4）。
- Windows 侧本机差异：**无**。收集数 396 与 WSL2 基线一致，全量 396 通过、退出码 0，无 Windows 特有失败、
  无 skip／xfail／deselected、无警告门触发。（若后续复跑出现本机差异，逐条记录用例名与 traceback，
  不以「Linux 通过」代替判定。）
- 其余残留风险与 flake 观察见 `S1-evidence-linux-2026-09-09.md` §8（Stage 1 遗留，未在本阶段复验）。

## 6. 声明

本阶段结项只表示「档案草稿确认与共用事务达到本阶段门槛」，不是完整产品、完整第 01 章或真实
对话建档已完成；离线自动化通过 ≠ 产品端到端验收。本文件结果由 Windows 执行者补齐后才是阶段证据。
