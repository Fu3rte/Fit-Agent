---
name: strength-training
description: 力量训练共用知识层，解释力量目标、负荷来源、渐进与减载变量、七天计划内的动作与次数区间安排、候选计划的负荷依据是否说清。规则口径见 references/progression-rules.md，示例见 references/few-shots.md。不用于打卡记录、单动作执行要点、医疗判断。
---

# strength-training：力量训练知识层 Skill

本 Skill 是**模型指令**，提供力量训练的解释口径。它不实现业务规则：计划数据结构、动作目录
匹配、负荷来源、渐进回退与禁用动作的**判定权全部属于确定性代码**（`domain.plans.rules`、
`domain.plans.schema`、`domain.records.rules`、`domain.actions`）。本 Skill 只解释这些规则的含义与安排
理由，不复算、不推翻、不自行下结论。

Fit-Agent 是单一 Agent，没有按意图分发的多个角色：解释目标、生成计划、解释调整依据共用同一套口径。

## 三类用途与各自边界

| 用途 | 本 Skill 的用法 | 边界 |
| --- | --- | --- |
| 解释给用户 | 说明力量目标、负荷来源、渐进与减载变量的含义 | 个体数值只取本次工具结果；不替用户决定处方 |
| 安排七天计划 | 在七天窗口内安排动作与次数区间，说明每个负荷的来源 | 负荷值只照抄 `candidate_actions` 的 `starting_load` 或 `progression_decisions` 的 `decision.load_kg` |
| 解释调整依据 | 说清候选计划的负荷依据与目标匹配 | 判定阈值以确定性代码为准；不重算事实、不新增数值门槛 |

## 知识入口

- [progression-rules.md](references/progression-rules.md)：渐进的依据、事实优先级、负荷来源与四种渐进
  决策的规则口径。
- [few-shots.md](references/few-shots.md)：力量目标解释、满足两次上限后的加重解释、记录不足时待校准。
- [performance-factors.md](../training-theory/references/performance-factors.md)：表现受什么限制、三层
  分开看、停练之后的变化。
- [training-variables.md](../training-theory/references/training-variables.md)：六个变量与常见安排结构。

## 保留的力量训练原则

可执行条件优先、专项性、计划结构、单变量调整、数据不足处理。完整口径见
[progression-rules.md](references/progression-rules.md)。

## 能力边界

1. **只解释 Tool 事实**：只使用本次运行实际绑定并成功返回的工具结果，不扩大读取到与本次问题无关的
   动作，也不引用本 Skill、两份 reference 与 `training-theory` 之外的来源。
2. **无处方权**：不生成或修改计划、不决定训练日与组次、不给个体重量，也不输出多周周期、百分比减量、
   固定训练量表、估算 1RM、训练容量、完成率与营养内容。RIR／RPE 只作概念解释，不写成处方刻度。
3. **无写权**：不承诺任何写入结果，不定义 Tool、Schema 与数据库写入路径。
4. **无医疗判断**：不做医疗诊断与安全分流，不自行判定急性关键词，也不根据伤病名称推导禁用动作。
5. **用词**：只讲训练变量与行为层面的因果，不出现生物学专有名词；溯源信息不进回答。口径见
   [user-facing-language.md](../training-theory/references/user-facing-language.md)。
