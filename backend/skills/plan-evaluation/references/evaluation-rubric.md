# 评审规则

## 输入

评审环节在确定性校验通过后运行。模型输入包含用户 `request`、候选 `plan`、`business_day`、本 Skill、`evaluation_tools` 返回的 `facts`，以及可选的历史消息。

模型仅依据 `facts` 判定。候选计划中的声明不构成用户事实。

## Rubric

`RubricResult` 只有三个维度，每个维度只返回 `passed` 与 `reason`：

- `goal_alignment`：硬门槛。候选目标必须符合用户请求与 `read_user_profile` 的目标事实。
- `schedule_reasonableness`：硬门槛。七天安排不得与画像、日历、训练历史和目录事实冲突。
- `explanation_quality`：建议项。解释应说明安排采用的事实依据。

`explanation_quality` 额外核查两项：解释是否说清该安排的适用前提（在哪些条件下成立），以及是否说明
仍缺哪些证据、哪些部分只能保持未知。前提与限制来源见
[performance-factors.md](../../training-theory/references/performance-factors.md)，用词见
[user-facing-language.md](../../training-theory/references/user-facing-language.md)。

维度仍只返回 `passed` 与 `reason`：模型不输出分数、权重、总分或新阈值。

## 结果合成

确定性代码负责合成 `EvaluationResult`：

- `passed` 要求确定性结果、`goal_alignment` 和 `schedule_reasonableness` 全部通过。
- 两个硬门槛的失败进入 `blocking_failures`。
- `explanation_quality` 失败只进入 `warnings`。
- `evidence` 保存本轮评审 Tool 的 revision 证据。

评审环节不修改 `PlanDraft`。一次修订由计划流程交回计划生成环节；第二次失败进入丢弃路径。

## Revision 边界

`snapshot_mismatch_failures` 比较出候选时读取的与本次评审读到的 `(tool_name, revision_domain, revision)`。revision 不一致时，确定性结果产生 `snapshot_mismatch` 并阻断本轮。模型不核对、不重试、不消解该失败。

## 写入边界

评审环节只持有模型与只读事实依赖。计划持久化、激活和归档属于后续写入节点及用户确认流程。

## 来源

采用 `Lzheng-fitness/skills/lzheng-fitness-plan/references/plan-contract.md` 中“结构化候选是唯一计划数据源、先验证再输出”的原则，以及 `evidence-base.md` 中“当前可核验事实优先、缺失事实保持未知”的原则。

HTML 输出协议、周期文件、肌群折算、百分比调重与外部来源目录不进入本 Skill；不把 RIR／RPE 写成处方
刻度，不输出估算 1RM、训练容量、完成率，解释概念时按 training-theory 的口径：处方变量范围见
[training-variables.md](../../training-theory/references/training-variables.md)，用词见
[user-facing-language.md](../../training-theory/references/user-facing-language.md)。
