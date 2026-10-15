# 打卡契约

## 提取输入

`prepare_workout_record` 接收用户描述，并从 Runtime 获取 `business_day`、canonical 动作目录和模型预算。模型只能使用目录返回的 `exercise_id`、`record_type` 与 `load_convention`。

`ExtractedWorkout` 包含：

- `performed_on`：训练发生的业务日期。
- `sets`：1–50 个 `ExtractedWorkoutSet`。

每组包含 `exercise_id`、`set_no`、`set_type`、`reps`、`load_convention`、`weight_kg`、`duration_seconds`。`set_no` 在同一动作内从 1 开始；`set_type` 仅允许 `work`、`warmup`、`assisted`。

## 记录口径

- `reps_weight`：必须提供 `reps`、`weight_kg` 和目录规定的 `load_convention`；`duration_seconds` 为空。
- `reps_bodyweight`：只提供 `reps`；负重字段与 `duration_seconds` 为空。
- `time`：只提供 `duration_seconds`；次数与负重字段为空。

重量口径完全沿用目录值：`barbell_includes_bar_total`、`dumbbell_per_hand`、`machine_pin_displayed_value`、`plate_loaded_total_excluding_empty`、`unilateral_setting_per_side`、`external_added_weight`。模型不得换算或补猜。

`WorkoutSetInput` 与 `RecordsService.validate_record_facts` 负责组数、序号、次数、重量、时长、动作存在性和记录口径校验。校验失败时保留领域错误，且不产生确认载荷。

## 候选日程与确认

`candidate_plan_sessions` 只包含训练日期当天尚可关联的日程：

- 零个：提示用户选择“额外训练”。
- 一个：说明确认后自动关联该日程。
- 多个：要求用户选择日程或“额外训练”。

确认载荷包含 `workout.performed_on`、逐组事实、`plan_session_id`、`auto_link` 和 `candidate_plan_sessions`。模型只负责复述这些事实。

完整内容展示后，用户点击具体快照的确认按钮，通过独立确认 HTTP 调用现有保存服务；普通聊天文字不产生授权。确认请求绑定操作 ID、业务类型、快照与展示节点，重复提交返回原结果，模型不参与保存。HTTP 基线见 [结构化保存确认方案](../../../../tmp/optimization/structured-save-confirmation.md)。

## 记录即长期证据

- 三态 `set_type` 与口径一致是可比记录与渐进判定的前提：同一个动作、同一负重口径下的正式组记录才能
  互相比较，也才是后续渐进判定读得懂的事实。
- 主观感受字段只作自己观察用，不参与任何判定，不回写成结论。
- 单次波动不构成趋势：按
  [performance-factors.md](../../training-theory/references/performance-factors.md) 的三层分离，训练刺激、
  即时反应与长期适应分开看，一次训练的数字不能当作长期结论。

## 范围

本 Skill 不计算 PB、趋势、完成率、训练容量或估算 1RM，不生成或调整计划，不提供医疗判断。

