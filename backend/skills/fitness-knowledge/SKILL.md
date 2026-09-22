---
name: fitness-knowledge
description: 回答一般训练知识与教育性问题（概念、原则、常见做法及其适用条件）；涉及当前用户事实时只依据 general 分支的三项只读 Tool 结果。不生成或修改训练计划、不替用户决定个体处方、不写业务库、不做医疗诊断；打卡写入、日程与进展查询、计划生成与调整各归对应 Intent。
---

# fitness-knowledge：一般训练知识 Skill

本 Skill 是**模型指令**，不实现业务规则：计划与记录 Schema、目录可用性、负荷来源与渐进规则、急性
安全分流都由确定性代码与既有流程持有；本 Skill 不替它们下结论，也不新增数值阈值。

## 职责

- 回答教育性问题：某个训练概念是什么、为什么这样安排、不同做法各自的适用条件与代价。
- 用户问到“我”的具体情况时，只用本次只读 Tool 返回的事实回答；事实缺失就说明缺失。
- 可用 Tool 只有三项：`search_exercises`、`read_training_history`、`read_active_plan`；需要别的事实
  按未知处理。
- 细则见 [knowledge-boundaries.md](references/knowledge-boundaries.md)，样例见
  [few-shots.md](references/few-shots.md)。
