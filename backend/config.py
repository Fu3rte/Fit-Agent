"""本地数据目录、模型环境变量状态和前端构建路径。"""

import os
from pathlib import Path

import platformdirs
import tzlocal
from dotenv import load_dotenv

from business_time import require_iana_timezone

APP_NAME = "Fit-Agent"
DATABASE_FILENAME = "fit_agent_langgraph.db"
# LangGraph Checkpointer 存档：独立文件名，绝不与业务库同库（讨论总结 §9）。
CHECKPOINT_DATABASE_FILENAME = "langgraph_checkpoints.db"
DATA_DIR_OVERRIDE_ENV = "FIT_AGENT_DATA_DIR"
MODEL_API_KEY_ENV = "MODEL_API_KEY"
MODEL_BASE_URL_ENV = "MODEL_BASE_URL"
MODEL_MODEL_ENV = "MODEL_MODEL"

# 本地开发读取仓库根目录 .env；已存在的进程环境变量优先。
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def resolve_data_dir(override: str | os.PathLike[str] | None = None) -> Path:
    if override is not None:
        return Path(override)
    if env := os.environ.get(DATA_DIR_OVERRIDE_ENV):
        return Path(env)
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def database_path(data_dir: Path) -> Path:
    return data_dir / DATABASE_FILENAME


def checkpoint_database_path(data_dir: Path) -> Path:
    """Checkpoint 存档的独立路径入口（讨论总结 §9、REFACTOR_PLAN §5.6）。

    单独一个函数与文件名常量：存档路径独立配置，不复用业务库路径；测试把临时目录传进来，
    也可以直接给 ``open_checkpointer`` 传任意路径。
    """
    return data_dir / CHECKPOINT_DATABASE_FILENAME


def frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


def model_api_key_configured() -> bool:
    """只返回配置状态，不暴露密钥值。"""
    return bool(os.environ.get(MODEL_API_KEY_ENV))


def local_timezone_name() -> str:
    """读取并校验本机 IANA 时区；失败时不静默降级（校验语义见 business_time）。"""
    name = tzlocal.get_localzone_name()
    if not name:
        raise RuntimeError("本机时区采样失败：未返回 IANA 地区名")
    return require_iana_timezone(name)
