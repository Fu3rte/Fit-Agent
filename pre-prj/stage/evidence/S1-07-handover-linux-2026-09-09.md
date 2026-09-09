# Stage 1 交接（S1-07）：领域/存储回归汇总、Stage 2–4 接入点与同版本 Windows 验证步骤

> 子任务：Stage 1 S1-07（阶段验证与交接）。验收正本：`pre-prj/stage/stage1.md` §5 S1-07、§6、§8、§9。
> 本文件只做汇总与交接，**不改任何业务代码**，不改变 PLAN.md / design-decisions.md / architecture 正本约束。
> 本轮全部命令在 **Linux（WSL2）** 实跑；**Windows 未执行任何命令**，第 6 节为交付给用户执行的方案 A 步骤，
> 在同版本 Windows 自动化 + 隔离库人工实测回传前，Stage 1 正式结项保持**待验**。

## 0. 结论摘要（据实）

- 全量自动化实跑：`cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning`
  → **`243 passed in 4.01s`，退出码 `0`**（Stage 0 69 + S1-02 12 + S1-03 29 + S1-04 79 + S1-05 54）。
- 迁移链 001–003 实跑：空库 `user_version=3`；Stage 0 库升级保留会话/时区；重开不重置；`user_version=99` 拒绝启动。
- 产品种子实跑：24 行、`recommendable` 全 0、`active` 全 1、无「哑铃分腿蹲」；`user_profile=(1, None, 0)`，`context_version` 未被目录查询/档案写入推进。
- 工作区：HEAD `dd168bb` + 未提交 Stage 1 差异；`git diff --cached --name-only` 为空；未 `git add`/commit/stash。
- **未完成**：同版本 Windows 自动化与隔离库人工实测（第 6 节，本轮未执行）；Stage 2–4 接线（第 4 节只给责任，未实现）。

### 0.1 本轮 provenance

| 产物 | 来源 |
|---|---|
| 第 1 节版本与差异 | `git rev-parse HEAD`、`git status --porcelain -- backend pre-prj/stage`、`git diff --cached --name-only` 实跑 |
| 第 2 节测试基线 | 全量实跑 + `--collect-only` 逐模块计数 |
| 第 3 节迁移/存储回归 | 全量实跑（覆盖迁移用例）+ 临时库独立探针 `/tmp/s107_probe.py`（绕开 pytest 直调 `Database`/`migrate`，不触碰真实用户库） |
| 第 4–5 节 | 核对 `domain/actions/service.py`、`domain/profile/{service,repo,safety}.py` 实际签名与既有证据 S1-03 §3、S1-05 §6 |
| 第 6 节 | 参照 `pre-prj/stage/stage0-handover.md` §7 结构改写为 Stage 1 版本，**未执行** |

## 1. 代码版本与工作区差异（实测）

| 项 | 值 | 命令 |
|---|---|---|
| HEAD | `dd168bba79cbcec7e93a23ccbdec4e1945e08e5e`（`dd168bb`） | `git rev-parse HEAD` |
| 暂存区 | 空 | `git diff --cached --name-only`（输出为空） |
| 平台 | Linux WSL2（`6.6.114.1-microsoft-standard-WSL2`，x86_64） | `uname -a` |
| 解释器 | Python 3.13.15 | `.venv/bin/python -V` |
| pytest | 9.1.1 | `.venv/bin/python -m pytest --version` |
| uv | 0.12.3 | `uv --version` |
| 新增依赖 | 无（`pyproject.toml`/`uv.lock` 未改） | `git status --porcelain` 无命中 |

`git status --porcelain -- backend pre-prj/stage` 全量清单（31 条 = 13 `M` + 18 `??`；本文件自身未跟踪，加入后为 32 条）：

```
 M backend/domain/actions/repo.py
 M backend/domain/actions/rules.py
 M backend/domain/actions/schema.py
 M backend/domain/actions/service.py
 M backend/domain/profile/repo.py
 M backend/domain/profile/rules.py
 M backend/domain/profile/schema.py
 M backend/domain/profile/service.py
 M backend/tests/test_db.py
 M backend/tests/test_migrations.py
 M backend/tests/test_provider_settings.py
 M pre-prj/stage/evidence/S0-08-windows-2026-09-09.md
 M pre-prj/stage/stage0.md
?? backend/domain/profile/safety.py
?? backend/storage/migrations/002_stage1_actions_profile.sql
?? backend/storage/migrations/003_stage1_action_seed.sql
?? backend/tests/test_stage1_actions_catalog.py
?? backend/tests/test_stage1_actions_rules.py
?? backend/tests/test_stage1_actions_seed.py
?? backend/tests/test_stage1_profile_facts.py
?? backend/tests/test_stage1_profile_patch.py
?? backend/tests/test_stage1_profile_restrictions.py
?? backend/tests/test_stage1_profile_safety.py
?? backend/tests/test_stage1_profile_write.py
?? backend/tests/test_stage1_schema.py
?? pre-prj/stage/evidence/S1-01-baseline-linux-2026-09-09.md
?? pre-prj/stage/evidence/S1-02-storage-linux-2026-09-09.md
?? pre-prj/stage/evidence/S1-03-actions-linux-2026-09-09.md
?? pre-prj/stage/evidence/S1-04-profile-linux-2026-09-09.md
?? pre-prj/stage/evidence/S1-05-safety-linux-2026-09-09.md
?? pre-prj/stage/stage1.md
```

- 其中 `backend/**` 23 条为 Stage 1 轨道差异（迁移 002/003、`domain/actions`、`domain/profile` 含 `safety.py`、9 个 Stage 1 测试模块 + 3 个 Stage 0 测试文件按方案 A 维护）。
- `pre-prj/stage/evidence/S0-08-windows-2026-09-09.md`、`pre-prj/stage/stage0.md` 两条 `M` 与本阶段无关（Stage 0/文档轨既有改动），本轮未触碰。
- `pre-prj/stage/stage1.md` 本身为未跟踪文件（正本由人维护），本文件不改它。
- 迁移目录实况：`001_stage0_runtime_and_settings.sql`、`002_stage1_actions_profile.sql`、`003_stage1_action_seed.sql`（无 004+）。

## 2. 测试基线（实跑）

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | `0` | `243 passed in 4.01s` |
| 2 | `timeout 120s .venv/bin/python -m pytest tests -q --collect-only` | `0` | `243 tests collected in 1.04s` |
| 3 | 逐模块 `--collect-only` 计数 | `0` | 见下表 |
| 4 | `for i in 1..10: pytest tests/test_app.py -q -W error::...` | 每次 `0` | 10/10 `5 passed`（复现历史 flake 检查，本轮全绿） |

用例分布（逐模块实测，合计与总数一致）：

| 归属 | 模块 | 用例数 | 小计 |
|---|---|---|---|
| Stage 0 | `test_app.py` 5、`test_config.py` 6、`test_db.py` 11、`test_migrations.py` 11、`test_provider_settings.py` 12、`test_run_repo.py` 17、`test_timezone.py` 7 | — | **69** |
| S1-02 | `test_stage1_schema.py` | 12 | **12** |
| S1-03 | `test_stage1_actions_catalog.py` 10、`test_stage1_actions_rules.py` 9、`test_stage1_actions_seed.py` 10 | — | **29** |
| S1-04 | `test_stage1_profile_facts.py` 43、`test_stage1_profile_patch.py` 16、`test_stage1_profile_restrictions.py` 12、`test_stage1_profile_write.py` 8 | — | **79** |
| S1-05 | `test_stage1_profile_safety.py` | 54（含 P1 修复新增 4） | **54** |
| **合计** | | | **243** |

`-W error::pytest.PytestUnhandledThreadExceptionWarning` 门：把 aiosqlite 工作线程在收尾阶段抛出的
`PytestUnhandledThreadExceptionWarning`（典型为 `RuntimeError: Event loop is closed`）从警告升级为硬失败，
防止「用例通过但连接/线程未收尾」。本轮加门全量 `243 passed`、退出码 `0`；Windows 命令必须带同一门。

## 3. 迁移与存储回归

### 3.1 自动化覆盖（随全量套件实跑通过）

| 回归项 | 覆盖用例 |
|---|---|
| 001–003 链 / 空库迁移 | `test_stage1_schema.py::test_fresh_database_migrates_to_latest_with_seed_and_no_profile_facts`、`test_migrations.py::test_fresh_initialize_creates_runtime_tables` |
| Stage 0 库升级 | `test_stage1_schema.py::test_stage0_upgrade_preserves_runtime_rows_timezone_and_credentials`、`test_migrations.py::test_upgrade_preserves_existing_data` |
| 重复启动不重置 | `test_stage1_schema.py::test_repeated_start_does_not_reset_profile_version_or_catalog_rows`、`test_migrations.py::test_repeated_start_does_not_rerun_migrations`、`test_stage1_actions_seed.py::test_seed_import_keeps_context_version_untouched` |
| 失败迁移原子回滚 | `test_stage1_schema.py::test_failed_stage1_migration_leaves_no_partial_structure`、`test_migrations.py::test_failed_migration_leaves_no_partial_structure`、`test_stage1_actions_seed.py::test_reimporting_seed_via_later_migration_is_rejected_atomically` |
| 高版本拒绝 | `test_stage1_schema.py::test_higher_schema_version_is_refused_with_stage1_migrations`、`test_migrations.py::test_future_schema_version_refuses_to_start` |
| 种子 24 行 | `test_stage1_actions_seed.py::test_seed_rows_match_checked_product_table`、`test_seed_covers_decided_catalog_except_unmatched_item` |
| `context_version` 不被推进 | `test_stage1_schema.py::test_catalog_activity_never_advances_context_version`、`test_stage1_profile_write.py::test_write_never_changes_existing_context_version` / `test_profile_module_never_writes_context_version`、`test_stage1_actions_seed.py::test_seed_import_keeps_context_version_untouched` |

### 3.2 临时库独立探针（绕开 pytest，直调存储层；不触碰真实用户库）

命令：`cd backend && PYTHONPATH=. timeout 120s .venv/bin/python /tmp/s107_probe.py`，退出码 `0`，关键输出：

```
latest_migrations= 3
fresh: migrate_returns= 3 user_version= 3
fresh: tables= ['app_config','conversations','exercises','messages','provider_config','run_events','runs','user_profile']
fresh: seed_count= 24
fresh: profile= [(1, None, 0)]
fresh: recommendable_true= 0
fresh: active_false= 0
fresh: dumbbell_split_squat= 0
fresh: context_version_after_catalog_read= 0
legacy: user_version_before= 1
legacy: migrate_returns= 3
legacy: conversation_kept= 1
legacy: tz= Asia/Shanghai
legacy: seed_count= 24
legacy: profile= [(1, None, 0)]
reopen: seed_count= 24
reopen: profile= [(1, None, 0)]
reopen: user_version= 3
future: refused -> FutureSchemaVersion 数据库 user_version=99 高于程序支持的最新迁移 3：停止启动，不降级、不重建用户数据库（07 7.2）
future: user_version_after= 99
future: db_exists= True
```

逐项结论：

1. **001–003 链**：`load_migrations()` 返回 3 条（编号连续）；空库 `migrate_returns=3`、`user_version=3`。
2. **空库迁移**：表集合 = Stage 0 六表 + `exercises` + `user_profile`（无 drafts/plans/records/stats）；种子 24 行；`user_profile=(1, None, 0)`。
3. **Stage 0 库升级**：先用只含 `001` 的迁移目录建 `user_version=1` 库并写入会话 + 业务时区；换生产迁移目录重开 → 只补跑 002/003，会话保留、`business_timezone=Asia/Shanghai` 保留、种子 24、档案仍 `(1, None, 0)`。
4. **重复启动不重置**：同一库再重开 → 种子仍 24、档案仍 `(1, None, 0)`、`user_version=3`（未重复插入、未重置版本）。
5. **失败迁移原子回滚**：探针未重跑该分支，由 `test_failed_stage1_migration_leaves_no_partial_structure` 覆盖并随全量通过——002 失败时 `user_version` 停在 1、不留半套 `exercises`、Stage 0 表完好，修复迁移文件后可继续、不删库。
6. **高版本拒绝**：`user_version=99` → `FutureSchemaVersion`，版本仍 99、库文件仍在（不降级、不重建）。
7. **产品种子**：24 行；`recommendable` 全 0（`recommendable_true=0`）；`active` 全 1（`active_false=0`）；无「哑铃分腿蹲」。
8. **`context_version` 不被推进**：目录查询后仍 `0`；档案写入/种子导入/预览路径均无该列写入（自动化用例守住）。

## 4. Stage 2–4 接入点清单（逐项「必须由谁调用」）

### 4.1 必须由 Stage 2 确认事务调用（Stage 1 不写、不推进）

| 入口（实际签名） | 必须由谁调用 | 约束 |
|---|---|---|
| `ProfileService.write_profile_in_transaction(conn, profile)` → `ProfileRepo.write_in_transaction(conn, profile)` | **Stage 2 确认事务**（在 `Database.transaction()` 内） | 只接受外层事务连接；不在事务内即 `RuntimeError`。只发一条 `UPDATE user_profile SET profile_json=? WHERE id=1`，**不自行 BEGIN/COMMIT、不改 `context_version`** |
| `context_version` 推进 | **Stage 2 确认事务** | Stage 1 只有只读载体，**不存在推进入口**（生产代码仅 `domain/profile/repo.py` 读该列）。必须由确认事务在提交时完成，**一次业务提交仅 +1**；不得新增独立计数器，不得在目录查询/档案读取/预览/写入函数内推进 |
| 受限组合原子提交（正式限制增删 + 档案/计划变更） | **Stage 2 确认事务**（同一事务内） | `preview_patch`/`apply_patch` 只是纯内存条件计算；若只提交计划而不提交限制删除，就会违反正式限制。原子生效责任在确认事务，S1-04/S1-05 不代替 |
| 正式档案事实 / 限制增删 / 红旗解除 | **Stage 2 确认事务** | Stage 1 不写、不推进、不自动解除；`profile_json` 是唯一正式事实载体 |

### 4.2 只是「候选」或「局部安全检查」的返回（不得当作完整许可/产品保证）

| 返回 | 性质 | 接线要求 |
|---|---|---|
| `ActionCatalogService.resolve(term) -> AliasResolution` | **别名候选**（`candidates` 元组；`is_ambiguous = len(candidates)>1`） | 命中多个身份时必须由调用方**询问用户**，不静默取第一项；Stage 1 不实现交互 |
| `ActionCatalogService.recommendation_candidates() -> tuple[Exercise, ...]` | **推荐候选集合**（仅未停用且 `recommendable=1`；当前为空集） | 不等于「Agent 会推荐」；可推荐检查标准与执行人未拍板，Agent 主动推荐当前对产品种子为空集 |
| `evaluate_safety(profile, actions, *, patch=None, session=None) -> SafetyCheckResult` | **纯函数局部安全检查**（无 IO） | 结果无 `is_safe` 之类完整安全许可字段；`needs_clarification` 非空即不得放行 |
| `ProfileService.check_candidate_actions_safety(ids, *, patch=None, session=None) -> SafetyCheckResult` | **只读局部安全检查** | 候选身份须存在于目录（含停用动作），未知身份抛 `UnknownExerciseReference`；须由 Stage 2 确认事务 / Stage 3 生成与复核调用 |
| `preview_patch(...).action_restrictions` | **拟议条件**（纯内存） | 直接读它的调用方必须另判正式三态：`apply_patch` 在补丁触及限制时会把该字段写成 `known`（S1-05 P2 交接） |
| `RED_FLAG_BLOCK_ADVICE` / `clarification_reasons` | **提示层素材** | 红旗固定文案「存在已明确红旗症状：不生成常规训练处方，建议线下专业评估。」；`needs_clarification` 非空不得当安全放行 |

### 4.3 仍需 Agent / 计划 / 前端接线的规则（Stage 1 只给确定性基础）

| 规则 | 必须由谁接线 | Stage 1 现状 |
|---|---|---|
| 动作歧义询问 | Stage 3 打卡交互 / Stage 4 Agent 工具 / 前端 | 只提供候选集合 + `is_ambiguous` 标记，不询问、不确认、不写入 |
| 整份计划阻断 | Stage 3 生成/修订计划、整份当前计划使用时复核；Stage 4 Agent 工具 | 只给单候选动作的确定性限制/红旗校验；不生成计划、不阻断整份计划 |
| 打卡匹配 | Stage 3 | 只提供候选集合与歧义标记，不实现打卡匹配交互 |
| 自然语言症状识别 | 上游分流（建档/确认事务/当次条件） | 红旗必须落在 `red_flags` 事实三来源；`body_state` 文本不参与阻断；清单外原文只归「未知/需澄清」 |
| 正式档案写入 / 限制增删 / 版本推进 | Stage 2 确认事务 | 无 HTTP/Agent/CLI 写入入口；`ProfileService` 未被 `api/`、`app/`、`runtime/` 引用 |

### 4.4 业务版本推进责任

`context_version` 由 **Stage 2 确认事务**推进，**一次业务提交仅 +1**；Stage 1 只建载体与同一快照读取，
不提供推进入口，也不新增任何独立计数器（01 1.4）。

## 5. 产品种子检查状态

| 检查项 | 实测结果 | 证据 |
|---|---|---|
| 清单规模 | **24 项全部导入**（每项逐条来源核对，非名称相似） | S1-03 证据 §3 逐项来源核对表（#1–#4、#6–#25；#5 为已移出项） |
| 来源可追溯 | 24/24 有来源：`source_ref=exercises-dataset:<id>`、`attribution="© Gym visual — https://gymvisual.com/"`、`instructions_zh` 用数据集中文说明 | S1-03 证据 §3 末注 |
| 「哑铃分腿蹲」 | **已按 2026-09-09 拍板（方案 C）移出清单**；探针 `dumbbell_split_squat=0`；`resolve` 返回空、`modes_for` 抛 `UnknownCatalogExercise` | S1-03 证据 §3 #5、§9 |
| 可推荐标记 | **全 0**（探针 `recommendable_true=0`）；检查标准未拍，未检查不得推荐 | S1-03 证据 §9 |
| 停用状态 | 全 1（探针 `active_false=0`）；停用只改标记不删行 | S1-03 证据 §9、`test_stop_keeps_row_and_defaults_stay_not_recommendable` |
| 未覆盖 | 媒体（图片/GIF）未导入、未下载、未建字段；中文口语别名未入库（待人工确认）；`leverage machine` 三项按插销口径记录 | S1-03 证据 §4、§5、§9 |

## 6. 同版本 Windows 验证步骤（方案 A）——**本轮未执行**

> **状态声明：本节所有命令与人工步骤在 Linux 侧未以 Windows 形态执行，Windows 侧亦未执行。**
> 不得把本节当作通过证据；回传时必须逐条填「命令 / 退出码 / 实际输出 / 预期 vs 实际 / 判定」。
> 前提：同一代码版本（记录 `git rev-parse HEAD` = `dd168bb` 与 `git status --porcelain -- backend pre-prj/stage` 清单，
> 须与第 1 节一致或逐项说明差异）、Python 3.13+、`uv sync --group dev --locked` 已完成。

### 6.1 自动化测试（同版本）

```powershell
cd <repo>\backend
uv sync --group dev --locked
.\.venv\Scripts\python.exe -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
$LASTEXITCODE
```

预期：`243 passed`，`$LASTEXITCODE = 0`。若出现 `PytestUnhandledThreadExceptionWarning` 即计为失败（非警告），
需回传用例名与日志原文。**（本轮未执行）**

### 6.2 隔离库人工清单

统一用隔离数据目录，不触碰 `%LOCALAPPDATA%\Fit-Agent`：

```powershell
$env:FIT_AGENT_DATA_DIR = Join-Path $env:TEMP ("fitagent-s107-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $env:FIT_AGENT_DATA_DIR | Out-Null
```

#### 6.2.1 空库迁移 + 种子/档案基线

```powershell
cd <repo>\backend
@'
import asyncio, os
from storage.db import Database
path = os.path.join(os.environ["FIT_AGENT_DATA_DIR"], "app.db")
async def main():
    db = Database(path); await db.open()
    print("migrate=", await db.migrate(), "user_version=", await db.pragma_value("user_version"))
    async with db.transaction() as conn:
        for sql in ["SELECT COUNT(*) FROM exercises",
                    "SELECT COUNT(*) FROM exercises WHERE recommendable=1",
                    "SELECT COUNT(*) FROM exercises WHERE active=0",
                    "SELECT COUNT(*) FROM exercises WHERE standard_name_zh LIKE '%哑铃分腿蹲%'",
                    "SELECT id, profile_json, context_version FROM user_profile"]:
            cur = await conn.execute(sql); print(sql, "->", await cur.fetchall())
    await db.close()
asyncio.run(main())
'@ | Set-Content -Encoding utf8 (Join-Path $env:TEMP "s107_probe.py")
.\.venv\Scripts\python.exe (Join-Path $env:TEMP "s107_probe.py"); $LASTEXITCODE
```

预期：`migrate=3 user_version=3`；`exercises=24`；`recommendable=1` 计数 `0`；`active=0` 计数 `0`；
哑铃分腿蹲 `0`；`user_profile` 恰 `(1, None, 0)`。**（本轮未执行）**

#### 6.2.2 Stage 0 库升级（只含 001 的旧库 → 生产迁移目录）

```powershell
cd <repo>\backend
@'
import asyncio, os, shutil
from pathlib import Path
from storage.db import Database
from storage.migrations import DEFAULT_MIGRATIONS_DIR
root = Path(os.environ["FIT_AGENT_DATA_DIR"])
d = root / "stage0_migrations"; d.mkdir(exist_ok=True)
shutil.copy(DEFAULT_MIGRATIONS_DIR / "001_stage0_runtime_and_settings.sql", d)
path = root / "legacy.db"
async def main():
    db = Database(path, migrations_dir=d); await db.open(); await db.migrate()
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO conversations (id, created_at) VALUES ('c1','2026-09-09T00:00:00Z')")
        await conn.execute("INSERT INTO app_config (key, value, updated_at) VALUES ('business_timezone','Asia/Shanghai','2026-09-09T00:00:00Z')")
    print("before_version=", await db.pragma_value("user_version")); await db.close()
    db = Database(path); await db.open()
    print("after_migrate=", await db.migrate())
    async with db.transaction() as conn:
        for sql in ["SELECT COUNT(*) FROM conversations",
                    "SELECT value FROM app_config WHERE key='business_timezone'",
                    "SELECT COUNT(*) FROM exercises",
                    "SELECT id, profile_json, context_version FROM user_profile"]:
            cur = await conn.execute(sql); print(sql, "->", await cur.fetchall())
    await db.close()
asyncio.run(main())
'@ | Set-Content -Encoding utf8 (Join-Path $env:TEMP "s107_upgrade.py")
.\.venv\Scripts\python.exe (Join-Path $env:TEMP "s107_upgrade.py"); $LASTEXITCODE
```

预期：`before_version=1`；`after_migrate=3`；会话数 `1`、`business_timezone=Asia/Shanghai` 保留、种子 `24`、档案 `(1, None, 0)`。**（本轮未执行）**

#### 6.2.3 启动 / 停服 / 重开（回环与 Host/Origin 边界）

```powershell
cd <repo>\backend
.\.venv\Scripts\python.exe main.py --host 127.0.0.1 --port 8000      # 前台窗口，保持运行
```

另开终端：

```powershell
curl.exe -s http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "host-bad=%{http_code}\n" -H "Host: 198.51.100.7:8000" http://127.0.0.1:8000/healthz
curl.exe -s -o NUL -w "origin-bad=%{http_code}\n" -H "Origin: http://evil.example" http://127.0.0.1:8000/healthz
Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 | Select-Object LocalAddress,LocalPort,OwningProcess
```

预期：`/healthz` 返回 `status=ok`、`database=open`、`business_timezone` 为 IANA 地区名、`provider_has_api_key=false`；
两次边界请求均 `403`；监听只在 `127.0.0.1`。回到服务窗口按 `Ctrl+C`（**勿用 `Stop-Process -Force`**），
观察 `Application shutdown complete` / `Finished server process`；确认端口释放后，用**同一** `$env:FIT_AGENT_DATA_DIR`
重开并再次 `curl.exe -s http://127.0.0.1:8000/healthz`：`business_timezone` 必须与首次完全相同（不重取样），
停服后目录只剩 `app.db`（WAL 已 checkpoint）。**（本轮未执行）**

#### 6.2.4 种子清单人工检查（只读）

服务停止后，用 6.2.1 的探针脚本再跑一次（同一目录）核对 24 项、`recommendable` 全 0、无哑铃分腿蹲；
如需逐项名称，追加 `SELECT standard_name_zh FROM exercises ORDER BY standard_name_zh` 与 S1-03 证据 §3 清单对照。**（本轮未执行）**

### 6.3 证据记录模板（Windows 回传时逐条填写）

```
任务编号：S1-07 / Windows 同版本自动化 + 隔离库人工实测
平台：Windows <版本 build>，PowerShell <版本>，Python 3.13.x，uv <版本>
代码版本：git rev-parse HEAD = dd168bba79cbcec7e93a23ccbdec4e1945e08e5e
工作区差异：git status --porcelain -- backend pre-prj/stage = <31–32 条清单（含本文件），或与第 1 节差异说明>
步骤/命令：<逐条粘贴 6.1–6.2.4 实际命令或人工动作>
退出码：<$LASTEXITCODE / 服务 Ctrl+C 后状态>
实际结果：<243 passed 原文、user_version、种子计数、profile 行、403 计数、Get-NetTCPConnection 行、收尾日志>
判定：预期 vs 实际，逐项 通过/不通过/偏离说明
未覆盖部分：<列出仍未执行/不适用的项>
系统设置：<本阶段不要求切换系统时区，故无需恢复步骤>
```

## 7. 未覆盖范围与残留风险

1. **Windows 未实测**：本文件全部为 Linux/WSL2 证据；Stage 1 结项门槛（方案 A）要求同版本 Windows 全量自动化 + 隔离库人工实测，第 6 节本轮未执行。
2. **Stage 2–4 接线未做**：确认事务、草稿/Diff/纠错/幂等/受限组合原子提交、`context_version` 推进、整份计划阻断、打卡歧义询问与匹配、HTTP/Agent/前端接口均未实现（第 4 节只给责任与接缝）。
3. **总 review 遗留两条 P2（未修）**：① `validate_patch` 限制分支未查重复项（记于 `stage1.md` §9.7）；② `preview_patch().action_restrictions` 在补丁触及限制时写 `known`（已在 S1-05 §6 交接，调用方须另判正式三态）。
4. **`test_app` 单次 flake**：历史观察 1/11（进程内用例），当时 8 次复跑全绿；本轮 10 次复跑 **10/10 `5 passed`**，未复现，仍留观察。
5. **reviewer 模型 fallback**：本轮四个 reviewer 全部落到 `deepseek/deepseek-v4.1-flash-expires-on-0910:high`（配置的 `openai-codex/gpt-5.6-sol:medium` 不可用），reviewer 结论只作线索，结论以父会话独立探针与边界核查为准。
6. **未拍事项**：可推荐检查标准、数值范围/医学阈值、红旗解除语义、中文口语别名、`leverage machine` 口径复核均未拍，不自行扩充。
7. **数据/媒体**：媒体未导入、未下载、未建字段；文字数据逐行保留 attribution；旧库中任意非九项键集的 `profile_json` 经 `domain.profile.schema.profile_from_json`（经 `ProfileRepo.read`）读取会抛 `InvalidProfileRow`（视为数据损坏，正式建档路径接入前不会自动遇到）。
8. **Stage 0 豁免**：系统时区切换仍为豁免未验，不转写为通过。

## 8. 声明

本文件汇总的是**离线领域与存储层验证**：它证明动作目录/档案/安全规则的确定性基础、迁移与重开回归在 Linux 上通过，
**不等于**对话建档、确认事务、训练指导闭环或产品端到端验收；不把本阶段完成当作完整开发获批。
Windows 实测回传并判定通过前，Stage 1 不结项；Stage 2–4 与前端验收按各自阶段边界另行授权。
