import os
from pathlib import Path

import platformdirs
import tzlocal
from dotenv import load_dotenv

from business_time import require_iana_timezone

APP_NAME = "Fit-Agent"
DATABASE_FILENAME = "fit_agent_langgraph.db"
CHECKPOINT_DATABASE_FILENAME = "langgraph_checkpoints.db"
PROVIDER_CONFIG_FILENAME = "provider.json"
DATA_DIR_OVERRIDE_ENV = "FIT_AGENT_DATA_DIR"
MODEL_API_KEY_ENV = "MODEL_API_KEY"
MODEL_BASE_URL_ENV = "MODEL_BASE_URL"
MODEL_MODEL_ENV = "MODEL_MODEL"

MODEL_REQUEST_TIMEOUT_SECONDS = 60
GRAPH_RUN_TIMEOUT_SECONDS = 180
MAX_MODEL_REQUESTS_PER_RUN = 5

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
    return data_dir / CHECKPOINT_DATABASE_FILENAME


def frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


def local_timezone_name() -> str:
    name = tzlocal.get_localzone_name()
    if not name:
        raise RuntimeError("本机时区采样失败：未返回 IANA 地区名")
    return require_iana_timezone(name)
