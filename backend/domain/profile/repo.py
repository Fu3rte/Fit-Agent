"""profile 业务表手写 SQL：正式档案读取与复用外层事务的内部写入（正本 architecture/02）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）。三条硬边界：

- **只读正式档案**：读取 ``user_profile`` 的档案 JSON 与统一业务版本，返回同一快照
  （01 1.4）。本层不提供建档旁路、不预填用户事实。
- **事务内读取是独立入口**：:meth:`ProfileRepo.read` 走唯一锁（普通查询用）；
  :meth:`ProfileRepo.read_in_transaction` 只接受外层 ``transaction()`` 给出的连接，供确认
  事务（S2-05）在同一事务内做基线检查与领域复查——在持锁事务内调用自取锁方法会死锁，
  因为锁不可重入（stage2.md §2 已记录该约束）。
- **写入必须复用外层事务**：``write_in_transaction`` 只接受外层 ``transaction()`` 给出的
  连接；不在事务内即拒绝，本层不 BEGIN／COMMIT／ROLLBACK，也不改写 ``context_version``
  （推进归 Stage 2 确认事务，stage1.md §5 S1-04 验收 5）。
- **业务版本只有一条推进路径**：``bump_context_version_in_transaction`` 是同一载体上唯一的
  ``context_version +1`` 语句，只由确认编排（``app/confirm.py``）在确认事务内调用一次；
  本层不在任何其他写入路径自行推进，也不新增第二个版本计数器（stage2.md §4.2、§5 S2-05）。

SQL 一律以字面量书写并参数化（storage/README 硬规则 3）。
"""

import aiosqlite

from domain.profile.schema import (
    Profile,
    ProfileSnapshot,
    profile_from_json,
    profile_to_json,
)
from storage.db import Database, require_outer_transaction


async def _read_snapshot(conn: aiosqlite.Connection) -> ProfileSnapshot:
    """用给定连接读档案与 ``context_version``（同一快照）；连接由调用方决定。"""
    async with conn.execute(
        "SELECT profile_json, context_version FROM user_profile WHERE id = 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:  # 002 迁移恒建该行；缺失即载体损坏
        raise RuntimeError("user_profile 单例载体缺失（id=1）")
    raw = row["profile_json"]
    profile = None if raw is None else profile_from_json(str(raw))
    return ProfileSnapshot(profile=profile, context_version=int(row["context_version"]))


class ProfileRepo:
    """``user_profile`` 单例行（id=1）的读取与事务内写入。"""

    def __init__(self, db: Database):
        self._db = db

    async def read(self) -> ProfileSnapshot:
        """读取正式档案与 ``context_version`` 同一快照；未建档时 ``profile`` 为 None。"""
        return await self._db.under_lock(_read_snapshot)

    async def read_in_transaction(self, conn: aiosqlite.Connection) -> ProfileSnapshot:
        """在**外层事务**内读取正式档案与 ``context_version`` 同一快照。

        ``conn`` 必须是 ``Database.transaction()`` 交出的连接：确认事务（S2-05）要在同一
        事务内做业务基线检查与领域复查，而 :meth:`read` 会自行取锁（锁不可重入，事务内调用
        即死锁）。本方法不自取锁、不 BEGIN／COMMIT，读到的就是该事务连接自己的快照，
        包括本事务尚未提交的写入；不会读到另一连接的快照。
        """
        require_outer_transaction(conn, "档案读取")
        return await _read_snapshot(conn)

    async def write_in_transaction(
        self, conn: aiosqlite.Connection, profile: Profile
    ) -> None:
        """在**外层事务**内写入档案 JSON；不提交、不改 ``context_version``。

        ``conn`` 必须是 ``Database.transaction()`` 交出的连接：本函数只发一条 UPDATE，
        原子性、版本递增与草稿状态变更由外层确认事务统一负责（01 1.4；Stage 2 接线）。
        异常时由外层事务回滚，档案与版本一并保持原样。
        """
        require_outer_transaction(conn, "档案写入")
        payload = profile_to_json(profile)
        cursor = await conn.execute(
            "UPDATE user_profile SET profile_json = ? WHERE id = 1", (payload,)
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError("user_profile 单例载体缺失（id=1），档案写入未生效")
        finally:
            await cursor.close()

    async def bump_context_version_in_transaction(
        self, conn: aiosqlite.Connection
    ) -> int:
        """在**外层事务**内把 ``context_version`` 推进恰好一次，返回推进后的版本。

        统一业务版本只有这一处推进语句（01 1.4：一次成功确认仅 +1），且只由确认编排
        （``app/confirm.py``）在确认事务内调用：本方法不提交、不回滚、不自行决定是否推进，
        异常时由外层事务回滚，版本与档案、草稿提交状态同成败。草稿创建／纠错／丢弃与普通
        档案写入都不调用本方法（01 1.3：草稿不推进业务版本）。
        """
        require_outer_transaction(conn, "业务版本推进")
        cursor = await conn.execute(
            "UPDATE user_profile SET context_version = context_version + 1 WHERE id = 1"
        )
        try:
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "user_profile 单例载体缺失（id=1），业务版本推进未生效"
                )
        finally:
            await cursor.close()
        async with conn.execute(
            "SELECT context_version FROM user_profile WHERE id = 1"
        ) as version_cursor:
            row = await version_cursor.fetchone()
        if row is None:  # 与 _read_snapshot 同口径：单例载体缺失即损坏
            raise RuntimeError("user_profile 单例载体缺失（id=1）")
        return int(row["context_version"])
