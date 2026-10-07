-- 旧记录须经显式授权清理；迁移仅删除已空的旧结构。
CREATE TEMP TABLE legacy_confirmation_cleanup_guard (
    empty INTEGER NOT NULL CHECK (empty = 1)
) STRICT;
INSERT INTO legacy_confirmation_cleanup_guard (empty)
SELECT NOT EXISTS (SELECT 1 FROM confirmations)
   AND NOT EXISTS (SELECT 1 FROM confirmation_receipts);
DROP TABLE legacy_confirmation_cleanup_guard;
DROP TABLE confirmations;
DROP TABLE confirmation_receipts;
