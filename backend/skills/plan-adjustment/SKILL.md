---
name: plan-adjustment
description: 在当前 active 七天计划之上做有依据的局部调整：用户请求修改已有训练计划（intent=adjust_plan）时加载，以本次 read_active_plan 返回的 active_plan 与 progression_decisions 为基线，保留未被证据推翻的训练日、动作、组数、次数区间与解释，外加负重只取渐进决策的 increase／keep／regress／needs_calibration 结果，产出待用户确认的新版本 PlanDraft。不用于首次生成计划、打卡记录、日程与进展解释、一般问答。
---

# plan-adjustment：计划调整 Skill

本 Skill 是**模型指令**，不是业务规则的实现者。调整结果必须通过统一计划 Schema `PlanDraft` 与确定性
校验器 `validate_plan_adjustment`（`app/domain/plans/rules.py`）才算成立；本 Skill 不定义第二套输出
结构，也不替确定性规则下结论。

## 职责

- 在当前 active 计划之上按用户本次请求做**局部调整**，产出一份待用户确认的新版本。
- 保留未被本次调整证据推翻的部分：`scheduled_on`、动作、`sets`、次数区间与未涉及的解释文字原样沿用。
- 每个被改动的动作，其依据只来自本次工具事实；`explanation` 说明改动来自哪条事实。

## 何时加载

只在 `intent = adjust_plan` 且已有 active 计划时加载；首次生成计划走 `workout-planning`，急性关键词在
路由前由 `safety_scan` 分流，本 Skill 不做安全判定。

## 事实边界

出候选前必须经 `planning_tools` 真实读到 `adjust_plan` 的全部必需事实：`read_user_profile`、
`read_active_plan`、`read_training_calendar`、`read_training_history`、`read_progress`、
`search_exercises`；缺任一项即 `MissingPlanFacts`，候选不得交给评审。

- 动作身份只能取 `candidate_actions` 里的稳定 `exercise_id`；画像明确禁用的动作已被确定性排除在候选之外。
- 只依据工具结果判断，工具没返回的事实不得编造；不读 active 之外的动作事实，也不自行重算任何统计量。

## 调整规则

负荷只取渐进决策结果、保留 active 基线、一次只改一个主要变量、不从 PB 推算负荷，这四项的完整口径与
确定实现位置见 [adjustment-rules.md](references/adjustment-rules.md) §1–§5。

## 输出 Contract

- 输出严格复用 `PlanDraft`：`goal`、`starts_on`、`explanation`、`weekly_frequency`、`training_days`，
  处方仍是 `weighted_reps`／`bodyweight_reps`／`timed` 判别联合；本 Skill 不新增字段、不定义第二套结构。
- 七天窗口由 Schema 强制：训练日数量等于 `weekly_frequency`，日期互不重复且全部落在 `starts_on` 起连续
  七天之内，`starts_on` 不早于 `business_day`；active 窗口已过期时保留训练日结构与相对间隔，日期随新
  窗口平移。
- 一次修订、确认前不生效与失败候选不写入的边界，以及负荷与结构的完整口径，见
  [adjustment-rules.md](references/adjustment-rules.md)；行为样例见 [few-shots.md](references/few-shots.md)。

## 零写入

本 Skill 不写库、不激活、不归档，不承诺保存任何内容，也不引入两份 reference 之外的来源或示例数值：
全部业务写入由用户确认后的确定性流程执行。
