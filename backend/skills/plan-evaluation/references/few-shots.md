# 行为示例

示例里的目标、频率与状态都是演示数据，只说明检查口径。

## 示例 1：三项符合

**用户请求**：生成每周一练、目标增肌的计划。

**Tool 事实**：画像目标为增肌；当前计划与用户请求一致；`list_workouts` 近期无冲突；目录确认动作可核实；
计划的 `notes` 说明了目标、动作来源与仍缺的证据。

**期望行为**：三项都给出「符合」与一句理由，作为内容检查意见。

```json
{
  "goal_alignment": {"passed": true, "reason": "计划目标与画像及用户请求一致"},
  "schedule_reasonableness": {"passed": true, "reason": "训练日与休息日同当前事实无冲突"},
  "explanation_quality": {"passed": true, "reason": "notes 说明了目标、动作来源与待校准状态"}
}
```

## 示例 2：目标不一致

**用户请求**：沿用上次的计划；画像目标仍为增肌。

**Tool 事实**：待确认计划的目标写成了增强心肺耐力，本次请求没有改变目标。

**期望行为**：`goal_alignment` 给出「不符合」并说明依据；其余两项按事实分别给出。

```json
{
  "goal_alignment": {"passed": false, "reason": "计划目标与画像及用户请求不一致"},
  "schedule_reasonableness": {"passed": true, "reason": "训练日与休息日同当前事实无冲突"},
  "explanation_quality": {"passed": true, "reason": "notes 说明了本次采用的依据"}
}
```

## 示例 3：解释不足

**用户请求**：生成每周一练、目标增肌的计划。

**Tool 事实**：目标与安排均合理；`notes` 只有「已按目标安排」。

**期望行为**：`explanation_quality` 给出「不符合」，理由指出未说明依据、适用前提与仍缺的证据。

```json
{
  "goal_alignment": {"passed": true, "reason": "计划目标与画像及用户请求一致"},
  "schedule_reasonableness": {"passed": true, "reason": "训练日与休息日同当前事实无冲突"},
  "explanation_quality": {"passed": false, "reason": "notes 没有说明事实依据、适用前提与仍缺的证据"}
}
```

## 示例 4：依据不足

**Tool 事实**：画像缺少目标，用户本次请求也没有说明目标。

**期望行为**：`goal_alignment` 标记未知并说明缺哪条事实；不替用户补造目标，也不推断为任一目标。
