-- 版本 12：会话消息节点新增可空结构化保存授权列。
-- 仅独立确认 HTTP 创建的真实用户节点可以携带；授权节点 run_id 为空、正文为后端固定确认文本。
-- 列约束只强制节点形状；授权内部字段与目标关系由确认操作账本与业务层校验。
ALTER TABLE session_entries ADD COLUMN confirmation TEXT
    CHECK (confirmation IS NULL OR (type = 'message' AND run_id IS NULL
        AND json_valid(confirmation) AND json_type(confirmation) IS 'object'
        AND json_extract(messages, '$[0].role') = 'user'));
