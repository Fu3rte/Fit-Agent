"""records 名下业务表手写 SQL，只经 storage.db 的连接与锁（正本 architecture/05）。

S3-10 只需要**读**：同日既有训练身份（05 5.4 同日多练的归属歧义在草稿前显式解决）与既有
修订的完整事实（更正草稿的「基线 → 拟议」Diff 基线）。S3-11 增加记录侧正式写入：

- **写入只经确认事务**（01 1.4、不变量 7）：``create_session_in_transaction`` 与
  ``append_revision_in_transaction`` 只接受外层 ``transaction()`` 连接，只由
  ``app/confirm.py`` 的记录确认编排调用；本层不自行 BEGIN／COMMIT、不推进
  ``context_version``。
- **只追加，不删除**（05 5.3）：修订与其动作／逐组事实只 INSERT；对旧修订唯一的 UPDATE 是
  ``training_sessions.current_revision_id`` 的原子切换（旧修订保留、作废不物理删除）。
- ``read_by_source_draft_in_transaction`` 供已 Committed 草稿的幂等重放读回该次写下的修订
  （来源草稿唯一确定，不取「最新修订」）。

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
from typing import cast

import aiosqlite

from domain.records.rules import load_kg_key
from domain.records.schema import (
    Assistance,
    DraftExerciseLog,
    ExerciseLogFacts,
    InvalidRecordRow,
    LoadConvention,
    LoadUnit,
    RawLoad,
    RecordDraftPayload,
    RecordType,
    SetFacts,
    SetType,
    TimePrecision,
    _decode_int,
    _decode_optional_int,
    _decode_optional_number,
)
from storage.db import Database, require_outer_transaction

_SELECT_SESSION = (
    "SELECT s.id, s.created_at, s.current_revision_id, r.revision_no, r.status,"
    " r.occurred_on FROM training_sessions s"
    " LEFT JOIN session_revisions r ON r.id = s.current_revision_id"
)
_SELECT_REVISION = (
    "SELECT id, session_id, revision_no, status, occurred_on FROM session_revisions"
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
            revision_no=_decode_int("revision_no", revision_no),
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


def _row_bool(label: str, raw: object) -> bool:
    """0／1 存储位 → bool；非整数或不在 0／1 内即数据损坏。

    库层 CHECK 已限制取值，但读取侧仍不得用 ``bool()`` 兜底：``bool(2.5)``／``bool('no')``
    会把损坏行静默读成「已申报完成」或「回归期」。
    """
    flag = _decode_int(label, raw)
    if flag not in (0, 1):
        raise InvalidRecordRow(f"{label} 必须是 0／1：{raw!r}")
    return bool(flag)


def _row_to_revision(row: aiosqlite.Row) -> SessionRevisionRecord:
    return SessionRevisionRecord(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        revision_no=_decode_int("revision_no", row["revision_no"]),
        status=str(row["status"]),
        occurred_on=_iso_date("occurred_on", row["occurred_on"]),
    )


async def _read_by_source_draft(
    conn: aiosqlite.Connection, draft_id: str
) -> SessionRevisionRecord | None:
    """按来源草稿读取该次确认写下的修订；一草稿最多确认一次，多行即数据损坏。"""
    async with conn.execute(
        f"{_SELECT_REVISION} WHERE source_draft_id = ?", (draft_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row is None:
            return None
        extra = await cursor.fetchone()
    if extra is not None:
        raise InvalidRecordRow(f"同一草稿对应多条训练修订：{draft_id}")
    return _row_to_revision(row)


async def _create_session(
    conn: aiosqlite.Connection, *, session_id: str, created_at: str
) -> None:
    await conn.execute(
        "INSERT INTO training_sessions (id, created_at) VALUES (?, ?)",
        (session_id, created_at),
    )


async def _insert_revision(
    conn: aiosqlite.Connection,
    *,
    revision_id: str,
    session_id: str,
    revision_no: int,
    previous_revision_id: str | None,
    status: str,
    source_draft_id: str,
    confirmed_at: str,
    payload: RecordDraftPayload,
) -> None:
    """只追加：INSERT 修订行 + 动作／逐组事实，再把当前修订指针原子切到本笔。"""
    await conn.execute(
        "INSERT INTO session_revisions (id, session_id, revision_no,"
        " previous_revision_id, status, occurred_on, started_at, time_precision,"
        " arrangement_revision_id, completion_declared, is_return_phase,"
        " feedback_json, source_draft_id, confirmed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            revision_id,
            session_id,
            revision_no,
            previous_revision_id,
            status,
            payload.occurred_on.isoformat(),
            payload.started_at,
            payload.time_precision,
            payload.arrangement_revision_id,
            1 if payload.completion_declared else 0,
            1 if payload.is_return_phase else 0,
            (
                None
                if payload.feedback is None
                else json.dumps(payload.feedback, ensure_ascii=False)
            ),
            source_draft_id,
            confirmed_at,
        ),
    )
    for item in payload.exercises:
        log_id = f"{revision_id}-log-{item.position}"
        await conn.execute(
            "INSERT INTO exercise_logs (id, session_revision_id, exercise_id, position,"
            " target_item_key, record_type, load_notation, warmup_summary_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                log_id,
                revision_id,
                item.facts.exercise_id,
                item.position,
                item.facts.target_item_key,
                item.facts.record_type,
                item.facts.load_notation,
                item.facts.warmup_summary_text,
            ),
        )
        for single in item.sets:
            await conn.execute(
                "INSERT INTO training_sets (id, exercise_log_id, set_no, set_type,"
                " target_set_key, load_value_text, load_unit, load_kg_key, reps,"
                " duration_seconds, rir, assistance, assisted_reps, quality_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{log_id}-set-{single.set_no}",
                    log_id,
                    single.set_no,
                    single.set_type,
                    single.target_set_key,
                    None if single.load is None else single.load.value_text,
                    None if single.load is None else single.load.unit,
                    load_kg_key(single.load),
                    single.reps,
                    single.duration_seconds,
                    single.rir,
                    single.assistance,
                    single.assisted_reps,
                    single.quality_text,
                ),
            )
    cursor = await conn.execute(
        "UPDATE training_sessions SET current_revision_id = ? WHERE id = ?",
        (revision_id, session_id),
    )
    try:
        if cursor.rowcount != 1:
            raise InvalidRecordRow(f"训练身份不存在，无法切换当前修订：{session_id}")
    finally:
        await cursor.close()


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


async def _read_sessions(
    conn: aiosqlite.Connection,
) -> tuple[TrainingSessionRecord, ...]:
    """全部训练身份及其当前修订摘要（05 5.1：稳定身份列表；不按日期合并同日多练）。"""
    async with conn.execute(f"{_SELECT_SESSION} ORDER BY s.created_at, s.id") as cursor:
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
                position=_decode_int("position", log["position"]),
                facts=ExerciseLogFacts(
                    exercise_id=str(log["exercise_id"]),
                    record_type=cast(RecordType, str(log["record_type"])),
                    load_notation=(
                        None
                        if log["load_notation"] is None
                        else cast(LoadConvention, str(log["load_notation"]))
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
        load = RawLoad(
            value_text=str(load_value_text), unit=cast(LoadUnit, str(load_unit))
        )
    return SetFacts(
        set_no=_decode_int("set_no", row["set_no"]),
        set_type=cast(
            SetType | None,
            None if row["set_type"] is None else str(row["set_type"]),
        ),
        target_set_key=(
            None if row["target_set_key"] is None else str(row["target_set_key"])
        ),
        load=load,
        reps=_decode_optional_int("reps", row["reps"]),
        duration_seconds=_decode_optional_int(
            "duration_seconds", row["duration_seconds"]
        ),
        rir=_decode_optional_number("rir", row["rir"]),
        assistance=cast(
            Assistance | None,
            None if row["assistance"] is None else str(row["assistance"]),
        ),
        assisted_reps=_decode_optional_int("assisted_reps", row["assisted_reps"]),
        quality_text=None if row["quality_text"] is None else str(row["quality_text"]),
    )


async def _is_completed_arrangement(
    conn: aiosqlite.Connection, arrangement_revision_id: str
) -> bool:
    """该安排修订是否已有「当前有效修订申报完成且含至少一个实际工作组」的记录。

    只认当前修订（``training_sessions.current_revision_id``）与有效状态：旧修订不重复计入，
    待补全与已作废的当前修订都不算完成（06 6.1 分子、6.3）。
    """
    async with conn.execute(
        "SELECT 1 FROM session_revisions r"
        " JOIN training_sessions s ON s.current_revision_id = r.id"
        " WHERE r.arrangement_revision_id = ?"
        " AND r.status = 'valid' AND r.completion_declared = 1"
        " AND EXISTS (SELECT 1 FROM exercise_logs e"
        "   JOIN training_sets t ON t.exercise_log_id = e.id"
        "   WHERE e.session_revision_id = r.id AND t.set_type = 'work')"
        " LIMIT 1",
        (arrangement_revision_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


class RecordRepo:
    """``training_sessions``／``session_revisions``／``exercise_logs``／``training_sets`` 的读写。

    普通查询经唯一锁（``read_session``）；草稿准备与查询在单一事务快照内读（``*_in_transaction``），
    不嵌套取锁（锁不可重入）。写入入口（``create_session_in_transaction`` ／
    ``append_revision_in_transaction``）只接受外层事务连接，只由 ``app/confirm.py`` 的记录确认
    编排调用：正式事实只经确认事务写入（不变量 7），且只追加、不删除（05 5.3）。
    """

    def __init__(self, db: Database):
        self._db = db

    async def read_session(self, session_id: str) -> TrainingSessionRecord | None:
        """按身份读取训练身份与当前修订摘要；不存在即 None。"""
        return await self._db.under_lock(lambda conn: _read_session(conn, session_id))

    async def read_by_source_draft_in_transaction(
        self, conn: aiosqlite.Connection, draft_id: str
    ) -> SessionRevisionRecord | None:
        """在**外层事务**内按来源草稿读回该次确认写下的修订（幂等重放凭据用）。

        一个草稿最多确认一次（01 1.4）：重复确认因此能返回首次确认那一笔，而不依赖
        「最新修订」（后续更正／作废会推进当前修订，重放不得改成它们）。
        """
        require_outer_transaction(conn, "训练修订来源读取")
        return await _read_by_source_draft(conn, draft_id)

    async def create_session_in_transaction(
        self, conn: aiosqlite.Connection, *, session_id: str, created_at: str
    ) -> None:
        """在**外层事务**内建立一次实际训练的稳定身份（同日多练各自身份，05 5.4）。

        只 INSERT 身份行；首个修订与当前指针由同事务的
        :meth:`append_revision_in_transaction` 一并写入（身份与修订不会半边存在）。
        """
        require_outer_transaction(conn, "训练身份写入")
        await _create_session(conn, session_id=session_id, created_at=created_at)

    async def append_revision_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        revision_id: str,
        session_id: str,
        revision_no: int,
        previous_revision_id: str | None,
        status: str,
        source_draft_id: str,
        confirmed_at: str,
        payload: RecordDraftPayload,
    ) -> SessionRevisionRecord:
        """在**外层事务**内追加一笔完整训练修订（含全部动作与逐组事实）并切换当前修订指针。

        记录侧正式事实的唯一写入入口（只由记录确认编排调用）：

        - **只追加，不删除**：只 INSERT ``session_revisions``／``exercise_logs``／
          ``training_sets``，绝不 UPDATE／DELETE 旧修订或其子行；唯一 UPDATE 是
          ``training_sessions.current_revision_id`` 的原子切换（旧修订保留、作废不物理删除，
          05 5.3）。
        - ``revision_no`` 与 ``previous_revision_id`` 由调用方按该身份当前修订算出（只追加、
          恰好 +1）；本层不按日期或「最新」推断归属（05 5.4）。
        - 事实逐层原样落盘：未明确的可空字段保持 NULL，不补造工作组、RIR 0 或无辅助；显式
          声明的 ``assistance='none'`` 同样原样落盘（05 5.5）。
        - ``load_kg_key`` 经 :func:`domain.records.rules.load_kg_key` 唯一实现换算。
        """
        require_outer_transaction(conn, "训练修订写入")
        await _insert_revision(
            conn,
            revision_id=revision_id,
            session_id=session_id,
            revision_no=revision_no,
            previous_revision_id=previous_revision_id,
            status=status,
            source_draft_id=source_draft_id,
            confirmed_at=confirmed_at,
            payload=payload,
        )
        async with conn.execute(
            f"{_SELECT_REVISION} WHERE id = ?", (revision_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:  # 写成功却读不到即存储状态异常，显式失败不静默兜底
            raise InvalidRecordRow(f"训练修订写入后读回失败：{revision_id}")
        return _row_to_revision(row)

    async def read_session_in_transaction(
        self, conn: aiosqlite.Connection, session_id: str
    ) -> TrainingSessionRecord | None:
        """在**外层事务**内按身份读取训练身份（同一快照，不嵌套取锁）。"""
        require_outer_transaction(conn, "训练身份读取")
        return await _read_session(conn, session_id)

    async def is_completed_arrangement_revision_in_transaction(
        self, conn: aiosqlite.Connection, arrangement_revision_id: str
    ) -> bool:
        """在**外层事务**内判定该安排修订是否已由某次确认完成（完成率分子口径）。

        完成口径（06 6.1）：当前有效修订明确申报完成，且至少含一个实际工作组。
        """
        require_outer_transaction(conn, "已完成安排修订判定")
        return await _is_completed_arrangement(conn, arrangement_revision_id)

    async def list_sessions_on_in_transaction(
        self, conn: aiosqlite.Connection, occurred_on: date
    ) -> tuple[TrainingSessionRecord, ...]:
        """在**外层事务**内列出该日期现有训练身份（同日多练归属决策的显式输入）。"""
        require_outer_transaction(conn, "同日训练身份列表读取")
        return await _read_sessions_on(conn, occurred_on)

    async def list_sessions_in_transaction(
        self, conn: aiosqlite.Connection
    ) -> tuple[TrainingSessionRecord, ...]:
        """在**外层事务**内列出全部训练身份及其当前修订摘要（S3-14 记录查询）。

        只读：不按日期合并同日多练（05 5.1/5.4 稳定身份），不返回已排除的旧修订。
        """
        require_outer_transaction(conn, "训练身份列表读取")
        return await _read_sessions(conn)

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
            time_precision=cast(
                TimePrecision | None,
                None if time_precision is None else str(time_precision),
            ),
            completion_declared=_row_bool(
                "completion_declared", row["completion_declared"]
            ),
            is_return_phase=_row_bool("is_return_phase", row["is_return_phase"]),
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
