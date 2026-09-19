"""本地数据目录、模型环境变量名与前端构建路径。"""

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
# 模型配置文件（设置页可编辑；provider.json 非空字段优先，空字段回落 MODEL_* 环境变量）。
PROVIDER_CONFIG_FILENAME = "provider.json"
DATA_DIR_OVERRIDE_ENV = "FIT_AGENT_DATA_DIR"
MODEL_API_KEY_ENV = "MODEL_API_KEY"
MODEL_BASE_URL_ENV = "MODEL_BASE_URL"
MODEL_MODEL_ENV = "MODEL_MODEL"

# 模型与 Run 的固定上限（讨论总结 §10、stage4.md §3.7 决策 8B）：非敏感常量，可进日志与响应。
# 请求预算按每次 workflow invocation 的运行上下文持有（不新增 WorkflowState 字段）。
#: 单次模型请求超时（秒）。
MODEL_REQUEST_TIMEOUT_SECONDS = 60
#: 单次 Graph Run 的模型请求总时限（秒）。
GRAPH_RUN_TIMEOUT_SECONDS = 180
#: 单次 Run 的模型请求次数上限；当前无 Router 的计划链路拓扑最多实际调用 4 次。
MAX_MODEL_REQUESTS_PER_RUN = 5

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


def local_timezone_name() -> str:
    """读取并校验本机 IANA 时区；失败时不静默降级（校验语义见 business_time）。"""
    name = tzlocal.get_localzone_name()
    if not name:
        raise RuntimeError("本机时区采样失败：未返回 IANA 地区名")
    return require_iana_timezone(name)
