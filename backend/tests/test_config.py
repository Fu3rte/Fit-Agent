"""S0-02：数据目录与数据库路径解析（10.2；Windows 路径断言在 Windows 人工验收复核）。"""

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import platformdirs
import pytest
import tzlocal

import config
from config import (
    DATABASE_FILENAME,
    database_path,
    local_timezone_name,
    resolve_data_dir,
)


def test_default_uses_platformdirs_without_vendor_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认解析必须走 platformdirs 用户数据目录，且 appauthor=False 不产生厂商子目录。"""
    captured: dict[str, object] = {}

    def fake_user_data_dir(*args: object, **kwargs: object) -> str:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "/data-root/Fit-Agent"

    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)
    assert resolve_data_dir() == Path("/data-root/Fit-Agent")
    assert captured["kwargs"] == {"appauthor": False}
    assert captured["args"] == ("Fit-Agent",)


def test_env_override_isolates_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(config.DATA_DIR_OVERRIDE_ENV, str(tmp_path / "isolated"))
    assert resolve_data_dir() == tmp_path / "isolated"


def test_explicit_override_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(config.DATA_DIR_OVERRIDE_ENV, str(tmp_path / "from-env"))
    assert resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_database_file_is_app_db_directly_under_data_dir(tmp_path: Path) -> None:
    """数据库文件是数据目录下的 app.db；Windows 上应为 %LOCALAPPDATA%\\Fit-Agent\\app.db。"""
    assert database_path(resolve_data_dir(tmp_path)) == tmp_path / DATABASE_FILENAME
    assert DATABASE_FILENAME == "app.db"


def test_local_timezone_source_detects_resolvable_iana_name() -> None:
    """S0-05：生产采样来源返回非空且可被 ZoneInfo 解析的 IANA 地区名（07 7.3）。

    Windows 实机（注册表时区 ID → zoneinfo 地区名 + tzdata 解析）由人工验收复核。
    """
    name = local_timezone_name()
    assert isinstance(name, str) and name
    assert ZoneInfo(name) is not None


def test_local_timezone_detection_failure_is_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S0-05：检测失败大声上抛，不得静默选 UTC 或固定偏移（07 7.3）。"""

    def broken() -> str:
        raise ZoneInfoNotFoundError("模拟本机时区检测失败")

    monkeypatch.setattr(tzlocal, "get_localzone_name", broken)
    with pytest.raises(ZoneInfoNotFoundError):
        local_timezone_name()
