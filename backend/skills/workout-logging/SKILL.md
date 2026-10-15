---
name: workout-logging
description: 用户用自然语言报告一次训练时加载；约束 prepare_workout_record 的提取与确认摘要——只使用动作目录的稳定 exercise_id，日期按 business_day 解释，逐组给出 set_no、set_type 与对应记录口径字段，候选日程按当前数量口径提示。不计算 PB、趋势、完成率与估算 1RM，不做计划调整，不写业务库；写入只由用户确认后的确认端点执行。
---

# workout-logging：自然语言打卡 Skill

本 Skill 是**模型指令**，不实现业务规则：训练事实的校验、目录口径复验、候选日程查询与确认写入都由
`prepare_workout_record`、`RecordsService` 与确认端点持有；本 Skill 只约束“描述 → 结构化事实 →
确认摘要”这一段的写法。

Fit-Agent 是单一 Agent，没有按意图分发的多个角色：本 Skill 随会话装载，只贡献知识层指令，不取得写入权限。

## 职责

- 用户用自然语言报告一次训练时，把这次描述的**原文**交给 `prepare_workout_record`，由该 Tool 内部
  完成提取（业务自然日 ＋ 逐组事实）与校验；模型不自行提取，也不代替用户补事实。
- 校验通过后按待确认载荷写面向用户的确认摘要；摘要只描述载荷里已有的事实。

## 知识入口

- [logging-contract.md](references/logging-contract.md)：字段、值域与确认载荷。
- [few-shots.md](references/few-shots.md)：行为样例。
- [performance-factors.md](../training-theory/references/performance-factors.md)：记录为什么是长期证据，
  单次波动与趋势怎么分开看。
- [user-facing-language.md](../training-theory/references/user-facing-language.md)：面向用户的用词规约。

## 可用 Tool

| Tool | 用途 |
| --- | --- |
| `prepare_workout_record` | 本次描述的唯一事实入口：内部按 `business_day` 与目录完成提取、校验与候选日程查询，返回 `workout_confirmation` 载荷 |
| `search_exercises` | 需要核对某动作是否在目录里时检索；`exercise_id`、`record_type`、`load_convention` 一律取返回值 |

## 五条提取口径

1. **动作身份只用目录**：只使用载荷里给出的稳定 `exercise_id`；匹配不到的动作不编造 id、不把名称相近
   的动作当作同一个动作、不改写 id。名称相近的动作在目录里是彼此独立的记录，各自口径独立。
2. **日期按业务日解释**：`performed_on` 是训练发生的业务自然日（`YYYY-MM-DD`）；用户说“今天／昨天”
   时按 `payload.business_day` 折算，说绝对日期时原样使用。
3. **组序号按动作重置**：`set_no` 在同一动作内从 1 开始连续编号；换动作重新从 1 开始，不跨动作连续。
4. **组类型三态**：`set_type` 只用 `work`、`warmup`、`assisted`；`assisted` 不计入 PB 口径。
5. **字段随目录记录口径**：`reps_weight` 给 `weight_kg` ＋ `reps` ＋ 与目录一致的 `load_convention`；
   `reps_bodyweight` 只给 `reps`；`time` 只给 `duration_seconds`。自重与计时组不带任何负荷字段。

## 确认与写入边界

- 本次 Run 只产出待确认载荷：`workout`（日期、逐组事实与关联默认值）与 `candidate_plan_sessions`
  （当天未完成日程）。前端把载荷回填成确认界面，用户核对后才提交确认端点。
- 摘要让用户核对日期、动作、组数、次数、重量与时长；候选日程数量按当前
  `NATURAL_LANGUAGE_RECORD_SYSTEM_PROMPT` 的三态口径写：恰一个时说明将自动关联，零个时只提示用户
  显式选择「额外训练」，多个时必须让用户选择某个日程或标记为额外训练。
- 完整展示后，用户点击具体快照的确认按钮，由独立确认 HTTP 调用现有业务服务写入；普通聊天文字不产生授权，确认过程不调用模型。请求与幂等遵循 [结构化保存确认方案](../../../tmp/optimization/structured-save-confirmation.md)。
- 不计算 PB、趋势、完成率、训练容量与估算 1RM；不判定计划是否达成；不生成或调整计划；不做医疗判断。
