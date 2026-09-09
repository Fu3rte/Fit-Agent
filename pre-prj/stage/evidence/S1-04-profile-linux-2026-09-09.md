# S1-04 档案事实与拟议补丁隔离（Linux/WSL2）

> 子任务：Stage 1 S1-04。只交付领域结构、正式档案读取、拟议补丁校验与纯内存应用、最小
> 内部写入能力（复用外层事务）与离线测试。**不建表、不新增迁移、不接 HTTP／Agent／CLI、
> 不推进 `context_version`、不提供正式档案写入旁路。**
> 验收对照：stage1.md §5 S1-04 验收 1–5 与「验证」；正本依据 02 章 2.1–2.4、01 章 1.2–1.5、
> design-decisions 架构不变量、S1-01 证据 §5.2／§6、S1-03 证据 §6（共享契约）。

## 0. 结论摘要

- 新增 `backend/domain/profile/{schema,rules,repo,service}.py`（占位 docstring → 实现）与
  4 个测试模块，共 **79 个新用例**；全量 **189 passed**，退出码 0。
- 共享契约按 S1-03 证据 §6 复用，未另造词表：`domain.actions.rules.MODE_VOCABULARY` /
  `validate_modes` / `InvalidMode`、`domain.actions.service.ActionCatalogService`。
- 未新增迁移、未新增依赖、未新增业务模块目录；`profile_json` 已承载全部档案事实，
  因此**不需要**新表（与任务预期一致）。
- 生产调用点写入旁路检查：`user_profile` 的 INSERT/UPDATE/DELETE 只出现在
  `domain/profile/repo.py`（一条 UPDATE，且必须在 `Database.transaction()` 内）。
- 未拍事项未擅定：除 `body_weight_kg` 外不新增必填规则、不新增医学阈值、不新增红旗解除
  语义、不新增限制状态语义。

## 1. 交付物

| 文件 | 职责 |
|---|---|
| `backend/domain/profile/schema.py` | 三态事实 `Fact`、档案 `Profile`、`ProfileSnapshot`、两类动作限制 `ActionRestriction`、长期补丁 `ProfilePatch`、当次条件 `SessionConditions`、`profile_json` 编解码 |
| `backend/domain/profile/rules.py` | 结构校验、缺失表达、限制结构校验、补丁校验、纯内存应用 `apply_patch` |
| `backend/domain/profile/repo.py` | `user_profile` 读取（档案 + `context_version` 同一快照）与事务内写入 |
| `backend/domain/profile/service.py` | 正式档案读取、补丁校验（含动作身份引用）、拟议条件预览、事务内写入编排 |
| `backend/tests/test_stage1_profile_facts.py` | 验收 1：三态、不补造、体重必填、结构校验、红旗承载、JSON 编解码 |
| `backend/tests/test_stage1_profile_patch.py` | 验收 2、3：补丁预览不改档案与版本、输入不变性、错误补丁不部分修改、当次条件分流 |
| `backend/tests/test_stage1_profile_restrictions.py` | 验收 4：两类限制表示/引用校验/跨重开保留、无状态语义、删除只作拟议 |
| `backend/tests/test_stage1_profile_write.py` | 验收 5：外层事务边界、版本不递增、回滚不变、写入旁路扫描 |

## 2. 结构说明

### 2.1 三态事实与 `profile_json`

每个档案事实是一个 `Fact`：`unknown`（尚未收集）／`denied`（用户明确否认）／`known`
（已收集值）；`denied` 与 `unknown` 都不带值且可区分，`known` 必须带值。字段固定为
`FACT_FIELDS` 九项：`training_goal`、`training_experience`、`weekly_frequency`、
`session_duration_minutes`、`available_equipment`、`action_restrictions`、`body_state`、
`red_flags`、`body_weight_kg`（与 02 2.1 事实类别逐项对应）。

`profile_json` 形状（`user_profile.profile_json`，TEXT）：

```json
{
  "training_goal": {"state": "known", "value": "增肌"},
  "training_experience": {"state": "unknown", "value": null},
  "weekly_frequency": {"state": "known", "value": 3},
  "session_duration_minutes": {"state": "unknown", "value": null},
  "available_equipment": {"state": "denied", "value": null},
  "action_restrictions": {"state": "known", "value": [
    {"scope": "specific_action", "target": "barbell-back-squat"},
    {"scope": "movement_pattern", "target": "深蹲"}
  ]},
  "body_state": {"state": "unknown", "value": null},
  "red_flags": {"state": "denied", "value": null},
  "body_weight_kg": {"state": "known", "value": 70.0}
}
```

- 读取时缺字段、含未登记字段、状态非法、`denied` 带值、类型不符一律抛
  `InvalidProfileRow`（档案数据损坏，不静默补默认值）。
- 缺失表达：`Profile.missing_required_fields` 只按 `REQUIRED_FACT_FIELDS=("body_weight_kg",)`
  计算；只有 `known` 满足（`denied` 不构成数值事实）。`ensure_complete_profile` 在缺失时抛
  `IncompleteProfile`——**缺失时不生成完整档案、不填默认值**。

### 2.2 两类动作限制

`ActionRestriction(scope, target)`，只有两个字段，**没有**观察中／暂禁／永久等状态字段：

- `scope="specific_action"`：`target` 是动作稳定身份 `exercises.id`，引用校验经
  `ActionCatalogService.get_by_id`（停用动作仍可引用，符合「停用不删除」）。
- `scope="movement_pattern"`：`target` 取 `MODE_VOCABULARY` 13 项原词，经
  `validate_modes` 校验；越界抛 `InvalidRestriction`。
- 两类可同时存在、可分别校验；限制是否命中动作（集合交集）**不在此实现**，归 S1-05。

### 2.3 长期补丁与当次条件分流

- `ProfilePatch(facts, add_restrictions, remove_restrictions)`：长期拟议变更。`facts` 只允许
  `known`／`denied`（补丁不表达「重新变为未知」）；限制变更只能用 add/remove 列表表达，
  删除**只能**作为拟议变更存在。补丁结构与限制引用不合法即整体抛
  `InvalidProfilePatch`／`UnknownExerciseReference`，不产生部分修改。
- `SessionConditions(available_equipment, red_flags)`：当次条件（「今天只能用哑铃」）。与
  `ProfilePatch` 字段集不相交；把当次条件传给补丁接口在**触碰数据库前**抛 `TypeError`。
  本阶段只处理已被上游分流的结构化输入，不解析自然语言意图。
- `apply_patch(profile, patch)` 是纯函数：先整体校验再构造新档案，绝不修改输入对象、不访问
  数据库；无关补丁不会把「未收集限制」写成「明确无限制」。

### 2.4 读取与内部写入

- `ProfileService.read_formal_profile()` → `ProfileSnapshot(profile, context_version)`：
  同一快照读取；未建档（`profile_json IS NULL`）时 `profile is None`。
- `ProfileService.preview_patch(patch)`：读正式档案 → 纯内存应用 → 返回拟议条件；不写库。
- `ProfileService.write_profile_in_transaction(conn, profile)` / `ProfileRepo.write_in_transaction`：
  只接受外层 `Database.transaction()` 的连接（不在事务内即 `RuntimeError`），只发一条
  `UPDATE user_profile SET profile_json = ? WHERE id = 1`，**不自行 BEGIN/COMMIT、不改
  `context_version`**；版本递增与草稿状态变更由 Stage 2 确认事务在同一事务内完成（01 1.4）。

## 3. 已拍口径落地对照

| 已拍口径（2026-09-09） | 落地 | 证据 |
|---|---|---|
| `body_weight_kg` 完整档案必填，缺失不生成完整档案、不填默认值 | `REQUIRED_FACT_FIELDS=("body_weight_kg",)`；`missing_required_fields`/`is_complete`/`ensure_complete_profile` | `test_body_weight_kg_is_the_only_required_fact`、`test_denied_is_not_unknown_and_not_a_required_value` |
| 未知与明确否认可区分；不补造训练经验、身体状态、「无红旗」 | `Fact` 三态；`Profile.empty()` 全 unknown；`denied` 不等于 unknown | `test_fact_states_are_three_and_carry_values_only_when_known`、`test_empty_profile_fabricates_nothing`、`test_red_flags_carry_reports_and_unknown_is_not_denied` |
| 限制只保存当前有效，不新增观察中／暂禁／永久 | `ActionRestriction` 只有 `scope`/`target` | `test_restriction_carries_no_status_semantics` |
| 删除只能表达为拟议变更，不直接生效 | `ProfilePatch.remove_restrictions` + 预览不落库 | `test_removal_is_only_a_proposed_change` |
| 红旗只承载与三态表达，清单外只返回未知/需澄清（判定归 S1-05） | `RED_FLAG_KINDS` 6 项原词；`unlisted_red_flag_labels` 只返回原文，不做判定 | `test_red_flag_kinds_are_the_six_decided_labels`、`test_unlisted_symptom_text_is_carried_without_safety_verdict` |
| 「今天只能用哑铃」不进入长期补丁、不改长期档案 | `SessionConditions` 与 `ProfilePatch` 类型分离；传入补丁接口即 `TypeError` | `test_session_conditions_are_not_a_long_term_patch`、`test_preview_rejects_session_conditions_before_touching_database` |
| 版本推进归 Stage 2 | 读取只读 `context_version`；写入 SQL 不含该列 | `test_profile_module_never_writes_context_version`、`test_write_never_changes_existing_context_version` |
| 其余必填阈值／默认处方条件未拍 | 只做类型与结构校验，无数值范围/阈值规则 | `test_invalid_profile_values_are_rejected`（类型面）；见 §6 |

## 4. 验收 1–5 逐条对照

| 验收（stage1.md §5 S1-04） | 覆盖用例 |
|---|---|
| 1. 未知与明确否认可区分；不补造；`body_weight_kg` 必填，缺失不生成完整档案、不填默认值；其余必填不擅定 | `test_fact_states_are_three_and_carry_values_only_when_known`、`test_empty_profile_fabricates_nothing`、`test_denied_is_not_unknown_and_not_a_required_value`、`test_body_weight_kg_is_the_only_required_fact`、`test_valid_profile_structure_passes`、`test_invalid_profile_values_are_rejected`(14)、`test_profile_from_json_rejects_corrupt_payloads`(14)、`test_red_flags_carry_reports_and_unknown_is_not_denied` |
| 2. 「以后只能用哑铃」拟议条件后正式档案与版本原样；错误补丁不部分修改输入对象或数据库 | `test_proposed_patch_preview_keeps_formal_profile_and_version`、`test_preview_without_formal_profile_returns_proposal_only`、`test_apply_patch_does_not_modify_input_objects`、`test_invalid_patches_are_rejected_before_any_change`(7)、`test_invalid_patch_does_not_partially_write_database` |
| 3. 「今天只能用哑铃」不进入长期补丁；不声称解析自然语言意图 | `test_session_conditions_are_not_a_long_term_patch`、`test_preview_rejects_session_conditions_before_touching_database`、`test_session_condition_shape_is_disjoint_from_patch_and_profile`、`test_today_only_condition_does_not_enter_patch_or_formal_profile`、`test_session_conditions_validate_structure` |
| 4. 具体动作／动作模式限制分别表示、校验引用、跨重开保留；无状态语义；删除只作拟议 | `test_two_restriction_scopes_are_representable`、`test_restriction_carries_no_status_semantics`、`test_mode_restriction_must_come_from_the_decided_vocabulary`、`test_specific_action_reference_is_checked_against_catalog`、`test_restrictions_survive_database_reopen`、`test_restriction_json_roundtrip_keeps_scope_and_target`、`test_removal_is_only_a_proposed_change`、`test_unrelated_patch_does_not_fabricate_no_restrictions` |
| 5. 内部写入复用外层事务、不自行提交、不无条件递增版本；异常回滚后档案与版本均不变 | `test_write_is_rejected_outside_an_outer_transaction`、`test_write_commits_with_outer_transaction_and_keeps_version`、`test_write_never_changes_existing_context_version`、`test_rollback_leaves_profile_and_version_unchanged`、`test_structural_validation_happens_before_write`、`test_no_profile_write_bypass_in_production_code`、`test_profile_service_is_not_wired_into_api_app_or_runtime` |

「验证」项对照：有效/非法结构（`test_valid_profile_structure_passes` / `test_invalid_profile_values_are_rejected`）、
缺失与明确否认（§4 验收 1 用例）、补丁前后快照（`test_proposed_patch_preview_keeps_formal_profile_and_version`）、
输入不变性（`test_apply_patch_does_not_modify_input_objects`）、临时库重开（`test_restrictions_survive_database_reopen`、
`test_write_commits_with_outer_transaction_and_keeps_version`）、回滚（`test_rollback_leaves_profile_and_version_unchanged`）、
生产调用点无写入旁路（`test_no_profile_write_bypass_in_production_code`）。

## 5. 实测命令与结果（2026-09-09，Linux/WSL2）

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 1 | `cd backend && timeout 300s .venv/bin/python -m pytest tests -q -W error::pytest.PytestUnhandledThreadExceptionWarning` | 0 | `189 passed in 2.89s`（基线 110 + 新增 79） |
| 2 | `timeout 120s .venv/bin/python -m pytest tests --collect-only -q` | 0 | `189 tests collected` |
| 3 | 同上仅 4 个 S1-04 模块 | 0 | `79 passed in 0.57s` |
| 4 | 临时库探针（`Database` + `ProfileService`） | 0 | `fresh: ProfileSnapshot(profile=None, context_version=0)`；写入后 `version=0 restrictions=(movement_pattern 深蹲,) complete=True`；预览后 `formal_equipment=unknown version=0 proposed_equipment=known(("哑铃",))`；回滚后 `profile_equal=True version=0` |
| 5 | `grep -rniE "(insert into|update|delete from|replace into)\s+user_profile" --include='*.py' domain storage api app runtime config.py main.py` | 0 | 仅 `domain/profile/repo.py:64`（一条 UPDATE） |
| 6 | `git diff --cached --name-only` / `git status --porcelain -- backend` | 0 | 暂存区为空；本任务改动为 `domain/profile/*.py`(4) + `tests/test_stage1_profile_*.py`(4)；`domain/actions/*`、`tests/test_db.py` 等为 S1-02／S1-03 既有未提交改动，本任务未触碰 |
| 7 | `git status --porcelain -- frontend` | 0 | 9 条，全部为前端 mock 并行轨道改动，本任务未触碰 |

## 6. 未覆盖范围与残留风险

- **判定类能力未实现**：限制是否命中动作（模式集合交集）、红旗是否阻断、清单外症状的
  「未知/需澄清」结论均归 S1-05；本模块只提供结构与引用校验。
- **数值范围与医学阈值未实现**：只做类型校验；`weekly_frequency`／`session_duration_minutes`／
  `body_weight_kg` 的取值范围未拍，未新增任何阈值规则。
- **红旗解除、限制状态语义未实现**：无自动解除能力，无观察中／暂禁／永久字段。
- **未接线的调用方**：`ProfileService` 尚未被 `api`／`app`／`runtime` 引用（本阶段禁止接线，
  由 `test_profile_service_is_not_wired_into_api_app_or_runtime` 守住边界）；`context_version`
  推进入口不存在（归 Stage 2 确认事务）。
- **未做**：HTTP／Agent／CLI 接口、草稿表与确认流程、`proposed_profile_patch_json` 落库、
  左右侧与记录写入、Windows 实测（本阶段结项门槛由 S1-07 汇总）。
- **风险**：`profile_json` 键集为固定九项，后续若新增档案事实需同步 `FACT_FIELDS` 与
  编解码；旧库中任意 JSON（如 `{"goal": "力量"}`）经本模块读取会抛 `InvalidProfileRow`
  （视为数据损坏而非静默降级），正式建档路径接入前不会自动遇到。

## 7. needsDecision

无。本任务未触发「新增依赖／业务模块／迁移／新表」「新增业务必填规则／医学阈值／红旗解除
语义」「与 02 章正本冲突」任一停手条件；`profile_json` 已承载全部档案事实，无需新迁移。
