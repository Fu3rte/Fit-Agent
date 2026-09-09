# S1-01 开工基线与契约核对（Linux/WSL2）

> 子任务：Stage 1 S1-01。只读核对 + 落证据，**不写业务代码、不建表、不装依赖、不访问真实用户库或真实凭据、不发起模型调用**。
> 本文件是 S1-02~S1-05 的基线正本：后续 worker 直接引用，不再重复核对。
> 验收对照：stage1.md §5 S1-01（记录版本/平台/差异/基线；字段映射不新增医学阈值、业务枚举或事务边界；写入测试只用临时目录）。

## 0. 结论摘要

- 基线：HEAD `dd168bb`，工作区差异**全部与 Stage 1 后端无关**（前端 mock 轨道 + 阶段文档），`backend/**` 无未提交差异。差异条数是活动快照：首次记录 9 条，复核时 11 条（前端轨道在本文件写入后仍并行改动 `frontend/**`），详见第 1 节。
- 测试：`69 passed`，退出码 0（两次实跑：首次 2.49s、复核 2.27s，均带 `-W` 门）。
- 测试模块数修正：`tests/` 下 `test_*.py` 为 **7 个**（另 `conftest.py`、`support.py`、`__init__.py`），共 69 用例；首次记录写的“8 个测试文件”有误，已在第 4.4 节更正。
- 底座可复用接缝确认：`storage/db.py` 单连接+单锁+`transaction()`、`storage/migrations.py` 编号迁移+`user_version`、`tests/support.py` 临时库夹具。**Stage 1 迁移编号只能从 002 起且必须连续。**
- `backend/domain/actions/`、`backend/domain/profile/` 确认为**单行 docstring 空壳**（无函数/类），目录职责与 architecture 02/03 一致，直接沿用。
- 本子任务**未改任何 backend 代码**，只新增本证据文件；无 `git add`/commit/stash。

## 1. 代码版本与工作区差异（实测）

| 项 | 实测值 | 命令 |
|---|---|---|
| HEAD | `dd168bba79cbcec7e93a23ccbdec4e1945e08e5e` | `git rev-parse HEAD` |
| 分支 | `main` | `git rev-parse --abbrev-ref HEAD` |
| 工作区差异条目数 | 11（复核时点 `2026-09-09 22:24 CST`；首次记录为 9） | `git status --porcelain \| wc -l` |
| `backend/**` 差异条目数 | 0 | `git status --porcelain -- backend \| wc -l` |
| 已暂存文件 | 无（`git diff --cached --name-only` 为空） | — |

> 差异条数是**活动快照**：前端 mock 轨道在本文件首次写入后仍并行改动 `frontend/**`（复核时新增 `frontend/src/features/chat/ChatPage.tsx`），故两次记录不同。判断 Stage 1 边界的依据是 `backend/**` 差异为 0，不依赖总数。

`git status --porcelain` 全量清单（复核时点，退出码 0）：

```
 M frontend/plans/stage1.md
 M frontend/src/features/chat/ChatPage.tsx
 M frontend/src/features/chat/DraftCard.tsx
 M frontend/src/features/profile/ProfilePage.tsx
 M frontend/src/lib/contract.ts
 M frontend/src/mock/server.ts
 M pre-prj/stage/evidence/S0-08-windows-2026-09-09.md
 M pre-prj/stage/stage0.md
?? frontend/src/lib/profile.ts
?? pre-prj/stage/evidence/S1-01-baseline-linux-2026-09-09.md
?? pre-prj/stage/stage1.md
```

**与本阶段（Stage 1 后端）相关的差异：无。** 11 条全部属另外两条轨道：

| 轨道 | 差异条目 | 处置 |
|---|---|---|
| 前端 mock 并行轨道 | `frontend/plans/stage1.md`、`frontend/src/features/chat/ChatPage.tsx`、`frontend/src/features/chat/DraftCard.tsx`、`frontend/src/features/profile/ProfilePage.tsx`、`frontend/src/lib/contract.ts`、`frontend/src/mock/server.ts`、`frontend/src/lib/profile.ts`(新) | 只读，不得改；该轨道仍在活动，`frontend/**` 快照会继续变 |
| 阶段文档/证据 | `pre-prj/stage/stage0.md`、`pre-prj/stage/evidence/S0-08-windows-2026-09-09.md`、`pre-prj/stage/stage1.md`(新，本阶段计划正本) | 只读，不得改 |

## 2. 平台、工具链与依赖（实测）

| 项 | 实测值 |
|---|---|
| 平台 | Linux `DESKTOP-PVL18AR` 6.6.114.1-microsoft-standard-WSL2（WSL2, x86_64） |
| 解释器 | `backend/.venv/bin/python` → Python 3.13.15 |
| uv | 0.12.3 |
| pytest | 9.1.1 |
| 项目依赖（`uv pip list`） | aiosqlite 0.22.1、fastapi 0.141.1、platformdirs 4.11.8、pydantic 2.13.5、pydantic-ai-slim 2.41.0、pydantic-graph 2.41.0、starlette 1.6.0、tzdata 2026.3、tzlocal 5.4.4、uvicorn 0.52.4、pytest 9.1.1 |
| `pyproject.toml` | `requires-python >=3.13`；`[tool.uv] package = false`（非可安装包）；dev 组仅 pytest；`[tool.pytest.ini_options] norecursedirs = ["archive"]` |

依赖与 PLAN.md「技术栈」已拍清单一致，**无 Stage 1 需新增依赖**（S1-02~S1-05 不得新增）。

## 3. 测试基线（实跑，非沿用旧文档）

```
cd backend
timeout 240s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning
```

- 结果：`69 passed in 2.49s`，退出码 `0`。
- 与 Stage 0 交接文档记载的 69 用例一致；本次为实跑数字，不是转抄。
- `-W error::pytest.PytestUnhandledThreadExceptionWarning` 门已带（Stage 0 已知 aiosqlite 工作线程 teardown 噪声会变硬失败）。

### 3.1 测试写入隔离核对（验收项 ⑤）

| 检查 | 结果 | 依据 |
|---|---|---|
| 用例只用 `tmp_path` 临时库 | 是 | `tests/support.py:41` `open_database(path)`；各用例 `tmp_path / "data"`（`test_app.py:21` 等） |
| 真实数据目录 override 只用于隔离 | 是 | `config.py:16` `FIT_AGENT_DATA_DIR` 仅测试/Windows 人工验收隔离；`tests/support.py:115` 把它指向 tmp 目录 |
| 是否触碰真实用户库 | 未发现 | 全 backend grep `FIT_AGENT_DATA_DIR` 仅 `config.py` 与 `tests/support.py` |
| 是否读真实 API Key | 未发现 | `tests/test_provider_settings.py:61-66` 假 Key 前缀 `sk-fitagent-fake-` + `secrets.token_hex` |
| 硬编码家目录/平台路径 | 未发现 | grep `LOCALAPPDATA`/`.local/share`/`Path.home` 在 tests 无命中；`test_config.py` 用 monkeypatch 替换 `platformdirs.user_data_dir` |
| 例外 | **无** | — |

## 4. 底座与占位文件实况（验收项 ②）

### 4.1 `backend/storage/db.py`（S1-02 起必须复用，不新建连接封装）

| 接缝 | 位置 | 语义 |
|---|---|---|
| `Database(path, migrations_dir=None)` | db.py:109 | 单连接 + 单 `asyncio.Lock`，连接对象不对外暴露（07 7.1） |
| `open()` / `close()` | db.py:131 / :146 | `isolation_level=None`（autocommit）；PRAGMA `journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout=5000`；重复 open 抛错 |
| `under_lock(op)` | db.py:175 | 读与单语句写入；回调内禁止再取锁（锁不可重入） |
| `transaction()` | db.py:188 | 多语句原子：BEGIN→COMMIT/ROLLBACK 全程持锁；异常与取消一律回滚后重抛 |
| `migrate()` | db.py:169 | 持锁调用 `run_migrations`；失败抛异常 → 启动流程据此拒绝对外服务 |
| `pragma_value(name)` | db.py:275 | 白名单 `journal_mode`/`foreign_keys`/`busy_timeout`/`user_version` |
| `parameter_echo_suppressed()` | db.py:214 | 凭据写入窗口的日志参数回显抑制（S0-03 修复） |

### 4.2 `backend/storage/migrations.py`（编号迁移硬约束）

- 文件名 `NNN_*.sql`，编号**必须从 1 连续**：`load_migrations` 逐项校验（migrations.py:24-41），不连续立即 `MigrationError`，不执行任何脚本。
- 每个文件在 `BEGIN; … PRAGMA user_version=N; COMMIT;` 内执行 → DDL 与版本推进同事务；失败 rollback + `MigrationError`，不虚报版本、不留半套结构（migrations.py:66-77）。
- `current > len(migrations)` → `FutureSchemaVersion`，高版本库拒绝启动、不降级、不重建（migrations.py:61-65）。
- **对 Stage 1 的硬含义：现有最新为 001，S1-02 只能新增 `002_*.sql`，S1-03 种子只能接 `003_*.sql`（若 S1-02 未占 003）；不得跳号、不得改 001。**

### 4.3 `001_stage0_runtime_and_settings.sql` 现状

6 张表：`conversations`、`runs`、`messages`、`run_events`、`app_config`、`provider_config`；4 个索引。**业务表 0 张**（`exercises`、`user_profile` 均未创建）。

### 4.4 测试设施（沿用）

- `tests/conftest.py`：`anyio_backend = "asyncio"`，自动给 async 用例加 `anyio` 标记（不新增依赖）。
- `tests/support.py:41` `open_database(path, *, migrate=True, migrations_dir=None)` 上下文管理器：打开临时文件库、可选迁移、退出关闭。
- `tests/support.py:106-179`：真实入口子进程助手 `start_app`/`stop_app`/`wait_for_healthz`/`http_get`（`FIT_AGENT_DATA_DIR` 指向 tmp，仅回环）。
- **7 个测试模块**（`test_app.py`、`test_config.py`、`test_db.py`、`test_migrations.py`、`test_provider_settings.py`、`test_run_repo.py`、`test_timezone.py`）+ `conftest.py`/`support.py`/`__init__.py`，共 69 用例（`pytest --collect-only -q` → `69 tests collected`）。

### 4.5 占位文件实况（据实核对，非文档转抄）

| 范围 | 实况 |
|---|---|
| `backend/domain/**` | 25 个 `.py` 全部为**单行 docstring**，无函数、无类、无 SQL（`wc -l` 全为 1） |
| `domain/actions/{schema,rules,repo,service}.py` | 占位 docstring 已声明职责：schema 类型 / rules 纯规则 / repo 唯一 SQL / service 编排，与 03 章一致 |
| `domain/profile/{schema,rules,repo,service}.py` | 同上，与 02 章一致 |
| `domain/plan|records|stats/**` | 同为占位，属 Stage 3，**本阶段不得实现** |
| `app/confirm.py`、`app/drafts.py`、`runtime/*.py`、`api/deps.py`、`api/routes_*.py` | 单行占位，本阶段不接 |

→ S1-02~S1-05 沿用 `domain/actions/` 与 `domain/profile/` 既有组织位置，不新建业务模块目录。

## 5. 字段映射表（验收项 ③）

状态列含义：**已拍**＝有正本/已拍依据；**实现者自定**＝字段名/类型/索引；**待拍**＝不得擅自实现。

### 5.1 动作目录（`exercises`，03 章）

| 字段（语义） | 正本条目 | 状态 |
|---|---|---|
| 身份 ID | 03 3.1 动作身份归一；03「本章已拍结论」停用不删除 | 实现者自定（取值须稳定，停用后按 ID 仍可读） |
| 标准名（中文） | stage1.md §5 S1-03 已拍 25 项清单原词 | 已拍 |
| 别名 | 03 3.1 别名/器械变式/负重方式必须可区分 | 已拍（数据集英文 `name` 入别名；中文口语别名待人工确认，不入库） |
| 器械变式 | 03 3.1 | 已拍 |
| 动作模式（13 词表 + 25 归属，多归属仅 2 处） | stage1.md §5 S1-03「已确认的动作模式映射」 | 已拍 |
| `record_type` ∈ {负重次数, 自重次数, 计时} | 03「本章已拍结论」 | 已拍（首批无计时动作；不新增第四类，不建辅助负重型） |
| 负重口径 | stage1.md §5 S1-03「已确认的负重口径」 | 已拍 |
| 可推荐标记 | 03 3.2「只有经过检查并标记为可用于计划的动作才会被主动推荐」 | 已拍（默认不可推荐，逐条检查后标记） |
| 停用标记 | 03「本章已拍结论」停用不删除 | 已拍 |
| 来源 ID / attribution | stage1.md §5 S1-03（来源核对 + 许可声明） | 已拍（核不上不导入） |
| 单侧 / 每侧次数口径 | stage1.md §5 S1-03 已确认负重口径 | 已拍（本阶段只验证口径保留，输入/展示后续） |
| 本地媒体关联（图片/GIF） | 03 3.2；stage1.md §3 | **移出本阶段，不预建字段** |

### 5.2 用户档案（`user_profile`，02 章）

| 字段（语义） | 正本条目 | 状态 |
|---|---|---|
| 训练目标 | 02 2.1 | 已拍（值域不擅定） |
| 训练经验 | 02 2.1 | 已拍（未知 vs 明确否认可区分） |
| 每周训练频率 | 02 2.1 | 已拍 |
| 单次可用时长 | 02 2.1 | 已拍 |
| 可用器械 | 02 2.1 | 已拍 |
| 动作限制：具体动作 | 02 2.2 | 已拍（引用动作身份） |
| 动作限制：动作模式 | 02 2.2 | 已拍（引用 13 项模式词表） |
| 当前身体状态 | 02 2.1 | 已拍 |
| 红旗症状 | 02 2.3；stage1.md §5 S1-05（6 类结构化样例） | 已拍 |
| `body_weight_kg` | stage1.md §5 S1-04 验收 1、§7 | 已拍：完整档案必填，缺失不生成完整档案、不填默认值 |
| `context_version` | 02「责任边界」（载体归档案）+ 01 1.4 | 已拍（载体本阶段；**推进入口归 Stage 2**） |
| 拟议补丁 `proposed_profile_patch` | 02 2.4 | 结构 + 本地校验本阶段；落库归 Stage 2 草稿 |
| 当次条件（如「今天只能用哑铃」） | 02 2.4 | 结构分流；**不写长期档案、不进拟议补丁** |

> 本映射**不新增**任何医学阈值、业务枚举或事务边界；`record_type` 三类与 13 项模式词表均为已拍枚举，原样照搬。

## 6. 未拍依赖清单（验收项 ④：现在不得实现）

| # | 事项 | 现状 | 本阶段处置 |
|---|---|---|---|
| 1 | 计时动作的具体归类 | 首批 25 项无计时动作；`record_type` 保留计时类型但无条目归类依据 | 保留类型枚举，不实现归类、不擅自加计时动作 |
| 2 | `body_weight_kg` 以外的档案必填阈值与默认处方条件 | 未拍 | 不擅定，只做结构校验与缺失表达 |
| 3 | 红旗清单外症状的判定与阻断 | 已拍：只返回「未知/需澄清」 | 按已拍实现，不得自行扩充医学规则 |
| 4 | 红旗解除语义 | 无解除策略 | 不提供自动解除能力 |
| 5 | 种子更新与历史身份冲突 | stage1.md §7 条件性事项 | 若实现撞上，暂停受影响部分并列选项 |
| 6 | 中文口语别名表 | 已拍延后 | 只出「待人工确认别名提议表」，不入库 |
| 7 | 来源核不上条目的补拍/替换 | 已拍 B：不导入 | 列缺项表，人不决定前不补、不替换 |
| 8 | `context_version` 推进时机与确认事务 | 归 Stage 2 | 本阶段不得实现推进入口或独立计数器 |
| 9 | 只读 API / 页面 / 前端接口形状 | 后续接线阶段 | 本阶段不接 HTTP |
| 10 | 左右侧实际输入保存、展示与统计聚合规则 | 后续阶段 | 本阶段只验证口径保留 |
| 11 | 本地媒体关联 / 目录选择 / 展示 | 已移出本阶段（S1-06） | 不实现、不预建字段 |
| 12 | 新依赖 / 新业务模块 | design-decisions 不变量 6 须拍板 | 不新增 |

## 7. 与本阶段无关的既有工作区改动清单（原文，供 reviewer/后续 worker 对照）

见第 1 节表格。要点：`backend/**` 无任何未提交差异；`frontend/**` 与 `pre-prj/stage/*.md` 的改动属其他轨道，Stage 1 任何 worker 不得触碰。

## 8. 本子任务实际执行记录

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `git rev-parse HEAD` / `--abbrev-ref HEAD` / `status --porcelain` | 0 | 见第 1 节 |
| 2 | `uname -a`、`.venv/bin/python -V`、`uv --version`、`pytest --version`、`uv pip list` | 0 | 见第 2 节 |
| 3 | `timeout 240s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | 0 | `69 passed in 2.49s` |
| 4 | `grep -rn "FIT_AGENT_DATA_DIR" --include='*.py' .`（backend） | 0 | 仅 `config.py:16`、`tests/support.py:115` |
| 5 | `grep -rnE "tmp_path\|tempfile" tests/*.py`；`grep -rnE "LOCALAPPDATA\|\.local/share\|Path\.home" tests/*.py` | 0 / 1（无命中） | 见 3.1 |
| 6 | `cat tests/conftest.py`、`cat tests/support.py`、`grep -n` 于 `storage/db.py`、`cat storage/migrations.py` | 0 | 见第 4 节 |
| 7 | `grep -nE "CREATE TABLE\|CREATE INDEX" storage/migrations/001_*.sql` | 0 | 6 表 4 索引 |
| 8 | 逐个 `cat` `domain/**/*.py`（25 文件）、`wc -l` | 0 | 全部单行占位 |
| 9 | 只读 `pre-prj/architecture/02-profile-security.md`、`03-action-catalog.md`、`07-data-persistence.md`、`design-decisions.md` 架构不变量节 | 0 | 见第 5 节映射依据 |

### 8.1 复核轮（本文件第 2 次执行，2026-09-09 22:24 CST）

独立重跑关键命令，确认本文件断言未失效：

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| R1 | `git rev-parse HEAD` / `status --porcelain` / `-- backend \| wc -l` | 0 | HEAD 仍 `dd168bb`；总数 9→11（前端轨道活动）；`backend/**` 差异 0 |
| R2 | `timeout 240s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | 0 | `69 passed in 2.27s` |
| R3 | `.venv/bin/python -V`、`pytest --version`、`uv --version`、`uv pip list` | 0 | 与第 2 节逐项一致（Python 3.13.15、pytest 9.1.1、uv 0.12.3） |
| R4 | `wc -l domain/*/*.py`、`find domain -name '*.py'` | 0 | 25 行 / 25 个模块文件（+ `domain/__init__.py`），仍全部单行占位 |
| R5 | `grep -cE 'CREATE TABLE\|CREATE INDEX' storage/migrations/001_*.sql`；`ls storage/migrations/` | 0 | 6 表 4 索引；目录仅 `001_*.sql` + `README.md`（Stage 1 只能从 002 起） |
| R6 | `grep -rn FIT_AGENT_DATA_DIR --include='*.py'`（backend） | 0 | 仅 `config.py:16,26` 与 `tests/support.py:115` |
| R7 | `ls tests/test_*.py \| wc -l`；`pytest --collect-only -q` | 0 | 7 个测试模块 / 69 collected（更正首次的“8 个测试文件”） |

未执行（按授权禁止）：安装依赖、建表、改 backend 代码、访问真实用户库/真实 Key、模型调用、Windows 命令。

## 9. 未覆盖范围与残留风险

- **Windows 未实测**：本文件全部为 Linux/WSL2 证据；Stage 1 结项门槛（方案 A）要求同版本 Windows 全量自动化 + 隔离库人工实测，本轮不含。
- 既有 69 用例未逐条重读断言语义，只确认通过数与隔离性；`domain/` 占位无实现，故 S1-02~S1-05 是全新代码路径，风险集中在迁移编号连续性与 `context_version` 边界。
- 前端 mock 轨道与后端业务语义须保持一致（同一批 2026-09-09 拍板），但前端改动不在本文件核对范围。
