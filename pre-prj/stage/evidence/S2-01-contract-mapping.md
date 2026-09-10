# Stage 2 S2-01：开工基线与传输契约核对（字段／调用路径／错误映射）

> 本文是 S2-01 的交付物（`pre-prj/stage/stage2.md` §5 S2-01：记录代码版本与工作区差异，核对
> Stage 1 实际接口、迁移、测试入口与前端 DTO，输出字段与调用路径映射，并落实首次建档完整性
> 契约）。本文不改动 `PLAN.md`、设计正本、`stage2.md` 与 Stage 1 交付；不含实现计划外的承诺。
> 前端只读核对，本任务未改动 `frontend/**`。

## 0. 基线与工作区（实跑记录，不沿用旧文档数字）

| 项 | 实测值 |
|---|---|
| 仓库／分支 | `Fit-Agent`，`main`（`origin/main` 前 2 个提交未推） |
| HEAD | `6b4aee3`（`frontend: Stage 1 建档闭环…`） |
| 平台 | WSL2 Linux（`6.6.114.1-microsoft-standard-WSL2`，主机名 `DESKTOP-PVL18AR`）；**非 Windows** |
| Python / pytest | `backend/.venv` 内 Python 3.13.15、pytest 9.1.1（`backend/pyproject.toml` 要求 `>=3.13`） |
| 开工基线（改前实跑） | `cd backend && .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` → **243 passed**，退出码 0 |
| 实施后（实跑） | 同上命令 → **282 passed**（243 + 39 新增），退出码 0 |
| 拍板 A 修正后（实跑） | 同上命令 → **282 passed**（首次建档完整性自动化仍为 39 用例：`test_explicit_none_is_distinct_from_unknown` 参数由 6 减为 4，新增 `test_explicit_none_cannot_replace_a_required_text_value` 2 例），退出码 0 |
| 开工前工作区差异 | `M backend/domain/actions/schema.py`（既有缩进重排，**非本任务**）、`M pre-prj/stage/stage0.md`、`?? pre-prj/stage/stage2.md`；三者全部原样保留，未回退、未覆盖 |
| 本任务新增差异 | `backend/domain/profile/schema.py`、`backend/domain/profile/rules.py`、`backend/tests/test_stage2_profile_first_time_complete.py`（见 §5）、本文 |
| Windows 证据 | **本轮无 Windows 执行环境，未运行**：Stage 2 阶段门槛（Windows 全量自动化）记「待 Windows 验证」，不以 Linux 结果替代 |

迁移目录当前为 `001_stage0_runtime_and_settings.sql`、`002_stage1_actions_profile.sql`、
`003_stage1_action_seed.sql`；`storage/migrations.py:load_migrations` 要求编号自 1 **连续**，
故 S2-02 的 `business_drafts` 迁移编号为 **004**，且不得改动 001–003（stage2.md §2）。

## 1. Stage 1 实际可复用入口（逐项核对，非"目录存在即已实现"）

| 入口 | 位置 | 核对结论 |
|---|---|---|
| 单连接 + 单锁 + 事务 | `storage/db.py`：`Database.open/close/migrate/under_lock/transaction/pragma_value` | `transaction()` 从 BEGIN 到 COMMIT/ROLLBACK 全程持锁；**锁不可重入**，事务体内再取锁即死锁；事务体内禁止模型／SSE |
| 编号迁移 | `storage/migrations.py`：`load_migrations`、`run_migrations` | DDL 与 `user_version` 同事务；高版本库 `FutureSchemaVersion` 拒绝启动 |
| 档案读取（同一快照） | `domain/profile/repo.py:31 read()` → `ProfileSnapshot(profile, context_version)` | `profile is None` 表示未建档（`profile_json` 为 NULL）；`context_version` 与档案同快照读出 |
| 档案事务内写入 | `domain/profile/repo.py:49 write_in_transaction()` | 只在**外层事务**内可用（非事务内直接 `RuntimeError`）；不提交、不推进 `context_version` |
| 档案用例服务 | `domain/profile/service.py`：`read_formal_profile`(43)、`validate_patch`(47)、`preview_patch`(61)、`check_candidate_actions_safety`(71)、`write_profile_in_transaction`(99) | 普通查询方法**自取锁**，不能在已持锁的确认事务内调用（stage2.md §2 已记录）；事务内需读档案时走 repo 的 `read`/`op(conn)` 路径 |
| 档案结构／补丁规则 | `domain/profile/rules.py`：`validate_profile_structure`、`validate_patch`、`apply_patch`、`validate_session_conditions`、`ensure_complete_profile`、`IncompleteProfile` | 已拍必填只有 `body_weight_kg`；红旗判定归 `domain/profile/safety.py` |
| 档案结构载体 | `domain/profile/schema.py`：三态 `Fact`、`ActionRestriction`、`Profile`、`ProfilePatch`、`SessionConditions`、`ProfileSnapshot` | 三态 unknown／denied／known；限制无状态语义；长期补丁与当次条件互不兼容 |
| 安全判定 | `domain/profile/safety.py`：`evaluate_safety`、`check_action_restrictions`、`assess_red_flags` | 只读；不自动解除红旗／限制；红旗任一来源出现即阻断处方 |
| HTTP 装配 | `api/app.py`：`create_app`、`LoopbackGuardMiddleware`、`/healthz` | 仅回环 Host/Origin 放行；业务路由尚未装配 |
| 测试入口 | `backend/tests/`（pytest；`tests/conftest.py` 自动为 async 用例打 `anyio` 标记）、`tests/support.py` 提供 `open_database` / 真实进程 `start_app` | 临时文件库隔离，不触碰真实数据目录 |
| 仍为空壳（后续任务落点） | `app/drafts.py`、`app/confirm.py`、`api/deps.py`、`api/routes_chat.py`、`api/routes_drafts.py`、`api/routes_media.py`、`api/routes_readonly.py`、`api/routes_settings.py` | 均为单行 docstring：S2-03–S2-07 的接线点，S2-01 不改它们 |

## 2. 字段映射：前端 DTO ↔ 后端档案事实

前端契约 `frontend/src/lib/contract.ts`（只读）中 `Profile` / `ProfileDraftPayload` /
`PhysicalState` / `Restriction` ↔ 后端 `Profile`（九个三态 `Fact`）：

| 后端事实（`FACT_FIELDS`） | 值类型（`FACT_VALUE_KINDS`） | 前端草稿字段（`ProfileDraftPayload.profile`） | 未知 vs 明确无的表达 |
|---|---|---|---|
| `training_goal` | text | `goal: string` | 字段缺省＝未知；须给有效文本，不得以「无」替代（§5 契约） |
| `training_experience` | text | `experience: string` | 同上 |
| `weekly_frequency` | integer | `weekly_frequency: number` | 必须为有效整数；不得以「无」或 `0` 占位 |
| `session_duration_minutes` | integer | `session_minutes: number` | 同上 |
| `available_equipment` | text_list | `equipment: string[]` | 缺省＝未知；`[]`＝明确无器械（后端 `denied` 或 `known(())`） |
| `body_weight_kg` | number | `body_weight_kg: number` | 必须为有效数值；缺省＝未知，不得补造 |
| `action_restrictions` | restrictions | `ProfileDraftPayload.restrictions?: Restriction[]` | 缺省＝未知；`[]`＝明确无限制 |
| `body_state` | text_list | `physical_state.notes: string[]` | 缺省＝未知；`[]`＝明确无其他身体状态 |
| `red_flags` | text_list | `physical_state.red_flags: string[]` | 缺省＝未知；`[]`＝明确无症状 |

三态映射硬边界（S2-01 验收要求，逐条确认不存在反例）：

- **未知不得压成空数组／默认值**：后端 `unknown`（`Fact.unknown()`）不带值，前端以字段缺省
  表达；`Profile.empty()` 不补造任何事实，`profile_json is NULL` 表示未建档。
- **明确无与未知必须可分**：列表中三类字段（`equipment`／`notes`／`red_flags`）与
  `restrictions` 用「显式空集合」或后端 `denied` 表达「明确无」，与字段缺省（未知）不是同一
  语义；`Profile.restrictions` / `reported_red_flags` 便捷访问器会把两者折叠成空元组，判定
  时必须读 `Fact` 三态（`safety.py` 已如此处理）。
- **限制名称不是稳定身份**：前端 `Restriction.name` 只是展示名，身份在后端
  `ActionRestriction.scope`（`specific_action` = `exercises.id` 稳定身份；`movement_pattern` =
  13 项已拍模式词表原词）＋ `target`。S2-07 的 DTO 映射必须按身份对齐，禁止把 `name` 当身份
  或把身份反推成名称。
- **部分档案不得显示成完整档案**：`GET /api/profile` 的后端 DTO 需显式表达「未建档」
  （`profile: null`）与「已建档但事实缺失」。前端 `Profile` 的字段均为非可选（`goal: string`、
  `weekly_frequency: number`、`equipment: string[]`），**S2-07 必须定义部分档案的传输表达**
  （建议：草稿/正式档案一律只发已收集字段，缺省即未知；不得用 `0`／`[]` 填满），这是本阶段
  未落实的映射缺口，记入 §6。
- **伤病事实≠安全许可**：`physical_state.red_flags` 非空只是用户报告原文（含清单外文本），
  不表示判定为不安全；为空表示明确无症状，也不表示已获训练许可。

## 3. 调用路径映射（本阶段端点 → 应用层 → 领域／存储）

| 接口 | 应用层归属 | 领域／存储入口 | 备注 |
|---|---|---|---|
| `GET /api/profile` | S2-07（`api/routes_readonly.py`） | `ProfileService.read_formal_profile` → `ProfileRepo.read` | 只读；未建档发 `profile: null` + `restrictions: []` + `context_version` |
| `GET /api/sessions/{session_id}/drafts` | S2-03/S2-07（`api/routes_drafts.py`） | S2-02 草稿 repo（`business_drafts`） | 会话已持久化草稿的当前状态，不依赖历史通知 |
| `GET /api/drafts/{draft_id}` | S2-03/S2-07 | 草稿 repo + Diff 计算 | 单草稿查询为本阶段补充接口 |
| `POST /api/drafts/{draft_id}/revise` | S2-04（`app/drafts.py`） | 只改 Pending 草稿：结构复查 → revision+1 → 重算拟议结果与 Diff | 正式档案与业务版本不变 |
| `POST /api/drafts/{draft_id}/confirm` | S2-05（`app/confirm.py`） | 幂等返回 → 拒绝 Discarded → 基线／revision 检查 → 领域复查（含 §5 首次建档完整性） → `ProfileService.write_profile_in_transaction` + `context_version +1` + 草稿 Committed | 事务内只用 `Database.transaction()` 的 `conn`，不调用自取锁的 service 查询方法 |
| `POST /api/drafts/{draft_id}/discard` | S2-04 | 草稿状态 → Discarded | 正式事实与版本不变；Committed 不可撤销 |
| `POST /api/drafts/{draft_id}/recalc` | **本阶段不提供** | — | 重算能力后移 Stage 4；不得发假成功接口 |

草稿创建（内部应用层）不属于 HTTP 面：S2-03 在应用层准备 Pending 草稿，来源关联现有会话／Run
身份，自动化的来源数据在测试库准备（不新增测试专用生产路由）。

## 4. 错误响应映射（前端 `ApiError` ↔ 后端语义）

| 前端 `ErrorCode` | HTTP | 后端语义 | 本阶段归属 |
|---|---|---|---|
| `draft_stale` | 409 | 确认时 `draft.base_business_version != context_version`；`detail` 只报告可核实的字段变化 | S2-06 |
| `draft_modified` | 409 | 确认/纠错携带的所见 `revision` 与库内草稿不符 | S2-04／S2-06 |
| `invalid_request` | 4xx | 非法 JSON／类型、未知字段、不存在身份、非法状态 | S2-04–S2-07 |
| `conversation_busy` | 409 | 全局已有活跃 Run | Stage 4（本阶段不实现） |
| `not_configured` | 409 | 未配置模型 | 设置面（本阶段不实现） |
| `interrupted_by_restart` | — | 重启中断的遗留 Run | Stage 4（本阶段不实现） |

契约差异（S2-04／S2-07 必须处理）：前端 `reviseDraft(draftId, payload)` 目前只传 payload，
S2-04 的纠错请求需**新增所见 revision**；`recalcDraft`（`/recalc`）不在本阶段可用接口内。
响应不得包含凭据配置或内部异常堆栈。

## 5. 首次建档完整性契约（S2-01 本次落实）

**契约（stage2.md §4.3 已拍 1B、§8 已拍补充，及 2026-09-10 用户拍板 A）**：目标、经验、
频率、时长、器械、体重、动作限制、身体状态与症状询问**九项全部要求明确回答**；「未知」不算
完整；缺失时拒绝首次确认且不补造字段；明确回答仍须通过对应字段的类型与领域校验，不能以
「无」替代必需的有效值；完整性仅表示信息齐备，不等于没有症状或已获训练安全许可。

**「明确无」的适用范围（2026-09-10 用户拍板 A，本次修正）**：只有集合／限制类四项——
`available_equipment`、`action_restrictions`、`body_state`、`red_flags`——允许以「明确无」
（后端 `denied` 或显式空集合）满足；`training_goal` 与 `training_experience` **必须是有效
`known` 文本**，`weekly_frequency` / `session_duration_minutes` / `body_weight_kg` 必须是有效
数值。四项之外的「无」不算明确回答，仍计入缺口并拒绝首次确认。

**落实位置（最小改动，复用既有底座，不新增模块／依赖）**：

| 位置 | 新增内容 |
|---|---|
| `domain/profile/schema.py:46` | `FIRST_TIME_REQUIRED_FACT_FIELDS`：九项已拍清单（与 Stage 1 的 `REQUIRED_FACT_FIELDS` 并存、语义不同） |
| `domain/profile/schema.py:62` | `EXPLICIT_NONE_FACT_FIELDS`：可用「明确无」满足的字段，2026-09-10 拍板 A 后仅集合／限制类四项（`available_equipment`、`action_restrictions`、`body_state`、`red_flags`） |
| `domain/profile/schema.py:189` | `Profile.first_time_missing_fields`（按清单顺序给出缺口）、`is_first_time_complete` |
| `domain/profile/rules.py:134` | `missing_first_time_fields(profile)`：先做结构校验再算缺口（非法值不得被算作已回答） |
| `domain/profile/rules.py:140` | `ensure_first_time_complete(profile)`：首次确认入口的完整性门；缺失抛既有 `IncompleteProfile` |

**调用方约定**：S2-05 的首次确认事务在正式档案尚未建立（`ProfileSnapshot.profile is None`）时，
对数据库保存的最终草稿档案调用 `rules.ensure_first_time_complete`，失败即拒绝确认且不写正式
事实；事务编排与 HTTP 错误映射不在 S2-01 范围。Stage 1 的「部分事实可保存」（
`write_profile_in_transaction` 只做结构校验）**保持原样**，不被当作生产确认规则，也不被本契约
取消。

**口径（2026-09-10 用户拍板 A，已拍定）**：`EXPLICIT_NONE_FACT_FIELDS` 仅含集合／限制类四项
（`available_equipment`、`action_restrictions`、`body_state`、`red_flags`）。训练目标与训练经验
按拍板 A 必须是有效 `known` 文本，`denied` 不算回答；三个数值字段（`weekly_frequency`、
`session_duration_minutes`、`body_weight_kg`）的 `denied` 同样不算回答。该口径由
`test_explicit_none_fields_are_exactly_the_collection_fields`、
`test_explicit_none_cannot_replace_a_required_text_value` 与
`test_explicit_none_cannot_replace_a_required_numeric_value` 硬编码锁死，口径漂移会大声失败。

**自动化**：`backend/tests/test_stage2_profile_first_time_complete.py`（39 用例，新增）

| 覆盖 | 用例 |
|---|---|
| 契约内容（九项清单、覆盖全部事实字段、明确无仅限集合／限制类四项） | `test_first_time_contract_covers_the_nine_decided_facts`、`test_explicit_none_fields_are_exactly_the_collection_fields` |
| 全部明确回答可通过并进入后续确认校验 | `test_all_nine_explicit_answers_pass_the_first_time_gate` |
| 每类信息缺失／未知时拒绝首次确认（九项逐项） | `test_unknown_field_blocks_first_time_confirmation` |
| 未回答字段不被补造默认值；全未知档案全缺 | `test_unanswered_fields_are_not_filled_with_defaults`、`test_empty_profile_reports_every_field_missing` |
| 允许「明确无」的字段：明确无 vs 未知的区别 | `test_explicit_none_is_distinct_from_unknown`、`test_known_empty_collection_is_an_explicit_answer` |
| 「无」不能替代必需有效数值（频率／时长／体重） | `test_explicit_none_cannot_replace_a_required_numeric_value` |
| 「无」不能替代必需有效文本（拍板 A：训练目标／训练经验） | `test_explicit_none_cannot_replace_a_required_text_value` |
| 明确回答仍须通过字段类型与领域校验（整数、数值、文本、元组、模式词表） | `test_explicit_answers_still_need_valid_type` |
| Stage 1 部分事实可保存能力与确认门并存 | `test_stage1_partial_profile_stays_saveable_but_is_not_confirmable` |
| 已持久化的部分档案重开后仍不满足确认门，且判定不写库不推进版本 | `test_persisted_partial_profile_still_fails_the_first_time_gate` |
| 未建档（`profile is None`）不得被当成无限制／无症状 | `test_unbuilt_profile_is_not_confirmable` |
| 完整性≠安全许可（已报告红旗仍阻断；清单外症状仍需澄清；限制仍阻断） | `test_reported_red_flag_counts_as_answered_but_still_blocks`、`test_unlisted_symptom_is_answered_but_not_treated_as_safe`、`test_complete_profile_keeps_restriction_blocking` |

自动化命令与结果：`cd backend && .venv/bin/python -m pytest tests/test_stage2_profile_first_time_complete.py -q` →
39 passed（拍板 A 修正后重跑）；全量见 §0 与 worker 报告。类型检查：`pyright domain/profile/schema.py
domain/profile/rules.py tests/test_stage2_profile_first_time_complete.py` → 0 errors。

## 6. 交接与未完成项

- **未做（不属 S2-01）**：草稿表与迁移（S2-02）、草稿创建／查询／Diff（S2-03）、纠错与丢弃
  （S2-04）、确认事务与幂等（S2-05）、过期拦截（S2-06）、业务 API 与前端契约交接（S2-07）、
  Windows 全量自动化证据（S2-08）。
- **S2-07 待定映射缺口**：① 部分档案在 `GET /api/profile` 与草稿 DTO 中的表达（前端 `Profile`
  字段非可选，缺省表达需在交接中统一）；② 纠错请求新增所见 revision 的前端改动清单；
  ③ `Restriction` 的 `note` 字段在后端暂无对应承载（前端可选字段，本阶段不新增业务字段）。
- **S2-05 调用点**：首次确认事务必须调用 `rules.ensure_first_time_complete`（§5），并在
  `409` 错误映射中区分「不完整／非法」与「基线冲突／revision 冲突」。
- **安全询问通俗化交接（§8 已拍 方案 A）**：症状询问就是九项中的 `red_flags` 结构化事实，
  内部字段与领域规则保留原名；面向用户不要求理解「红旗」术语，前端文案改用具体症状询问
  （如「最近训练时有没有胸部不适、晕厥、异常气短等情况？」）。示例不缩减 6 类已拍症状
  清单，未知与明确无仍须区分。文案落地属 S2-07 的「前端待改清单」，真实对话询问归 Stage 4，
  本阶段不提前接入 Agent。
- **拍板 A 已落实（2026-09-10）**：`denied` 可满足范围从「非数值字段」收紧为集合／限制类
  四项（器械、动作限制、身体状态、红旗）；训练目标与训练经验必须是有效 `known` 文本。
  原「口径待复核」项已由用户拍板 A 关闭；S2-02 起无遗留口径疑问。
- **Windows**：未运行，阶段门槛仍待 Windows 全量自动化。
