---
name: plan-adjustment
description: 在当前计划或上传计划之上做有依据的局部调整：用 get_profile、get_current_plan、list_workouts、search_exercises、get_exercise_details 读取本次真实事实，明确调整对象与位置，保留未涉及字段，把改动依据写进 notes，用 prepare_plan_adjustment 产出待用户确认的完整快照。不用于首次生成计划、打卡记录、统计解释或伤病判断。
---

# plan-adjustment：计划调整 Skill

本 Skill 是**模型指令**，不是业务规则的实现者。调整结果按 `prepare_plan_adjustment` 的参数模型校验并保存；
本 Skill 不定义第二套输出结构。

Fit-Agent 是单一 Agent，没有按意图分发的多个角色：本 Skill 只贡献计划调整这一段的知识层指令，不取得
写入权限。

## 何时使用

用户请求修改已有计划、或要求直接调整上传的计划内容时使用。首次生成计划走 `workout-planning`。出现明确
医疗风险时停止相关训练建议并提示就医。

## 读取真实事实

- `get_profile` 读取已保存画像的完整内容与 `version`。
- `get_current_plan` 读取当前计划的 `id` 与完整 `content`。
- `list_workouts(date_from=null, date_to=business_context.business_date, page=1, page_size=10)` 查询最近训练，
  `date_from` 写 JSON null；返回的 `items` 全部使用，总数为 x 时用 min(x, 10) 条，0 条为 `[]`。每条是一个
  训练日期的完整最新记录，按 `performed_on` 倒序。
- `search_exercises` 核实动作身份，`get_exercise_details` 读取完整步骤与重量口径。

工具没返回的事实保持未知，不编造、不重算。

## 调整口径

- 明确本次的调整对象、位置与用户目标；对象或位置有歧义时先澄清，没有可调整内容时向用户索取。
- 保留未被本次请求涉及的部分：训练日结构、动作顺序、组数与次数、休息、原有说明与 `suggested_fields`
  建议来源。
- 把修改位置、原值、新值、理由、实际训练依据、记录日期、必要的重量口径与不确定性写进计划级或训练日的
  `notes`。说明必须写进 payload，不能只写在对话回复里。
- 需要补充或更新画像时，先完成画像展示、确认与保存，取得真实新版本再继续。
- 上传计划调整与当前计划调整使用同一依据：真实画像版本与当前计划 ID。

## 信息充分性

按任务索取必要信息，缺失保持未知：结构或频率调整允许动作详情为空；指定动作改组数要明确位置；替换动作要
核实目录事实、相关伤病与器械限制；按实际表现调整要明确相关逐组数据、重量口径与必要反馈。未知标量写
JSON null，未知动作详情与休息日 `exercises=[]`，`days` 至少一项；重量有值（包括 0）必须明确
`load_convention`；目录外动作保留真实名称、`exercise_id` 写 null 并说明核实状态。

## 保存与确认

`prepare_plan_adjustment` 只创建待确认快照。完整展示后，用户点击对应快照的确认按钮，由独立确认 HTTP 入口
保存；普通聊天文字不产生保存授权，模型不参与保存。

提案固定使用准备时查询到的训练事实。确认前新增训练或修正记录、用户要求采用新事实时，重新查询、准备完整
提案、展示全部业务字段与依据，等待用户点击新快照的确认按钮。结果未知时按原 `proposal_id` 调用
`get_plan_save_status` 核对原操作。

## 边界与参考

不做医疗判断；不计算 PB、趋势与任何统计量；不输出 RIR／RPE 作为处方刻度，不输出训练容量与估算 1RM。

- [adjustment-rules.md](references/adjustment-rules.md)：事实依据、信息充分性与 notes 写法。
- [few-shots.md](references/few-shots.md)：上传计划调整、记录不足与按新事实重新准备的示例。
- [diagnosis-and-experiment.md](../training-theory/references/diagnosis-and-experiment.md)：一次只改一件事与对照方法。
- [performance-factors.md](../training-theory/references/performance-factors.md)：单次波动与长期趋势、停练回归。
- [user-facing-language.md](../training-theory/references/user-facing-language.md)：用词规约。
