import json
import re
import ssl
import threading
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = "api.stepfun.com"
FORWARDED_HEADERS = ("content-type", "authorization", "x-api-key", "anthropic-version")

# 公开正文标志：OpenAI 兼容取 delta.content 非空串，Anthropic 取 text_delta 的 text 非空串。
CONTENT_PATTERN = re.compile(r'"(?:content|text)"\s*:\s*"[^"]+"')


def initial_state(mode: str) -> dict:
    return {
        "mode": mode,
        "client_requests": 0,
        "upstream_requests": 0,
        "upstream_statuses": [],
        "content_frames": 0,
        "cut_frame": None,
        "done_sent": False,
    }


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data = initial_state("normal")

    def reset(self, mode: str) -> None:
        with self.lock:
            self.data = initial_state(mode)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data, upstream_statuses=list(self.data["upstream_statuses"]))


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args) -> None:
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with STATE.lock:
            STATE.data["client_requests"] += 1
            attempt = STATE.data["client_requests"]
            mode = STATE.data["mode"]
        if mode == "fail_first" and attempt == 1:
            # 上游零调用：注入一次可重试的 5xx，验证公开事件前的重试窗口走真实请求。
            self._json(
                500,
                {
                    "error": {
                        "type": "api_error",
                        "message": "task6 injected upstream failure before first attempt",
                    }
                },
            )
            return
        self._forward(raw)

    def _forward(self, raw: bytes) -> None:
        headers = {name: self.headers[name] for name in FORWARDED_HEADERS if name in self.headers}
        headers["accept-encoding"] = "identity"
        connection = HTTPSConnection(
            UPSTREAM_HOST, timeout=120, context=ssl.create_default_context()
        )
        try:
            connection.request("POST", self.path, body=raw, headers=headers)
            response = connection.getresponse()
        except OSError as error:
            with STATE.lock:
                STATE.data["upstream_statuses"].append(type(error).__name__)
            self._json(
                502, {"error": {"type": "api_error", "message": "proxy upstream unreachable"}}
            )
            return
        with STATE.lock:
            STATE.data["upstream_requests"] += 1
            STATE.data["upstream_statuses"].append(response.status)
        content_type = response.getheader("content-type") or ""
        if response.status != 200 or "text/event-stream" not in content_type:
            body = response.read()
            self.send_response(response.status)
            self.send_header("Content-Type", content_type or "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            connection.close()
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        cut = STATE.data["mode"] == "cut"
        while True:
            line = response.readline()
            if not line:
                break
            self.wfile.write(line)
            self.wfile.flush()
            text = line.decode("utf-8", "replace").strip()
            with STATE.lock:
                payload = text[5:].strip() if text.startswith("data:") else ""
                if payload == "[DONE]":
                    STATE.data["done_sent"] = True
                    should_cut = False
                elif payload and CONTENT_PATTERN.search(payload):
                    STATE.data["content_frames"] += 1
                    should_cut = cut and not STATE.data["done_sent"]
                    if should_cut:
                        # 真实正文已发布，就此截断：不转发终止帧，直接收尾连接。
                        STATE.data["cut_frame"] = STATE.data["content_frames"]
                else:
                    should_cut = False
            if should_cut:
                break
        connection.close()


class RealProviderProxy:
    def __init__(self, path_prefix: str = "/step_plan") -> None:
        self.path_prefix = path_prefix
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def openai_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path_prefix}/v1"

    def anthropic_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.path_prefix}"

    def set_mode(self, mode: str) -> None:
        STATE.reset(mode)

    def snapshot(self) -> dict:
        return STATE.snapshot()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
