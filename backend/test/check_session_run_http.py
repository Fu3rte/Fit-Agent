# 运行：在 backend 执行 python -m test.check_session_run_http
import subprocess
from pathlib import Path
from uuid import uuid4

from app.interfaces.http import app
from test.regression_support import Server, patch_default_database

patch_default_database("session-run-http")
frontend = Path(__file__).resolve().parents[2] / "frontend"
storage = frontend / "temp" / f"run-http-{uuid4().hex}.storage"
with Server(app) as server:
    subprocess.run(
        [
            "node",
            "--experimental-webstorage",
            f"--localstorage-file={storage}",
            "scripts/session-run-http-check.mjs",
            server.base_url,
        ],
        cwd=frontend,
        check=True,
        timeout=60,
    )
