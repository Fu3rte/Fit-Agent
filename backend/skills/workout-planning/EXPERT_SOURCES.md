# workout-planning 来源登记（仓库文档）

记录训练计划知识的来源与适用范围，供维护者核对；本文件不加载到模型输入。具体计划判断依据
`references/` 中的规则与本次 Tool 事实。下表的短文件名均位于 `references/`。

## 来源层的位置

- 来源登记只记录判断的由来与边界，不表示这些结论已在本链路验证过。
- 来源登记不作为训练处方的判断依据。
- 不模拟来源本人，不声称代表其最新观点，不把来源判断写成已经确认的用户事实。
- 任何来源都不能覆盖本次 Tool 事实、画像事实或确定性校验结果。
- 证据不足时不生成假精确处方：按未知处理，并说明缺口。

## 来源登记

只登记本链路有落点的来源。

| 来源 | 版本 | 本链路取什么 | 本链路不取什么 |
| --- | --- | --- | --- |
| Brad Schoenfeld | Muscle Hypertrophy, 2e (2021) | 训练量、频率、动作选择、活动范围 | 医疗决定、出版年之后作者的观点 |
| Eric Helms | Training Pyramid: Training, 2e (2018) | 依从性、计划结构、训练量、强度、频率、渐进 | 医疗决定、出版年之后作者的观点 |
| Dan John | Intervention (2013)、Easy Strength Omnibook (2022) | 动作缺口、回到基础、低疲劳重复 | 医疗决定、营养处方、精确剂量 |

## 变量路由

按变量取本目录对应的小节；来源用最少必要的一位，不投票、不因名气增加人数。

| 变量 | 落地小节 | 出处 |
| --- | --- | --- |
| 计划结构与依从性（三个目标通用） | program-design.md §频率与训练日；training-principles.md §原则 | Eric Helms |
| 增肌的组数与频率 | goal-content.md §增肌；program-design.md §训练变量 | Brad Schoenfeld |
| 增力的主线安排与次数区间 | goal-content.md §增力 | Eric Helms |
| 动作选择与替代 | planning-rules.md §2；exercise-selection.md §替代与回退 | Brad Schoenfeld |
| 水平与阶段判定 | trainee-classification.md §四层 | Eric Helms |
| 反复中断或动作缺口后的起点 | exercise-selection.md §P0—L3 起点 | Dan John |

## 本链路不适用的内容

以下内容不进入本 Skill 的处方与解释：

- 跨周排期、百分比减量、峰值与测试日：本链路固定七天窗口，`PlanDraft` 无对应字段。
- 余力刻度与力竭判定：见 SKILL.md 的边界，本 Skill 不输出该口径。
- 停滞归因与变量实验：输入只有本次 Tool 事实与七天窗口，没有跨周执行数据，不做归因也不设计实验。
- 营养、宏量与补给：本链路不含营养输出。
- 医疗返场与安全分流：`safety_scan` 在 Router 与模型之前判定，本文件不复现也不做二次分流。
- 来源的原文、页码与链接：本文件与整个 Skill 不携带外部链接。

## 写进 explanation 的口径

- 说明安排的适用前提、保留条件与仍缺的证据；不把假设讲成结论。
- 不指名来源、不把自己的判断写成来源的判断、不罗列读过的文件、不出现内部字段名与路径。
