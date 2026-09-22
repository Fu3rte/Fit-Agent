"""profile 业务表手写 SQL：``athlete_profile`` 单例行（id=1）的读取与整份覆盖写入。"""

import aiosqlite

from app.domain.profile.schema import Profile, profile_from_json, profile_to_json
from app.infrastructure.database.connection import Database
from app.infrastructure.database.repositories.tool_cache_repository import (
    ToolCacheRevisionsRepo,
)


async def _read_profile(conn: aiosqlite.Connection) -> Profile | None:
    async with conn.execute(
        "SELECT profile_json FROM athlete_profile WHERE id = 1"
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("athlete_profile 单例载体缺失（id=1）")
    raw = row["profile_json"]
    return None if raw is None else profile_from_json(str(raw))


class ProfileRepo:
    """``athlete_profile`` 单例行的读取与整份覆盖写入。"""

    def __init__(self, db: Database, revisions: ToolCacheRevisionsRepo):
        self._db = db
        self._revisions = revisions

    async def read(self) -> Profile | None:
        """读取画像；``profile_json`` 为 NULL（未建档）时返回 None。"""
        return await self._db.under_lock(_read_profile)

    async def write(self, profile: Profile) -> None:
        """整份覆盖写入画像七字段：画像行与 ``profile`` revision 同一事务提交或一起回滚。"""
        payload = profile_to_json(profile)

        async with self._db.transaction() as conn:
            # 先 bump 再写行：单例行缺失等写入失败必须把 revision 一起带走，不能虚报新版本。
            await self._revisions.bump_in_transaction(conn, "profile")
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
