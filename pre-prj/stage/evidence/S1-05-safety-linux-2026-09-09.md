# S1-05 动作限制与红旗安全校验（Linux/WSL2）

> 子任务：Stage 1 S1-05。只交付**本地确定性校验**：把正式限制、拟议条件和当次状态作为
> 显式输入，返回命中的限制与红旗阻断原因。**不接 HTTP／Agent／CLI、不写档案、不新增/删除
> 永久限制、不解除红旗、不实现记录写入、不新增依赖/迁移/业务模块目录。**
> 验收对照：stage1.md §5 S1-05 验收 1–6 与「验证」；正本依据 02 章 2.2–2.4、01 章 1.4、
> design-decisions 架构不变量、S1-03 证据 §6（共享契约）、S1-04 证据（三态事实与 P2 提醒）。

## 0. 结论摘要

- 新增 `backend/domain/profile/safety.py`（纯规则）与 `ProfileService.check_candidate_actions_safety`
  （只读编排），新增测试模块 `backend/tests/test_stage1_profile_safety.py`（**54 个新用例**）。
- 全量 **243 passed**（基线 189 + 新增 54），退出码 0；单跑本模块 54 passed，退出码 0。
- 复审 P1 已修：正式限制未收集（`unknown`）时，触及限制的补丁不再把状态静默升级为
  「已知无限制」；命中集仍按补丁后条件计算（见 §2.5）。
- 共享契约全部复用、未另造：模式词表与校验用 `domain.actions.rules.MODE_VOCABULARY` /
  `modes_for` / `validate_modes` / `InvalidMode`，动作身份用 `domain.actions.schema.Exercise`，
  三态事实与两类限制用 `domain.profile.schema`，结构校验/补丁应用用 `domain.profile.rules`。
- 三态读取按 S1-04 reviewer P2 落地：判定一律读 `Fact`（`unknown` / `denied` / `known`），
  不使用会折叠 unknown 与 denied 的 `Profile.restrictions` / `Profile.reported_red_flags`。
- 未新增依赖、未新增迁移、未新增表、未新增业务模块目录、未改 S1-03／S1-04 已交付语义；
  `git diff --cached` 为空。

## 1. 交付物

| 文件 | 职责 |
|---|---|
| `backend/domain/profile/safety.py`（新增） | 纯规则：`check_action_restrictions`（限制命中）、`assess_red_flags`（三来源红旗评估）、`evaluate_safety`（组合校验）；结果类型 `SafetyCheckResult` / `RestrictionCheck` / `RedFlagCheck` / `RestrictionHit` / `RedFlagFinding`；常量 `RED_FLAG_BLOCK_ADVICE` 与三条「需澄清」原因 |
| `backend/domain/profile/service.py`（追加方法） | `ProfileService.check_candidate_actions_safety(candidate_exercise_ids, *, patch=None, session=None)`：读正式档案同一快照 → 解析候选动作身份 → 调用纯规则；只读 |
| `backend/tests/test_stage1_profile_safety.py`（新增） | 验收 1–6 参数化用例（54 个，含 P1 回归 4 个） |

未改：`backend/domain/profile/{schema,rules,repo}.py`、`backend/domain/actions/**`、
`backend/storage/**`、`backend/tests/test_stage1_profile_*.py`（S1-04 已交付）、Stage 0 测试。

## 2. 结构说明

### 2.1 输入与三态口径

`evaluate_safety(profile, candidate_actions, *, patch=None, session=None)`：

- `profile`：正式档案（`Fact` 三态）。
- `patch`：拟议长期补丁（`ProfilePatch`，纯内存 `apply_patch`，不落库、不改输入对象）。
- `session`：当次条件（`SessionConditions`）；当前只承载当次红旗，器械条件不构成动作限制（02 2.2、2.4）。
- `candidate_actions`：`Exercise` 身份元组（稳定 `id` + `modes`，来自 S1-03 目录契约）。

| 维度 | 事实三态读取 | 语义 |
|---|---|---|
| 动作限制 | 正式事实 `profile.action_restrictions.state`（命中集按 `effective` 补丁后条件） | `unknown` → `needs_clarification`（不得读成「无限制」；补丁触及限制也不升级）；`denied` → 明确无限制；`known` → 用值判定 |
| 红旗 | 正式档案 / 拟议补丁 `facts['red_flags']` / 当次条件 `red_flags` 三来源各自读 | `unknown` → 记入 `unknown_sources`（需澄清）；`denied` → 该来源无红旗；`known` → 逐条分类 |

### 2.2 限制判定口径

「动作的模式集合 ∩ 被限制模式集合 ≠ ∅ 即命中」：`specific_action` 比对 `exercises.id`，
`movement_pattern` 用 `matched_modes = 动作模式集合 ∩ {限制目标}`。多归属动作按每个归属分别
命中（哑铃上斜卧推＝水平推＋垂直推；自重双杠臂屈伸＝垂直推＋肘伸），其余动作不因名称相似
被误报。越界模式与非法候选模式抛 `InvalidRestriction` / `InvalidMode`，不静默当「不命中」。

### 2.3 红旗阻断与来源独立

- 6 类已明确红旗（`RED_FLAG_KINDS`）任一出现 → `red_flags.confirmed` 非空 → `is_blocked` 为真，
  `advice == (RED_FLAG_BLOCK_ADVICE,)`，固定文案「存在已明确红旗症状：不生成常规训练处方，
  建议线下专业评估。」，不含疾病诊断措辞。
- 清单外症状原文 → `red_flags.unlisted` + `clarification_reasons`（「只返回未知/需澄清」），
  不判无红旗、不判安全放行、不扩充医学规则。
- 三来源独立评估：任一来源有红旗，其他来源的 `denied`／`known(())` 不覆盖；补丁把红旗改为
  `denied` 不能清除正式档案已报告的红旗；限制检查通过不消除红旗（02 2.3「不因计划修订自动解除」）。
- 结果类型不提供 `is_safe` 之类的完整安全许可字段；限制未命中且红旗未收集仍 `needs_clarification`。

### 2.4 只读边界

`safety.py` 无任何 IO：不 import `aiosqlite`／`storage`，无 INSERT/UPDATE/DELETE/commit，
无 `clear*` 命名；测试逐项扫描源码与导出名。`check_candidate_actions_safety` 只调
`ProfileRepo.read` 与 `ActionCatalogService.get_by_id`，不调用任何写入方法；临时库实测档案
JSON 与 `context_version` 校验前后逐字节一致。

### 2.5 复审修复（独立 reviewer 轮，2026-09-09）

**P1（已修）：触及限制的补丁把「未收集」静默升级为「已知无限制」。**

- 症状：`Profile(red_flags=Fact.denied())`（`action_restrictions` 为 `unknown`）
  ＋ `ProfilePatch(add_restrictions=(movement_pattern 膝伸,))` ＋ 候选「高位下拉」，
  修复前得到 `restrictions.state=='known'`、`hits==()`、`needs_clarification==False`——
  正式限制从未收集，却得到「无命中、无需澄清」。
- 根因：`evaluate_safety` 直接取 `effective.action_restrictions.state`，而
  `domain.profile.rules.apply_patch` 在补丁涉及限制时无条件写 `Fact.known(proposed)`，
  于补丁路径上把 unknown 提升为 known。
- 修复（只改 `safety.py`，未改 `apply_patch` 与 S1-04 测试）：
  `state = "unknown" if formal_restriction_fact.is_unknown else restriction_fact.state`。
  命中集仍取 `effective`（补丁后条件），因此未知正式限制不会被当 denied 放行。
- 复现探针（修复后，退出码 0）：
  `state=unknown hits=() blocked=False needs_clarification=True`；
  `reasons=('动作限制未收集：不得当作无限制',)`；
  同一补丁下候选「腿屈伸」→ `state=unknown hits=[('腿屈伸', ('膝伸',))]`。
- 回归用例：`test_patch_touching_restrictions_does_not_upgrade_unknown_to_known`、
  `test_unknown_formal_restrictions_still_hit_under_post_patch_conditions`、
  `test_unrelated_patch_keeps_unknown_restrictions_unknown`；
  正式 `denied` ＋补丁加限制仍为 `known`（由 `test_post_patch_conditions_reveal_conflict_missed_by_old_profile` 继续锁定）。

**P2（已处理）：**

- 交接补充：红旗阻断只认 `red_flags` 事实（长期／拟议／当次）；`body_state` 是文本事实，
  不参与阻断，自然语言症状识别未实现。用例：`test_body_state_text_does_not_block_or_clear_red_flags`。
- 措辞收敛：`test_safety_module_has_no_write_or_red_flag_clearing_capability` 与
  `test_safety_module_contains_no_record_write_flow` 是**结构性防护**（无写入 SQL/IO、
  无解除 API、无记录写入函数），不等于「已用行为测试验证解除流程会被拒绝」；解除语义未拍，
  本阶段不提供解除能力也不验证解除行为。

## 3. 验收 1–6 对照

| 验收 | 落地 | 代表用例 |
|---|---|---|
| 1. 具体动作命中；模式交集覆盖多归属；未命中不误报；未命中≠完整许可 | `check_action_restrictions`（id 相等 / 交集）+ 正式事实三态 | `test_pattern_restriction_hits_every_action_sharing_the_mode`(3)、`test_specific_action_restriction_hits_that_identity_only`、`test_multi_mode_actions_hit_each_belonging_pattern`(4)、`test_non_matching_action_is_not_reported`(5)、`test_multiple_restrictions_report_only_covered_actions`、`test_restriction_clearance_is_not_full_safety_clearance`、`test_unknown_restrictions_are_not_read_as_no_restrictions`、`test_denied_restrictions_are_distinct_from_unknown`、`test_patch_touching_restrictions_does_not_upgrade_unknown_to_known`、`test_unrelated_patch_keeps_unknown_restrictions_unknown`、`test_invalid_pattern_restriction_is_rejected_not_silently_ignored`、`test_candidate_modes_must_be_in_shared_vocabulary` |
| 2. 6 类红旗结构化样例阻断 + 专业评估提示 + 不诊断；清单外只「未知/需澄清」 | `assess_red_flags` / `RED_FLAG_BLOCK_ADVICE` | `test_each_listed_red_flag_blocks_prescription_with_offline_evaluation`(6)、`test_red_flag_advice_is_fixed_text_without_disease_diagnosis`、`test_unlisted_symptom_only_returns_clarification`、`test_listed_and_unlisted_reports_are_kept_apart`、`test_denied_red_flags_need_no_clarification`、`test_body_state_text_does_not_block_or_clear_red_flags` |
| 3. 任一来源红旗不被其他来源「无红旗」覆盖；限制通过不消除红旗 | 三来源独立评估 | `test_other_source_without_red_flag_cannot_override`(3)、`test_proposed_patch_denial_cannot_clear_formal_red_flag`、`test_proposed_patch_red_flag_blocks_even_when_formal_denies`、`test_passing_restriction_check_does_not_clear_red_flag`、`test_unknown_session_red_flags_need_clarification`、`test_each_source_reports_its_own_red_flag` |
| 4. 不写档案、不增删永久限制、不解除红旗、无自动解除能力 | 纯函数 + 只读 service（行为）；源码扫描（结构性防护） | `test_evaluation_does_not_mutate_inputs`、`test_service_check_does_not_write_profile_or_version`（临时库，行为）；`test_safety_module_has_no_write_or_red_flag_clearing_capability`（结构：无写入 SQL/IO、无解除 API） |
| 5. 阻断处方/指导，不阻断历史读取/已发生事实；不实现记录写入 | 结果类型无历史阻断字段；文案限定处方 | `test_result_exposes_only_prescription_blocking_outputs`、`test_advice_scope_is_prescription_not_history`、`test_safety_module_contains_no_record_write_flow`（结构：无记录写入流程） |
| 6. 用拟议补丁后的条件校验，能发现只看旧档案会漏的冲突 | `apply_patch` → 限制判定（命中集按补丁后，状态按正式事实） | `test_post_patch_conditions_reveal_conflict_missed_by_old_profile`、`test_post_patch_specific_action_restriction_hits`、`test_post_patch_removal_is_only_proposed_conditions`、`test_unknown_formal_restrictions_still_hit_under_post_patch_conditions`、`test_seeded_multi_mode_actions_hit_pattern_restriction`（临时库 + 003 种子）、`test_unbuilt_profile_is_unknown_not_safe`、`test_service_rejects_unknown_candidate_identity`、`test_service_session_red_flag_blocks_without_writing` |

「验证」项额外覆盖：参数化规则测试（限制/模式/多限制/两来源/补丁后条件/输入不变性）；
未知状态未被任何断言写成「安全」——`test_restriction_clearance_is_not_full_safety_clearance`
与 `test_unbuilt_profile_is_unknown_not_safe` 断言的是 `needs_clarification` 与
`unknown_sources`，不是放行。

## 4. 实跑命令与结果

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `cd backend && timeout 300s .venv/bin/python -m pytest tests/test_stage1_profile_safety.py -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | 0 | `54 passed in 0.64s`（P1 修复前为 50 passed） |
| 2 | `cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning`（全量） | 0 | `243 passed in 3.78s`（189 基线 + 54 新增；P1 修复前为 239 passed） |
| 3 | `timeout 60s .venv/bin/python -m pytest tests/test_stage1_profile_safety.py -q --collect-only` | 0 | `54 tests collected` |
| 4 | 导入探针（`from domain.profile import safety, service`） | 0 | `safety is domain.profile.safety → True`；`check_candidate_actions_safety` 存在 |
| 5 | 纯规则探针（限制 垂直推＋补丁加 膝伸；候选 哑铃上斜卧推／腿屈伸；当次红旗 denied；正式红旗 晕厥） | 0 | `hits=[('哑铃上斜卧推', ('垂直推',)), ('腿屈伸', ('膝伸',))]`；`state=known blocked=True`；`confirmed=(RedFlagFinding('formal_profile','晕厥'),)`；`advice=('存在已明确红旗症状：不生成常规训练处方，建议线下专业评估。',)`；`unchanged=True` |
| 6 | P1 复现探针（正式限制 unknown ＋ 补丁加 膝伸；候选 高位下拉／腿屈伸） | 0 | 高位下拉：`state=unknown hits=() blocked=False needs_clarification=True`、`reasons=('动作限制未收集：不得当作无限制',)`；腿屈伸：`state=unknown hits=[('腿屈伸', ('膝伸',))]` |
| 7 | `git diff --cached --name-only` | 0 | 输出为空（无暂存文件） |
| 8 | `git status --porcelain -- backend pre-prj/stage/evidence` | 0 | 本任务新增 `backend/domain/profile/safety.py`、`backend/tests/test_stage1_profile_safety.py`；`backend/domain/profile/service.py` 含本任务追加方法；其余为 S1-02～S1-04 既有工作区改动 |

环境：WSL2 Linux；HEAD `dd168bb`；Python 3.13.15；pytest 9.1.1；未安装新依赖。

## 5. 未覆盖范围与残留风险

- **Windows 未实测**：本文件全部为 Linux/WSL2 证据；结项门槛（方案 A）要求同版本 Windows
  全量自动化 + 隔离库人工实测。
- **事务内复查未实现**：S1-05 只提供本地确定性校验函数；「确认事务内按最终草稿复查」归
  Stage 2 接线（01 1.4）。
- **拟议删除限制的生效顺序**：本函数按补丁后条件判定，因此补丁删除限制后候选动作不再命中。
  限制删除与计划提交必须同事务原子生效；若 Stage 2 只提交计划而不提交补丁，就会违反正式限制。
  该顺序责任在确认事务，S1-05 不代替。
- **自然语言症状识别未实现**：输入按结构化文本处理，清单外文本只归入「未知/需澄清」，
  不做医学推断，也不扩充医学规则（未拍）。
- **红旗解除无策略**：未拍解除语义，故只提供「不自动解除」；任何解除流程属后续拍板事项。
- **器械可用性不构成动作限制**：当次器械条件（`SessionConditions.available_equipment`）只做
  结构校验，不参与 S1-05 命中判定；替代建议归第 4 章后续阶段。
- **未接 HTTP／Agent／CLI**：`domain/profile` 未被 `app/`、`runtime/` 引用；无只读 API。
- **可推荐标记仍全 0**：S1-05 不检查 `recommendable`，不声称目录已可用于计划生成。

## 6. 交接要点（Stage 2–4 接线）

| 入口 | 必须由谁调用 | 说明 |
|---|---|---|
| `ProfileService.check_candidate_actions_safety(ids, *, patch, session)` | Stage 2 确认事务 / Stage 3 生成与复核 | 只读校验；候选身份须存在于目录（含停用动作），未知身份抛 `UnknownExerciseReference` |
| `domain.profile.safety.evaluate_safety(profile, actions, *, patch, session)` | 需要纯函数复查的调用方（事务内、无 IO） | 不读库；输入对象不被修改 |
| `RED_FLAG_BLOCK_ADVICE` / `clarification_reasons` | 提示层 | 红旗提示固定为「线下专业评估」；`needs_clarification` 非空即不得当作安全放行 |
| 红旗输入面 | 上游分流（建档／确认事务／当次条件） | 红旗必须落在 `red_flags` 事实（长期／拟议／当次三来源）；`body_state` 文本不参与阻断，自然语言症状识别未实现，清单外原文只归入「未知/需澄清」 |
| 限制状态读取 | 调用方 | 命中集按补丁后条件；`restrictions.state` 取正式事实三态，正式未收集（`unknown`）时不得因补丁触及限制当作「已知无限制」，`needs_clarification` 非空即不得放行；直接读 `preview_patch(...).action_restrictions` 的调用方必须另判正式三态（`apply_patch` 在补丁触及限制时把该字段写成 `known`） |
| 正式档案写入 / 限制增删 / 版本推进 | Stage 2 确认事务 | S1-05 不写、不推进、不解除 |
