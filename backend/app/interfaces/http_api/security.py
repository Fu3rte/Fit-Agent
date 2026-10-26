import os
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import URL
from starlette.routing import compile_path
from starlette.types import ASGIApp, Receive, Scope, Send

from app.interfaces.http_api.errors import NO_STORE, reject

frontend_port = os.environ.get("FIT_AGENT_FRONTEND_PORT", "5173")
if re.fullmatch(r"[0-9]+", frontend_port) is None or not 1 <= int(frontend_port) <= 65535:
    raise ValueError("FIT_AGENT_FRONTEND_PORT 必须是 1–65535 的整数")
FRONTEND_PORT = int(frontend_port)
ALLOWED_HOSTS = {f"{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (8000, FRONTEND_PORT)}


async def check_boundary(request: Request) -> None:
    hosts = request.headers.getlist("host")
    origins = request.headers.getlist("origin")
    if len(hosts) != 1 or hosts[0] not in ALLOWED_HOSTS:
        reject(403, "host_forbidden", "Host 不允许")
    if origins:
        if len(origins) != 1:
            reject(403, "origin_forbidden", "Origin 不允许")
        origin = URL(origins[0])
        local = re.fullmatch(r"(?:localhost|127\.0\.0\.1):([0-9]{1,5})", origin.netloc)
        if (local is None or not 1 <= int(local[1]) <= 65535
                or origins[0] != f"http://{origin.netloc}"):
            reject(403, "origin_forbidden", "Origin 不允许")


CLIENT_ATTEMPT_PATH = "/api/agent/client-attempts"
CLIENT_ATTEMPT_BODY_LIMIT = 4096
ATTACHMENT_BODY_LIMIT = 1048576


class AttachmentBodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.steering_path = compile_path("/api/agent/runs/{run_id}/steering")[0]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        protected = scope["type"] == "http" and scope["method"] == "POST" and (
            scope["path"] in ("/api/agent/run", "/api/agent/edit", CLIENT_ATTEMPT_PATH)
            or self.steering_path.fullmatch(scope["path"]) is not None
        )
        if not protected:
            await self.app(scope, receive, send)
            return
        limit = (
            CLIENT_ATTEMPT_BODY_LIMIT
            if scope["path"] == CLIENT_ATTEMPT_PATH
            else ATTACHMENT_BODY_LIMIT
        )
        request = Request(scope, receive)
        try:
            await check_boundary(request)
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, error.status_code, headers=NO_STORE)(scope, receive, send)
            return
        body = bytearray()
        # 逐块累计，不信任 Content-Length；缺失或伪造该头同样按实际字节数判定。
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                await JSONResponse({"detail": {
                    "code": "request_size_exceeded", "message": "请求体大小超过限制。",
                }}, 413, headers=NO_STORE)(scope, receive, send)
                return
            body.extend(chunk)
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)
