# 评审规则

## 输入

评审读取只读事实：用户 `request`、待确认计划内容、可信 `business_date`、`get_profile` 返回的画像、
`get_current_plan` 的当前计划、`list_workouts` 的近期训练，以及 `search_exercises` 与 `get_exercise_details`
的目录事实。模型只依据这些事实判断，计划里的声明不构成用户事实。

## 三个维度

每个维度只给结论与理由，不返回分数、权重、总分或新阈值：

- `goal_alignment`：计划目标是否符合用户请求与画像目标。
- `schedule_reasonableness`：训练日与休息日的安排是否与画像、训练记录、目录事实自相矛盾。
- `explanation_quality`：计划的 `notes` 是否说明安排采用的事实依据、适用前提（在哪些条件下成立），以及
  仍缺哪些证据、哪些部分只能保持未知。前提与限制来源见
  [performance-factors.md](../../training-theory/references/performance-factors.md)，用词见
  [user-facing-language.md](../../training-theory/references/user-facing-language.md)。

## 输出形态

- 三个维度分别给出「符合」或「不符合」与一句理由。
- 结论只作为内容检查意见，不改写计划、不产生新版本、不承诺保存结果。
- 依据不足时明确写「未知」，并说明缺哪条事实，不补造。

## 边界

- 不计算业务事实：PB、趋势与完成率不由本 Skill 计算。
- 不把 RIR／RPE 写成处方刻度，不输出估算 1RM、训练容量与完成率；解释概念时按 training-theory 的口径：
  处方变量范围见 [training-variables.md](../../training-theory/references/training-variables.md)，用词见
  [user-facing-language.md](../../training-theory/references/user-facing-language.md)。
- 不引入外部来源或示例数值充当用户事实。
