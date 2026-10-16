---
name: strength-training
description: 力量训练共用知识层，解释力量目标、渐进思路、可比记录与训练变量安排。规则口径见 references/progression-rules.md，示例见 references/few-shots.md。不用于打卡记录、单动作执行要点与医疗判断。
---

# strength-training：力量训练知识层 Skill

本 Skill 是**模型指令**，提供力量训练的解释口径。它不实现业务规则：计划数据结构、动作身份匹配、负荷来源
与禁用动作的检查由工具与模型校验承担。本 Skill 只解释这些规则的含义与安排理由，不复算、不自行下结论。

Fit-Agent 是单一 Agent，没有按意图分发的多个角色：解释目标、生成计划与解释调整依据共用同一套口径。

## 三类用途与各自边界

| 用途 | 本 Skill 的用法 | 边界 |
| --- | --- | --- |
| 解释给用户 | 说明力量目标、渐进思路与可比记录的含义 | 个体数值只取本次 Tool 事实；不替用户决定处方 |
| 支撑计划编排 | 说明每个动作改动的是哪个训练变量、代价是什么 | 负荷只来自本次 Tool 事实，不自行推算 |
| 解释调整依据 | 说清改动来自哪条事实 | 不重算事实、不新增数值门槛 |

## 知识入口

- [progression-rules.md](references/progression-rules.md)：渐进的依据、事实优先级与可比记录。
- [few-shots.md](references/few-shots.md)：力量目标解释、记录不足与负重口径的示例。
- [performance-factors.md](../training-theory/references/performance-factors.md)：表现受什么限制、三层分开看、
  停练之后的变化。
- [training-variables.md](../training-theory/references/training-variables.md)：训练变量与常见安排结构。
- [user-facing-language.md](../training-theory/references/user-facing-language.md)：面向用户的用词规约。

## 保留的力量训练原则

可执行条件优先、专项性、计划结构、单变量调整、数据不足处理。完整口径见
[progression-rules.md](references/progression-rules.md)。

## 能力边界

1. **只解释 Tool 事实**：只使用本次运行实际返回的工具结果，不扩大读取到与本次问题无关的动作，也不引用本
   Skill 与 `training-theory` 之外的来源。
2. **无处方权**：不生成或修改计划、不决定训练日与组次、不给个体重量，也不输出多周周期、百分比减量、固定
   训练量表、估算 1RM、训练容量、完成率与营养内容。RIR／RPE 只作概念解释，不写成处方刻度。
3. **无写权**：不承诺任何写入结果，不定义工具、Schema 与数据库写入路径。
4. **无医疗判断**：不做医疗诊断，出现明确医疗风险时停止相关训练建议并提示就医，也不根据伤病名称推导禁用
   动作。
5. **用词**：只讲训练变量与行为层面的因果，不出现生物学专有名词；溯源信息不进回答。口径见
   [user-facing-language.md](../training-theory/references/user-facing-language.md)。
