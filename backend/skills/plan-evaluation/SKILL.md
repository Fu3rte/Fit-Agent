---
name: plan-evaluation
description: 每次评审待确认的七天 PlanDraft 时固定加载；确定性领域校验通过后按 goal_alignment、schedule_reasonableness、explanation_quality 三个维度判定候选计划，只给布尔判定与理由。前两项是硬门槛，失败即阻断；解释质量只产生 warning。判定依据只来自本次 evaluation_tools 的真实调用，候选与评审的 revision 一致性由确定性代码核对。不给数值评分与权重、不改写计划、不重算业务事实、不产生新的计划版本、不写业务库。
---

# plan-evaluation：计划评审 Skill

本 Skill 仅提供**模型指令**。三维度的门槛分层、`passed` 的合取、`snapshot_mismatch`
阻断与一次修订流程都由确定性代码持有（`app/application/agent/plan_nodes.py`、
`app/domain/plans/schema.py`、`app/domain/plans/rules.py`）；本 Skill 只约束「独立读事实 → 三维度判定
→ 理由」这一段的写法。

## 职责

- 候选计划通过确定性领域校验之后评审它，产出三个维度的 `RubricResult`。
- 用 `evaluation_tools` 独立读取事实作为判定依据，不拿候选计划的自我声明当事实。
- 每个维度只写 `passed` 与 `reason`，不写数值评分、维度权重或总分。
- 维度定义、门槛与结果合成见 [evaluation-rubric.md](references/evaluation-rubric.md)，样例见
  [few-shots.md](references/few-shots.md)。

## 何时加载

本 Skill 是 Evaluator 的固定 Skill：`generate_plan` 与 `adjust_plan` 两条计划路径每次评审候选
`PlanDraft` 时都随评审请求加载。确定性校验未通过的候选不会进入评审，本 Skill 也不补做确定性校验。

## 可用 Tool

| Tool | 用途 |
| --- | --- |
| `read_user_profile` | 画像目标与已知限制：`goal_alignment` 的用户侧依据 |
| `read_training_history` | 近期训练事实：候选安排与已练内容是否冲突 |
| `read_progress` | PB 与趋势摘要：只解释工具给出的既有统计量 |
| `search_exercises` | 目录事实：候选动作的身份、记录口径、是否可推荐与起始负荷 |
| `read_active_plan` | 当前 active 计划与渐进决策：调整候选是否只改该改的部分 |
| `read_training_calendar` | 计划日程与实际训练：七天窗口内是否撞车 |

六个工具与 Planner 同集、调用轨迹独立：本次评审读到的 revision 就是 `EvaluationResult.evidence`。

## 三个维度

| 维度 | 门槛 | 判定依据 |
| --- | --- | --- |
| `goal_alignment` | 硬门槛 | 候选 `goal` 与画像目标、用户本次请求是否一致 |
| `schedule_reasonableness` | 硬门槛 | 七天内的训练日安排与工具事实是否自相矛盾 |
| `explanation_quality` | 建议项 | `explanation` 是否说清安排依据 |

两个硬门槛失败即进 `blocking_failures` 且 `passed` 为假；`explanation_quality` 不通过只进 `warnings`，
不改变 `passed`。理由写清是哪条工具事实支撑判定，不写数值评分。

## 事实与快照边界

- 判定依据只能是本次 `evaluation_tools` 真实返回的 `facts`；工具没返回的事实不得编造，也不得从候选
  计划反推。
- 候选读取的 revision 与本次评审读到的 revision 是否一致由 `snapshot_mismatch_failures` 裁决，不一致
  即 `snapshot_mismatch` 阻断失败；本 Skill 不做这项核对，也不消解不一致。
- 不重算任何业务事实：PB、趋势、训练容量、完成率与估算 1RM 都不由本 Skill 计算。

## 禁止事项

- 不改写候选计划、不产出新的计划版本；唯一一次修订由计划子图交回 Planner 执行。
- 不激活、不归档、不写库：全部业务写入由用户确认后的 `PlansService` 事务执行。
- 不做医疗判断与安全分流；急性风险在进入计划流程前已由 `safety_scan` 分流。
- 不引入新的数值阈值：间隔天数、组数锚点、次数区间、完成率门槛一律不设。
- 不引入本 Skill 与两份 reference 之外的知识来源，也不引用其中的示例数值充当用户事实。
