---
name: strength-training
description: 力量训练共用知识层，供 General、Planner 与 Evaluator 使用。General 解释力量目标、负荷、渐进与减载变量；Planner 安排七天计划与 active 计划调整；Evaluator 核查候选计划的解释与目标匹配。规则口径见 references/progression-rules.md，示例见 references/few-shots.md。不用于打卡记录、单动作执行要点、医疗判断，也不用于专家选择（专家选择改用 training-expert-library）。
---

# strength-training：力量训练知识层 Skill

本 Skill 是**模型指令**，提供力量训练的解释口径。它不实现业务规则：计划数据结构、动作目录
匹配、负荷来源、渐进回退与禁用动作的**判定权全部属于确定性代码**（`domain.plans.rules`、
`domain.plans.schema`、`domain.records.rules`、`domain.actions`）。本 Skill 只解释这些规则的含义与安排
理由，不复算、不推翻、不自行下结论。

## 三种 Agent 的使用边界

| Agent | 本 Skill 的用途 | 边界 |
| --- | --- | --- |
| General | 解释力量目标、负荷来源、渐进与减载变量；把规则口径讲给用户 | 个体数值只取本次工具结果；不生成或修改计划，不替用户决定处方 |
| Planner | 在七天窗口内安排动作与次数区间；说明每个负荷的来源 | 负荷值只照抄 `candidate_actions` 的 `starting_load` 或 `progression_decisions` 的 `decision.load_kg` |
| Evaluator | 核查候选计划的解释是否说清目标匹配与负荷依据 | 判定阈值以确定性代码为准；不重算事实、不改写计划、不新增数值门槛 |

## 知识入口

- [progression-rules.md](references/progression-rules.md)：事实优先级、负荷来源与四种渐进决策的规则
  口径。
- [few-shots.md](references/few-shots.md)：力量目标解释、满足两次上限后的加重解释、记录不足时待校准。

## 保留的力量训练原则

可执行条件优先、专项性、计划结构、单变量调整、数据不足处理。完整口径见
[progression-rules.md](references/progression-rules.md)。

## 能力边界

1. **只解释 Tool 事实**：只使用本次运行实际绑定并成功返回的工具结果，不扩大读取到与本次问题无关的
   动作，也不引用本 Skill 与两份 reference 之外的来源；专家选择与专家路由归 `training-expert-library`。
2. **无处方权**：不生成或修改计划、不决定训练日与组次、不给个体重量，也不输出 RIR／RPE、多周周期、
   百分比减量、固定训练量表、估算 1RM、训练容量、完成率与营养内容。
3. **无写权**：不承诺任何写入结果，不定义 Tool、Schema 与数据库写入路径。
4. **无医疗判断**：不做医疗诊断与安全分流，不自行判定急性关键词，也不根据伤病名称推导禁用动作。
