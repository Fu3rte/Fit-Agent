"""业务时区与 Provider 配置（含密钥安全投影 has_api_key）的存取（07 7.3、10.3）。

密钥边界（10.3）：
- Provider 配置与明文 API Key 与业务数据同库；默认查询只返回 ``has_api_key``，
  首版底座不主动增加掩码字段。
- 完整 Key 不进入查询投影、日志、Trace、``run_events`` 或异常详情；
  仅 ``get_provider_api_key_internal`` 是进程内取 Key 路径，与公开投影隔离。
  凭据写入的 Key 是 aiosqlite 绑定参数，驱动层 DEBUG 日志会回显参数，
  因此该写入由 ``Database.parameter_echo_suppressed`` 包住（10.3 绝对禁止进日志）。
- 本层不打印任何 Key；错误信息只携带 provider 标识。
SQL 全部在本 repo 内以字面量书写并参数化（README 硬规则 3）。
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from storage.db import Database
from storage.errors import InvalidInput, NotFound

# 07 7.3 固定业务时区在 app_config 中的键
BUSINESS_TIMEZONE_KEY = "business_timezone"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_provider_id(provider: object) -> None:
    """Provider 标识校验；错误信息只携带固定文案，不回显任何调用方传入的值。"""
    if not isinstance(provider, str) or not provider.strip():
        raise InvalidInput("provider 标识必须是非空字符串")


def _require_api_key(provider: str, api_key: object) -> str:
    """Key 录入前置校验（10.3 投影语义）。

    空/纯空白/非字符串的 Key 落库会让 has_api_key 虚报为已配置，存储层即拒绝；
    清除凭据必须走显式删除 delete_provider_api_key。异常信息只带 provider 标识，
    绝不回显 Key 内容（S0-07：失败路径的异常详情不得含 Key）。
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise InvalidInput(f"api_key 必须是非空字符串（provider={provider}）")
    return api_key


def business_date(instant: datetime, timezone_name: str) -> date:
    """固定业务时区下的自然日（07 7.3：今天/训练日期/日程到期的解释口径基础）。

    使用 IANA 地区规则（ZoneInfo，含夏令时全年规则），不以启动时的固定 UTC
    偏移替代；时区名无法解析时异常向上抛，不静默回退 UTC（S0-05 验收）。
    """
    if instant.tzinfo is None:
        raise ValueError("instant 必须是带时区的绝对时刻")
    return instant.astimezone(ZoneInfo(timezone_name)).date()


class SettingRepo:
    """固定业务时区与 Provider 配置存取；访问经 Database 唯一锁串行化（07 7.1）。"""

    def __init__(self, db: Database):
        self._db = db

    # ---------- 固定业务时区（07 7.3） ----------

    async def get_business_timezone(self) -> str | None:
        async def op(conn: aiosqlite.Connection) -> str | None:
            async with conn.execute(
                "SELECT value FROM app_config WHERE key = ?",
                (BUSINESS_TIMEZONE_KEY,),
            ) as cursor:
                row = await cursor.fetchone()
            return None if row is None else str(row["value"])

        return await self._db.under_lock(op)

    async def initialize_business_timezone(
        self, source: Callable[[], str]
    ) -> dict[str, Any]:
        """首次初始化固定业务时区；已存在则原样返回，不随系统时区变化重取（07 7.3）。

        ``source`` 为本机时区采样来源（生产默认 config.local_timezone_name，tzlocal）。
        采样在事务外进行——事务只包含数据库操作（07 7.1）。采样结果必须能被
        ZoneInfo 解析为地区规则（Windows 依赖 tzdata），否则大声上抛不写库：
        检测/解析失败不得静默选 UTC 或固定偏移（S0-05 验收）。
        """
        existing = await self.get_business_timezone()
        if existing is not None:
            return {"timezone": existing, "initialized": False}
        name = source()
        ZoneInfo(name)  # 可解析性门槛：解析失败不持久化，由调用方（启动流程）大声失败
        async with self._db.transaction() as conn:
            # 锁内复核：并发初始化时第一个提交者获胜，不覆盖已保存值。
            async with conn.execute(
                "SELECT value FROM app_config WHERE key = ?",
                (BUSINESS_TIMEZONE_KEY,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return {"timezone": str(row["value"]), "initialized": False}
            await conn.execute(
                "INSERT INTO app_config (key, value, updated_at) VALUES (?, ?, ?)",
                (BUSINESS_TIMEZONE_KEY, name, _now()),
            )
        return {"timezone": name, "initialized": True}

    # ---------- Provider 配置与密钥（10.3） ----------

    async def set_provider_api_key(self, provider: str, api_key: str) -> dict[str, Any]:
        """录入或替换明文 Key（同库存储，10.3）。返回值只含 provider 与 has_api_key。

        Key 只能作为绑定参数下发（绝不拼进 SQL 文本），而 aiosqlite 在 DEBUG 下会把
        绑定参数连语句一起打进日志，因此整段写入必须在
        :meth:`Database.parameter_echo_suppressed` 内完成：即使 root/aiosqlite logger
        被拉到 DEBUG，任何 logging record 也不得含完整 Key（10.3 绝对边界）。
        压制只覆盖本方法的写入事务，退出时恢复原 logger 状态。
        """
        _require_provider_id(provider)
        api_key = _require_api_key(provider, api_key)
        async with (
            self._db.parameter_echo_suppressed(),
            self._db.transaction() as conn,
        ):
            await conn.execute(
                "INSERT INTO provider_config (provider, api_key, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(provider) DO UPDATE SET api_key = excluded.api_key,"
                " updated_at = excluded.updated_at",
                (provider, api_key, _now()),
            )
        # 不携带 Key 的返回投影
        return {"provider": provider, "has_api_key": True}

    async def delete_provider_api_key(self, provider: str) -> dict[str, Any]:
        """显式删除：清空 Key（保留 provider 行，has_api_key 变 False）。"""
        _require_provider_id(provider)
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "UPDATE provider_config SET api_key = NULL, updated_at = ? WHERE provider = ?",
                (_now(), provider),
            )
            if cursor.rowcount != 1:
                raise NotFound(f"Provider 配置不存在: {provider}")
        return {"provider": provider, "has_api_key": False}

    async def get_provider_status(self, provider: str) -> dict[str, Any] | None:
        """默认安全查询投影：只返回 has_api_key，不返回完整 Key（10.3）。

        首版底座不主动增加掩码字段（10.3：仅在确有识别需求时才返回掩码）。
        SQLite 没有布尔类型，``api_key IS NOT NULL`` 返回整数 0/1；投影层显式转成
        真 bool，避免 0/1 以数字形态进入后续响应投影。
        """
        _require_provider_id(provider)

        async def op(conn: aiosqlite.Connection) -> aiosqlite.Row | None:
            async with conn.execute(
                "SELECT provider, (api_key IS NOT NULL) AS has_api_key, updated_at"
                " FROM provider_config WHERE provider = ?",
                (provider,),
            ) as cursor:
                return await cursor.fetchone()

        row = await self._db.under_lock(op)
        if row is None:
            return None
        projection = dict(row)
        projection["has_api_key"] = bool(projection["has_api_key"])
        return projection

    async def get_provider_api_key_internal(self, provider: str) -> str | None:
        """进程内取 Key 路径：后续真实模型调用按需读取（10.3），与公开投影隔离。

        返回值只允许进入调用进程的请求装配，不得持久化到其他表或输出。
        """
        _require_provider_id(provider)

        async def op(conn: aiosqlite.Connection) -> str | None:
            async with conn.execute(
                "SELECT api_key FROM provider_config WHERE provider = ?",
                (provider,),
            ) as cursor:
                row = await cursor.fetchone()
            return (
                None if row is None or row["api_key"] is None else str(row["api_key"])
            )

        return await self._db.under_lock(op)
