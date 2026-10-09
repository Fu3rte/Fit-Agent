import asyncio
import os
import subprocess
import sys

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def probe():
    from app.interfaces.http import (
        ALLOWED_HOSTS,
        ALLOWED_ORIGINS,
        FRONTEND_PORT,
        check_boundary,
    )

    port = int(os.environ.get("FIT_AGENT_FRONTEND_PORT", "5173"))
    assert FRONTEND_PORT == port
    expected = {f"{host}:{value}" for host in ("localhost", "127.0.0.1") for value in (8000, port)}
    assert ALLOWED_HOSTS == expected
    assert ALLOWED_ORIGINS == {f"http://{host}" for host in expected}

    def request(host, origin):
        return Request({"type": "http", "headers": [(b"host", host.encode()), (b"origin", origin.encode())]})

    for host in expected:
        asyncio.run(check_boundary(request(host, f"http://{host}")))
    for origin in ("http://evil.example", f"http://localhost:{port}.evil.example",
                   f"https://localhost:{port}", "http://localhost:5175"):
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(check_boundary(request("localhost:8000", origin)))
        assert rejected.value.status_code == 403
        assert rejected.value.detail["code"] == "origin_forbidden"
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(check_boundary(request("evil.example:8000", f"http://localhost:{port}")))
    assert rejected.value.detail["code"] == "host_forbidden"


def check():
    for value in (None, "5176", "1", "65535", "0", "65536", "abc", "5176.0", "", " 5176"):
        env = dict(os.environ)
        env.pop("FIT_AGENT_FRONTEND_PORT", None)
        if value is not None:
            env["FIT_AGENT_FRONTEND_PORT"] = value
        result = subprocess.run([sys.executable, "-m", "test.check_frontend_port", "--probe"],
                                env=env, capture_output=True, text=True, timeout=20)
        if value in (None, "5176", "1", "65535"):
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert "FIT_AGENT_FRONTEND_PORT" in result.stderr
    print("PASS: shared frontend port; default/custom/range; exact local Host/Origin allowlist; invalid configuration fails")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        check()
