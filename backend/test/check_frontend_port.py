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
        FRONTEND_PORT,
        check_boundary,
    )

    port = int(os.environ.get("FIT_AGENT_FRONTEND_PORT", "5173"))
    assert FRONTEND_PORT == port
    expected = {f"{host}:{value}" for host in ("localhost", "127.0.0.1") for value in (8000, port)}
    assert ALLOWED_HOSTS == expected

    def request(host, origin):
        return Request({"type": "http", "headers": [(b"host", host.encode()), (b"origin", origin.encode())]})

    for host in expected:
        asyncio.run(check_boundary(request(host, f"http://{host}")))
    for host in ("localhost", "127.0.0.1"):
        for value in (1, 80, 5173, 5175, 5176, 8000, 65535):
            asyncio.run(check_boundary(request("localhost:8000", f"http://{host}:{value}")))
    for origin in (
        "http://evil.example", f"http://localhost:{port}.evil.example",
        f"https://localhost:{port}", "http://localhost:0", "http://localhost:65536",
        "http://localhost:abc", "http://localhost:-1", "http://localhost:999999",
        "http://localhost", "null", "http://192.168.1.2:5175", "http://[::1]:5175",
        "http://user@localhost:5175", "http://localhost:5175/", "http://localhost:5175/path",
        "http://localhost:5175?query", "http://localhost:5175#fragment",
        "http://localhost:5175?", "http://localhost:5175#", "http://localhost:5175\n",
    ):
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(check_boundary(request("localhost:8000", origin)))
        assert rejected.value.status_code == 403
        assert rejected.value.detail["code"] == "origin_forbidden"
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(check_boundary(request("evil.example:8000", f"http://localhost:{port}")))
    assert rejected.value.detail["code"] == "host_forbidden"
    asyncio.run(check_boundary(Request({"type": "http", "headers": [(b"host", b"localhost:8000")]})))
    for headers, code in (
        ([(b"host", b"localhost:8000"), (b"origin", b"http://localhost:5175"),
          (b"origin", b"http://localhost:5176")], "origin_forbidden"),
        ([(b"host", b"localhost:8000"), (b"host", b"localhost:8000")], "host_forbidden"),
        ([(b"origin", b"http://localhost:5175")], "host_forbidden"),
    ):
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(check_boundary(Request({"type": "http", "headers": headers})))
        assert rejected.value.status_code == 403
        assert rejected.value.detail["code"] == code


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
    print("PASS: configured Host allowlist; local HTTP Origin ports 1–65535; malformed/foreign/duplicate headers rejected; invalid configuration fails")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        check()
