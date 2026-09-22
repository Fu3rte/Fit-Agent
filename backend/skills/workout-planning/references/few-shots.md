# 示例（workout-planning reference）

示例按「用户请求／Tool 事实／期望行为」组织，只展示当前判断需要的字段。

## 示例 1：照抄已有负荷

**用户请求**：我一周练一次，目标是增肌。

**Tool 事实**：四项必需事实已读齐，画像给出 `weekly_frequency=1`；`candidate_actions` 包含：

```json
[
  {
    "exercise_id": "barbell-back-squat",
    "record_type": "reps_weight",
    "starting_load": {
      "status": "known",
      "weight_kg": 60.0,
      "source_workout_session_id": 12,
      "source_set_no": 2
    }
  },
  {
    "exercise_id": "pull-up",
    "record_type": "reps_bodyweight",
    "starting_load": {"status": "needs_calibration"}
  }
]
```

**期望行为**：只使用这两个 canonical ID；背蹲完整照抄 `starting_load`，引体不携带负荷字段。

```json
{
  "goal": "增肌",
  "starts_on": "2026-06-01",
  "explanation": "每周一练；背蹲沿用最近有效工作组负荷，引体按自重次数记录。",
  "weekly_frequency": 1,
  "training_days": [
    {
      "scheduled_on": "2026-06-01",
      "exercises": [
        {
          "exercise_id": "barbell-back-squat",
          "sets": 3,
          "prescription": {
            "type": "weighted_reps",
            "reps_min": 5,
            "reps_max": 8,
            "load": {
              "status": "known",
              "weight_kg": 60.0,
              "source_workout_session_id": 12,
              "source_set_no": 2
            }
          }
        },
        {
          "exercise_id": "pull-up",
          "sets": 3,
          "prescription": {
            "type": "bodyweight_reps",
            "reps_min": 6,
            "reps_max": 10
          }
        }
      ]
    }
  ]
}
```

## 示例 2：无负荷时待校准

**用户请求**：我刚开始练，帮我安排杠铃卧推。

**Tool 事实**：`candidate_actions` 中 `barbell-bench-press` 的 `starting_load` 为
`{"status":"needs_calibration"}`。

**期望行为**：`weighted_reps` 只使用待校准状态，不猜空杆重量或其他数值。

```json
{
  "exercise_id": "barbell-bench-press",
  "sets": 3,
  "prescription": {
    "type": "weighted_reps",
    "reps_min": 8,
    "reps_max": 12,
    "load": {"status": "needs_calibration"}
  }
}
```

## 示例 3：目录无匹配

**用户请求**：计划里加入壶铃摆荡。

**Tool 事实**：对应检索没有产生任何 `candidate_actions`。

**期望行为**：不创建、猜测或改写 `exercise_id`；候选计划只能使用其余已返回动作，并在 `explanation` 说明
本次未纳入该动作。没有可用候选时明确说明目录缺少动作，停止生成无效草稿。
