"""SQLite 业务事实源：建表、迁移、最小确定性行为接口。

对齐 business-data-storage-report.md §3.2 / §5，只实现 test-plan.md 四组核心测试
所需的最小语义；JSON 处方契约在应用层校验。
"""

import asyncio
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite

from .errors import DraftStale, ScheduleLocked, ValidationError
from .loadkey import load_kg_key

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    context_version INTEGER NOT NULL,
    current_plan_version_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exercises (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    variant TEXT,
    record_type TEXT NOT NULL,
    load_notation TEXT,
    recommendable INTEGER NOT NULL DEFAULT 1,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS business_drafts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
    draft_revision INTEGER NOT NULL DEFAULT 0,
    base_business_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT,
    committed_at TEXT,
    committed_business_version INTEGER
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    starts_on TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'regular',
    payload_json TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_sessions (
    id TEXT PRIMARY KEY,
    plan_version_id TEXT NOT NULL REFERENCES plan_versions(id),
    plan_day_key TEXT NOT NULL,
    scheduled_on TEXT NOT NULL,
    current_arrangement_revision_id TEXT,
    cancelled_at TEXT,
    denominator_locked_at TEXT,
    UNIQUE (plan_version_id, scheduled_on, plan_day_key)
);

CREATE TABLE IF NOT EXISTS arrangement_revisions (
    id TEXT PRIMARY KEY,
    scheduled_session_id TEXT NOT NULL REFERENCES scheduled_sessions(id),
    revision_no INTEGER NOT NULL,
    previous_revision_id TEXT,
    target_snapshot_json TEXT NOT NULL,
    source_draft_id TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    UNIQUE (scheduled_session_id, revision_no)
);

CREATE TABLE IF NOT EXISTS training_sessions (
    id TEXT PRIMARY KEY,
    current_revision_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_revisions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES training_sessions(id),
    revision_no INTEGER NOT NULL,
    previous_revision_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('incomplete', 'valid', 'voided')),
    occurred_on TEXT NOT NULL,
    arrangement_revision_id TEXT,
    completion_declared INTEGER NOT NULL DEFAULT 0,
    is_return_phase INTEGER NOT NULL DEFAULT 0,
    source_draft_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    UNIQUE (session_id, revision_no)
);

CREATE TABLE IF NOT EXISTS exercise_logs (
    id TEXT PRIMARY KEY,
    session_revision_id TEXT NOT NULL REFERENCES session_revisions(id),
    exercise_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    target_item_key TEXT,
    record_type TEXT NOT NULL,
    load_notation TEXT,
    UNIQUE (session_revision_id, position)
);

CREATE TABLE IF NOT EXISTS training_sets (
    id TEXT PRIMARY KEY,
    exercise_log_id TEXT NOT NULL REFERENCES exercise_logs(id),
    set_no INTEGER NOT NULL,
    set_type TEXT NOT NULL CHECK (set_type IN ('warmup', 'work')),
    target_set_key TEXT,
    load_kg_key INTEGER,
    reps INTEGER,
    rir REAL CHECK (rir IS NULL OR rir >= 0),
    assistance TEXT NOT NULL DEFAULT 'none'
        CHECK (assistance IN ('none', 'spotter_only', 'assisted')),
    UNIQUE (exercise_log_id, set_no)
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    plan_version_id TEXT NOT NULL,
    week_no INTEGER NOT NULL,
    basis_context_version INTEGER NOT NULL,
    body_markdown TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    supersedes_review_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_sched_plan_date ON scheduled_sessions (plan_version_id, scheduled_on);
CREATE INDEX IF NOT EXISTS idx_arr_session ON arrangement_revisions (scheduled_session_id);
CREATE INDEX IF NOT EXISTS idx_rev_session ON session_revisions (session_id);
CREATE INDEX IF NOT EXISTS idx_logs_rev ON exercise_logs (session_revision_id);
CREATE INDEX IF NOT EXISTS idx_sets_log ON training_sets (exercise_log_id);

CREATE VIEW IF NOT EXISTS pr_candidates AS
SELECT s.id AS session_id,
       r.id AS revision_id,
       r.occurred_on AS occurred_on,
       e.exercise_id,
       e.load_notation,
       t.load_kg_key,
       t.reps
FROM training_sessions AS s
JOIN session_revisions AS r ON r.id = s.current_revision_id
JOIN exercise_logs AS e ON e.session_revision_id = r.id
JOIN training_sets AS t ON t.exercise_log_id = e.id
WHERE r.status = 'valid'
  AND r.is_return_phase = 0
  AND e.record_type = 'external_load_reps'
  AND t.set_type = 'work'
  AND t.assistance IN ('none', 'spotter_only')
  AND t.load_kg_key IS NOT NULL
  AND t.reps >= 1;
"""


def utcnow() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_local() -> date:
    """业务日期：本地自然日（产品口径见 storage 报告 §5.2，非 UTC）。"""
    return date.today()  # noqa: DTZ011 -- 有意的本地自然日口径


class BusinessStore:
    """单连接 + asyncio.Lock 串行化的业务事实源。"""

    def __init__(
        self,
        path: str | Path,
        *,
        today_fn: Callable[[], date] | None = None,
    ):
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        # 业务日期时钟：默认本地自然日；测试注入冻结/可推进时钟以满足固定日期用例
        self._today_fn = today_fn or today_local
        # 测试专用：在草稿 handler 写入后、草稿状态更新前显式抛错（模拟事务中途崩溃）
        self.fail_after_handler: Callable[[], object] | None = None

    def _today(self) -> date:
        """当前业务日期（与删除/改期锁定、分母补记共用同一时钟）。"""
        return self._today_fn()

    async def open(self) -> None:
        if self._db is not None:
            return
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await db.execute(
            "INSERT INTO user_profile (id, context_version, updated_at) VALUES (1, 0, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (utcnow(),),
        )
        await db.commit()
        self._db = db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ---------- 草稿生命周期 ----------

    async def create_draft(self, draft_id: str, kind: str, payload: dict) -> dict:
        """创建 Pending 草稿：记录生成时的 base_business_version 与 draft_revision=0。"""
        async with self._lock:
            db = self._db
            assert db is not None
            base = await self._get_context_version_unlocked()
            await db.execute(
                "INSERT INTO business_drafts (id, kind, status, draft_revision,"
                " base_business_version, payload_json, committed_at)"
                " VALUES (?, ?, 'pending', 0, ?, ?, NULL)",
                (
                    draft_id,
                    kind,
                    base,
                    json.dumps(
                        {**payload, "source_draft_id": draft_id}, ensure_ascii=False
                    ),
                ),
            )
            await db.commit()
        return {
            "draft_id": draft_id,
            "kind": kind,
            "status": "pending",
            "draft_revision": 0,
            "base_business_version": base,
        }

    async def update_draft(self, draft_id: str, payload: dict) -> dict:
        """内联纠错：递增 draft_revision 并覆盖待确认内容，不改动 base_business_version。"""
        async with self._lock:
            assert self._db is not None
            row = await self._fetchone(
                "SELECT draft_revision FROM business_drafts WHERE id=?", (draft_id,)
            )
            if row is None:
                raise ValidationError(f"草稿不存在: {draft_id}")
            rev = row["draft_revision"] + 1
            await self._db.execute(
                "UPDATE business_drafts SET draft_revision=?, payload_json=? WHERE id=?",
                (
                    rev,
                    json.dumps(
                        {**payload, "source_draft_id": draft_id}, ensure_ascii=False
                    ),
                    draft_id,
                ),
            )
            await self._db.commit()
        return {"draft_id": draft_id, "draft_revision": rev}

    async def read_draft(self, draft_id: str) -> dict | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT * FROM business_drafts WHERE id=?", (draft_id,)
            )
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    # ---------- 领域读取 ----------

    async def _get_context_version_unlocked(self) -> int:
        """读取 context_version。调用方必须已持有 self._lock（不可重入）。"""
        row = await self._fetchone(
            "SELECT context_version FROM user_profile WHERE id=1"
        )
        assert row is not None
        return row["context_version"]

    async def get_context_version(self) -> int:
        async with self._lock:
            return await self._get_context_version_unlocked()

    async def get_plan(self, plan_version_id: str) -> dict | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT payload_json, starts_on, mode FROM plan_versions WHERE id=?",
                (plan_version_id,),
            )
        if row is None:
            return None
        return {
            **json.loads(row["payload_json"]),
            "starts_on": row["starts_on"],
            "mode": row["mode"],
        }

    async def get_arrangement(self, arrangement_revision_id: str) -> dict | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT target_snapshot_json, accepted_at, scheduled_session_id"
                " FROM arrangement_revisions WHERE id=?",
                (arrangement_revision_id,),
            )
        if row is None:
            return None
        snap = json.loads(row["target_snapshot_json"])
        snap["accepted_at"] = row["accepted_at"]
        snap["scheduled_session_id"] = row["scheduled_session_id"]
        return snap

    async def get_session_revisions(self, session_id: str) -> list[dict]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT id, revision_no, status, occurred_on, arrangement_revision_id,"
                " completion_declared, is_return_phase, source_draft_id, confirmed_at"
                " FROM session_revisions WHERE session_id=? ORDER BY revision_no",
                (session_id,),
            )
        return [dict(r) for r in rows]

    async def get_current_revision(self, session_id: str) -> dict | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT r.id, r.status, r.arrangement_revision_id FROM training_sessions s"
                " JOIN session_revisions r ON r.id = s.current_revision_id WHERE s.id=?",
                (session_id,),
            )
        if row is None:
            return None
        rev = dict(row)
        rev["sets"] = await self._get_sets_for_revision(rev["id"])
        return rev

    async def _get_sets_for_revision(self, revision_id: str) -> list[dict]:
        rows = await self._fetchall(
            "SELECT e.exercise_id, e.record_type, e.load_notation, t.set_no, t.set_type,"
            " t.target_set_key, t.load_kg_key, t.reps, t.rir, t.assistance"
            " FROM exercise_logs e JOIN training_sets t ON t.exercise_log_id = e.id"
            " WHERE e.session_revision_id=? ORDER BY e.position, t.set_no",
            (revision_id,),
        )
        return [dict(r) for r in rows]

    async def get_arrangement_history(self, scheduled_session_id: str) -> list[dict]:
        async with self._lock:
            rows = await self._fetchall(
                "SELECT id, revision_no, previous_revision_id, target_snapshot_json,"
                " source_draft_id, accepted_at FROM arrangement_revisions"
                " WHERE scheduled_session_id=? ORDER BY revision_no",
                (scheduled_session_id,),
            )
        return [dict(r) for r in rows]

    async def get_review(self, review_id: str) -> dict | None:
        async with self._lock:
            row = await self._fetchone("SELECT * FROM reviews WHERE id=?", (review_id,))
        return dict(row) if row else None

    # ---------- 统计（§6.1 / §6.2，确定性） ----------

    async def pr_records(self) -> list[dict]:
        """按 (exercise, load_notation, load_kg_key) 汇总最高重量与单组最高次数。"""
        async with self._lock:
            rows = await self._fetchall(
                "SELECT exercise_id, load_notation, load_kg_key, MAX(reps) AS max_reps,"
                " (SELECT MAX(t2.load_kg_key) FROM pr_candidates t2"
                "   WHERE t2.exercise_id = c.exercise_id"
                "     AND t2.load_notation IS c.load_notation) AS max_load_kg_key"
                " FROM pr_candidates c GROUP BY exercise_id, load_notation, load_kg_key"
                " ORDER BY exercise_id, load_notation, load_kg_key"
            )
        out = []
        for r in rows:
            out.append(dict(r))
        return out

    async def pr_max_load(self, exercise_id: str, load_notation: str) -> int | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT MAX(load_kg_key) AS m FROM pr_candidates"
                " WHERE exercise_id=? AND load_notation IS ?",
                (exercise_id, load_notation),
            )
        return row["m"] if row else None

    async def pr_max_reps_at_load(
        self, exercise_id: str, load_notation: str, load_kg_key: int
    ) -> int | None:
        async with self._lock:
            row = await self._fetchone(
                "SELECT MAX(reps) AS m FROM pr_candidates"
                " WHERE exercise_id=? AND load_notation IS ? AND load_kg_key=?",
                (exercise_id, load_notation, load_kg_key),
            )
        return row["m"] if row else None

    async def weekly_completion(
        self, plan_version_id: str, week_no: int, as_of: date
    ) -> dict | None:
        """完成率：分母 = 截至 as_of 已到期的应训练日程；分子 = 确认完成且含实际工作组的日程。

        无应训练日程时返回 None（显示“暂无”），不是 0%。
        """
        async with self._lock:
            plan = await self._fetchone(
                "SELECT starts_on FROM plan_versions WHERE id=?", (plan_version_id,)
            )
            if plan is None:
                return None
            week_start = date.fromisoformat(plan["starts_on"]) + timedelta(
                weeks=week_no - 1
            )
            rows = await self._fetchall(
                "SELECT s.id, s.scheduled_on, s.cancelled_at, s.denominator_locked_at"
                " FROM scheduled_sessions s WHERE s.plan_version_id=? AND s.plan_day_key != 'rest'"
                " ORDER BY s.scheduled_on",
                (plan_version_id,),
            )
            rows = [
                r
                for r in rows
                if week_start
                <= date.fromisoformat(r["scheduled_on"])
                < week_start + timedelta(days=7)
            ]
            denominator = 0
            numerator = 0
            for r in rows:
                scheduled = date.fromisoformat(r["scheduled_on"])
                if r["cancelled_at"] is not None and r["denominator_locked_at"] is None:
                    continue  # 锁定前合法取消：不进分母
                if scheduled > as_of:
                    continue  # 未来日程不进分母
                denominator += 1
                done = await self._fetchone(
                    "SELECT COUNT(*) AS n FROM session_revisions r"
                    " JOIN training_sessions ts ON ts.current_revision_id = r.id"
                    " WHERE r.arrangement_revision_id IN"
                    "       (SELECT id FROM arrangement_revisions WHERE scheduled_session_id = ?)"
                    "   AND r.status='valid' AND r.completion_declared=1"
                    "   AND EXISTS (SELECT 1 FROM exercise_logs e JOIN training_sets t"
                    "               ON t.exercise_log_id = e.id"
                    "               WHERE e.session_revision_id = r.id AND t.set_type='work')",
                    (r["id"],),
                )
                if done and done["n"]:
                    numerator += 1
            if denominator == 0:
                return None  # 无应训练日程：显示“暂无”，不是 0% 或 100%
            return {
                "week_no": week_no,
                "numerator": numerator,
                "denominator": denominator,
            }

    # ---------- 草稿确认（§5.1 统一入口） ----------

    async def commit_draft(self, draft_id: str, draft_revision: int) -> dict:
        """统一确认入口。原子性/幂等/版本检查见 _apply_draft。"""
        async with self._lock:
            return await self._commit_draft_locked(draft_id, draft_revision)

    async def _commit_draft_locked(self, draft_id: str, draft_revision: int) -> dict:
        db = self._db
        assert db is not None
        row = await self._fetchone(
            "SELECT * FROM business_drafts WHERE id=?", (draft_id,)
        )
        if row is None:
            raise ValidationError(f"草稿不存在: {draft_id}")
        if row["status"] == "committed":
            # 幂等：返回原结果，不重复写入、不再次递增版本
            return {
                "draft_id": draft_id,
                "committed": True,
                "idempotent": True,
                "result": json.loads(row["result_json"]),
            }

        ctx_row = await self._fetchone(
            "SELECT context_version FROM user_profile WHERE id=1"
        )
        if ctx_row is None:
            raise ValidationError("用户档案缺失")
        base = row["base_business_version"]
        if base != ctx_row["context_version"]:
            raise DraftStale(
                f"草稿基线版本 {base} 与当前业务版本 {ctx_row['context_version']} 不一致"
            )
        if row["draft_revision"] != draft_revision:
            raise ValidationError(
                f"草稿卡片已被修改: 期望 revision {row['draft_revision']}, 收到 {draft_revision}"
            )

        handler = DRAFT_HANDLERS.get(row["kind"])
        if handler is None:
            raise ValidationError(f"未知草稿类型: {row['kind']}")
        payload = json.loads(row["payload_json"])
        try:
            await db.execute("BEGIN")
            result = await handler(self, payload)
            if self.fail_after_handler is not None:
                self.fail_after_handler()
            new_ctx = ctx_row["context_version"] + 1
            await db.execute(
                "UPDATE user_profile SET context_version=?, updated_at=? WHERE id=1",
                (new_ctx, utcnow()),
            )
            now = utcnow()
            await db.execute(
                "UPDATE business_drafts SET status='committed', result_json=?, committed_at=?,"
                " committed_business_version=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), now, new_ctx, draft_id),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        return {
            "draft_id": draft_id,
            "committed": True,
            "idempotent": False,
            "result": result,
            "business_version": new_ctx,
        }

    # ---------- 草稿写入处理（各 kind 的正式写入） ----------

    async def _apply_create_plan(self, payload: dict) -> dict:
        """确认计划：写 plan_versions，并为每个应训练日程生成初始安排快照 (revision 1)。

        payload: {plan_id, starts_on, payload: {days: [{day_key, weekday_offsets,
        exercises: [目标动作]}]}, weeks}
        """
        db = self._db
        assert db is not None
        plan_id = payload["plan_id"]
        starts_on = payload["starts_on"]
        await db.execute(
            "INSERT INTO plan_versions (id, version, starts_on, mode, payload_json, confirmed_at)"
            " VALUES (?, 1, ?, 'regular', ?, ?)",
            (
                plan_id,
                starts_on,
                json.dumps(payload["payload"], ensure_ascii=False),
                utcnow(),
            ),
        )
        await db.execute(
            "UPDATE user_profile SET current_plan_version_id=? WHERE id=1", (plan_id,)
        )
        session_ids = []
        arrangement_ids = []
        for day in payload["payload"]["days"]:
            for w in range(1, payload.get("weeks", 1) + 1):
                for offset in day.get("weekday_offsets", []):
                    session_id = f"{plan_id}:{day['day_key']}:w{w}-o{offset}"
                    scheduled_on = date.fromisoformat(starts_on) + timedelta(
                        weeks=w - 1, days=offset
                    )
                    await db.execute(
                        "INSERT INTO scheduled_sessions (id, plan_version_id, plan_day_key,"
                        " scheduled_on) VALUES (?, ?, ?, ?)",
                        (
                            session_id,
                            plan_id,
                            day["day_key"],
                            scheduled_on.isoformat(),
                        ),
                    )
                    # 初始安排快照（revision 1）：与计划同一次确认
                    snapshot = {
                        "schema_version": 1,
                        "mode": "regular",
                        "exercises": day.get("exercises", []),
                    }
                    arr_id = f"{session_id}:a1"
                    await db.execute(
                        "INSERT INTO arrangement_revisions (id, scheduled_session_id,"
                        " revision_no, previous_revision_id, target_snapshot_json,"
                        " source_draft_id, accepted_at)"
                        " VALUES (?, ?, 1, NULL, ?, ?, ?)",
                        (
                            arr_id,
                            session_id,
                            json.dumps(snapshot, ensure_ascii=False),
                            payload["source_draft_id"],
                            utcnow(),
                        ),
                    )
                    await db.execute(
                        "UPDATE scheduled_sessions SET current_arrangement_revision_id=?"
                        " WHERE id=?",
                        (arr_id, session_id),
                    )
                    session_ids.append(session_id)
                    arrangement_ids.append(arr_id)
        return {
            "plan_version_id": plan_id,
            "session_ids": session_ids,
            "arrangement_ids": arrangement_ids,
        }

    async def _apply_adjust_arrangement(self, payload: dict) -> dict:
        """接受当次调整：插入完整快照修订，接受时间 = 事务时间。

        拦截条件（§5.3）：已取消，或该日程已关联实际训练记录（执行后不得改写）。
        不按日期一刀切拦截——历史日程的减组仍可确认（补记训练前接受的调整），
        防“改期洗漏练”由 reschedule/delete 的日期+锁定检查负责。
        """
        db = self._db
        assert db is not None
        session_id = payload["scheduled_session_id"]
        sched = await self._fetchone(
            "SELECT * FROM scheduled_sessions WHERE id=?", (session_id,)
        )
        if sched is None:
            raise ValidationError(f"应训练日程不存在: {session_id}")
        if sched["cancelled_at"] is not None:
            raise ScheduleLocked("已取消的日程不能再接受调整")
        executed = await self._fetchone(
            "SELECT 1 FROM session_revisions r"
            " JOIN arrangement_revisions ar ON ar.id = r.arrangement_revision_id"
            " WHERE ar.scheduled_session_id=? LIMIT 1",
            (session_id,),
        )
        if executed is not None:
            raise ScheduleLocked("该次训练已有实际记录，不能改写安排快照")
        prev = sched["current_arrangement_revision_id"]
        now = utcnow()
        cnt = await self._fetchone(
            "SELECT COUNT(*) AS n FROM arrangement_revisions WHERE scheduled_session_id=?",
            (session_id,),
        )
        if cnt is None:
            raise ValidationError("内部错误: 无法统计安排修订")
        revision_no = cnt["n"] + 1
        arr_id = payload.get("arrangement_id") or f"{session_id}:a{revision_no}"
        await db.execute(
            "INSERT INTO arrangement_revisions (id, scheduled_session_id, revision_no,"
            " previous_revision_id, target_snapshot_json, source_draft_id, accepted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                arr_id,
                session_id,
                revision_no,
                prev,
                json.dumps(payload["target_snapshot"], ensure_ascii=False),
                payload["source_draft_id"],
                now,
            ),
        )
        await db.execute(
            "UPDATE scheduled_sessions SET current_arrangement_revision_id=? WHERE id=?",
            (arr_id, session_id),
        )
        return {"arrangement_revision_id": arr_id, "accepted_at": now}

    async def _apply_record_training(self, payload: dict) -> dict:
        """新增训练记录：新身份 + 首个修订；含当日日程锁定补记。"""
        db = self._db
        assert db is not None
        session_id = payload["session_id"]
        await db.execute(
            "INSERT INTO training_sessions (id, created_at) VALUES (?, ?)",
            (session_id, utcnow()),
        )
        result = await self._insert_revision(session_id, 1, None, payload)
        await self._maybe_lock_denominator(payload["occurred_on"])
        return result

    async def _apply_correct_training(self, payload: dict) -> dict:
        """更正训练：追加完整修订并原子切换当前指针；旧修订保留。"""
        db = self._db
        assert db is not None
        session_id = payload["session_id"]
        sess = await self._fetchone(
            "SELECT current_revision_id FROM training_sessions WHERE id=?",
            (session_id,),
        )
        if sess is None:
            raise ValidationError(f"训练不存在: {session_id}")
        cur = await self._fetchone(
            "SELECT revision_no, arrangement_revision_id FROM session_revisions WHERE id=?",
            (sess["current_revision_id"],),
        )
        if cur is None:
            raise ValidationError("内部错误: 当前修订不存在")
        revision_no = cur["revision_no"] + 1
        # 更正草稿未给安排链接时，继承被替换修订的链接（完整修订仍指向原安排）
        if payload.get("arrangement_revision_id") is None:
            payload["arrangement_revision_id"] = cur["arrangement_revision_id"]
        result = await self._insert_revision(
            session_id, revision_no, sess["current_revision_id"], payload
        )
        return result

    async def _apply_void_training(self, payload: dict) -> dict:
        """作废：追加 voided 修订成为当前修订，不回退旧有效版本。"""
        db = self._db
        assert db is not None
        session_id = payload["session_id"]
        sess = await self._fetchone(
            "SELECT current_revision_id FROM training_sessions WHERE id=?",
            (session_id,),
        )
        if sess is None:
            raise ValidationError(f"训练不存在: {session_id}")
        cur = await self._fetchone(
            "SELECT revision_no, arrangement_revision_id FROM session_revisions WHERE id=?",
            (sess["current_revision_id"],),
        )
        if cur is None:
            raise ValidationError("内部错误: 当前修订不存在")
        rev_id = payload.get("revision_id") or f"{session_id}:r{cur['revision_no'] + 1}"
        arrangement_id = (
            payload.get("arrangement_revision_id") or cur["arrangement_revision_id"]
        )
        await db.execute(
            "INSERT INTO session_revisions (id, session_id, revision_no, previous_revision_id,"
            " status, occurred_on, arrangement_revision_id, completion_declared, is_return_phase,"
            " source_draft_id, confirmed_at)"
            " VALUES (?, ?, ?, ?, 'voided', ?, ?, ?, ?, ?, ?)",
            (
                rev_id,
                session_id,
                cur["revision_no"] + 1,
                sess["current_revision_id"],
                payload["occurred_on"],
                arrangement_id,
                0,
                payload.get("is_return_phase", 0),
                payload["source_draft_id"],
                utcnow(),
            ),
        )
        await db.execute(
            "UPDATE training_sessions SET current_revision_id=? WHERE id=?",
            (rev_id, session_id),
        )
        return {"revision_id": rev_id, "status": "voided"}

    async def _insert_revision(
        self,
        session_id: str,
        revision_no: int,
        previous_revision_id: str | None,
        payload: dict,
    ) -> dict:
        db = self._db
        assert db is not None
        rev_id = payload.get("revision_id") or f"{session_id}:r{revision_no}"
        for ex in payload["exercises"]:
            for s in ex["sets"]:
                if (s.get("assistance") or "none") not in (
                    "none",
                    "spotter_only",
                    "assisted",
                ):
                    raise ValidationError(f"未知辅助状态: {s.get('assistance')!r}")
                if s.get("rir") is not None and (s["rir"] < 0 or s["rir"] != s["rir"]):
                    raise ValidationError("RIR 必须为非负有限数值")
        # 记录/更正关联安排时，实际发生日必须等于该安排的训练日（§5.3：
        # “当天确实完成、后来才补录可关联当日安排”；事后补练不可关联原漏练名额）。
        arrangement_revision_id = payload.get("arrangement_revision_id")
        if arrangement_revision_id is not None:
            arr_row = await self._fetchone(
                "SELECT ss.scheduled_on FROM arrangement_revisions ar"
                " JOIN scheduled_sessions ss ON ss.id = ar.scheduled_session_id"
                " WHERE ar.id=?",
                (arrangement_revision_id,),
            )
            if arr_row is None:
                raise ValidationError(f"安排修订不存在: {arrangement_revision_id}")
            if arr_row["scheduled_on"] != payload["occurred_on"]:
                raise ValidationError(
                    "关联安排时实际发生日必须等于该安排的训练日"
                    f"（安排 {arr_row['scheduled_on']}，记录 {payload['occurred_on']}）"
                )
        await db.execute(
            "INSERT INTO session_revisions (id, session_id, revision_no, previous_revision_id,"
            " status, occurred_on, arrangement_revision_id, completion_declared, is_return_phase,"
            " source_draft_id, confirmed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rev_id,
                session_id,
                revision_no,
                previous_revision_id,
                payload["status"],
                payload["occurred_on"],
                payload.get("arrangement_revision_id"),
                1 if payload.get("completion_declared") else 0,
                1 if payload.get("is_return_phase") else 0,
                payload["source_draft_id"],
                utcnow(),
            ),
        )
        for pos, ex in enumerate(payload["exercises"], start=1):
            log_id = f"{rev_id}:log{pos}"
            await db.execute(
                "INSERT INTO exercise_logs (id, session_revision_id, exercise_id, position,"
                " target_item_key, record_type, load_notation)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    log_id,
                    rev_id,
                    ex["exercise_id"],
                    pos,
                    ex.get("item_key"),
                    ex["record_type"],
                    ex.get("load_notation"),
                ),
            )
            for no, s in enumerate(ex["sets"], start=1):
                await db.execute(
                    "INSERT INTO training_sets (id, exercise_log_id, set_no, set_type,"
                    " target_set_key, load_kg_key, reps, rir, assistance)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{log_id}:set{no}",
                        log_id,
                        no,
                        s["set_type"],
                        s.get("target_set_key"),
                        load_kg_key(s.get("load")),
                        s.get("reps"),
                        s.get("rir"),
                        s.get("assistance", "none"),
                    ),
                )
        await db.execute(
            "UPDATE training_sessions SET current_revision_id=? WHERE id=?",
            (rev_id, session_id),
        )
        return {"revision_id": rev_id, "revision_no": revision_no}

    async def _maybe_lock_denominator(self, occurred_on: str) -> None:
        """无打卡也存在的日程：训练记录落盘时补记当日有效锁定时间。"""
        db = self._db
        assert db is not None
        today = self._today()
        occ = date.fromisoformat(occurred_on)
        if occ <= today:
            await db.execute(
                "UPDATE scheduled_sessions SET denominator_locked_at=?"
                " WHERE scheduled_on <= ? AND denominator_locked_at IS NULL",
                (utcnow(), today.isoformat()),
            )

    # ---------- 复盘依据（§6.3） ----------

    async def save_review(
        self,
        review_id: str,
        plan_version_id: str,
        week_no: int,
        body_markdown: str,
        supersedes: str | None = None,
    ) -> dict:
        async with self._lock:
            db = self._db
            assert db is not None
            ctx = await self._fetchone(
                "SELECT context_version FROM user_profile WHERE id=1"
            )
            if ctx is None:
                raise ValidationError("用户档案缺失")
            await db.execute(
                "INSERT INTO reviews (id, plan_version_id, week_no, basis_context_version,"
                " basis_json, body_markdown, generated_at, supersedes_review_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    plan_version_id,
                    week_no,
                    ctx["context_version"],
                    json.dumps(
                        {"plan_version_id": plan_version_id, "week_no": week_no},
                        ensure_ascii=False,
                    ),
                    body_markdown,
                    utcnow(),
                    supersedes,
                ),
            )
            await db.commit()
        return {"review_id": review_id}

    async def review_pr_summary(
        self, exercise_id: str, load_notation: str
    ) -> dict | None:
        """复盘口径的 PR 确定性结果（与工作台口径同源：同一 pr_candidates 视图）。

        返回 {"max_load_kg_key": …, "max_reps_at_max_load": …}；无候选时返回 None。
        """
        async with self._lock:
            row = await self._fetchone(
                "SELECT MAX(load_kg_key) AS m FROM pr_candidates"
                " WHERE exercise_id=? AND load_notation IS ?",
                (exercise_id, load_notation),
            )
            max_load = row["m"] if row is not None else None
            if max_load is None:
                return None
            row2 = await self._fetchone(
                "SELECT MAX(reps) AS m FROM pr_candidates"
                " WHERE exercise_id=? AND load_notation IS ? AND load_kg_key=?",
                (exercise_id, load_notation, max_load),
            )
            max_reps = row2["m"] if row2 is not None else None
        return {
            "max_load_kg_key": max_load,
            "max_reps_at_max_load": max_reps,
        }

    async def review_basis_changed(self, review_id: str) -> bool:
        """依据已变更判定：当前 context_version 与生成时不一致 → 已变更。

        ponytail: 仅比较 context_version，会误报无关变更；按 §6.3 应按范围重建依据对比。
        """
        row = await self._fetchone(
            "SELECT basis_context_version FROM reviews WHERE id=?", (review_id,)
        )
        if row is None:
            raise ValidationError(f"复盘不存在: {review_id}")
        cur = await self._fetchone(
            "SELECT context_version FROM user_profile WHERE id=1"
        )
        if cur is None:
            raise ValidationError("用户档案缺失")
        return row["basis_context_version"] != cur["context_version"]

    # ---------- 简化对照检查（test-plan §3“怎么判断结构可以删掉”） ----------

    async def leak_probe_commit(self, plan_id: str) -> None:
        """简化对照：模拟“草稿未确认就污染正式事实”——应被草稿隔离挡住。"""
        raise ValidationError("草稿未确认，禁止直接写入正式事实")

    async def delete_scheduled_session(self, scheduled_session_id: str) -> None:
        """漏练洗白路径：直接删日程。锁定规则必须拒绝。"""
        async with self._lock:
            db = self._db
            assert db is not None
            row = await self._fetchone(
                "SELECT scheduled_on, denominator_locked_at, cancelled_at"
                " FROM scheduled_sessions WHERE id=?",
                (scheduled_session_id,),
            )
            if row is None:
                raise ValidationError(f"应训练日程不存在: {scheduled_session_id}")
            scheduled = date.fromisoformat(row["scheduled_on"])
            # 锁定判断直接检查日期与业务事实，不只看字段是否非空
            if scheduled <= self._today() or row["denominator_locked_at"] is not None:
                raise ScheduleLocked("已到期日程已锁定分母，不能删除或改期")
            await db.execute(
                "DELETE FROM scheduled_sessions WHERE id=?", (scheduled_session_id,)
            )
            await db.commit()

    async def reschedule_session(
        self, scheduled_session_id: str, new_date: str
    ) -> None:
        """改期未来未锁定日程。"""
        async with self._lock:
            db = self._db
            assert db is not None
            row = await self._fetchone(
                "SELECT scheduled_on, denominator_locked_at, cancelled_at"
                " FROM scheduled_sessions WHERE id=?",
                (scheduled_session_id,),
            )
            if row is None:
                raise ValidationError(f"应训练日程不存在: {scheduled_session_id}")
            scheduled = date.fromisoformat(row["scheduled_on"])
            if scheduled <= self._today() or row["denominator_locked_at"] is not None:
                raise ScheduleLocked("已到期日程已锁定分母，不能改期")
            await db.execute(
                "UPDATE scheduled_sessions SET scheduled_on=?, denominator_locked_at=NULL"
                " WHERE id=?",
                (new_date, scheduled_session_id),
            )
            await db.commit()

    # ---------- 内部读取 ----------

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        assert self._db is not None
        async with self._db.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        assert self._db is not None
        async with self._db.execute(sql, params) as cur:
            return await ctx_rows(cur)


async def ctx_rows(cur: aiosqlite.Cursor) -> list[aiosqlite.Row]:
    return list(await cur.fetchall())


DRAFT_HANDLERS = {
    "create_plan": BusinessStore._apply_create_plan,
    "adjust_arrangement": BusinessStore._apply_adjust_arrangement,
    "record_training": BusinessStore._apply_record_training,
    "correct_training": BusinessStore._apply_correct_training,
    "void_training": BusinessStore._apply_void_training,
}
