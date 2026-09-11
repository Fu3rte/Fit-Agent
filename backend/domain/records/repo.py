"""records 名下业务表手写 SQL，只经 storage.db 的连接与锁（正本 architecture/05）。

S3-10 只需要**读**：同日既有训练身份（05 5.4 同日多练的归属歧义在草稿前显式解决）与既有
修订的完整事实（更正草稿的「基线 → 拟议」Diff 基线）。记录侧正式写入（确认追加修订、切换
当前修订指针）归 S3-11，不在本层。

读取口径：

- ``TrainingSessionRecord.current`` 是该身份的当前修订摘要（``current_revision_id`` 指向的
  那一条）；指针为空表示该身份还没有任何修订（库层允许，草稿侧不得据此推断归属）。
- ``read_current_payload_in_transaction`` 把当前修订还原成
  :class:`~domain.records.schema.RecordDraftPayload`（与草稿载荷同一结构，Diff 才可逐层比较），
  ``training_session_id`` 填该身份 id、``schema_version`` 用当前草稿版本；读不到的指针或
  无法解码的行都是数据损坏，显式失败不静默兜底。
"""

import json
from dataclasses import dataclass
from datetime import date

import aiosqlite

from domain.records.schema import (
    DraftExerciseLog,
    ExerciseLogFacts,
    InvalidRecordRow,
    RawLoad,
    RecordDraftPayload,
    SetFacts,
)
from storage.db import Database, require_outer_transaction

_SELECT_SESSION = (
    "SELECT s.id, s.created_at, s.current_revision_id, r.revision_no, r.status,"
    " r.occurred_on FROM training_sessions s"
    " LEFT JOIN session_revisions r ON r.id = s.current_revision_id"
)


@dataclass(frozen=True, slots=True)
class SessionRevisionRecord:
    """一条训练修订的摘要（不含动作与组事实）；正式修订的状态含 ``voided``。"""

    id: str
    session_id: str
    revision_no: int
    status: str
    occurred_on: date


@dataclass(frozen=True, slots=True)
class TrainingSessionRecord:
    """一次实际训练的稳定身份 + 其当前修订摘要（``current=None`` 表示尚无修订）。"""

    id: str
    created_at: str
    current_revision_id: str | None
    current: SessionRevisionRecord | None


def _row_to_session(row: aiosqlite.Row) -> TrainingSessionRecord:
    current_revision_id = row["current_revision_id"]
    revision_no = row["revision_no"]
    current = None
    if current_revision_id is not None:
        if revision_no is None:
            # 当前修订指针指向的修订不存在：复合外键本不允许，读不到即数据损坏。
            raise InvalidRecordRow(
                f"训练身份 {row['id']} 的当前修订指针无对应修订：{current_revision_id}"
            )
        current = SessionRevisionRecord(
            id=str(current_revision_id),
            session_id=str(row["id"]),
            revision_no=int(revision_no),
            status=str(row["status"]),
            occurred_on=_iso_date("occurred_on", row["occurred_on"]),
        )
    return TrainingSessionRecord(
        id=str(row["id"]),
        created_at=str(row["created_at"]),
        current_revision_id=(
            None if current_revision_id is None else str(current_revision_id)
        ),
        current=current,
    )


def _iso_date(label: str, raw: object) -> date:
    if not isinstance(raw, str):
        raise InvalidRecordRow(f"{label} 不是 ISO 日期文本：{raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidRecordRow(f"{label} 不是 ISO 日期：{raw!r}") from exc


async def _read_session(
    conn: aiosqlite.Connection, session_id: str
) -> TrainingSessionRecord | None:
    async with conn.execute(
        f"{_SELECT_SESSION} WHERE s.id = ?", (session_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _row_to_session(row)


async def _read_sessions_on(
    conn: aiosqlite.Connection, occurred_on: date
) -> tuple[TrainingSessionRecord, ...]:
    """该日期现有的训练身份（按当前修订的实际发生日期归属）；日期不唯一（05 5.4）。"""
    async with conn.execute(
        f"{_SELECT_SESSION} WHERE r.occurred_on = ? ORDER BY s.created_at, s.id",
        (occurred_on.isoformat(),),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_row_to_session(row) for row in rows)


async def _read_exercise_logs(
    conn: aiosqlite.Connection, revision_id: str
) -> tuple[DraftExerciseLog, ...]:
    async with conn.execute(
        "SELECT id, exercise_id, position, target_item_key, record_type,"
        " load_notation, warmup_summary_text FROM exercise_logs"
        " WHERE session_revision_id = ? ORDER BY position",
        (revision_id,),
    ) as cursor:
        logs = await cursor.fetchall()
    result: list[DraftExerciseLog] = []
    for log in logs:
        sets = await _read_sets(conn, str(log["id"]))
        result.append(
            DraftExerciseLog(
                position=int(log["position"]),
                facts=ExerciseLogFacts(
                    exercise_id=str(log["exercise_id"]),
                    record_type=str(log["record_type"]),  # type: ignore[arg-type]
                    load_notation=(
                        None
                        if log["load_notation"] is None
                        else str(log["load_notation"])  # type: ignore[arg-type]
                    ),
                    target_item_key=(
                        None
                        if log["target_item_key"] is None
                        else str(log["target_item_key"])
                    ),
                    warmup_summary_text=(
                        None
                        if log["warmup_summary_text"] is None
                        else str(log["warmup_summary_text"])
                    ),
                ),
                sets=sets,
            )
        )
    return tuple(result)


async def _read_sets(
    conn: aiosqlite.Connection, exercise_log_id: str
) -> tuple[SetFacts, ...]:
    async with conn.execute(
        "SELECT set_no, set_type, target_set_key, load_value_text, load_unit, reps,"
        " duration_seconds, rir, assistance, assisted_reps, quality_text"
        " FROM training_sets WHERE exercise_log_id = ? ORDER BY set_no",
        (exercise_log_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_row_to_set(row) for row in rows)


def _row_to_set(row: aiosqlite.Row) -> SetFacts:
    load_value_text = row["load_value_text"]
    load_unit = row["load_unit"]
    load = None
    if load_value_text is not None or load_unit is not None:
        if load_value_text is None or load_unit is None:
            # 009 的 CHECK 要求原文与单位同现同隐；缺一即数据损坏。
            raise InvalidRecordRow("负重原文与单位必须同现同隐")
        load = RawLoad(value_text=str(load_value_text), unit=str(load_unit))  # type: ignore[arg-type]
    return SetFacts(
        set_no=int(row["set_no"]),
        set_type=None if row["set_type"] is None else str(row["set_type"]),  # type: ignore[arg-type]
        target_set_key=(
            None if row["target_set_key"] is None else str(row["target_set_key"])
        ),
        load=load,
        reps=None if row["reps"] is None else int(row["reps"]),
        duration_seconds=(
            None if row["duration_seconds"] is None else int(row["duration_seconds"])
        ),
        rir=None if row["rir"] is None else float(row["rir"]),
        assistance=(
            None if row["assistance"] is None else str(row["assistance"])  # type: ignore[arg-type]
        ),
        assisted_reps=(
            None if row["assisted_reps"] is None else int(row["assisted_reps"])
        ),
        quality_text=None if row["quality_text"] is None else str(row["quality_text"]),
    )


class RecordRepo:
    """``training_sessions``／``session_revisions``／``exercise_logs``／``training_sets`` 的读取。

    普通查询经唯一锁（``read_session``）；草稿准备与查询在单一事务快照内读（``*_in_transaction``），
    不嵌套取锁（锁不可重入）。写入入口归 S3-11 的确认事务，本层不提供。
    """

    def __init__(self, db: Database):
        self._db = db

    async def read_session(self, session_id: str) -> TrainingSessionRecord | None:
        """按身份读取训练身份与当前修订摘要；不存在即 None。"""
        return await self._db.under_lock(lambda conn: _read_session(conn, session_id))

    async def read_session_in_transaction(
        self, conn: aiosqlite.Connection, session_id: str
    ) -> TrainingSessionRecord | None:
        """在**外层事务**内按身份读取训练身份（同一快照，不嵌套取锁）。"""
        require_outer_transaction(conn, "训练身份读取")
        return await _read_session(conn, session_id)

    async def list_sessions_on_in_transaction(
        self, conn: aiosqlite.Connection, occurred_on: date
    ) -> tuple[TrainingSessionRecord, ...]:
        """在**外层事务**内列出该日期现有训练身份（同日多练归属决策的显式输入）。"""
        require_outer_transaction(conn, "同日训练身份列表读取")
        return await _read_sessions_on(conn, occurred_on)

    async def read_current_payload_in_transaction(
        self, conn: aiosqlite.Connection, session_id: str
    ) -> RecordDraftPayload | None:
        """当前修订的完整事实（与草稿载荷同结构）；身份不存在返回 None。

        尚无当前修订（指针为空）视为数据损坏：更正草稿的 Diff 需要基线，不能凭空当「无基线」。
        """
        require_outer_transaction(conn, "训练修订事实读取")
        session = await _read_session(conn, session_id)
        if session is None:
            return None
        if session.current is None:
            raise InvalidRecordRow(f"训练身份没有当前修订：{session_id}")
        async with conn.execute(
            "SELECT occurred_on, started_at, time_precision, arrangement_revision_id,"
            " completion_declared, is_return_phase, feedback_json"
            " FROM session_revisions WHERE id = ?",
            (session.current.id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise InvalidRecordRow(f"训练修订不存在：{session.current.id}")
        feedback_json = row["feedback_json"]
        started_at = row["started_at"]
        time_precision = row["time_precision"]
        return RecordDraftPayload(
            occurred_on=_iso_date("occurred_on", row["occurred_on"]),
            training_session_id=session.id,
            exercises=await _read_exercise_logs(conn, session.current.id),
            arrangement_revision_id=(
                None
                if row["arrangement_revision_id"] is None
                else str(row["arrangement_revision_id"])
            ),
            started_at=None if started_at is None else str(started_at),
            time_precision=(
                None if time_precision is None else str(time_precision)  # type: ignore[arg-type]
            ),
            completion_declared=bool(row["completion_declared"]),
            is_return_phase=bool(row["is_return_phase"]),
            feedback=(
                None if feedback_json is None else _decode_feedback(str(feedback_json))
            ),
        )


def _decode_feedback(raw: str) -> dict[str, object]:
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRecordRow(f"训练反馈 JSON 无法解析：{raw!r}") from exc
    if not isinstance(decoded, dict):
        raise InvalidRecordRow(f"训练反馈必须是 JSON 对象：{raw!r}")
    return decoded
