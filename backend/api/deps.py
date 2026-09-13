"""请求依赖注入：连接、锁、服务句柄、生产模型构造。

S3-14 增加**业务日期**依赖：按固定业务时区（07 7.3）把当刻绝对时刻折算成业务自然日。业务
日期由服务端算出（计划确认、到期锁定、指导复核都要求「当刻」按业务时区解释），不接受客户端
传入——客户端时钟不是可信基线。测试用 FastAPI ``dependency_overrides`` 覆盖 `business_today`
固定时钟，不靠系统真实日期（stage3.md §6「固定时钟」）。

S4-07 增加**生产模型构造**：每次 Run 开始前才从 Provider 配置仓储读一次凭据（进程内、不打印、
不落盘，10.3），并用已冻结的 Harness 传超时（单次请求总时限与连接时限，08 8.5）。测试整体
替换 ``app.state.model_factory``，不在套件里发真实请求。
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from fastapi import Request
from pydantic_ai.models import Model

import config
from runtime.provider import build_model
from storage.db import Database
from storage.setting_repo import SettingRepo, business_date


class ProviderNotConfigured(RuntimeError):
    """生产路径缺少模型 Provider 凭据：不发起任何请求，不伪造成功。

    调用方（对话路由的 Run 工作体）把它翻成已冻结的 Run 终态原因码
    ``model_request_failed``（08「永久失败与计数池耗尽的错误码」），不新增错误码。
    """


def business_today(request: Request) -> datetime:
    """当刻绝对时刻（UTC）：真实时钟只在传输层读取一次，便于测试整体覆盖。"""
    return datetime.now(UTC)


def current_business_date(request: Request) -> date:
    """请求当刻的业务自然日：按本实例固定业务时区解释（07 7.3）。"""
    return business_date(
        business_today(request), str(request.app.state.business_timezone)
    )


def make_model_factory(
    db: Database, harness: config.EffectiveHarness
) -> Callable[[], Awaitable[Model]]:
    """生产模型工厂（每次 Run 调用一次）：凭据只在进程内读取，不进日志与事件。

    ``harness`` 是启动时冻结的有效配置（08 8.5）；本 Run 的限制由它固定，
    运行中修改配置文件不影响已开始的 Run。凭据槽位与端点、模型 id 同源（都取冻结目录的
    模型事实）：换了端点就不会误用上一个端点的 Key（10.3 只固定同库存储与 Key 边界）。
    """

    async def factory() -> Model:
        api_key = await SettingRepo(db).get_provider_api_key_internal(
            harness.spec.provider
        )
        if api_key is None:
            raise ProviderNotConfigured(
                "未配置模型 Provider 密钥：本次 Run 不发起任何模型请求"
            )
        return build_model(
            api_key,
            model_id=harness.spec.model_id,
            timeout_seconds=harness.request_timeout_seconds,
            connect_timeout_seconds=harness.connect_timeout_seconds,
        )

    return factory
