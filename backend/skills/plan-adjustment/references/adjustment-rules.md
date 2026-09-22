# 调整规则（plan-adjustment reference）

本文件随 `plan-adjustment` 正文一起加载，给出生成调整候选时**必须遵守**的规则，以及这些规则在
Fit-Agent 里的确定实现位置。

权威顺序：已合入源码 > 本文件。本文件只复述 `app/domain/plans/rules.py`、
`app/domain/plans/schema.py`、`app/application/agent/prompts.py`、
`app/application/agent/plan_nodes.py` 已经实现的口径，不新增业务口径；与源码冲突时以源码为准。

## 1. 基线与保留

调整候选建立在 `payload.active_plan` 之上，未被本次调整证据推翻的部分原样沿用：

| active 字段 | 沿用方式 |
| --- | --- |
| `goal` | 原样保留，除非用户本次明确改变目标 |
| `weekly_frequency` | 必须等于画像里 `known` 的每周训练次数，本 Skill 无法改动 |
| `training_days[].scheduled_on` | 保留训练日结构与相对间隔；日期必须落在新窗口内（见 §5） |
| 动作与 `sets` | 原样保留，除非用户本次请求改变它们 |
| 次数区间与 `progression_note` | 原样保留；自重与计时处方不含负荷字段 |
| `explanation` | 未被涉及的文字保留，只为改动追加依据 |

## 2. 负荷来源

外加负重动作（`weighted_reps`，对应目录 `record_type='reps_weight'`）的 `load` 有两种写法：

- `known`：`weight_kg` **必须等于**同动作 `progression_decisions[].decision.load_kg`，并且
  `decision.action` 是 `increase`／`keep`／`regress` 之一；`source_workout_session_id` 与
  `source_set_no` 沿用 `candidate_actions[].starting_load` 给出的最近一次有效工作组身份。
- `needs_calibration`：`decision.action` 为 `needs_calibration` 时只能写
  `{"status": "needs_calibration"}`，禁止给出任何具体重量。

`progression_decisions` 里没有该动作时（active 里它没有带 `known` 负荷的目标处方，或目录未给出
`min_load_increment_kg`），照抄 `candidate_actions[].starting_load`：`known` 时连同来源训练与组序号
一起照抄，`needs_calibration` 时保持待校准。

禁止从 PB 推算训练负荷：`read_progress` 的三类成绩是指标，不是当前训练能力，也不构成加重依据。
无有效工作组历史时不得给出具体重量。

## 3. 渐进与回退（当前 `resolve_progression` 规则，10B／10B-1）

输入：active 的目标处方（`sets`、`reps_min`、`reps_max`、`target_load_kg`）、目录
`min_load_increment_kg`、该计划关联日程下的训练身份，以及全部有效工作组
（`set_type='work'` 且度量与记录口径匹配）。单次训练的判定口径：按组序号取前 `sets` 个目标组，目标组
不齐、次数缺失或任一低于 `reps_min` 即**失败**；未失败且目标组同为单一重量即**完整完成**；完整完成且
每个目标组都不低于 `reps_max` 即**达到次数上限**。据此：

1. 参与判定的关联训练少于两次 → `keep`，负荷维持 `target_load_kg`。
2. 最近两次都完整完成、使用同一负荷且都达到次数上限 → `increase`，
   新负荷 = 该负荷 + `min_load_increment_kg`（按记录精度取一位小数）。
3. 最近两次都失败 → 回退到这些关联训练里**最近一次完整完成的负荷**（`regress`）；一次完整完成的
   训练都没有 → `needs_calibration`，不带负荷。
4. 其余情况 → `keep`。

由此得到的四条行为约束：

- 加重单位只有目录的 `min_load_increment_kg`，没有百分比加重与百分比减重。
- 达到上限加重后，次数区间本身不变，实际执行回到区间下部由 `progression_note` 说明。
- 决策的 `sets` 与次数区间一律取自 **active** 的目标处方（同一动作多处出现以首次出现
  为准）；`increase` 的负荷锚定这两次训练的实际有效工作组，其余决策的负荷见 §2 与上文。候选改写
  这些字段不会改变决策，也无法为新负荷提供理由。
- 只有关联到该计划训练日的训练参与判定：额外训练既不计入也不打断；未关联的历史不改变决策。

## 4. 一次只改一个主要变量

同一次调整里，一个动作只允许改变重量、次数区间、组数中的一个主要变量：

- 负荷变化由 §2 与 §3 唯一确定，模型没有裁量空间。
- 因此需要改变次数区间或组数时，负荷保持 `keep` 值，改动只体现用户本次明确要求的那一项。
- 禁止用缩窄次数区间、增删组数来"凑"出更高或更低的重量。
- 一个动作的判定不牵连同一天或同一计划的其他动作：每个动作各按自己的 `progression_decisions` 条目。

## 5. 结构、频率与七天窗口

- `PlanDraft` 强制：`training_days` 数量等于 `weekly_frequency`；日期互不重复且全部落在
  `starts_on` 起连续七天内；`starts_on` 不早于 `business_day`；每个训练日至少一个动作，同日不重复
  同一动作。
- `weekly_frequency` 与画像 `known` 值不一致时确定性阻断（`weekly_frequency_mismatch`）。改变每周
  训练次数属于结构调整：本 Skill 无写入权，用户需先更新画像，本 Skill 不静默改频率。
- 改变训练日结构、动作体系或目标本身同样属于结构调整：在 `explanation` 里写清触发事实与建议改动，
  由用户确认环节裁决，不在候选里偷偷替换。

## 6. 一次修订与用户确认

- 一次修订：结构校验或评估未通过时，把失败理由交回同一个 Planner 逐项修正一次；二次不通过即本次不
  产生可激活计划。
- 确认前不生效：候选通过后进入待确认状态，用户确认之前不得向用户宣称计划已生效；用户拒绝则原
  active 不变。
- 失败候选不写入：只有通过校验与评审的候选才被持久化，失败候选不产生可激活计划。

## 7. 本 Skill 不做

- 不计算 PB、趋势、月历与任何统计量；只解释与使用工具给出的事实。
- 不做医疗判断与安全分流；急性关键词由 `safety_scan` 决定。
- 不写复盘记录、不维护训练日志、不改写用户已记录的训练事实：训练记录全程只读。
- 不输出第二套数据结构，不以 Markdown 计划表代替结构化候选。
- 不引入 RIR／RPE、估算 1RM、训练容量、完成率等字段或结论。
