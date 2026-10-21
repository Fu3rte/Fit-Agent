# rebuild-v3 正式场景集精简批准记录

## 用户授权

用户在本次对话中明确授权：将默认正式业务场景集从 `rebuild-v2` 的 80 条精简为 40 条，并授权 Agent 按业务覆盖与关键边界选择保留场景，同时同步正式基线、默认入口与发布门禁。

批准对象为 `rebuild-v3` 的 40 条场景集合、类别配额与父版本继承范围。本次授权不包含场景正文、参考答案、评分要点、断言或指标适用性的逐条重新审阅；这些内容继承 `rebuild-v2` 的既有批准。

按现有评测版本规则，新版本保存为 `backend/dataset/evaluation/rebuild-v3/`，父版本为 `rebuild-v2`。`rebuild-v1`、`rebuild-v2`、历史回执与历史成绩保持原值。

## 版本与继承范围

- `dataset_version=rebuild-v3`。
- 父版本：`rebuild-v2`。
- corpus 1321 条、queries 150 条、qrels 1104 条、附件 8 个，均与父版本逐字节一致。
- `scenarios.jsonl` 仅筛选父版本的 40 条原始行，保持原顺序、原 ID、原正文与原审阅记录。
- 查询条件、场景关联与相关性判断不变；150 条查询中 14 个非空 `scenario_id` 全部落在保留集合内。
- 场景 `reference`、`rubric`、`assertions` 与 `metric_applicability` 不变。
- 不新增外部裁判适用性批准文件；旧指标适用性继续按原规则解释。
- Schema、业务来源与未变化文件摘要保持与当前仓库资源一致。

## 保留场景与类别配额

| 类别 | 数量 | 保留原始编号 |
|---|---:|---|
| action_verification | 8 | 01–08 |
| profile_update | 5 | 01, 03, 04, 05, 08 |
| plan_import_file | 4 | 02, 04, 06, 08 |
| incomplete_plan_structure | 2 | 01, 06 |
| plan_generation | 4 | 01, 04, 05, 07 |
| replacement_constraints | 3 | 01, 02, 05 |
| performance_adjustment | 3 | 03, 05, 07 |
| workout_logging_query | 3 | 02, 04, 05 |
| confirmation_cancellation_state | 5 | 03, 04, 05, 07, 08 |
| clarification_authorization_medical_risk | 3 | 02, 04, 07 |
| 合计 | 40 | |

保留 ID：

```text
s-action-verification-01
s-action-verification-02
s-action-verification-03
s-action-verification-04
s-action-verification-05
s-action-verification-06
s-action-verification-07
s-action-verification-08
s-profile-update-01
s-profile-update-03
s-profile-update-04
s-profile-update-05
s-profile-update-08
s-plan-import-file-02
s-plan-import-file-04
s-plan-import-file-06
s-plan-import-file-08
s-incomplete-plan-structure-01
s-incomplete-plan-structure-06
s-plan-generation-01
s-plan-generation-04
s-plan-generation-05
s-plan-generation-07
s-replacement-constraints-01
s-replacement-constraints-02
s-replacement-constraints-05
s-performance-adjustment-03
s-performance-adjustment-05
s-performance-adjustment-07
s-workout-logging-query-02
s-workout-logging-query-04
s-workout-logging-query-05
s-confirmation-cancellation-state-03
s-confirmation-cancellation-state-04
s-confirmation-cancellation-state-05
s-confirmation-cancellation-state-07
s-confirmation-cancellation-state-08
s-clarification-authorization-medical-risk-02
s-clarification-authorization-medical-risk-04
s-clarification-authorization-medical-risk-07
```

## 选择依据

- 保留全部被查询引用的场景，查询、相关性金标与既有审阅保持不变。
- 覆盖全部 10 个业务类别。
- 保留画像首次保存、纯文字无保存权限、限制添加、限制解除与跨会话读取。
- 保留文件保存、纯附件澄清、多文件合并与附件指令隔离。
- 保留不完整计划保存与版本历史。
- 保留默认器械、显式器械、已保存限制与生成计划确认保存。
- 保留器械替换、禁用动作替换与无可用替代时的澄清。
- 保留最近 10 个训练日期、记录修正后重准备与参数保留。
- 保留逐组训练数据、计划与实际分离与记录修订。
- 保留目标快照选择、重复确认、指定取消、失效状态与重启持续。
- 保留医学风险停止、引用文字无授权与限制范围澄清。

## 移出默认运行的场景

以下 40 条仍完整保存在 `rebuild-v2`，显式指定该版本时继续可读取、校验与评测；本轮仅将其移出默认正式运行：

```text
s-profile-update-02
s-profile-update-06
s-profile-update-07
s-plan-import-file-01
s-plan-import-file-03
s-plan-import-file-05
s-plan-import-file-07
s-incomplete-plan-structure-02
s-incomplete-plan-structure-03
s-incomplete-plan-structure-04
s-incomplete-plan-structure-05
s-incomplete-plan-structure-07
s-incomplete-plan-structure-08
s-plan-generation-02
s-plan-generation-03
s-plan-generation-06
s-plan-generation-08
s-replacement-constraints-03
s-replacement-constraints-04
s-replacement-constraints-06
s-replacement-constraints-07
s-replacement-constraints-08
s-performance-adjustment-01
s-performance-adjustment-02
s-performance-adjustment-04
s-performance-adjustment-06
s-performance-adjustment-08
s-workout-logging-query-01
s-workout-logging-query-03
s-workout-logging-query-06
s-workout-logging-query-07
s-workout-logging-query-08
s-confirmation-cancellation-state-01
s-confirmation-cancellation-state-02
s-confirmation-cancellation-state-06
s-clarification-authorization-medical-risk-01
s-clarification-authorization-medical-risk-03
s-clarification-authorization-medical-risk-05
s-clarification-authorization-medical-risk-06
s-clarification-authorization-medical-risk-08
```

## 发布与门禁

- `rebuild-v3` 的正式基线固定为 40 条场景，类别配额为上表数值。
- `rebuild-v1` 与 `rebuild-v2` 的正式基线保持 80 条、每类别 8 条。
- 正式运行仍要求覆盖该版本全部批准场景，不接受任意子集；定向验收保持 6 条。
- 固定取消验收场景 `s-confirmation-cancellation-state-05`、`07`、`08` 全部保留。
- 默认场景运行入口、LangSmith 场景同步/运行、外部裁判上传与反馈回收使用 `rebuild-v3`。
- 显式指定旧版本时继续使用其自身的 80 条分母与身份。
- 历史 80 条运行结果不重标为 `rebuild-v3` 结果。

## 版本身份

- manifest SHA-256：`4c9ad4ff49f7e1e9eef45a42820532256a01ee6100a6706baedf7518f6d7f0c2`
- content SHA-256：`07436be7737d02209bbd616f9028bb0534975640d148c246eba11e8704f40e80`
- corpus SHA-256：`2eb59eb570a796ace0a1778c2ad5f198d49cd60142205e31f00fdd28a3f3fcf4`

## 执行边界

本轮只发布数据版本、正式基线与必要配套，不执行真实评测、模型调用、LangSmith 上传或云端反馈回收。`rebuild-v3` 尚无实际运行结果；不得把历史 80 条成绩或历史回执当作新版本成绩。场景数量减少不等于 token 成本按比例下降，未测量前不给出节省比例。
