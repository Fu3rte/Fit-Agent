-- Stage 6 真实联调费用账本（08「Stage 6 联调费用护栏」；owner 2026-09-13 拍 USD 50）。
--
-- 一个 stage 一行：历史 Stage 4 的 USD 10 是脚本级护栏，不在此表留行，两批额度不混用。
-- reserved_usd 是在途预留（发送前预留、结算后清零）；跨重启保留，崩溃遗留由下一次 Run
-- 开始时按未知 usage 扣进 spent_usd（不释放预留额）。
CREATE TABLE fee_ledger (
    stage TEXT PRIMARY KEY,
    limit_usd REAL NOT NULL,
    spent_usd REAL NOT NULL,
    reserved_usd REAL NOT NULL,
    updated_at TEXT NOT NULL
);
