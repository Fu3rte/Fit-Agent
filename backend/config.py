"""数据目录（platformdirs）、固定业务时区来源、Harness 本地配置加载与硬边界校验（07 7.3、08 8.5、10.2）。

Stage 0 实现范围：数据目录/数据库路径解析 + 固定业务时区采样接缝。
Harness 本地配置加载归 Stage 4（08 8.5），本文件暂不实现。
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import platformdirs
import tzlocal

APP_NAME = "Fit-Agent"
DATABASE_FILENAME = "app.db"
DATA_DIR_OVERRIDE_ENV = "FIT_AGENT_DATA_DIR"


def resolve_data_dir(override: str | os.PathLike[str] | None = None) -> Path:
    """平台用户数据目录（10.2）。

    默认经 platformdirs 解析：``user_data_dir(APP_NAME, appauthor=False)``，
    Windows 为 ``%LOCALAPPDATA%\\Fit-Agent``——appauthor=False 保证不多出厂商或版本子目录，
    数据文件继承操作系统用户目录 ACL（10.2，不自建跨平台权限系统）。

    显式 override 与环境变量 ``FIT_AGENT_DATA_DIR`` 仅用于测试与 Windows 人工验收的
    临时数据隔离，不改变生产默认解析；优先级 override > 环境变量 > 默认。
    """
    if override is not None:
        return Path(override)
    env = os.environ.get(DATA_DIR_OVERRIDE_ENV)
    if env:
        return Path(env)
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def database_path(data_dir: Path) -> Path:
    """数据目录内的唯一数据库文件（10.2：Windows 为 ``%LOCALAPPDATA%\\Fit-Agent\\app.db``）。"""
    return data_dir / DATABASE_FILENAME


def local_timezone_name() -> str:
    """生产默认的本机时区采样来源（07 7.3：首次启动取本机时区并固定保存）。

    tzlocal（S0-05 拍板新增依赖）跨平台返回与系统时区匹配的 IANA 地区名：
    Linux/macOS 读系统时区配置；Windows 将注册表时区 ID 映射为 zoneinfo 兼容
    地区名，解析全年地区规则所需的 IANA 数据由 tzdata 提供。

    检测失败由 tzlocal 异常大声上抛；采样结果再用 ZoneInfo 复验可解析，
    失败同样上抛——不得静默选 UTC 或固定偏移替代地区规则（07 7.3）。
    """
    name = tzlocal.get_localzone_name()
    if not name:
        raise RuntimeError(
            "本机时区采样失败：tzlocal 未返回地区名，不降级为 UTC 或固定偏移"
        )
    ZoneInfo(name)  # 可解析性复验：Windows 依赖 tzdata；解析失败不持久化、不降级
    return name
