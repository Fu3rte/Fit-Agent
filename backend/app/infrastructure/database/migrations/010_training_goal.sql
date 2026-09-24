-- 训练目标收敛为闭集（增肌／增力／减脂）：恰为三值之一保留，其余已知值降为 unknown，不猜用户意图。
-- denied 表达「明确为空」，原样保留；三值以 app/domain/profile/schema.py 的 TRAINING_GOALS 为准。
UPDATE athlete_profile
SET profile_json = json_set(
    profile_json,
    '$.training_goal',
    json_object(
        'state', CASE
            WHEN json_extract(profile_json, '$.training_goal.state') = 'known'
                AND json_extract(profile_json, '$.training_goal.value') IN ('增肌', '增力', '减脂')
                THEN 'known'
            WHEN json_extract(profile_json, '$.training_goal.state') = 'denied' THEN 'denied'
            ELSE 'unknown'
        END,
        'value', CASE
            WHEN json_extract(profile_json, '$.training_goal.state') = 'known'
                AND json_extract(profile_json, '$.training_goal.value') IN ('增肌', '增力', '减脂')
                THEN json_extract(profile_json, '$.training_goal.value')
            ELSE NULL
        END
    )
)
WHERE profile_json IS NOT NULL;
