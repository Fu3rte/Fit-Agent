import os
import sys
from pathlib import Path

# 联调用启动器：以指定数据库文件启动现有 HTTP 入口，仅前端检查脚本使用。
# RESET=1 时重建数据库；否则复用同一文件，用于验证进程重启后的中断恢复。
BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

import uvicorn

from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app

DATABASE = Path(os.environ["LIST_PROBE_DB"])
if os.environ.get("LIST_PROBE_RESET") == "1" and DATABASE.exists():
    DATABASE.unlink()

database_module.default_database_path = lambda: DATABASE

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
