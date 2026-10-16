# 调整示例（plan-adjustment reference）

示例给出「请求 → 本次事实 → 工具参数」的对应关系。示例里的 ID、字段值与日期都是演示数据，`exercise_id`
取当前动作目录已存在的记录。需要工具参数时给出完整真实 schema。

## 示例 1：直接调整上传计划

**用户请求**：我上传了一份计划文件，直接把里面的深蹲从 5 组改成 3 组。

**Tool 事实**：`get_profile` 返回 `version=3`；`get_current_plan` 返回 `id=null`、`content=null`；
`list_workouts(date_from=null, date_to=business_date, page=1, page_size=10)` 返回 `items=[]`。

**期望行为**：上传计划调整使用真实画像版本与当前计划 ID 作依据。其余字段与原有说明保留，改动只落在目标
动作；修改位置、原值、新值、理由、日期与不确定性写进 `notes`。

```json
{
  "base_profile_version": 3,
  "base_plan_id": null,
  "payload": {
    "repeat": true,
    "days": [
      {
        "kind": "training",
        "focus": "下肢",
        "exercises": [
          {
            "exercise_id": "1436",
            "name": "杠铃高杠深蹲",
            "sets": 3,
            "reps": 5,
            "duration_seconds": null,
            "weight_kg": null,
            "load_convention": null,
            "rest_seconds": 120
          }
        ],
        "notes": null
      },
      {"kind": "rest", "focus": null, "exercises": [], "notes": null}
    ],
    "notes": "改动位置：/days/0/exercises/0/sets；原值 5，新值 3；依据：用户本次明确要求；本次没有可用的训练记录，负荷保持未知。",
    "suggested_fields": []
  }
}
```

## 示例 2：最近训练零条或不足十条

**用户请求**：根据我最近的训练调整一下计划。

**Tool 事实**：`list_workouts(date_from=null, date_to=business_date, page=1, page_size=10)` 返回 `items=[]`
（或只有若干条，不足十条）。

**期望行为**：使用返回的全部 `items`：0 条就是空数组，不足十条全部使用。记录不足或原因不明时说明依据与
不确定性，围绕本次调整收集必要信息；不编造记录、不把其他日期或他人的数据当成本次事实。若本次调整依赖
训练表现而当前没有记录，先向用户索取。

## 示例 3：按新增训练事实重新准备提案

**用户请求**：我刚补录了昨天的训练，按最新的再来一版。

**Tool 事实**：已有待确认提案 `proposal_id="..."`；用户补录后，`list_workouts` 能查到该条新记录。

**期望行为**：重新调用 `list_workouts` 查询、重新 `prepare_plan_adjustment` 准备完整提案，完整展示全部业务
字段、`notes` 与所用依据，等待用户点击新快照的确认按钮。同会话新快照成功使旧 pending 失效。若原操作结果
未知，按原 `proposal_id` 调用 `get_plan_save_status` 核对，核实前不创建新的保存操作。
