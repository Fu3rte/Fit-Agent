-- 已知器械列表按是否含器械归类；denied 可能是拒绝提供，保持 denied；未知保持未知。
UPDATE athlete_profile
SET profile_json = json_set(
    json_remove(profile_json, '$.available_equipment'),
    '$.training_mode',
    json_object(
        'state', CASE json_extract(profile_json, '$.available_equipment.state')
            WHEN 'known' THEN 'known'
            WHEN 'denied' THEN 'denied'
            ELSE 'unknown'
        END,
        'value', CASE json_extract(profile_json, '$.available_equipment.state')
            WHEN 'known' THEN CASE
                WHEN EXISTS (
                    SELECT 1 FROM json_each(profile_json, '$.available_equipment.value')
                    WHERE lower(value) NOT IN ('bodyweight', 'body weight', '徒手', '自重')
                ) THEN 'equipment'
                ELSE 'bodyweight'
            END
            ELSE NULL
        END
    )
)
WHERE profile_json IS NOT NULL;
