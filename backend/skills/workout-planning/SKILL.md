---
name: workout-planning
description: 生成循环训练计划、录入用户已有计划、调整上传计划三类任务的判断口径；用 get_profile、get_current_plan、list_workouts、search_exercises、get_exercise_details 读取本次事实，按真实 PlanContent 结构产出待用户确认的计划快照，声明 canonical exercise_id、负荷来源与零直接写入边界。不用于打卡记录、统计解释、复盘或伤病判断。
---

# workout-planning：计划生成与录入 Skill

本 Skill 是**模型指令**，提供生成与录入计划时的判断口径。计划是否成立由业务侧判定：结构与字段按
`prepare_plan`／`prepare_plan_import`／`prepare_plan_adjustment` 的参数模型校验，动作身份与画像限制由
确定性检查落地。本 Skill 不定义第二套输出结构、不重算业务数值。

Fit-Agent 是单一 Agent，没有按意图分发的多个角色：本 Skill 只贡献计划整理这一段的知识层指令，不取得
写入权限。

## 三类任务的边界

| 任务 | 工具 | 适用场景 | 载荷约束 |
| --- | --- | --- | --- |
| 生成 | `prepare_plan` | 用户表达目标与需求，需要一段可用于执行的安排 | 需已保存画像；`base_profile_version` 取 `get_profile` 的真实版本；训练日必须有动作，动作必须有组数且次数或时长至少一项 |
| 现状录入 | `prepare_plan_import` | 用户描述或粘贴自己已经在做的安排 | `base_profile_version`／`base_plan_id` 可写 JSON null；允许动作详情为空 |
| 调整上传计划 | `prepare_plan_adjustment` | 用户要求直接改动上传的计划内容 | 用真实画像版本与当前计划 ID 作依据；信息充分性由工具契约判断 |

生成工具的调用参数校验失败时，依据返回的真实错误修正原 `prepare_plan` 调用后重试。不得改用
`prepare_plan_import` 或 `prepare_plan_adjustment` 绕过生成约束。

纯附件消息只授权完整读取本次附件；读完后询问处理意图，等用户给出文字要求再决定走哪一类任务。

## 输入事实

生成前读取：`get_profile`（目标、经验、训练环境、可用时间、伤病与受限动作、不可用器械）、
`get_current_plan`（当前计划 id 与内容）、`list_workouts`（按当前任务取相关训练日，提供已核实的逐组重量与
口径）、`search_exercises`（`purpose="generation"` 或 `"replacement"`），以及按返回 id 调用的
`get_exercise_details`。Tool 未返回的事实保持未知。

## 输出结构

计划载荷使用真实 `PlanContent`，字段全部按工具模型提交：

- 顶层：`repeat`、`days`、`notes`、`suggested_fields`。
- 训练日：`kind`（`"training"`／`"rest"`）、`focus`、`exercises`、`notes`。
- 动作：`exercise_id`、`name`、`sets`、`reps`、`duration_seconds`、`weight_kg`、
  `load_convention`、`rest_seconds`。

`repeat` 与 `days` 共同表达循环模板：`days` 至少一项，`repeat=true` 表示循环执行。用户只描述训练循环
时，可以只保存训练结构，各训练日 `exercises` 留空。休息日 `kind="rest"`，`exercises` 必须为空数组。

生成任务下：训练日 `kind="training"` 必须有动作；每个动作 `sets` 有值；`reps` 或 `duration_seconds`
至少一个有值。重量有值（包括 0）时必须明确 `load_convention`。目录外动作保留真实名称，`exercise_id`
写 null。

`suggested_fields` 用有效 JSON Pointer 标记助手补充或修改的字段来源；数组位置变化时同步路径。

## 负荷来源

负荷只来自本次 Tool 事实，本 Skill 不重算：

- `list_workouts` 里已核实的相关逐组 `weight_kg` 与 `load_convention`；
- `get_current_plan` 中已有的计划值；
- 用户本次明确给出的重量与口径。

计划值是安排，训练记录里的逐组数值是实际完成事实，两者不同。判断可达到的负荷时实际完成记录优先，采用时
说明依据与不确定性。目录不提供个体起始负荷。PB、成绩指标与示例数值都不作为训练负荷。

所有已声明字段都必须保留，未知标量显式写 JSON null：没有重量的动作 `weight_kg` 与 `load_convention` 写
null，没有时长写 `duration_seconds: null`，没有建议来源写 `suggested_fields: []`。省略字段会触发字段缺失
错误。

## 修订与确认

用户修改建议时，重新准备快照并完整展示，等待新的按钮确认；未采纳的生成建议经修改时继续用
`prepare_plan` 创建新快照。本 Skill 无数据库连接与写权限：完整展示后，用户点击对应快照的确认按钮，
由独立确认 HTTP 入口保存；普通聊天文字不产生保存授权。

结果未知时按原 `proposal_id` 调用对应的状态查询工具核对原操作。

## 边界与参考

不做医疗判断；出现明确医疗风险时停止相关训练建议并提示就医。不输出 RIR／RPE 作为处方刻度，不输出训练
容量、估算 1RM 与完成率。

- [planning-rules.md](references/planning-rules.md)：事实优先级、动作选择、执行条件与环境默认。
- [goal-content.md](references/goal-content.md)：目标缺失口径与按目标的内容侧重。
- [training-principles.md](references/training-principles.md)：编排计划的定性原则、安全边界、用词与回答结构。
- [program-design.md](references/program-design.md)：频率与训练日、训练变量口径。
- [trainee-classification.md](references/trainee-classification.md)：经验文本的 P0—L3 定性视角。
- [exercise-selection.md](references/exercise-selection.md)：检索覆盖、起点优先级、传统硬拉准入与替代规则。
- [few-shots.md](references/few-shots.md)：生成、校验失败修正、不完整计划、附件澄清与目录无匹配示例。
- [training-variables.md](../training-theory/references/training-variables.md)：训练变量与常见安排结构。
- [methodology.md](../training-theory/references/methodology.md)：六步推理与三层分离。
- [user-facing-language.md](../training-theory/references/user-facing-language.md)：用词规约与回答结构。
