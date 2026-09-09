# S1-02 增量迁移与最小持久化结构（Linux/WSL2）

> **状态：已交付（2026-09-09 22:58 CST 重新落地，人拍方案 A）。** 本文件产物最初于 2026-09-09 22:47 按方案 B 整段回退；同日拍定方案 A（测试断言随 schema 版本维护）后，按归档副本 `/tmp/stage1-revert-20260909-224719/`（MD5 一致）重新落地并实测。历史回退记录见 `stage1.md` §9.3，重新落地见 §9.5。

> 子任务：Stage 1 S1-02。验收正本：`pre-prj/stage/stage1.md` §5 S1-02；语义正本：`pre-prj/architecture/03-action-catalog.md` 3.1–3.2、`02-profile-security.md` 2.1–2.4、`07-data-persistence.md` 7.2；硬约束：`pre-prj/design-decisions.md`「架构不变量」。
> 基线正本：`pre-prj/stage/evidence/S1-01-baseline-linux-2026-09-09.md`（HEAD `dd168bb`、69 用例、迁移编号只能从 002 起连续）。本文件不重复 S1-01 的字段映射表，只记本子任务的实际交付与实测。

## 0. 结论摘要

- 交付：`storage/migrations/002_stage1_actions_profile.sql`（新建 `exercises`、`user_profile` 两表 + 1 索引，不导入种子、不预填用户事实）；`tests/test_stage1_schema.py`（12 用例）；Stage 0 既有测试 3 处最小改动（见第 5 节）。
- 实测：`81 passed`，退出码 `0`（69 基线 + 12 新增），带 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 门。
- 空库迁移到 `user_version=2`；表集合恰为 Stage 0 六表 + `exercises` + `user_profile`（无 drafts/plans/records/stats 等后续阶段表）；`user_profile` 只有「未建档」技术载体 `(1, NULL, 0)`；`exercises` 行数 0。
- 未改 `001`、未新增依赖、未新建业务模块目录、未接 HTTP/Agent/CLI；`git diff --cached` 为空；`frontend/**` 与 S1-01 快照逐字节一致（第 6 节）。
- **Windows 未实测**：本文件全部为 Linux/WSL2 证据，Stage 1 结项门槛（方案 A）仍待同版本 Windows 回验。

### 0.1 本轮 provenance（据实记录）

上一轮派发在写作途中被人为中断（非失败）。本轮在该半成品上**核对并补全**，未推倒重写：

| 产物 | 来源 |
|---|---|
| `002_stage1_actions_profile.sql` | 上一轮已写完（66 行），本轮逐行核对 + 实跑验证 |
| `tests/test_stage1_schema.py` 前 11 个用例 | 上一轮已写完（本轮核对其可跑、逐条对照验收） |
| `tests/test_stage1_schema.py:492` 用例（标记列域约束 + 标准名唯一） | 本轮新增（补「约束检查」验证项，见第 4 节 V-3） |
| `tests/test_db.py`、`test_migrations.py`、`test_provider_settings.py` 改动 | 上一轮已写，本轮逐处复核强度（第 5 节） |
| 本证据文件、全量实测、schema 探针 | 本轮 |

## 1. 交付物与代码版本

| 项 | 值 | 命令 |
|---|---|---|
| HEAD | `dd168bba79cbcec7e93a23ccbdec4e1945e08e5e` | `git rev-parse HEAD` |
| 平台 | Linux WSL2 x86_64，Python 3.13.15（`backend/.venv`） | `.venv/bin/python -V` |
| 已暂存文件 | 0 | `git diff --cached --name-only \| wc -l` |
| `backend/**` 差异 | 5 条（3 改 + 2 新），恰为本任务清单 | `git status --porcelain -- backend` |

`git status --porcelain -- backend`（退出码 0）：

```
 M backend/tests/test_db.py
 M backend/tests/test_migrations.py
 M backend/tests/test_provider_settings.py
?? backend/storage/migrations/002_stage1_actions_profile.sql
?? backend/tests/test_stage1_schema.py
```

迁移目录（`ls backend/storage/migrations/`）：`001_stage0_runtime_and_settings.sql`、`002_stage1_actions_profile.sql`、`README.md` → 编号 1、2 连续，符合 `storage/migrations.py:24-41` 的硬校验；`001` 未改动（`git status` 无该文件）。

## 2. 迁移 002 的结构与正本对应

`002_stage1_actions_profile.sql` 不含 `BEGIN/COMMIT/PRAGMA user_version`：由 `storage/migrations.py:66-77` 统一包事务（DDL 与版本推进同事务，失败整体回滚）。

### 2.1 `exercises`（动作目录，03 章）

| 列 | 约束 | 正本对应 |
|---|---|---|
| `id TEXT PRIMARY KEY` | — | 稳定身份；停用后按 ID 仍可读（03 3.1、停用不删除） |
| `standard_name_zh TEXT NOT NULL UNIQUE` | 唯一 | 中文标准名 = stage1.md 已拍清单原词；一个身份一个标准名 |
| `equipment_variant TEXT NOT NULL` | — | 器械变式与别名、负重方式可区分（03 3.1） |
| `record_type` | `CHECK IN ('reps_weight','reps_bodyweight','time')` | 恰三类已拍口径；**无辅助负重型、无第四类**（03「本章已拍结论」） |
| `load_convention` | `CHECK IS NULL OR IN (5 值)` | 对应 stage1.md §5 S1-03「已确认的负重口径」5 条：`barbell_includes_bar_total`（含杆总重）、`dumbbell_per_hand`（每只）、`machine_pin_displayed_value`（插销/绳索标示值）、`plate_loaded_total_excluding_empty`（挂片类合计、不含空载）、`unilateral_setting_per_side`（单侧设定值） |
| 表级 `CHECK`（:45-49） | `reps_weight ⇒ load_convention NOT NULL`，其余类型 ⇒ `NULL` | 负重口径只属于负重次数型；不自重/计时虚构口径 |
| `unilateral` | `NOT NULL DEFAULT 0 CHECK IN (0,1)` | 单侧动作区分左右、次数按每侧（本阶段只保留口径，输入/展示后续） |
| `recommendable` | `NOT NULL DEFAULT 0 CHECK IN (0,1)` | 未经检查不得进推荐候选（03 3.2）；**默认 0，不自动置 1** |
| `active` | `NOT NULL DEFAULT 1 CHECK IN (0,1)` | 停用不删除；停用只置 0 |
| `aliases_json` | `NOT NULL DEFAULT '[]' CHECK json_valid` | 别名（含数据集英文 `name`）；多身份命中保留候选属 S1-03 |
| `modes_json` | `NOT NULL CHECK json_valid` | 13 项模式词表子集；**词表/归属校验归 rules/repo 层（SQLite CHECK 不能遍历 JSON 数组元素）**，属 S1-03 |
| `source_ref TEXT NOT NULL` | — | 来源 ID；核不上不导入（S1-03） |
| `attribution TEXT NOT NULL` | — | 文字数据许可声明（数据集 `© Gym visual`） |
| `instructions_zh TEXT` | 可空 | 动作说明文字；**无任何媒体字段**（媒体整体移出本阶段） |

索引：`idx_exercises_active_recommendable ON exercises (active, recommendable)`（:52）——推荐候选筛选的最小支撑。

### 2.2 `user_profile`（单用户档案，02 章）

| 列 | 约束 | 正本对应 |
|---|---|---|
| `id INTEGER PRIMARY KEY CHECK (id = 1)` | 单例 | 单用户单例行（02「责任边界」） |
| `profile_json TEXT` | `CHECK IS NULL OR json_valid` | 档案 JSON 载体；**NULL = 未建档技术载体**，不预填目标/经验/限制/身体状态/红旗 |
| `context_version INTEGER NOT NULL DEFAULT 0 CHECK >= 0` | 同载体 | 统一业务版本载体与档案同一行、同一快照（01 1.4）；**本阶段只建载体，不提供推进入口** |

初始化：`INSERT INTO user_profile (id, profile_json, context_version) VALUES (1, NULL, 0)`（:66）——只建立未建档载体，**不默认「无伤病」「无红旗」「已完成安全筛查」**。

## 3. 实测命令与结果

全部在 `backend/` 下、只用 `tmp_path` 临时文件库；无真实用户库、无真实 Key、无模型调用、无新依赖。

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `timeout 240s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | 0 | `81 passed in 2.33s` |
| 2 | `timeout 120s .venv/bin/python -m pytest tests/test_stage1_schema.py -q -W error::...` | 0 | `12 passed in 0.53s` |
| 3 | `.venv/bin/python -m pytest tests --collect-only -q` | 0 | `81 tests collected`（基线 69 + 新增 12） |
| 4 | schema 探针（临时库，`/tmp/s102_probe.py`，`timeout 60s`） | 0 | `migrate_returns=2 user_version=2`；`tables=['app_config','conversations','exercises','messages','provider_config','run_events','runs','user_profile']`；`exercises_indexes=['CREATE INDEX idx_exercises_active_recommendable ...']`；`profile_row=[(1, None, 0)]`；`exercise_count=0` |
| 5 | `ls backend/storage/migrations/` | 0 | 仅 `001`、`002`、`README.md`（编号连续） |
| 6 | `git diff --cached --name-only \| wc -l` | 0 | `0`（未 staged） |
| 7 | `git status --porcelain -- frontend` + `md5sum -c` 对 S1-01 快照 | 0 | 10/10 文件逐字节一致（第 6 节） |

## 4. 逐条验收对照（stage1.md §5 S1-02）

### 验收条目

| # | 验收 | 判定 | 证据 |
|---|---|---|---|
| A-1 | 空库可迁移 | 满足 | `test_stage1_schema.py:168`：`migrate()==2`、`user_version==2`、表集合恰为 8 张、`exercises` 0 行、档案 `(1,None,0)` |
| A-2 | Stage 0 库升级保留会话、消息、Run、时区及 Provider 配置 | 满足 | `:186`：先以只含 001 的临时迁移目录建库并写入会话/Run/消息/run_event/时区/假 Key，再用生产目录升级到 2；逐项断言会话、Run `pending`、`messages` 的 `user_request`、`run_events` 的 `note`、`Asia/Shanghai`、内部 Key 相等、`has_api_key is True` |
| A-3 | 重复启动不重置档案或版本、不重复种子 | 满足 | `:237`：写入样本档案 + `context_version=7` + 一行停用且已检查的动作 → 重开库 `migrate()==2`（无新迁移）→ 档案与版本原样、`exercises` 仍 1 行、`active=0` 与 `recommendable=1` 未被覆盖。另 `tests/test_migrations.py:42` 以哨兵会话验证不重跑迁移 |
| A-4 | 失败迁移不留下半套结构 | 满足 | `:267`：注入非法 002 → `MigrationError`、`user_version` 仍 1、`exercises` 不存在（已回滚）；修复后重跑 `migrate()==2` 且表就位（不删库） |
| A-5 | 高版本库仍拒绝启动 | 满足 | `:297`：`PRAGMA user_version=99` → `FutureSchemaVersion(match="99")`，库版本仍 99 |

### 边界条目

| # | 边界 | 判定 | 证据 |
|---|---|---|---|
| B-1 | 只新增本阶段两项业务存储 | 满足 | `:168` 断言 `tables == STAGE0_TABLES \| STAGE1_TABLES \| {sqlite_sequence}` 且与 `LATER_STAGE_TABLES` 交集为空；探针 #4 实表 8 张 |
| B-2 | 不预填用户事实 | 满足 | `:168`/`:186` 断言 `exercises` 0 行、`user_profile` 为 `(1,None,0)`；迁移只 INSERT 技术载体 |
| B-3 | 不重建旧库 | 满足 | `:186` 同一路径升级，Stage 0 数据全部保留；`tests/test_migrations.py` 既有失败用例亦断言不删库 |
| B-4 | `context_version` 不能由目录查询、种子、补丁计算或普通读取推进 | 满足（本阶段可验证部分） | `:470`：写入并读取推荐候选后档案仍 `(1,None,0)`，且不存在任何表名含 `version` 的独立计数器；补丁计算/确认事务推进属 S1-04 与 Stage 2，本阶段不提供推进入口 |

### 验证方式条目

| # | 要求 | 判定 | 证据 |
|---|---|---|---|
| V-1 | 临时文件库空库/旧库升级 | 满足 | 命令 #1/#2；`:168`、`:186` 均用 `tmp_path` 文件库 |
| V-2 | 失败注入 | 满足 | `:267` |
| V-3 | 约束检查 | 满足 | `record_type` 三类 + 拒绝第四类（`:312`）；5 种负重口径接受 + 缺口径/未知口径/自重带口径拒绝（`:359`）；JSON 有效性（`:456`）；档案单例与 JSON/版本域（`:430`）；标记列只接受 0/1 与标准名唯一（`:492`，本轮补） |
| V-4 | 停服重开 | 满足 | `open_database` 关闭后对同一路径重新打开（`:237`、`:186`、`tests/test_migrations.py:42`） |
| V-5 | 凭据保留检查只用假 Key，证据不输出 Key | 满足 | 假 Key 常量 `sk-fitagent-fake-s102-not-a-real-key`（测试内），本文件不复制其值以外的任何真实凭据；`get_provider_status` 只断言 `has_api_key` |

## 5. Stage 0 既有测试的改动（逐处说明：为什么必须、强度是否不变）

stage1.md §4 写着「不改 Stage 0」。加迁移 002 会必然打破三处**把 001 当作最新版本**的硬编码；以下改动只做参数化与集合扩展，**未放宽任何断言、未删用例、未跳过**（`git diff --stat -- backend/tests`：+42/-13）。

| 文件 | 改动 | 为什么必须 | 强度 |
|---|---|---|---|
| `tests/test_db.py:14,17,99` | 引入 `load_migrations`，新增 `LATEST_VERSION = len(load_migrations())`，把 `user_version == 1` 改为 `== LATEST_VERSION` | 事务提交/回滚用例断言的是「迁移后的版本」，硬编码 1 在 002 之后必然失败 | 不变：仍是精确相等，只是从常量改为按迁移目录推导 |
| `tests/test_migrations.py:25-33,42-51` | `EXPECTED_TABLES` → `EXPECTED_RUNTIME_TABLES`，新增 `STAGE1_TABLES = {exercises, user_profile}`、`LATEST_VERSION`；断言改为 `tables >= 运行时表 \| Stage 1 表` 与 `tables == 运行时表 \| Stage 1 表 \| {sqlite_sequence}` | 原断言把「库里恰有 6 张表」写死；002 之后必须显式承认两项新存储，否则用例失败 | 增强：仍为精确集合相等，且显式锁死「Stage 1 只加这两张、不得多出后续阶段表」 |
| `tests/test_provider_settings.py:56-66,152-161` | `_SCANNED_TABLES` 加入 `exercises`、`user_profile`；`all_text_cells` 增加两表扫描 | 该用例在 `:131` 断言「非 sqlite 表集合 == `_SCANNED_TABLES`」，并扫描全部文本单元格找泄漏 Key | 增强：凭据泄漏扫描覆盖面随新表扩大 |

其余 Stage 0 用例（`test_app.py`、`test_config.py`、`test_run_repo.py`、`test_timezone.py`、`test_migrations.py` 其余断言）零改动；全量 69 基线用例在新版本下仍全绿（命令 #1）。

## 6. 隔离、安全与并行轨道

| 检查 | 结果 | 依据 |
|---|---|---|
| 测试只用临时库 | 是 | 全部用例经 `tests/support.py:41` `open_database(tmp_path/...)`；探针用 `tempfile.TemporaryDirectory()` |
| 未触碰真实用户库 | 是 | 无 `~/.local/share/Fit-Agent/app.db`、无 `%LOCALAPPDATA%` 访问 |
| 未读真实凭据 | 是 | 只用测试内假 Key；未调用 `get_provider_api_key_internal` 以外路径 |
| 未新增依赖/改 pyproject/uv.lock | 是 | `git status --porcelain -- backend` 仅 5 条，无 `pyproject.toml`/`uv.lock` |
| 未新建业务模块目录 | 是 | 未新增 `backend/domain/**` 文件；002 只建表 |
| 未接 HTTP/Agent/SSE/CLI 写入 | 是 | 未改 `api/**`、`app/**`、`runtime/**` |
| 未 staged / 未 commit / 未 stash | 是 | 命令 #6 = 0 |
| 并行前端轨道未被影响 | 是 | 命令 #7：`frontend/**` 10 个文件 md5 与 S1-01 快照逐字节一致 |
| 无媒体字段/素材 | 是 | 002 无任何 image/gif/media 列 |

## 7. 未覆盖范围与残留风险

1. **Windows 未实测**：结项门槛（方案 A）要求同版本 Windows 全量自动化 + 隔离库人工实测，本轮不含。
2. **种子与目录语义未实现**：`exercises` 为空表，别名/模式词表校验、推荐候选筛选、停用保留的查询语义属 S1-03；`modes_json` 的 13 项词表约束刻意留在 rules/repo 层（SQLite CHECK 无法遍历 JSON 数组）。
3. **档案结构与写入未实现**：`profile_json` 的内容校验、缺失/否认表达、拟议补丁隔离属 S1-04；本阶段只提供 JSON 载体与单例约束。
4. **`context_version` 推进未实现**：本阶段只有载体与「不被普通读取推进」的负向验证；推进入口与确认事务属 Stage 2。
5. **计时动作归类无数据**：`record_type` 保留 `time`，但首批 25 项无计时动作，归类依据属未拍（S1-01 §6 第 1 项）。
6. **Stage 0 测试改动的口径**：已拍方案 A（2026-09-09）——迁移版本号与库内表集合属活动事实，随迁移推进维护；允许「版本按迁移目录推导 + 计划内表集合显式扩展 + 凭据泄漏扫描面扩展」，不放宽、不删、不跳过。本文件第 5 节的 3 文件 5 处改动即按此口径，见 `stage1.md` §7、§9.5。

## 8. 交接给后续任务

| 后续 | 必须接的接缝 | 约束 |
|---|---|---|
| S1-03 种子 | 新增 `003_*.sql` 编号迁移写入精选动作 | 编号连续；不得在启动时全量覆盖目录；不得重置 `active`/`recommendable`；核不上的条目不导入 |
| S1-03 目录查询 | `exercises` 的 `aliases_json`/`modes_json`/`load_convention`/`active`/`recommendable` | 候选歧义保留，不静默取第一项；词表校验在 rules/repo |
| S1-04 档案 | `user_profile(profile_json, context_version)` 同一行同一快照 | 不预填事实；不自行提交事务；不提供版本推进入口 |
| Stage 2 确认事务 | `context_version` 载体 | 一次业务提交仅 +1，且与档案同事务 |

## 9. 重新落地（2026-09-09 22:58 CST，人拍方案 A）

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | 0 | `81 passed in 2.99s` |
| 2 | schema 探针（临时库，`PYTHONPATH=.`） | 0 | `migrate_returns=2 user_version=2`；表 = 底座六表 + `exercises` + `user_profile` + `sqlite_sequence`；`exercise_count=0`；`profile_row=[(1, None, 0)]` |
| 3 | `git status --porcelain -- backend` | 0 | 5 条（3 改 + 2 新）；`git diff --cached` 为空；`frontend/**` 未触碰 |
| 4 | 归档 MD5 对比 | 0 | 002 与 test_stage1_schema.py 与归档逐字节一致（`817dffc5…` / `f5361b80…`） |

- 拍板依据：`stage1.md` §7 条件性事项（方案 A）、§9.5。
- 5 处断言改动：`test_db.py`（版本推导 1 处）、`test_migrations.py`（版本 2 处 + 表集合 1 处）、`test_provider_settings.py`（扫描清单 + 逐表扫描）。性质断言未放宽，表集合仍为精确相等。
- `tests/test_migrations.py` 工作区含一处**先于本任务存在**的纯格式化差异（换行），非本任务改动，未回退。
- 遗留同 §7：S1-03、S1-04、S1-05、S1-07 未开工；Windows 未实测。
