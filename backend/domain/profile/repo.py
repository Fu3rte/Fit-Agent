"""profile 业务表手写 SQL：``athlete_profile`` 单例行（id=1）的读取与整份覆盖写入（Stage 1 子任务 02 §5）。

全部访问经 ``storage.db.Database`` 的唯一连接与锁（07 7.1）。两条硬边界：

- **单例行**：画像只有 id=1 一行；载体行缺失即存储损坏，大声失败而不是当成「未建档」。
  未建档的技术载体是 ``profile_json IS NULL``，读取时返回 ``None``。
- **整份覆盖**：写入是一条 ``UPDATE``（PUT 语义），不需要外层事务；不复用旧实现的草稿确认
  写入路径，也不读写任何 ``context_version``（讨论总结 §8 已删除该机制）。

SQL 一律以字面量书写并参数化（storage/README 硬规则 3）。
"""

import aiosqlite

from domain.profile.schema import Profile, profile_from_json, profile_to_json
from storage.db import Database


async def _read_profile(conn: aiosqlite.Connection) -> Profile | None:
    async with conn.execute(
        "SELECT profile_json FROM athlete_profile WHERE id = 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:  # 001_initial.sql 恒建该行；缺失即载体损坏
        raise RuntimeError("athlete_profile 单例载体缺失（id=1）")
    raw = row["profile_json"]
    return None if raw is None else profile_from_json(str(raw))


class ProfileRepo:
    """``athlete_profile`` 单例行的读取与整份覆盖写入。"""

    def __init__(self, db: Database):
        self._db = db

    async def read(self) -> Profile | None:
        """读取画像；``profile_json`` 为 NULL（未建档）时返回 None。"""
        return await self._db.under_lock(_read_profile)

    async def write(self, profile: Profile) -> None:
        """整份覆盖写入画像七字段（一条 UPDATE，单语句写入不另开事务）。"""
        payload = profile_to_json(profile)

        async def op(conn: aiosqlite.Connection) -> None:
            cursor = await conn.execute(
                "UPDATE athlete_profile SET profile_json = ? WHERE id = 1",
                (payload,),
            )
            try:
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "athlete_profile 单例载体缺失（id=1），画像写入未生效"
                    )
            finally:
                await cursor.close()

        await self._db.under_lock(op)
