-- 007：画像 revision namespace（tool_cache_revisions 的 CHECK 取值域加入 'profile'）
-- 依据：ST-04 计划路径四域 revision；exercises 目录变化仍由 schema_version 承担，不新增 catalog namespace。
-- 重建范围与原因：SQLite 无法 ALTER 既有 CHECK，取值域新增 'profile' 必须重建 tool_cache_revisions；
--       既有三行（plans／workouts／metrics 的当前 revision）原样搬入新表，profile 从 0 起。
-- 边界：不改 001_initial.sql；本表列结构不变（namespace 主键 + revision NOT NULL）；
--       本文件不含 BEGIN/COMMIT/PRAGMA user_version，由 app/infrastructure/database/migrations.py 统一包事务并推进到 7。

CREATE TABLE tool_cache_revisions_new (
    namespace TEXT PRIMARY KEY CHECK (namespace IN ('plans', 'workouts', 'metrics', 'profile')),
    revision INTEGER NOT NULL
);

INSERT INTO tool_cache_revisions_new (namespace, revision)
    SELECT namespace, revision FROM tool_cache_revisions;

INSERT INTO tool_cache_revisions_new (namespace, revision) VALUES ('profile', 0);

DROP TABLE tool_cache_revisions;

ALTER TABLE tool_cache_revisions_new RENAME TO tool_cache_revisions;
