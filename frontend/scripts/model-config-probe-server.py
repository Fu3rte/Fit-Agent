import os
import sys
from pathlib import Path

# 使用隔离数据目录，保护 backend/data。
BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

import uvicorn

from app import model_config
from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app

DATA = Path(os.environ["MODEL_PROBE_DATA"])
DATA.mkdir(parents=True, exist_ok=True)
model_config.DATA_ROOT = DATA
database_module.default_database_path = lambda: DATA / "probe.db"

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["MODEL_PROBE_PORT"]), log_level="info")
