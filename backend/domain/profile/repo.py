"""profile 业务表手写 SQL：正式档案读取与复用外层事务的内部写入（正本 architecture/02）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）。两条硬边界：

- **只读正式档案**：读取 ``user_profile`` 的档案 JSON 与统一业务版本，返回同一快照
  （01 1.4）。本层不提供建档旁路、不预填用户事实。
- **写入必须复用外层事务**：``write_in_transaction`` 只接受外层 ``transaction()`` 给出的
  连接；不在事务内即拒绝，本层不 BEGIN／COMMIT／ROLLBACK，也不改写 ``context_version``
  （推进归 Stage 2 确认事务，stage1.md §5 S1-04 验收 5）。

SQL 一律以字面量书写并参数化（storage/README 硬规则 3）。
"""

import aiosqlite

from domain.profile.schema import (
    Profile,
    ProfileSnapshot,
    profile_from_json,
    profile_to_json,
)
from storage.db import Database


class ProfileRepo:
    """``user_profile`` 单例行（id=1）的读取与事务内写入。"""

    def __init__(self, db: Database):
        self._db = db

    async def read(self) -> ProfileSnapshot:
        """读取正式档案与 ``context_version`` 同一快照；未建档时 ``profile`` 为 None。"""

        async def op(conn: aiosqlite.Connection) -> ProfileSnapshot:
            async with conn.execute(
                "SELECT profile_json, context_version FROM user_profile WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:  # 002 迁移恒建该行；缺失即载体损坏
                raise RuntimeError("user_profile 单例载体缺失（id=1）")
            raw = row["profile_json"]
            profile = None if raw is None else profile_from_json(str(raw))
            return ProfileSnapshot(
                profile=profile, context_version=int(row["context_version"])
            )

        return await self._db.under_lock(op)

    async def write_in_transaction(
        self, conn: aiosqlite.Connection, profile: Profile
    ) -> None:
        """在**外层事务**内写入档案 JSON；不提交、不改 ``context_version``。

        ``conn`` 必须是 ``Database.transaction()`` 交出的连接：本函数只发一条 UPDATE，
        原子性、版本递增与草稿状态变更由外层确认事务统一负责（01 1.4；Stage 2 接线）。
        异常时由外层事务回滚，档案与版本一并保持原样。
        """
        if not conn.in_transaction:
            raise RuntimeError(
                "档案写入必须复用外层事务（Database.transaction()）：本层不自行 BEGIN/COMMIT"
            )
        payload = profile_to_json(profile)
        cursor = await conn.execute(
            "UPDATE user_profile SET profile_json = ? WHERE id = 1", (payload,)
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError("user_profile 单例载体缺失（id=1），档案写入未生效")
        finally:
            await cursor.close()
