# 调整示例（plan-adjustment reference）

三个示例给出「请求 → 本次事实 → 候选输出」的最小对应关系：每例只放一个动作的事实片段，其余字段与
active 原样一致；动作身份与增重单位取自目录，不代表任何用户的真实记录。

## 示例 1：证据支持加重

**用户请求**：「深蹲最近很轻松，帮我加重量。」

**本次事实**：`barbell-back-squat` 的目录 `min_load_increment_kg` 为 `2.5`，两次关联训练都在 60kg 上完成三个目标
组、都达到 `reps_max` 8 次。

```json
{ "progression_decisions": [ {
    "exercise_id": "barbell-back-squat", "sets": 3, "reps_min": 5, "reps_max": 8,
    "target_load_kg": 60.0, "decision": { "action": "increase", "load_kg": 62.5 } } ] }
```

**候选输出**：只改负荷与 `progression_note`，组数与次数区间不变。

```json
{ "exercise_id": "barbell-back-squat", "sets": 3,
  "prescription": { "type": "weighted_reps", "reps_min": 5, "reps_max": 8,
                    "progression_note": "加重后从区间下部起步，逐次回到 8 次。",
                    "load": { "status": "known", "weight_kg": 62.5,
                              "source_workout_session_id": 22, "source_set_no": 3 } } }
```

负荷写 65kg 或 70kg 都会被确定性层以 `load_source_mismatch` 阻断，期望值是 62.5kg。

## 示例 2：证据不足保持原计划

**用户请求**：「深蹲涨不动了，直接上 70kg 吧。」

**本次事实**：两次关联训练都在 60kg 上的次数为 8/8/8 与 7/8/8，只有一次全部达到上限，两次都没有失败。

```json
{ "progression_decisions": [ {
    "exercise_id": "barbell-back-squat", "sets": 3, "reps_min": 5, "reps_max": 8,
    "target_load_kg": 60.0, "decision": { "action": "keep", "load_kg": 60.0 } } ] }
```

**候选输出**：负荷与组次不变；`explanation` 在 active 原句后说明只有一次达到上限、递增条件未满足。

```json
{ "exercise_id": "barbell-back-squat", "sets": 3,
  "prescription": { "type": "weighted_reps", "reps_min": 5, "reps_max": 8,
                    "progression_note": null,
                    "load": { "status": "known", "weight_kg": 60.0,
                              "source_workout_session_id": 22, "source_set_no": 3 } } }
```

按用户要求写 70kg 会被 `load_source_mismatch` 阻断；用缩窄次数区间为负荷找理由违反一次只改一个主要
变量。

## 示例 3：无可回退负荷进入待校准

**用户请求**：「这周深蹲老是做不完，帮我调一下。」

**本次事实**：两次关联训练为 5/4/4 与 5/3/4，两次都失败，且这段关联训练里没有任何一次完整完成。

```json
{ "progression_decisions": [ {
    "exercise_id": "barbell-back-squat", "sets": 3, "reps_min": 5, "reps_max": 8,
    "target_load_kg": 60.0,
    "decision": { "action": "needs_calibration", "load_kg": null } } ] }
```

**候选输出**：负荷改为待校准，组数与 5–8 次区间保持不变。

```json
{ "exercise_id": "barbell-back-squat", "sets": 3,
  "prescription": { "type": "weighted_reps", "reps_min": 5, "reps_max": 8,
                    "progression_note": "待校准：先记录一次能完整完成三个目标组的重量。",
                    "load": { "status": "needs_calibration" } } }
```

渐进决策为 `needs_calibration` 时给出任何具体重量都是 `load_source_mismatch`；减少组数不会被确定性层
拦下，但违反一次只改一个主要变量，候选里不得出现。
