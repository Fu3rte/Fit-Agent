---
name: plan-evaluation
description: 对待确认训练计划做定性内容检查，从目标一致性、安排合理性、解释质量三项给出结论与理由；依据来自本次真实画像、当前计划、训练记录与动作目录事实，缺少依据时标记未知。只作内容检查意见，不保存、不改写计划、不做数值评分、不阻断保存，也不做医疗判断。
---

# plan-evaluation：计划评审 Skill

本 Skill 提供**模型指令**。它只约束「独立读事实 → 三维度定性检查 → 写理由」这一段，不持有保存门禁，也不
调度后台任务。

Fit-Agent 是单一 Agent，没有按意图分发的多个角色，也没有独立的评审角色或专用工具组。评审结论只作为内容
检查意见供后续整理参考。

## 检查维度

| 维度 | 检查什么 |
| --- | --- |
| `goal_alignment` | 计划的目标与画像目标、用户本次请求是否一致 |
| `schedule_reasonableness` | 训练日与休息日的安排同画像、训练记录与目录事实是否自相矛盾 |
| `explanation_quality` | 计划的 `notes` 是否说清安排依据、适用前提与仍缺的证据 |

每个维度只给结论与理由，不写数值评分、维度权重或总分。理由写清是哪条事实支撑判断。

## 输入事实

按需读取只读工具：`get_profile`（目标与已知限制）、`get_current_plan`（当前循环计划）、`list_workouts`
（近期训练事实）、`search_exercises` 与 `get_exercise_details`（目录事实）。判断依据只来自本次工具返回的
事实，候选计划的声明不当作已核实的用户事实。

缺少依据时明确标记为未知，不补造事实，也不从计划的自我声明反推。

## 边界

- 不改写计划、不产出新的计划版本；需要修改时由用户在计划流程里重新准备。
- 不保存、不激活、不归档：完整展示后由用户点击快照的确认按钮，独立确认 HTTP 入口负责保存；普通聊天文字
  不产生授权。
- 不做医疗判断；出现明确医疗风险时停止相关训练建议并提示就医。
- 不引入新的数值阈值：间隔天数、组数锚点、次数区间、完成率门槛一律不设。
- 不把 RIR／RPE 写成处方刻度，不输出估算 1RM、训练容量与完成率；用词见
  [user-facing-language.md](../training-theory/references/user-facing-language.md)。

## 参考

- [evaluation-rubric.md](references/evaluation-rubric.md)：三个维度的检查口径与前提核查。
- [few-shots.md](references/few-shots.md)：三项检查的示例。
- [training-variables.md](../training-theory/references/training-variables.md)：训练变量口径。
- [performance-factors.md](../training-theory/references/performance-factors.md)：前提与限制来源。
