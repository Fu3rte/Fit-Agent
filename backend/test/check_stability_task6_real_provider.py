import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.agent.budget import RequestBudget  # noqa: E402
from app.interfaces import http as http_module  # noqa: E402
from test.regression_support import (  # noqa: E402
    Server,
    client,
    install_test_model_config,
    patch_default_database,
    patch_trace_database,
)
from test.stability_task6_real_proxy import RealProviderProxy  # noqa: E402
from test.test_stability_task3 import read_events  # noqa: E402

API_KINDS = ("openai-completions", "anthropic-messages")
BUDGET_CALLS: list[dict] = []
# RequestBudget 的装配点随分层重构在 http 与 execution 之间移动，按属性实际归属选择。
BUDGET_HOSTS = ("app.application.session.execution", "app.interfaces.http")


class ObservedRequestBudget(RequestBudget):
    # 只登记真实扣款序列，计数与判定继续交给生产实现。
    def reserve(self, *, recovery: bool = False) -> int:
        used = super().reserve(recovery=recovery)
        BUDGET_CALLS.append(
            {"used": used, "recovery_used": self.recovery_used, "recovery": recovery}
        )
        return used


def install_observed_budget() -> object:
    for name in BUDGET_HOSTS:
        if importlib.util.find_spec(name) is None:
            continue
        module = importlib.import_module(name)
        if getattr(module, "RequestBudget", None) is RequestBudget:
            module.RequestBudget = ObservedRequestBudget
            return module
    raise RuntimeError("未找到 RequestBudget 装配点")


def read_api_key(auth_file: Path) -> str:
    value = os.environ.get("FIT_AGENT_TEST_MODEL_API_KEY")
    if value:
        return value
    key = json.loads(auth_file.read_text(encoding="utf-8")).get("api_key")
    if not key:
        raise RuntimeError("真实 provider 凭据缺失")
    return key


def use_provider(api: str, base_url: str, model: str, api_key: str) -> None:
    os.environ["FIT_AGENT_TEST_MODEL_API"] = api
    os.environ["FIT_AGENT_TEST_MODEL_BASE_URL"] = base_url
    os.environ["FIT_AGENT_TEST_MODEL_ID"] = model
    os.environ["FIT_AGENT_TEST_MODEL_API_KEY"] = api_key
    os.environ["FIT_AGENT_TEST_MODEL_PROVIDER"] = "stepfun"
    install_test_model_config(root=BACKEND / "tmp" / f"task6-config-{uuid4().hex}")


def run_scenario(proxy: RealProviderProxy, http, db_path: Path, api: str, mode: str, prompt: str) -> dict:
    BUDGET_CALLS.clear()
    proxy.set_mode(mode)
    session_id = str(uuid4())
    created = http.post("/api/sessions", json={"session_id": session_id, "title": "任务六真实批次"})
    assert created.status_code in {200, 201}, created.text
    started = time.monotonic()
    with http.stream(
        "POST",
        "/api/agent/run",
        json={"session_id": session_id, "operation_id": str(uuid4()), "request": prompt},
    ) as response:
        run_id = response.headers.get("x-run-id")
        events = read_events(response) if response.status_code == 200 else []
        status = response.status_code
    elapsed = round(time.monotonic() - started, 2)

    terminal = None
    for item in events:
        if item["event"] in {"done", "error"}:
            terminal = item
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT status, error_code, stop_reason, error_message FROM session_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return {
        "api": api,
        "mode": mode,
        "elapsed_s": elapsed,
        "http_status": status,
        "run_id": run_id,
        "proxy": proxy.snapshot(),
        "budget_calls": list(BUDGET_CALLS),
        "total_used": BUDGET_CALLS[-1]["used"] if BUDGET_CALLS else 0,
        "recovery_used": BUDGET_CALLS[-1]["recovery_used"] if BUDGET_CALLS else 0,
        "reserve_calls": len(BUDGET_CALLS),
        "event_names": [item["event"] for item in events],
        "terminal": terminal,
        "db_row": dict(row) if row else None,
    }


def verify(mode: str, result: dict) -> None:
    proxy = result["proxy"]
    assert result["http_status"] == 200, result
    assert result["db_row"] is not None, result
    assert proxy["upstream_requests"] >= 1, "真实上游至少收到一次请求"
    assert proxy["content_frames"] >= 1, "真实正文事件未发布"
    if mode == "normal":
        assert proxy["client_requests"] == 1 and proxy["upstream_requests"] == 1, result
        assert result["reserve_calls"] == proxy["upstream_requests"], result
        assert result["recovery_used"] == 0, result
    if mode == "fail_first":
        assert proxy["client_requests"] == 2 and proxy["upstream_requests"] == 1, result
        assert result["reserve_calls"] == 2, result
        assert result["recovery_used"] == 1, result
    if mode == "cut":
        assert proxy["client_requests"] == 1 and proxy["upstream_requests"] == 1, "公开事件后断流不得重试"
        assert result["reserve_calls"] == 1, result
        assert result["recovery_used"] == 0, result
        assert proxy["done_sent"] is False and proxy["cut_frame"], result
        assert result["db_row"]["status"] != "running", result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(BACKEND.parent / "tmp" / "stability-task6"))
    parser.add_argument("--model", default="step-3.7-flash")
    parser.add_argument("--prefix", default="/step_plan")
    parser.add_argument("--auth-file", default=str(BACKEND / "data" / "auth.json"))
    parser.add_argument("--max-real-requests", type=int, default=14)
    args = parser.parse_args()

    api_key = read_api_key(Path(args.auth_file))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    db_path = patch_default_database("task6-real-provider")
    patch_trace_database("task6-real-trace")

    execution = install_observed_budget()
    proxy = RealProviderProxy(path_prefix=args.prefix)
    results: list[dict] = []
    real_total = 0
    evidence_path = out / "real-provider.json"
    try:
        with Server(http_module.app) as started_server:
            with client(started_server.base_url) as http:
                for api in API_KINDS:
                    base_url = (
                        proxy.openai_base_url()
                        if api == "openai-completions"
                        else proxy.anthropic_base_url()
                    )
                    for mode in ("normal", "fail_first", "cut"):
                        use_provider(api, base_url, args.model, api_key)
                        result = run_scenario(
                            proxy, http, db_path, api, mode, "只回复四个字，不要调用任何工具。"
                        )
                        results.append(result)
                        real_total += result["proxy"]["upstream_requests"]
                        evidence_path.write_text(
                            json.dumps(
                                {"results": results, "real_total": real_total},
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        print("CASE " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
                        if real_total > args.max_real_requests:
                            raise RuntimeError(f"真实上游请求超过上限 {args.max_real_requests}")
                        verify(mode, result)
    finally:
        proxy.close()
        execution.RequestBudget = RequestBudget

    assert api_key not in json.dumps(results, ensure_ascii=False), "真实凭据出现在验收证据中"
    print(f"PASS: 任务六真实模型批次完成，真实上游请求 {real_total} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
