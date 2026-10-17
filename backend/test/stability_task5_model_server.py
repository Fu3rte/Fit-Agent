import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE = {
    "goal": "增肌",
    "experience": "新手",
    "environment": "健身房",
    "availability": "每周四次",
    "health_notes": None,
    "movement_restrictions": None,
    "unavailable_equipment": [],
    "forbidden_exercise_ids": [],
}


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "final"
        self.base_version = None
        self.requests = 0
        self.held = 0
        self.release = threading.Event()


STATE = State()


def chunk(content=None, finish_reason=None, tool_calls=None) -> dict:
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-task5",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


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

    def _sse(self, payload: str) -> None:
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def do_GET(self) -> None:
        if self.path == "/control/state":
            with STATE.lock:
                self._json(200, {
                    "mode": STATE.mode,
                    "base_version": STATE.base_version,
                    "requests": STATE.requests,
                    "held": STATE.held,
                    "released": STATE.release.is_set(),
                })
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.path == "/control/mode":
            payload = json.loads(body or b"{}")
            with STATE.lock:
                STATE.mode = payload.get("mode", "final")
                STATE.base_version = payload.get("base_version")
                STATE.requests = 0
                STATE.held = 0
                STATE.release.clear()
            self._json(200, {"mode": STATE.mode})
            return
        if self.path == "/control/release":
            STATE.release.set()
            self._json(200, {"released": True})
            return
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not_found"})
            return

        request = json.loads(body or b"{}")
        with STATE.lock:
            STATE.requests += 1
            mode = STATE.mode
            base_version = STATE.base_version
        has_tool_result = any(
            item.get("role") == "tool" for item in request.get("messages", [])
        ) or any(
            item.get("role") == "toolResult" for item in request.get("messages", [])
        )
        if mode == "hold":
            with STATE.lock:
                STATE.held += 1
            STATE.release.wait(60)
        if mode in {"tool_profile", "tool_profile_v1"} and not has_tool_result:
            version = base_version
            arguments = json.dumps({
                "profile_id": 1,
                "base_profile_version": version,
                "payload": PROFILE,
            }, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            first = chunk(tool_calls=[{
                "index": 0,
                "id": "call_profile",
                "type": "function",
                "function": {"name": "prepare_profile_update", "arguments": ""},
            }])
            first["choices"][0]["delta"]["role"] = "assistant"
            self._sse("data: " + json.dumps(first, ensure_ascii=False) + "\n\n")
            self._sse("data: " + json.dumps(chunk(tool_calls=[{
                "index": 0,
                "function": {"arguments": arguments},
            }]), ensure_ascii=False) + "\n\n")
            self._sse("data: " + json.dumps(chunk(finish_reason="tool_calls"), ensure_ascii=False) + "\n\n")
            self._sse("data: " + json.dumps({
                "id": "chatcmpl-task5",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "test-model",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            }, ensure_ascii=False) + "\n\n")
            self._sse("data: [DONE]\n\n")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._sse("data: " + json.dumps(chunk(content="完成"), ensure_ascii=False) + "\n\n")
        self._sse("data: " + json.dumps(chunk(finish_reason="stop"), ensure_ascii=False) + "\n\n")
        self._sse("data: " + json.dumps({
            "id": "chatcmpl-task5",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }, ensure_ascii=False) + "\n\n")
        self._sse("data: [DONE]\n\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--ready-file", default=None)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    if args.ready_file:
        Path(args.ready_file).write_text(str(args.port), encoding="utf-8")
    server.serve_forever()


if __name__ == "__main__":
    main()
