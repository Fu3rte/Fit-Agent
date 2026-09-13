-- 015：Stage 4 S4-08 —— 重新生成草稿的父子关联与辅助 Run 类型
-- 依据：stage4.md S4-08（`parent_draft_id` 迁移按第 7 章连续编号与回滚规则）、
--       architecture/01 1.6（新草稿关联旧草稿、不自动合并／不自动确认）、
--       architecture/06 6.4（复盘仅显式请求生成）。
-- 边界：只加两列，不改既有列、不加业务表、不建第二套版本或状态机。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

-- 重新生成草稿的父子关联：新草稿指向触发它的旧草稿；NULL = 普通生成（不是重算产物）。
-- 外键只保证旧草稿存在；「旧草稿必须 pending 且与同一会话」由 app/draft_repo.py 在写入
-- 事务内校验（与 004 的组合外键同口径，本列无法用 ALTER TABLE 追加表级 CHECK）。
ALTER TABLE business_drafts ADD COLUMN parent_draft_id TEXT
    REFERENCES business_drafts(id);

-- Run 类型：chat = 对话请求（唯一有用户消息的 Run）；recalc = 按最新数据重新生成草稿；
-- review = 显式请求生成复盘正文。辅助 Run 不写用户消息、不进对话历史投影
-- （runtime/context.py 过滤），但仍复用同一 Run 状态机、单 Run 名额与取消语义。
ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'
    CHECK (kind IN ('chat', 'recalc', 'review'));
