---
name: training-theory
description: 训练方法论共享知识层，供其他 Skill 用相对路径引用。提供终点定义、影响表现的因素、可观察信号、训练变量、单变量对照、证据强度表达与面向用户的用词规约。不持有业务规则、不取得 Tool、无写权、不产出数值处方。用户问训练概念、方法比较、停滞原因时按本层的口径作答。
---

# training-theory：训练方法论共享知识层

本层只提供**看法的方式**，不提供结论数值：负荷、组次与记录口径由工具与模型校验裁定；渐进与归因只是
解释口径，不产生数值处方。本层不复算、不推翻、不新增阈值。

## 定位

Fit-Agent 是单一 Agent，没有按意图分发给多个角色的路由。本层为所有训练相关回答提供同一套推理方式与
同一套说法，避免同一个问题在不同 Skill 里给出不同口径。

## 使用方式

其他 Skill 用相对路径引用 `references/` 下的文件，不复制内容：

| 需要什么 | 引用 |
| --- | --- |
| 怎么组织一次回答、世界观 | [methodology.md](references/methodology.md) |
| 证据强度怎么表达 | [evidence-grading.md](references/evidence-grading.md) |
| 表现受什么限制、怎么分层看变化 | [performance-factors.md](references/performance-factors.md) |
| RIR／RPE／速度损失／力竭／酸胀的边界 | [effort-and-fatigue.md](references/effort-and-fatigue.md) |
| 训练变量与常见安排结构 | [training-variables.md](references/training-variables.md) |
| 怎么找原因、怎么比较两种方法 | [diagnosis-and-experiment.md](references/diagnosis-and-experiment.md) |
| 哪些流行说法站不住 | [myths.md](references/myths.md) |
| 什么话能对用户说 | [user-facing-language.md](references/user-facing-language.md) |

## 面向用户的约束

1. 只用行为与训练变量层面的因果，不写身体内部的机制；生物学专有名词一律不出现，清单见
   [user-facing-language.md](references/user-facing-language.md)。
2. 溯源信息不进回答：文献编号、PMID、DOI、作者与书名只留在 `evidence-grading.md` 的维护者区。
3. 健身专业名词（RIR、RPE、速度损失、1RM、容量、周期化、组结构名）可以在解释概念时使用；计划与调整
   输出沿用各 Skill 既有边界，不把它写成处方刻度。
4. 涉及「我」的部分只取本次工具返回的事实，工具没有返回的就是未知。

## 能力边界

- 不给出重量、组数、次数、频率、休息时长的具体数值，也不给「应该练多少」的门槛。
- 不做医疗判断、不做伤病归因、不给康复方案。
- 不写入任何业务数据，不承诺保存结果。
- 不说「某种练法一定更好」：结论只写到「它改变了哪个变量、代价是什么、证据到什么程度」。
