"""plan 业务表手写 SQL：``plan_versions``／``scheduled_sessions`` 的读取与确认事务内的写入（正本 architecture/04 4.1/4.2）。

S3-04 提供计划草稿创建与查询所需的读取，S3-06 只增加**确认事务内**的写入入口，全部经
``storage.db.Database`` 的唯一连接与锁：

- **当前正式计划**：``read_current`` 取 ``plan_versions`` 最新 ``version``。D9 的当前计划由
  ``user_profile.current_plan_version_id``（**或等价唯一指针**）确定：本阶段采用等价口径——
  ``version`` 只追加、确认事务内恰好 +1（S3-06 强制），因此最新 version 就是当前计划，
  历史版本（version 更小）即「已归档、保留历史」；不另建可失步的指针列。
- **事务内读取是独立入口**：``*_in_transaction`` 只接受外层 ``transaction()`` 给出的连接，供
  生成输入准备与确认事务在同一快照内读写（不嵌套取锁，锁不可重入）。
- **写入只在确认事务内**：``append_version_in_transaction``／``insert_sessions_in_transaction``
  ／``cancel_sessions_in_transaction``／``append_arrangement_revision_in_transaction`` 只接受
  外层 ``transaction()`` 连接，只由 ``app/confirm.py`` 的确认编排调用（唯一正式写入入口，
  不变量 7）；本层不自行 BEGIN／COMMIT、不推进 ``context_version``、不新增第二个版本计数器。
  取消只写 ``cancelled_at``：日程行不物理删除，已锁定与历史事实保留（04 4.2）。
- **当次安排修订只追加**（S3-08）：``arrangement_revisions`` 每次接受一笔**完整目标快照**
  （不只差异补丁）与真实 ``accepted_at``；``revision_no`` 由该日程当前最大值 +1 得出，
  「当次安排」即该日程 ``revision_no`` 最大的一行（与当前计划版本同口径的等价唯一指针）。

负载解码归 ``domain/plan/schema``（形状不符大声失败）；本层在解码后再做一次
``rules.validate_payload`` 结构复查，损坏行不静默吞掉。SQL 一律以字面量书写并参数化。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import aiosqlite

from domain.plan.rules import validate_payload
from domain.plan.schema import (
    PLAN_MODES,
    ArrangementTarget,
    InvalidPlanRow,
    PlanMode,
    PlanPayload,
    arrangement_target_from_json,
    arrangement_target_to_json,
    payload_from_json,
    payload_to_json,
)
from storage.db import Database, require_outer_transaction

_SELECT_VERSION = (
    "SELECT id, version, source_plan_version_id, starts_on, review_on, mode,"
    " payload_json, source_draft_id, confirmed_at FROM plan_versions"
)
_SELECT_SESSION = (
    "SELECT id, plan_version_id, plan_workout_key, scheduled_on, cancelled_at,"
    " locked_at FROM scheduled_sessions"
)
_SELECT_ARRANGEMENT = (
    "SELECT id, scheduled_session_id, revision_no, target_snapshot_json,"
    " source_draft_id, accepted_at FROM arrangement_revisions"
)


@dataclass(frozen=True, slots=True)
class PlanVersionRecord:
    """一条已确认的正式计划版本（``plan_versions`` 行 + 解码后的 D9 payload）。"""

    id: str
    version: int
    source_plan_version_id: str | None
    starts_on: date
    review_on: date
    mode: PlanMode
    payload: PlanPayload
    source_draft_id: str
    confirmed_at: str


@dataclass(frozen=True, slots=True)
class ScheduledSessionRecord:
    """一条应训练名额（``scheduled_sessions`` 行）。

    ``cancelled_at``／``locked_at`` 是存储状态：取消或存储锁定标记都不是判定锁定的唯一依据，
    到期锁定按日期规则由 ``rules.is_locked_by_date`` 独立判定（04 4.2）。
    """

    id: str
    plan_version_id: str
    plan_workout_key: str
    scheduled_on: date
    cancelled_at: str | None
    locked_at: str | None


@dataclass(frozen=True, slots=True)
class ArrangementRevisionRecord:
    """一条已接受的当次目标修订（``arrangement_revisions`` 行 + 解码后的目标快照）。

    ``accepted_at`` 是接受时的真实时间（不得倒填）；同一日程可有多条修订，``revision_no``
    只追加、恰好 +1，因此「当次安排」取该日程 ``revision_no`` 最大的一行（与当前计划版本同
    口径的等价唯一指针，不另建可失步的指针列）。
    """

    id: str
    scheduled_session_id: str
    revision_no: int
    target: ArrangementTarget
    source_draft_id: str
    accepted_at: str


def _row_to_arrangement(row: aiosqlite.Row) -> ArrangementRevisionRecord:
    return ArrangementRevisionRecord(
        id=str(row["id"]),
        scheduled_session_id=str(row["scheduled_session_id"]),
        revision_no=int(row["revision_no"]),
        target=arrangement_target_from_json(str(row["target_snapshot_json"])),
        source_draft_id=str(row["source_draft_id"]),
        accepted_at=str(row["accepted_at"]),
    )


async def _read_arrangement(
    conn: aiosqlite.Connection, arrangement_revision_id: str
) -> ArrangementRevisionRecord | None:
    async with conn.execute(
        f"{_SELECT_ARRANGEMENT} WHERE id = ?", (arrangement_revision_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _row_to_arrangement(row)


async def _read_arrangement_by_source_draft(
    conn: aiosqlite.Connection, draft_id: str
) -> ArrangementRevisionRecord | None:
    """按来源草稿读取该次确认写下的安排修订（幂等重放凭据用）。"""
    async with conn.execute(
        f"{_SELECT_ARRANGEMENT} WHERE source_draft_id = ?", (draft_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row is None:
            return None
        extra = await cursor.fetchone()
    if extra is not None:  # 一个草稿最多确认一次（01 1.4 幂等）；多行即数据损坏
        raise InvalidPlanRow(f"同一草稿对应多条安排修订：{draft_id}")
    return _row_to_arrangement(row)


async def _read_arrangement_for_session(
    conn: aiosqlite.Connection, scheduled_session_id: str
) -> ArrangementRevisionRecord | None:
    """该日程**最新**的一条安排修订（无任何接受时返回 None，不当成空目标）。"""
    async with conn.execute(
        f"{_SELECT_ARRANGEMENT} WHERE scheduled_session_id = ?"
        " ORDER BY revision_no DESC LIMIT 1",
        (scheduled_session_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _row_to_arrangement(row)


def _iso_date(label: str, raw: object) -> date:
    if not isinstance(raw, str):
        raise InvalidPlanRow(f"{label} 不是 ISO 日期文本：{raw!r}")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidPlanRow(f"{label} 不是 ISO 日期：{raw!r}") from exc


def _row_to_version(row: aiosqlite.Row) -> PlanVersionRecord:
    mode = str(row["mode"])
    if mode not in PLAN_MODES:
        raise InvalidPlanRow(f"计划模式非法：{mode!r}")
    payload = payload_from_json(str(row["payload_json"]))
    validate_payload(payload)
    source = row["source_plan_version_id"]
    return PlanVersionRecord(
        id=str(row["id"]),
        version=int(row["version"]),
        source_plan_version_id=None if source is None else str(source),
        starts_on=_iso_date("starts_on", row["starts_on"]),
        review_on=_iso_date("review_on", row["review_on"]),
        mode=mode,
        payload=payload,
        source_draft_id=str(row["source_draft_id"]),
        confirmed_at=str(row["confirmed_at"]),
    )


def _row_to_session(row: aiosqlite.Row) -> ScheduledSessionRecord:
    cancelled_at = row["cancelled_at"]
    locked_at = row["locked_at"]
    return ScheduledSessionRecord(
        id=str(row["id"]),
        plan_version_id=str(row["plan_version_id"]),
        plan_workout_key=str(row["plan_workout_key"]),
        scheduled_on=_iso_date("scheduled_on", row["scheduled_on"]),
        cancelled_at=None if cancelled_at is None else str(cancelled_at),
        locked_at=None if locked_at is None else str(locked_at),
    )


async def _read_current(conn: aiosqlite.Connection) -> PlanVersionRecord | None:
    async with conn.execute(
        f"{_SELECT_VERSION} ORDER BY version DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _row_to_version(row)


async def _read_version(
    conn: aiosqlite.Connection, plan_version_id: str
) -> PlanVersionRecord | None:
    async with conn.execute(
        f"{_SELECT_VERSION} WHERE id = ?", (plan_version_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None else _row_to_version(row)


async def _read_version_by_source_draft(
    conn: aiosqlite.Connection, draft_id: str
) -> PlanVersionRecord | None:
    """按来源草稿读取该次确认建立的计划版本；草稿未确认或已确认多次都显式区分。"""
    async with conn.execute(
        f"{_SELECT_VERSION} WHERE source_draft_id = ?", (draft_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row is None:
            return None
        extra = await cursor.fetchone()
    if extra is not None:  # 一个草稿最多确认一次（01 1.4 幂等）；多行即数据损坏
        raise InvalidPlanRow(f"同一草稿对应多个计划版本：{draft_id}")
    return _row_to_version(row)


async def _read_sessions(
    conn: aiosqlite.Connection, plan_version_id: str
) -> tuple[ScheduledSessionRecord, ...]:
    async with conn.execute(
        f"{_SELECT_SESSION} WHERE plan_version_id = ? ORDER BY scheduled_on, id",
        (plan_version_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return tuple(_row_to_session(row) for row in rows)


class PlanRepo:
    """``plan_versions``／``scheduled_sessions`` 的读取与确认事务内写入（S3-06）。

    读取入口（``read_current``／``read_version``／``list_sessions``）经唯一锁供普通查询；
    ``*_in_transaction`` 入口供生成准备与确认事务在同一快照内读写。写入入口（``append_version_in_transaction``
    ／``insert_sessions_in_transaction``／``cancel_sessions_in_transaction``）只接受外层事务连接，
    只由 ``app/confirm.py`` 的计划确认编排调用——正式事实只经确认事务写入（不变量 7）。
    """

    def __init__(self, db: Database):
        self._db = db

    async def read_current(self) -> PlanVersionRecord | None:
        """当前正式计划版本（最新 ``version``）；尚无正式计划时返回 None。"""
        return await self._db.under_lock(_read_current)

    async def read_current_in_transaction(
        self, conn: aiosqlite.Connection
    ) -> PlanVersionRecord | None:
        """在**外层事务**内读当前正式计划版本（同一快照，不嵌套取锁）。"""
        require_outer_transaction(conn, "当前计划读取")
        return await _read_current(conn)

    async def read_session_in_transaction(
        self, conn: aiosqlite.Connection, session_id: str
    ) -> ScheduledSessionRecord | None:
        """在**外层事务**内按身份读一条应训练名额；不存在即 None。

        确认事务要用它与安排草稿绑定的 ``scheduled_session_id`` 对账（含已取消行：取消不
        物理删除，安排确认必须自己判定它已不再是应训练义务）。
        """
        require_outer_transaction(conn, "日程身份读取")
        async with conn.execute(
            f"{_SELECT_SESSION} WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else _row_to_session(row)

    async def read_arrangement_revision(
        self, arrangement_revision_id: str
    ) -> ArrangementRevisionRecord | None:
        """按身份读取一条安排修订（含历史修订）；不存在即 None。"""
        return await self._db.under_lock(
            lambda conn: _read_arrangement(conn, arrangement_revision_id)
        )

    async def read_arrangement_revision_in_transaction(
        self, conn: aiosqlite.Connection, arrangement_revision_id: str
    ) -> ArrangementRevisionRecord | None:
        """在**外层事务**内按身份读取安排修订（同一快照，不嵌套取锁）。

        记录草稿的事务内复查需要它：按绑定的安排修订核对目标项对应关系（05 5.2「关联时
        准确匹配安排」），自行取锁会死锁（锁不可重入）。
        """
        require_outer_transaction(conn, "安排修订读取")
        return await _read_arrangement(conn, arrangement_revision_id)

    async def read_arrangement_by_source_draft(
        self, draft_id: str
    ) -> ArrangementRevisionRecord | None:
        """按来源草稿读取该次确认写下的安排修订（重复确认与关闭重开后读回同一份）。"""
        return await self._db.under_lock(
            lambda conn: _read_arrangement_by_source_draft(conn, draft_id)
        )

    async def read_arrangement_by_source_draft_in_transaction(
        self, conn: aiosqlite.Connection, draft_id: str
    ) -> ArrangementRevisionRecord | None:
        """在**外层事务**内按来源草稿读取安排修订（幂等重放凭据用，同一快照）。"""
        require_outer_transaction(conn, "安排修订来源读取")
        return await _read_arrangement_by_source_draft(conn, draft_id)

    async def read_latest_arrangement(
        self, scheduled_session_id: str
    ) -> ArrangementRevisionRecord | None:
        """该日程当前生效的安排修订（``revision_no`` 最大的一行）；未接受过为 None。"""
        return await self._db.under_lock(
            lambda conn: _read_arrangement_for_session(conn, scheduled_session_id)
        )

    async def append_arrangement_revision_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        arrangement_revision_id: str,
        scheduled_session_id: str,
        target: ArrangementTarget,
        source_draft_id: str,
        accepted_at: str,
    ) -> ArrangementRevisionRecord:
        """在**外层事务**内追加一条安排修订（只追加，不更新旧行）并读回。

        ``revision_no`` 由该日程当前最大 ``revision_no`` + 1 决定（首次为 1）：这就是
        「只追加、恰好 +1」的等价唯一指针口径，不另建可失步的指针列。``accepted_at`` 由
        确认事务传入接受当刻的真实时间（PRD §5.5：立即落盘、不得倒填）；目标快照必须是
        **完整**目标（不只差异补丁）。本方法不写 ``plan_versions``／``scheduled_sessions``
        ／``user_profile``，也不推进 ``context_version``（临时调整不改长期计划版本，04 4.3）。
        """
        require_outer_transaction(conn, "安排修订写入")
        async with conn.execute(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM arrangement_revisions"
            " WHERE scheduled_session_id = ?",
            (scheduled_session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:  # 聚合查询恒返回一行；缺失即连接异常
            raise RuntimeError("安排修订号读取失败")
        await conn.execute(
            "INSERT INTO arrangement_revisions (id, scheduled_session_id, revision_no,"
            " target_snapshot_json, source_draft_id, accepted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                arrangement_revision_id,
                scheduled_session_id,
                int(row[0]),
                arrangement_target_to_json(target),
                source_draft_id,
                accepted_at,
            ),
        )
        record = await _read_arrangement(conn, arrangement_revision_id)
        if record is None:  # 写成功却读不到即存储状态异常，显式失败不静默兜底
            raise RuntimeError(f"安排修订写入后读回失败：{arrangement_revision_id}")
        return record

    async def read_version(self, plan_version_id: str) -> PlanVersionRecord | None:
        """按身份读取指定计划版本（含历史版本；不存在即 None）。

        草稿视图据此读取绑定的替换基线：来源版本只追加不删除，重开或后续提交后读到
        的仍是生成时那一版（``source_plan_version_id`` 不指向「最新」）。
        """
        return await self._db.under_lock(
            lambda conn: _read_version(conn, plan_version_id)
        )

    async def read_version_in_transaction(
        self, conn: aiosqlite.Connection, plan_version_id: str
    ) -> PlanVersionRecord | None:
        """在**外层事务**内按身份读取指定计划版本（含历史版本；不存在即 None）。

        只读投影（S3-07）要在同一快照内读版本、当刻正式计划与日程，不能读了一半再取锁，
        因此与 :meth:`read_version` 同语义、只是复用外层事务连接。
        """
        require_outer_transaction(conn, "计划版本读取")
        return await _read_version(conn, plan_version_id)

    async def list_sessions(
        self, plan_version_id: str
    ) -> tuple[ScheduledSessionRecord, ...]:
        """按计划版本读取全部应训练名额（含已取消／已锁定；按应训练日排序）。"""
        return await self._db.under_lock(
            lambda conn: _read_sessions(conn, plan_version_id)
        )

    async def list_sessions_in_transaction(
        self, conn: aiosqlite.Connection, plan_version_id: str
    ) -> tuple[ScheduledSessionRecord, ...]:
        """在**外层事务**内按计划版本读取应训练名额（同一快照，不嵌套取锁）。"""
        require_outer_transaction(conn, "日程读取")
        return await _read_sessions(conn, plan_version_id)

    async def read_by_source_draft_in_transaction(
        self, conn: aiosqlite.Connection, draft_id: str
    ) -> PlanVersionRecord | None:
        """在**外层事务**内按来源草稿读取已建立的计划版本（幂等重放凭据用）。

        重复确认走幂等返回（不重查基线、不重复建版本），凭据要能指向首次确认建立的版本；
        该版本由本表按 ``source_draft_id`` 唯一确定，不依赖「最新 version」。
        """
        require_outer_transaction(conn, "计划版本来源读取")
        return await _read_version_by_source_draft(conn, draft_id)

    async def append_version_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        plan_version_id: str,
        source_plan_version_id: str | None,
        starts_on: date,
        review_on: date,
        mode: PlanMode,
        payload: PlanPayload,
        source_draft_id: str,
        confirmed_at: str,
    ) -> PlanVersionRecord:
        """在**外层事务**内追加一个正式计划版本（只追加，不更新旧行）并读回。

        ``version`` 由本表当前最大 ``version`` + 1 决定（首次为 1）：这就是「只追加、恰好 +1」
        的等价唯一指针口径（见模块说明）。并发由外层确认事务的唯一锁串行化；两个确认不可能
        同时读到同一最大值并各自 +1（S3-06 并发验收）。旧版本行不被改写，历史因此保留。
        """
        require_outer_transaction(conn, "计划版本写入")
        if mode not in PLAN_MODES:
            raise InvalidPlanRow(f"计划模式非法：{mode!r}")
        async with conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM plan_versions"
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:  # 聚合查询恒返回一行；缺失即连接异常
            raise RuntimeError("计划版本号读取失败")
        await conn.execute(
            "INSERT INTO plan_versions (id, version, source_plan_version_id,"
            " starts_on, review_on, mode, payload_json, source_draft_id, confirmed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_version_id,
                int(row[0]),
                source_plan_version_id,
                starts_on.isoformat(),
                review_on.isoformat(),
                mode,
                payload_to_json(payload),
                source_draft_id,
                confirmed_at,
            ),
        )
        record = await _read_version(conn, plan_version_id)
        if record is None:  # 写成功却读不到即存储状态异常，显式失败不静默兜底
            raise RuntimeError(f"计划版本写入后读回失败：{plan_version_id}")
        return record

    async def insert_sessions_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        sessions: Sequence[tuple[str, str, str, date]],
    ) -> None:
        """在**外层事务**内写入新投影的应训练名额（D9 投影结果，rest 槽不在此列）。

        每项为 ``(id, plan_version_id, plan_workout_key, scheduled_on)``；``cancelled_at``／
        ``locked_at`` 初始为 NULL——锁定按日期规则判定，不靠存储字段预写（04 4.2）。
        """
        require_outer_transaction(conn, "日程写入")
        for session_id, plan_version_id, plan_workout_key, scheduled_on in sessions:
            await conn.execute(
                "INSERT INTO scheduled_sessions (id, plan_version_id, plan_workout_key,"
                " scheduled_on, cancelled_at, locked_at) VALUES (?, ?, ?, ?, NULL, NULL)",
                (
                    session_id,
                    plan_version_id,
                    plan_workout_key,
                    scheduled_on.isoformat(),
                ),
            )

    async def cancel_sessions_in_transaction(
        self,
        conn: aiosqlite.Connection,
        *,
        session_ids: Sequence[str],
        cancelled_at: str,
    ) -> None:
        """在**外层事务**内取消指定的旧版日程（只写 ``cancelled_at``，不物理删除）。

        取消集由确认编排在事务内按当刻业务日期与规则重算（只含旧版未来未锁定日程），本层不
        自行筛选取未取消者：条件更新只对 ``cancelled_at IS NULL`` 的行生效，重复取消不重复
        改写时间（仍保留历史）。"""
        require_outer_transaction(conn, "日程取消")
        for session_id in session_ids:
            await conn.execute(
                "UPDATE scheduled_sessions SET cancelled_at = ?"
                " WHERE id = ? AND cancelled_at IS NULL",
                (cancelled_at, session_id),
            )
