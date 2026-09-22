---
name: workout-planning
description: 生成七天训练计划草稿：用户请求新建训练计划时加载，先经 planning_tools 读齐画像、最近训练、进展与动作目录事实，再按 candidate_actions 与各动作 starting_load 产出待用户确认的 PlanDraft，并声明 canonical exercise_id、负荷来源与零直接写入边界。不用于调整已有 active 计划（改用 plan-adjustment）、打卡记录、统计解释、复盘或伤病判断。
---

# workout-planning：训练计划生成 Skill

本 Skill 是给 Planner 的模型指令。计划是否成立由代码判定：`PlanDraft` Schema 承担结构，`validate_plan`
承担目录、负荷来源与禁用检查，`evaluator_agent` 承担模型 Rubric。本 Skill 不定义第二套输出结构、
不重算业务数值、不替这三层下结论。

## 何时加载

`safety_scan` 未命中且 `router_node` 判定 `intent = generate_plan` 时加载。画像未建档、或
`weekly_frequency` 不是 `known` 时，流程在 Planner 之前就已经失败，本 Skill 不会被执行；调整已有
active 计划走 `plan-adjustment`。

## 输入事实

生成前必须读齐 `read_user_profile`、`read_training_history`、`read_progress` 与 `search_exercises`。计划只用
本次 `candidate_actions` 给出的 canonical `exercise_id`；Tool 未返回的事实保持未知。

## 输出

输出严格符合当前 `PlanDraft` Schema 的对象。`starts_on` 不早于 `business_day`，训练日全部落在连续七天
窗口内，数量等于画像中已知的 `weekly_frequency`。

动作与负荷逐项照抄 `candidate_actions`：处方类型遵循目录记录口径；`weighted_reps` 使用对应
`starting_load`，`known` 连同重量和来源身份原样使用，`needs_calibration` 不携带重量。PB 不作为训练负荷。

次数区间与组数只是可被后续记录推翻的起点，不引用固定次数表、长周期、百分比减量或硬编码阈值。

## 修订与确认

校验或评审失败时只按返回理由修订一次，保留已通过部分；再次失败不产生计划。候选在用户确认前不生效，
本 Skill 无 Repository、数据库连接和写权限。

## 边界与参考

不做安全分流、医疗判断或统计计算；不输出 RIR／RPE、训练容量、估算 1RM、完成率与进步判定。

- [planning-rules.md](references/planning-rules.md)：事实优先级、动作选择与有效工作组口径。
- [few-shots.md](references/few-shots.md)：已有负荷、待校准与目录无匹配示例。
