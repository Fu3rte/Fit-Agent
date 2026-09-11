"""records 用例编排：repo + rules 组合，供 app 层与 runtime.tools 使用（正本 architecture/05）。

S3-14 增加记录只读查询：稳定训练身份 + 当前修订的完整事实。三条硬边界：

- **只读**：不写 ``training_sessions``／``session_revisions``／``exercise_logs``／
  ``training_sets``，不推进 ``context_version``；正式事实只经确认事务写入（01 1.4）。
- **只消费当前修订**：一次身份一条当前修订（``current_revision_id`` 指向），旧修订不重复
  出现；同日多练是两个身份，不按日期合并（05 5.1／5.4）。
- **不派生第二份状态**：修订状态取存储的 ``session_revisions.status``（含 ``voided``），
  不按载荷另算一份；载荷只是当前修订的事实。
"""

from dataclasses import dataclass

import aiosqlite

from domain.records.repo import RecordRepo, TrainingSessionRecord
from domain.records.schema import InvalidRecordRow, RecordDraftPayload
from storage.db import Database


@dataclass(frozen=True, slots=True)
class TrainingRecordView:
    """一次实际训练的只读投影：稳定身份 + 当前修订摘要 + 当前修订的完整事实。

    ``session.current`` 给出修订摘要（``status`` 含 ``valid``／``incomplete``／``voided``），
    ``payload`` 是同一修订的完整动作与组事实（与草稿载荷同一结构）。
    """

    session: TrainingSessionRecord
    payload: RecordDraftPayload


class RecordReadService:
    """记录只读用例编排（05）：列出／按身份读取训练身份与当前修订事实；不写库。"""

    def __init__(self, db: Database):
        self._db = db
        self._records = RecordRepo(db)

    async def list_records(self) -> tuple[TrainingRecordView, ...]:
        """全部训练身份及其当前修订事实（按身份建立顺序，同日多练各自身份）。"""
        async with self._db.transaction() as conn:
            sessions = await self._records.list_sessions_in_transaction(conn)
            return tuple(
                [await self._view_in_transaction(conn, session) for session in sessions]
            )

    async def read_record(self, session_id: str) -> TrainingRecordView | None:
        """按身份读取一次训练与其当前修订事实；不存在即 ``None``（不创建、不推测归属）。"""
        async with self._db.transaction() as conn:
            session = await self._records.read_session_in_transaction(conn, session_id)
            if session is None:
                return None
            return await self._view_in_transaction(conn, session)

    async def _view_in_transaction(
        self, conn: aiosqlite.Connection, session: TrainingSessionRecord
    ) -> TrainingRecordView:
        """外层事务内取该身份的当前修订事实；指针为空即库内状态损坏，显式失败。"""
        payload = await self._records.read_current_payload_in_transaction(
            conn, session.id
        )
        if payload is None:
            raise InvalidRecordRow(f"训练身份没有当前修订：{session.id}")
        return TrainingRecordView(session=session, payload=payload)
