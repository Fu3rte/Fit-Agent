---
name: training-theory
description: 训练专业知识库的唯一入口，覆盖训练原理与表现因素、计划设计与编排、动作技术与重量口径、渐进与可比记录、停滞与调整分析、常见说法核对与专业问答。正文是主题索引与读取路径；按当前任务读取 references 中对应主题的文件，需要背景时再读关联文件。
---

# training-theory：训练专业知识库

本目录是训练专业知识的唯一入口。正文只放主题索引与读取路径，各主题的正文都在 references 下。

## 主题索引

| 主题 | 回答什么问题 | 文件 |
| --- | --- | --- |
| 分析原则 | 终点怎么定；刺激、即时反应、长期适应怎么分开；哪些推理跳跃站不住 | [methodology.md](references/methodology.md) |
| 证据强度 | 一句话该说「有研究支持」还是「经验推断」；结论的适用条件与不确定性怎么写 | [evidence-grading.md](references/evidence-grading.md) |
| 来源登记 | 每条结论来自哪项研究或哪本书；适用人群与不适用范围 | [sources.md](references/sources.md) |
| 表现限制 | 卡住的五类原因；单次波动怎么读；停练之后怎么变 | [performance-factors.md](references/performance-factors.md) |
| 努力与疲劳 | RIR、RPE、速度损失、力竭、练后酸胀各自能说明什么 | [effort-and-fatigue.md](references/effort-and-fatigue.md) |
| 训练变量 | 量、强度、频率、密度、休息、动作选择各改什么；常见组结构与代价 | [training-variables.md](references/training-variables.md) |
| 渐进与记录 | 凭什么加重；什么记录才可比；个体差异有多大；回归期怎么处理 | [progression.md](references/progression.md) |
| 目标与内容侧重 | 增肌、增力、减脂各侧重什么；目标缺失怎么处理 | [goal-content.md](references/goal-content.md) |
| 训练者差异 | 启动、新手、中级、进阶怎么识别；同一人不同动作怎么分层 | [trainee-classification.md](references/trainee-classification.md) |
| 计划编排与计划分析 | 频率与训练日怎么排、模式怎么覆盖、变量怎么分配；一份计划从哪几个角度看 | [program-design.md](references/program-design.md) |
| 动作技术 | 动作模式差别、稳定性与疲劳代价、活动范围、技术要点、重量口径的含义 | [exercise-technique.md](references/exercise-technique.md) |
| 动作选择 | 起点优先级、传统硬拉准入、替代与回退怎么取舍 | [exercise-selection.md](references/exercise-selection.md) |
| 停滞与调整 | 变化原因怎么归因、一次改哪个变量、复训怎么保守、怎么验证 | [adjustment.md](references/adjustment.md) |
| 诊断与对照 | 某个现象该怀疑什么、单变量对照怎么做、效率按什么分母算 | [diagnosis-and-experiment.md](references/diagnosis-and-experiment.md) |
| 常见说法 | 流行说法成不成立、理由是什么 | [myths.md](references/myths.md) |

## 按需读取

- 读与当前问题直接相关的主题即可。需要背景时，再读该主题里指向的关联文件。
- 计划生成、现状录入、计划分析：先从 [program-design.md](references/program-design.md) 入手，按需补
  [goal-content.md](references/goal-content.md)、[trainee-classification.md](references/trainee-classification.md)、
  [exercise-selection.md](references/exercise-selection.md)。
- 调整、停滞、复训：从 [adjustment.md](references/adjustment.md) 入手，配合
  [diagnosis-and-experiment.md](references/diagnosis-and-experiment.md) 与
  [performance-factors.md](references/performance-factors.md)。
- 动作解释与替代：从 [exercise-technique.md](references/exercise-technique.md) 入手，选择与回退再读
  [exercise-selection.md](references/exercise-selection.md)。
- 概念解释与说法核对：从 [myths.md](references/myths.md) 或 [training-variables.md](references/training-variables.md)
  入手，证据口径见 [evidence-grading.md](references/evidence-grading.md)。
- 结论要落到出处、适用人群与不确定性时，读 [sources.md](references/sources.md)。

各主题内的引用都相对本目录解析。传给 read 的路径统一写成 `skills/training-theory/references/<文件>`。
