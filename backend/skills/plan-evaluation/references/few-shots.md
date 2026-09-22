# 行为示例

## 示例 1：全部通过

**用户请求**：生成每周一练、目标增肌的计划。

**Tool 事实**：画像目标为增肌，频率为 1；目录确认 `pull-up` 可推荐；日历无冲突。

```json
{
  "goal_alignment": {"passed": true, "reason": "候选目标与画像及请求一致"},
  "schedule_reasonableness": {"passed": true, "reason": "训练日与当前事实无冲突"},
  "explanation_quality": {"passed": true, "reason": "解释说明了目标、动作来源和待校准状态"}
}
```

**期望行为**：无阻断项与 warning，确定性结果和 revision 一致时通过。

## 示例 2：目标不匹配

**用户请求**：沿用上周计划；画像目标仍为增肌。

**Tool 事实**：候选目标写成增强心肺耐力，本次请求没有改变目标。

```json
{
  "goal_alignment": {"passed": false, "reason": "候选目标与画像及请求不一致"},
  "schedule_reasonableness": {"passed": true, "reason": "训练日与当前事实无冲突"},
  "explanation_quality": {"passed": true, "reason": "解释说明了候选采用的依据"}
}
```

**期望行为**：`goal_alignment` 进入 `blocking_failures`，本轮不通过。

## 示例 3：解释不足

**用户请求**：生成每周一练、目标增肌的计划。

**Tool 事实**：目标和日程均合理；解释只有“已按目标安排”。

```json
{
  "goal_alignment": {"passed": true, "reason": "候选目标与画像及请求一致"},
  "schedule_reasonableness": {"passed": true, "reason": "训练日与当前事实无冲突"},
  "explanation_quality": {"passed": false, "reason": "解释没有说明事实依据"}
}
```

**期望行为**：只产生 `explanation_quality` warning，本轮仍可通过。

## Revision 不一致

Planner 与 Evaluator 的同一事实域 revision 不一致时，确定性代码产生 `snapshot_mismatch`。三个 Rubric 维度无法解除该阻断。
