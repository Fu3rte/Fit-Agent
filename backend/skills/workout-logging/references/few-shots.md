# 行为示例

示例里的 ID、字段值与日期都是演示数据，`exercise_id` 取当前动作目录已存在的记录。可信业务日为
`2026-06-01`。

## 示例 1：新增打卡

**用户请求**：今天练了负重引体，10 公斤 5 次做了三组。

**Tool 事实**：`search_exercises` 以 `name="负重引体"` 命中 `0841`（负重引体向上，`equipment="weighted"`）；
`get_exercise_details` 返回该动作 `load_convention="added_weight"`；`list_workouts` 当天返回 `items=[]`。

**期望行为**：新增记录的两项基础身份明确写 JSON null；动作身份用目录 id；重量有值给出 `load_convention`；
`notes` 无反馈写 null。

```json
{
  "performed_on": "2026-06-01",
  "base_workout_id": null,
  "base_workout_version": null,
  "payload": {
    "exercises": [
      {
        "exercise_id": "0841",
        "name": "负重引体向上",
        "load_convention": "added_weight",
        "sets": [
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null},
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null},
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null}
        ]
      }
    ],
    "notes": null
  }
}
```

## 示例 2：同日更新，保留已有反馈

**用户请求**：其实今天那次引体是 4 组，再加两组 8 次的引体向上。

**Tool 事实**：`list_workouts(date_from="2026-06-01", date_to="2026-06-01")` 返回当天已有记录
`id="0f6d2c1a-8b3e-4a51-9d7c-2e5f0a1b3c4d"`、`version=2`，内容为 3 组负重引体向上，
`notes="膝盖有点不舒服"`。

**期望行为**：`base_workout_id` 与 `base_workout_version` 用该记录的真实 ID 与版本；提交完整当天内容并保留
本次未涉及的部分。本次没有新增反馈，`notes` 原样保留查询到的 `"膝盖有点不舒服"`，不写 null。负重引体向上
改为 4 组，追加两组 8 次的引体向上（目录 id `0652`）。

```json
{
  "performed_on": "2026-06-01",
  "base_workout_id": "0f6d2c1a-8b3e-4a51-9d7c-2e5f0a1b3c4d",
  "base_workout_version": 2,
  "payload": {
    "exercises": [
      {
        "exercise_id": "0841",
        "name": "负重引体向上",
        "load_convention": "added_weight",
        "sets": [
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null},
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null},
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null},
          {"reps": 5, "weight_kg": 10.0, "duration_seconds": null}
        ]
      },
      {
        "exercise_id": "0652",
        "name": "引体向上",
        "load_convention": null,
        "sets": [
          {"reps": 8, "weight_kg": null, "duration_seconds": null},
          {"reps": 8, "weight_kg": null, "duration_seconds": null}
        ]
      }
    ],
    "notes": "膝盖有点不舒服"
  }
}
```

## 示例 3：目录外的动作

**用户请求**：今天做了 4 组颈后推举，每组 10 次。

**Tool 事实**：`search_exercises` 以 `name="颈后推举"` 返回 `items=[]`。

**期望行为**：换标准名、别名或器械词再检索一次，并如实说明这是新的检索结果；仍无匹配时保留用户给出的真实
名称、`exercise_id` 写 null（工具接受目录外动作），正常准备待确认快照，并说明该动作未经目录核实、相关限制
无从确认，只按用户给出的名称记录。只有同一称呼对应多个可能动作、存在必要歧义时，才先向用户澄清再准备。

## 示例 4：无法核实的重量口径

**用户请求**：今天硬拉 100 公斤 5 次。

**Tool 事实**：`search_exercises` 命中 `0032`（杠铃硬拉）；`get_exercise_details` 返回
`load_convention="barbell_total"`。

**期望行为**：重量有值必须写明口径，按目录返回的 `barbell_total` 写入，并说明该数字含杠铃杆总重。目录里
没有该动作、拿不到口径时不臆定，向用户澄清重量怎么计。

## 示例 5：普通聊天确认不保存

**用户请求**：确认。

**期望行为**：普通聊天文字不产生保存授权。说明需要点击对应快照的确认按钮；按钮点击后由独立确认 HTTP 入口
保存。结果未知时按原 `proposal_id` 调用 `get_workout_save_status` 核对原操作，不声称已经保存。
