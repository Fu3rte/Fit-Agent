# 行为示例

业务日均为 `2026-06-01`。

## 示例 1：外加重量

**用户请求**：今天练了负重引体，热身 5 公斤 5 次，正式组 10 公斤 5 次，共三组。

**Tool 事实**：`weighted-pull-up` 的 `record_type` 为 `reps_weight`，`load_convention` 为 `external_added_weight`；当天恰有一个候选日程。

```json
{
  "performed_on": "2026-06-01",
  "sets": [
    {"exercise_id": "weighted-pull-up", "set_no": 1, "set_type": "warmup", "reps": 5, "load_convention": "external_added_weight", "weight_kg": 5.0},
    {"exercise_id": "weighted-pull-up", "set_no": 2, "set_type": "work", "reps": 5, "load_convention": "external_added_weight", "weight_kg": 10.0},
    {"exercise_id": "weighted-pull-up", "set_no": 3, "set_type": "work", "reps": 5, "load_convention": "external_added_weight", "weight_kg": 10.0},
    {"exercise_id": "weighted-pull-up", "set_no": 4, "set_type": "work", "reps": 5, "load_convention": "external_added_weight", "weight_kg": 10.0}
  ]
}
```

**期望行为**：复述日期和四组事实，说明确认后自动关联唯一候选日程；确认前零写入。

## 示例 2：相对日期与无负荷动作

**用户请求**：昨天做了 4 组引体，8、7、7、6 次，再做 45 秒和 60 秒平板支撑。

**Tool 事实**：`pull-up` 为 `reps_bodyweight`，`plank` 为 `time`；当天没有候选日程。

```json
{
  "performed_on": "2026-05-31",
  "sets": [
    {"exercise_id": "pull-up", "set_no": 1, "set_type": "work", "reps": 8},
    {"exercise_id": "pull-up", "set_no": 2, "set_type": "work", "reps": 7},
    {"exercise_id": "pull-up", "set_no": 3, "set_type": "work", "reps": 7},
    {"exercise_id": "pull-up", "set_no": 4, "set_type": "work", "reps": 6},
    {"exercise_id": "plank", "set_no": 1, "set_type": "work", "duration_seconds": 45},
    {"exercise_id": "plank", "set_no": 2, "set_type": "work", "duration_seconds": 60}
  ]
}
```

**期望行为**：将“昨天”解释为 `2026-05-31`，两个动作分别从 `set_no=1` 开始，提示用户选择“额外训练”。

## 示例 3：目录无匹配

**用户请求**：今天做了 4 组颈后推举，每组 10 次。

**Tool 事实**：canonical 目录没有匹配动作。

**期望行为**：不生成 `exercise_id`，不产生确认载荷，说明动作无法匹配并请用户使用目录标准名或别名重述。

多个候选日程时，要求用户选择具体日程或“额外训练”。
