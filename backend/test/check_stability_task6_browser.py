import argparse
import base64
import json
import shutil
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from test.check_stability_task5_windows import (  # noqa: E402
    configure_model,
    create_session,
    db_rows,
    http_client,
    model_control,
    port_listeners,
    run_stream,
    set_mode,
    snapshot,
    start_app,
    start_lock_helper,
    start_model_server,
    start_run_thread,
    stop_tree,
    terminate,
    wait_backend,
    wait_for,
    wait_frontend,
)

BROWSER_EXE = Path(
    os.environ.get(
        "AGENT_BROWSER_EXE",
        r"C:\Users\Ericcc\AppData\Roaming\npm\node_modules\agent-browser"
        r"\bin\agent-browser-win32-x64.exe",
    )
)
SESSION_NAME = "fit-task6"
RUN_SQL = (
    "SELECT id, status, started_at, finished_at, error_code, stop_diagnostic "
    "FROM session_runs WHERE session_id = ? ORDER BY started_at DESC"
)
CAPACITY_MESSAGE = "当前已有任务执行中，请稍后重试。"
FAULT_MESSAGE = "服务正在恢复，暂不接受新运行。"
RATE_MESSAGE = "请求过于频繁，请稍后重试。"
SELECTOR_MESSAGE = "textarea[aria-label='消息']"
SELECTOR_STOP = "button[aria-label='停止']"
# Vite dev server 监听 localhost 解析到的地址（本机为 ::1），且后端 Origin 门只允许
# localhost 与 127.0.0.1；浏览器一律使用 http://localhost:{port}。

JS_STATE = """(() => {
  const txt = (el) => (el.innerText === undefined ? (el.textContent ?? '') : el.innerText);
  const scroller = document.querySelector('[data-slot=message-scroller-content]');
  const text = scroller ? txt(scroller) : '';
  const alerts = [...document.querySelectorAll('[role=alert]')].map(txt);
  const statuses = [...document.querySelectorAll('[role=status]')].map(txt);
  const input = document.querySelector("textarea[aria-label='消息']");
  const buttons = [...document.querySelectorAll('button')].map((el) => txt(el).trim());
  return {
    has_stop: !!document.querySelector("button[aria-label='停止']"),
    stopping: text.includes('正在停止') || !!document.querySelector("[aria-label='正在停止']"),
    midstream: text.includes('正在流式输出'),
    cancelled: text.includes('已取消'),
    interrupted: text.includes('已中断'),
    unknown_notice: statuses.some((t) => t.includes('提交结果未知')),
    alerts: alerts,
    statuses: statuses,
    text: text,
    input_disabled: input ? input.disabled : null,
    confirm_button: buttons.filter((t) => t.includes('确认保存画像')),
    has_confirm_button: buttons.some((t) => t.trim() === '确认保存画像'),
    has_textarea: !!input,
  };
})()"""

JS_SESSION = """(() => {
  const raw = localStorage.getItem('fit-agent:chat-client');
  if (!raw) return null;
  const store = JSON.parse(raw);
  if (store.selected_session_id) return store.selected_session_id;
  return store.draft ? store.draft.session_id : null;
})()"""

JS_BACKGROUND = """({ visibility: document.visibilityState })"""

JS_SEND_BATCH = """(async (sessionId, count, request) => {
  const results = [];
  for (let index = 0; index < count; index += 1) {
    const operation = crypto.randomUUID();
    const response = await fetch('/api/agent/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ session_id: sessionId, operation_id: operation, request: request })
    });
    const entry = {
      status: response.status,
      run_id: response.headers.get('x-run-id'),
      retry_after: response.headers.get('retry-after')
    };
    if (response.ok && (response.headers.get('content-type') || '').includes('text/event-stream')) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let seen = '';
      for (;;) {
        const next = await reader.read();
        if (next.done) break;
        seen += decoder.decode(next.value, { stream: true });
        if (/event: (done|error)/.test(seen)) break;
      }
      await reader.cancel();
    } else if (!response.ok) {
      const body = await response.text();
      try { entry.code = JSON.parse(body).detail.code; } catch (error) { entry.raw = body.slice(0, 160); }
    }
    results.push(entry);
  }
  return results;
})(SESSION_PLACEHOLDER, COUNT_PLACEHOLDER, REQUEST_PLACEHOLDER)"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Matrix:
    """浏览器故障矩阵证据收集器：短行进 stdout，完整结果进 evidence JSON。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "evidence-task6-browser.json"
        self.data: dict = {
            "started_at": now(), "phases": [], "assertions": [],
            "failures": [], "boundaries": [],
        }
        self.write()

    def write(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def phase(self, name: str, **values) -> None:
        item = {"phase": name, "at": now(), **values}
        self.data["phases"].append(item)
        brief = {
            key: value for key, value in values.items()
            if isinstance(value, (bool, int, float, str, type(None)))
        }
        print(
            "PHASE", name, json.dumps(brief, ensure_ascii=False, default=str)[:700], flush=True
        )
        self.write()

    def check(self, scenario: str, point: str, ok, detail=None) -> None:
        record = {"scenario": scenario, "point": point, "ok": bool(ok),
                  "detail": detail, "at": now()}
        self.data["assertions"].append(record)
        print(
            f"CHECK {scenario}/{point} {'PASS' if ok else 'FAIL'} "
            + json.dumps(detail, ensure_ascii=False, default=str)[:400], flush=True
        )
        if not ok:
            self.data["failures"].append(record)
        self.write()

    def boundary(self, text: str) -> None:
        self.data["boundaries"].append(text)
        print("BOUNDARY", text[:400], flush=True)
        self.write()

    @property
    def failed(self) -> list:
        return self.data["failures"]


class Browser:
    """每条 agent-browser 命令一个独立进程；stdout/stderr 落文件，命令逐条记日志。"""

    def __init__(self, out: Path) -> None:
        self.workdir = out / "commands"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.profile = self.workdir / "profile"
        self.profile.mkdir(parents=True, exist_ok=True)
        self.log_path = self.workdir / "browser-commands.jsonl"
        self.log = self.log_path.open("a", encoding="utf-8")
        self.counter = 0

    def call(self, *args: str, timeout: float = 25.0, tolerant: bool = False):
        self.counter += 1
        tag = f"{self.counter:05d}"
        out_path = self.workdir / f"cmd-{tag}.json"
        err_path = self.workdir / f"cmd-{tag}.err"
        argv = ["--session", SESSION_NAME, "--profile", str(self.profile), "--json", *args]
        write_out = out_path.open("wb")
        write_err = err_path.open("wb")
        returncode: object = "timeout"
        try:
            completed = subprocess.run(
                [str(BROWSER_EXE), *argv], stdin=subprocess.DEVNULL,
                stdout=write_out, stderr=write_err, timeout=timeout,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            pass
        finally:
            write_out.close()
            write_err.close()
        raw = out_path.read_text(encoding="utf-8", errors="replace")
        error_text = err_path.read_text(encoding="utf-8", errors="replace")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            envelope = None
        self.log.write(
            json.dumps(
                {
                    "id": tag, "at": now(), "argv": argv, "returncode": returncode,
                    "success": (envelope or {}).get("success"),
                    "error": (envelope or {}).get("error"),
                    "stderr": error_text[-300:],
                },
                ensure_ascii=False,
            ) + "\n"
        )
        self.log.flush()
        ok = bool(envelope and envelope.get("success")) and returncode == 0
        if not ok and not tolerant:
            raise RuntimeError(
                f"浏览器命令失败 #{tag} {list(args)[:3]} 退出码 {returncode} "
                f"信封 {(raw or error_text)[:300]}"
            )
        if envelope is None:
            return None
        return envelope.get("data")

    def evaluate(self, js: str, tolerant: bool = False):
        data = self.call(
            "eval", "-b", base64.b64encode(js.encode("utf-8")).decode(),
            tolerant=tolerant,
        )
        return (data or {}).get("result") if isinstance(data, dict) else None

    def ui_state(self) -> dict:
        state = self.evaluate(JS_STATE)
        if not isinstance(state, dict):
            raise RuntimeError(f"页面状态读取无效：{state!r}")
        return state

    def session_id(self):
        return self.evaluate(JS_SESSION)

    def screenshot(self, name: str) -> str:
        path = self.workdir.parent / "screenshots" / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.call("screenshot", str(path), tolerant=True)
        return str(path)

    def close(self) -> None:
        self.call("close", "--all", tolerant=True)
        if not self.log.closed:
            self.log.close()


def netstat_capture(out: Path, label: str, ports: list[int]) -> dict:
    completed = subprocess.run(
        ["netstat.exe", "-ano", "-p", "TCP"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    lines = [
        line.strip() for line in (completed.stdout or "").splitlines()
        if any(f":{port} " in line or line.rstrip().endswith(f":{port}") for port in ports)
    ]
    path = out / f"netstat-{label}.txt"
    path.write_text("\n".join(lines) or "(no matching listeners)", encoding="utf-8")
    return {
        "file": str(path),
        "listeners": {str(port): port_listeners(port) for port in ports},
        "matching_lines": lines[:40],
    }


def browser_run_requests(browser: Browser) -> dict:
    payload = browser.call("network", "requests", tolerant=True) or {}
    posts = [
        {"url": node["url"], "status": node.get("status"), "headers": node.get("headers")}
        for node in payload.get("requests", [])
        if "/api/agent/run" in str(node.get("url") or "")
        and str(node.get("method") or "").upper() == "POST"
    ]
    return {
        "post_count": len(posts),
        "posts": posts[:8],
        "raw_sample": json.dumps(payload, ensure_ascii=False, default=str)[:1500],
    }


class StatusSampler(threading.Thread):
    """独立线程按毫秒级采样运行行的真实持久化状态。"""

    def __init__(self, db_path: Path, run_id: str, reference: float,
                 interval: float = 0.03) -> None:
        super().__init__(daemon=True)
        self.db_path = db_path
        self.run_id = run_id
        self.reference = reference
        self.interval = interval
        self.stop_event = threading.Event()
        self.samples: list = []

    def run(self) -> None:
        last = object()
        while not self.stop_event.is_set():
            try:
                rows = db_rows(
                    self.db_path, "SELECT status FROM session_runs WHERE id = ?", (self.run_id,)
                )
                value = rows[0][0] if rows else None
            except Exception as error:
                value = f"read_error:{type(error).__name__}"
            stamp = round(time.monotonic() - self.reference, 3)
            if value != last:
                self.samples.append([stamp, value])
                last = value
            time.sleep(self.interval)

    def halt(self) -> list:
        self.stop_event.set()
        self.join(5)
        return self.samples


def latest_run(db_path: Path, session_id: str) -> dict | None:
    rows = db_rows(db_path, RUN_SQL, (session_id,))
    if not rows:
        return None
    keys = ("id", "status", "started_at", "finished_at", "error_code", "stop_diagnostic")
    return dict(zip(keys, rows[0]))


def wait_latest_run(db_path: Path, session_id: str, timeout: float,
                    statuses: tuple | None = None) -> dict:
    def probe():
        row = latest_run(db_path, session_id)
        if row is None:
            return None
        return row if statuses is None or row["status"] in statuses else None

    return wait_for(probe, timeout)


def new_session(browser: Browser, timeout: float = 10.0) -> str:
    browser.call("find", "role", "button", "click", "--name", "新建会话")

    def probe():
        session_id = browser.session_id()
        return session_id if session_id and browser.ui_state()["text"] == "" else None

    return wait_for(probe, timeout)


def ui_send(browser: Browser, text: str) -> None:
    browser.call("fill", SELECTOR_MESSAGE, text)
    browser.call("press", "Enter")


def wait_until_ui(browser: Browser, predicate, timeout: float, label: str,
                  interval: float = 0.15) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = browser.ui_state()
        if predicate(last):
            return last
        time.sleep(interval)
    raise RuntimeError(
        f"浏览器状态等待超时：{label}；最后状态 "
        + json.dumps(
            {key: value for key, value in last.items() if key != "text"},
            ensure_ascii=False, default=str,
        )[:600]
    )


def page_ready(browser: Browser) -> bool:
    state = browser.evaluate(JS_STATE, tolerant=True)
    return isinstance(state, dict) and state.get("has_textarea") is True


def page_text(browser: Browser) -> str:
    # 非会话路由（如 /profile）没有 message-scroller，正文一律取整页 innerText 取证。
    text = browser.evaluate("document.body.innerText", tolerant=True)
    return text if isinstance(text, str) else ""


def profile_rendered(text: str) -> bool:
    # 顶部导航常驻“个人画像”字样，须再看画像卡片正文描述（尚未建档 / 版本 N）才算渲染完成。
    return "个人画像" in text and ("尚未建档" in text or "版本 " in text)


def profile_page_ready(browser: Browser) -> bool:
    return profile_rendered(page_text(browser))


def ensure_page(browser: Browser, url: str, timeout: float = 45.0, ready=page_ready) -> None:
    # 新启浏览器后的第一条 open 只返回不绑定 target；标签页全部关闭后 open 也可能无效，
    # 因此统一先 open 再确认真实页面，必要时以 tab new 重建。
    browser.call("open", url, tolerant=True)
    browser.call("open", url, tolerant=True)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if ready(browser):
            return
        time.sleep(0.4)
    browser.call("tab", "new", url, tolerant=True)
    wait_for(lambda: ready(browser), timeout)


def run_query(client: httpx.Client, session_id: str, run_id: str) -> dict:
    response = client.get(f"/api/sessions/{session_id}/runs/{run_id}")
    return {
        "http_status": response.status_code,
        "body": response.json() if response.status_code == 200 else response.text[:400],
    }


def scenario_b1(matrix: Matrix, ctx) -> None:
    scenario = "B1"
    browser, db_path = ctx.browser, ctx.db
    set_mode("hold")
    session_id = new_session(browser)
    ui_send(browser, "取消按钮探针")
    run = wait_latest_run(db_path, session_id, 30, ("running",))
    wait_until_ui(browser, lambda s: s["has_stop"], 20, "停止控件出现")
    click_at = time.monotonic()
    sampler = StatusSampler(db_path, run["id"], click_at)
    sampler.start()
    browser.call("click", SELECTOR_STOP)
    stopping = wait_until_ui(browser, lambda s: s["stopping"], 10, "正在停止出现")
    at_stopping = browser.ui_state()
    shot = browser.screenshot("b1-stopping")
    running_samples = [item for item in sampler.samples if item[0] >= 0 and item[1] == "running"]
    matrix.check(
        scenario, "点击停止后展示正在停止且停止控件消失",
        at_stopping["stopping"] and not at_stopping["has_stop"],
        {"stopping": stopping["stopping"], "has_stop": at_stopping["has_stop"],
         "screenshot": shot},
    )
    matrix.check(
        scenario, "正在停止期间运行仍未结束", bool(running_samples),
        {"post_click_trajectory": sampler.samples[:20]},
    )
    model_control("/control/release", {})
    cleared = wait_until_ui(browser, lambda s: not s["stopping"], 60, "正在停止清除")
    ui_clear_at = time.monotonic()
    terminal = wait_latest_run(db_path, session_id, 30,
                              ("cancelled", "completed", "failed"))
    trajectory = sampler.halt()
    terminal_at = next(
        (stamp for stamp, status in trajectory
         if status in {"cancelled", "completed", "failed"}), None
    )
    final = wait_until_ui(browser, lambda s: s["cancelled"], 40, "界面展示已取消")
    browser.screenshot("b1-final")
    matrix.check(
        scenario, "正在停止仅在持久化终态后清除",
        terminal_at is not None and (ui_clear_at - click_at) >= terminal_at,
        {"ui_clear_relative_s": round(ui_clear_at - click_at, 3),
         "db_terminal_relative_s": terminal_at, "trajectory": trajectory[:20],
         "stopping_after_clear": cleared["stopping"]},
    )
    matrix.check(scenario, "后端行为 cancelled", terminal["status"] == "cancelled", terminal)
    matrix.check(
        scenario, "界面展示已取消终态", final["cancelled"] and not final["stopping"],
        {"statuses": final["statuses"], "alerts": final["alerts"]},
    )
    matrix.phase(
        "B1_cancel_button", session=session_id, run_id=run["id"],
        trajectory=trajectory[:40], terminal=terminal, statuses=final["statuses"],
        alerts=final["alerts"], screenshot=shot,
    )


def scenario_b2(matrix: Matrix, ctx) -> None:
    scenario = "B2"
    browser, client, db_path = ctx.browser, ctx.client, ctx.db
    set_mode("hold_after_start")
    session_id = new_session(browser)
    ui_send(browser, "刷新中断探针")
    run = wait_latest_run(db_path, session_id, 30, ("running",))
    streaming = wait_until_ui(
        browser, lambda s: s["has_stop"] and s["midstream"], 30, "SSE 正文流已建立"
    )
    browser.screenshot("b2-midstream")
    browser.call("reload")
    wait_until_ui(browser, lambda s: s["has_textarea"], 40, "重载后页面就绪")
    terminal = wait_latest_run(db_path, session_id, 45,
                              ("cancelled", "completed", "failed", "interrupted"))
    wait_until_ui(
        browser,
        lambda s: not s["has_stop"] and not s["stopping"] and s["input_disabled"] is False
        and not s["unknown_notice"],
        45, "重载后界面不再占用",
    )
    state = browser.ui_state()
    shot = browser.screenshot("b2-after-reload")
    queried = run_query(client, session_id, run["id"])
    matrix.check(
        scenario, "刷新前已进入流式执行", streaming["has_stop"] and streaming["midstream"],
        {"text_tail": streaming["text"][-160:]},
    )
    matrix.check(
        scenario, "浏览器刷新后后端结束为 cancelled",
        terminal["status"] == "cancelled", terminal,
    )
    matrix.check(
        scenario, "重载后界面不卡占用",
        not state["has_stop"] and not state["stopping"] and state["input_disabled"] is False,
        {"screenshot": shot, "statuses": state["statuses"], "alerts": state["alerts"]},
    )
    matrix.check(
        scenario, "运行查询回报同一终态",
        queried["http_status"] == 200 and queried["body"]["status"] == terminal["status"],
        queried,
    )
    matrix.phase(
        "B2_reload_mid_stream", session=session_id, run_id=run["id"], terminal=terminal,
        run_query=queried, statuses=state["statuses"], alerts=state["alerts"], screenshot=shot,
    )


def scenario_b3(matrix: Matrix, ctx) -> None:
    scenario = "B3"
    browser, client, db_path, url = ctx.browser, ctx.client, ctx.db, ctx.url
    set_mode("hold")
    session_id = new_session(browser)
    ui_send(browser, "关闭标签页探针")
    run = wait_latest_run(db_path, session_id, 30, ("running",))
    wait_until_ui(browser, lambda s: s["has_stop"], 30, "运行中停止控件")
    tabs = browser.call("tab", "list")
    run_tab_index = next(
        item["index"] for item in tabs["tabs"] if item.get("active")
    )
    browser.screenshot("b3-before-close")
    # agent-browser 0.25.4 拒绝关闭最后一个标签页（Cannot close the last tab），
    # 因此先另开一个标签页占住实例，再按索引关闭运行所在的标签页，真实断开 SSE 连接。
    browser.call("tab", "new", url)
    closed = browser.call("tab", "close", str(run_tab_index))
    matrix.phase("B3_tab_close", session=session_id, run_id=run["id"],
                 tabs_before_close=tabs, closed_tab_index=run_tab_index,
                 tabs_after_close=browser.call("tab", "list"), close_reply=closed)
    terminal = wait_latest_run(db_path, session_id, 45,
                              ("cancelled", "completed", "failed", "interrupted"))
    queried = run_query(client, session_id, run["id"])
    ensure_page(browser, f"{url}/sessions/{session_id}")
    state = wait_until_ui(
        browser, lambda s: s["has_textarea"] and (s["cancelled"] or s["interrupted"]),
        45, "重开页面后展示终态",
    )
    shot = browser.screenshot("b3-after-tab-close")
    matrix.check(
        scenario, "关闭标签页后后端结束为 cancelled", terminal["status"] == "cancelled",
        terminal,
    )
    matrix.check(
        scenario, "GET 运行查询回报 cancelled 终态",
        queried["http_status"] == 200 and queried["body"]["status"] == "cancelled", queried,
    )
    matrix.check(
        scenario, "重开页面后界面展示终态且不占用",
        state["cancelled"] and not state["has_stop"] and not state["stopping"],
        {"screenshot": shot, "statuses": state["statuses"]},
    )
    matrix.phase(
        "B3_close_tab", terminal=terminal, run_query=queried,
        ui_status=state["statuses"], screenshot=shot,
    )


def scenario_b4(matrix: Matrix, ctx) -> None:
    scenario = "B4"
    browser, client, db_path = ctx.browser, ctx.client, ctx.db
    set_mode("hold")
    busy_session = create_session(client, "任务六容量占位")
    thread, holder, events = start_run_thread(client, busy_session, "占用全局名额")
    wait_for(lambda: model_control("/control/state")["held"] >= 1, 30)
    matrix.phase("B4_slot_held", busy_session=busy_session, run_id=holder.get("run_id"))
    browser.call("network", "requests", "--clear", tolerant=True)
    session_id = new_session(browser)
    ui_send(browser, "容量拒绝探针")
    blocked = wait_until_ui(
        browser, lambda s: any(CAPACITY_MESSAGE in item for item in s["alerts"]), 40,
        "容量拒绝提示"
    )
    immediate = browser_run_requests(browser)
    shot = browser.screenshot("b4-capacity-rejected")
    time.sleep(3.0)
    after = browser_run_requests(browser)
    state = browser.ui_state()
    matrix.check(
        scenario, "界面呈现 503 capacity_exceeded 拒绝",
        any(CAPACITY_MESSAGE in item for item in blocked["alerts"]),
        {"alerts": blocked["alerts"], "screenshot": shot},
    )
    matrix.check(
        scenario, "容量拒绝不自动重试（会话 B 仅一次运行请求）",
        after["post_count"] == 1, after,
    )
    matrix.check(
        scenario, "拒绝后界面保持可发送", state["input_disabled"] is False and not state["has_stop"],
        {"input_disabled": state["input_disabled"], "has_stop": state["has_stop"]},
    )
    model_control("/control/release", {})
    thread.join(90)
    busy_run = latest_run(db_path, busy_session)
    matrix.phase(
        "B4_capacity", session=session_id, alerts=blocked["alerts"], requests_immediate=immediate,
        requests_after_wait=after, busy_run=busy_run, busy_events=len(events), screenshot=shot,
    )


def scenario_b5(matrix: Matrix, ctx) -> None:
    scenario = "B5"
    browser, client, db_path, url = ctx.browser, ctx.client, ctx.db, ctx.url
    set_mode("final")
    session_id = create_session(client, "任务六频率额度")
    results: list = []
    started = time.monotonic()
    # 单次 eval 受 25 秒命令超时约束，11 笔请求分四批在页内串行发出。
    for batch in (3, 3, 3, 2):
        js = (
            JS_SEND_BATCH
            .replace("SESSION_PLACEHOLDER", json.dumps(session_id))
            .replace("COUNT_PLACEHOLDER", str(batch))
            .replace("REQUEST_PLACEHOLDER", json.dumps(f"频率探测 {len(results) + 1}"))
        )
        chunk = browser.evaluate(js)
        if not isinstance(chunk, list):
            raise RuntimeError(f"批量发送返回异常：{chunk!r}")
        results.extend(chunk)
    elapsed = round(time.monotonic() - started, 2)
    accepted = [item for item in results if item["status"] == 200]
    limited = [item for item in results if item["status"] == 429]
    matrix.phase("B5_transport", session=session_id, elapsed_s=elapsed, results=results)
    ensure_page(browser, f"{url}/sessions/{session_id}")
    browser.call("network", "requests", "--clear", tolerant=True)
    ui_send(browser, "额度界面探针")
    alerted = wait_until_ui(
        browser, lambda s: any(RATE_MESSAGE in item for item in s["alerts"]), 45,
        "限流提示出现在界面",
    )
    requests = browser_run_requests(browser)
    shot = browser.screenshot("b5-rate-limited")
    time.sleep(2.0)
    after = browser_run_requests(browser)
    run_rows = db_rows(db_path, RUN_SQL, (session_id,))
    matrix.check(
        scenario, "十次运行受理成功", len(accepted) == 10,
        {"accepted": len(accepted), "elapsed_s": elapsed},
    )
    matrix.check(
        scenario, "第十一次返回 429 且携带 Retry-After",
        len(limited) >= 1 and limited[0]["retry_after"] not in (None, ""),
        limited[:3],
    )
    matrix.check(
        scenario, "界面呈现限流提示与可重试秒数",
        any(RATE_MESSAGE in item for item in alerted["alerts"])
        and any("秒后可重试" in item for item in alerted["alerts"]),
        {"alerts": alerted["alerts"], "screenshot": shot},
    )
    matrix.check(
        scenario, "限流拒绝不自动重试", after["post_count"] == 1, after
    )
    matrix.check(
        scenario, "被拒运行未落库（会话仅十次运行）", len(run_rows) == 10,
        {"run_rows": len(run_rows)},
    )
    matrix.phase(
        "B5_rate_limit", alerts=alerted["alerts"], requests=after, run_rows=len(run_rows),
        screenshot=shot,
    )


def scenario_b6(matrix: Matrix, ctx) -> None:
    scenario = "B6"
    browser, client, db_path, url = ctx.browser, ctx.client, ctx.db, ctx.url
    set_mode("tool_profile", base_version=None)
    session_id = new_session(browser)
    ui_send(browser, "整理画像并保存")
    with_button = wait_until_ui(browser, lambda s: s["has_confirm_button"], 90, "画像确认按钮出现")
    matrix.check(
        scenario, "画像提案在界面给出确认入口", with_button["has_confirm_button"],
        {"confirm_buttons": with_button["confirm_button"]},
    )
    browser.call("find", "role", "button", "click", "--name", "确认保存画像")
    saved = wait_until_ui(
        browser, lambda s: any("确认保存画像 · 已保存" in item for item in s["statuses"]),
        60, "界面展示已保存",
    )
    profile = client.get("/api/profile").json()
    save_rows = db_rows(db_path, "SELECT COUNT(*) FROM profile_save_records")[0][0]
    shot = browser.screenshot("b6-profile-saved")
    matrix.check(
        scenario, "确认按钮保存真实业务结果",
        any("确认保存画像 · 已保存" in item for item in saved["statuses"])
        and profile.get("version") == 1,
        {"profile_version": profile.get("version"), "save_rows": save_rows, "screenshot": shot},
    )
    matrix.phase(
        "B6_confirmation_saved", session=session_id, profile=profile, save_rows=save_rows,
        statuses=saved["statuses"], screenshot=shot,
    )

    wait_until_ui(
        browser, lambda s: s["input_disabled"] is False and not s["has_stop"], 45,
        "确认后界面恢复可发送",
    )
    set_mode("hold")
    ui_send(browser, "停机中断探针")
    run = wait_latest_run(db_path, session_id, 45, ("running",))
    wait_until_ui(browser, lambda s: s["has_stop"], 30, "停机前运行中")
    browser.screenshot("b6-running-before-stop")
    snapshot_path = ctx.out / "process-b6-before-stop.json"
    tree = snapshot(ctx.process_script, ctx.app.pid, ctx.ports, snapshot_path)
    matrix.phase(
        "B6_running_before_stop", run_id=run["id"], process_count=len(tree["processes"]),
        listeners=tree["listeners"],
    )
    stop_result = stop_tree(ctx.process_script, snapshot_path, ctx.ports,
                            ctx.out / "process-b6-stop.json")
    row_at_stop = latest_run(db_path, session_id)
    matrix.check(
        scenario, "强制停机时运行行保持 running", row_at_stop["status"] == "running",
        row_at_stop,
    )
    matrix.check(
        scenario, "整棵进程树停止且端口释放",
        not stop_result["remaining_pids"] and not stop_result["listeners"],
        {"stopped": stop_result["results"], "remaining_pids": stop_result["remaining_pids"],
         "listeners": stop_result["listeners"]},
    )
    terminate(ctx.app)
    ctx.app = start_app(ctx.root, ctx.env, ctx.out / "app-b6-restart.log")
    wait_backend()
    wait_frontend(ctx.frontend_port)
    restarted = snapshot(ctx.process_script, ctx.app.pid, ctx.ports,
                         ctx.out / "process-b6-restarted.json")
    queried = run_query(client, session_id, run["id"])
    ensure_page(browser, f"{url}/sessions/{session_id}")
    state = wait_until_ui(browser, lambda s: s["interrupted"], 45, "界面展示已中断")
    shot = browser.screenshot("b6-interrupted")
    profile_after = client.get("/api/profile").json()
    matrix.check(
        scenario, "运行查询回报 interrupted",
        queried["http_status"] == 200 and queried["body"]["status"] == "interrupted", queried,
    )
    matrix.check(
        scenario, "重启后界面展示已中断", state["interrupted"],
        {"statuses": state["statuses"], "alerts": state["alerts"], "screenshot": shot},
    )
    matrix.check(
        scenario, "已保存画像结果仍在界面展示",
        any("确认保存画像 · 已保存" in item for item in state["statuses"])
        and profile_after.get("version") == 1,
        {"statuses": state["statuses"], "profile_version": profile_after.get("version")},
    )
    set_mode("final")
    wait_until_ui(
        browser, lambda s: s["input_disabled"] is False and not s["has_stop"], 45,
        "重启后界面恢复可发送",
    )
    ui_send(browser, "重启后新运行")
    finished = wait_until_ui(
        browser, lambda s: not s["has_stop"] and "重启后新运行" in s["text"], 60,
        "重启后新运行结束",
    )
    new_run = wait_latest_run(db_path, session_id, 30, ("completed",))
    browser.screenshot("b6-new-run-accepted")
    matrix.check(
        scenario, "人工重启后接受新运行", new_run["status"] == "completed",
        {"run": new_run, "text_tail": finished["text"][-140:]},
    )
    matrix.phase(
        "B6_manual_restart", session=session_id, run_id=run["id"], run_query=queried,
        ui_statuses=state["statuses"], interrupted_display=state["interrupted"],
        restarted_processes=len(restarted["processes"]), profile_after=profile_after,
        new_run=new_run, screenshots=[shot],
    )


def scenario_b7(matrix: Matrix, ctx) -> None:
    scenario = "B7"
    browser, client, db_path, url = ctx.browser, ctx.client, ctx.db, ctx.url
    root, out = ctx.root, ctx.out
    set_mode("hold")
    fault_session = create_session(client, "任务六故障封闭")
    thread, holder, events = start_run_thread(client, fault_session, "故障封闭触发")
    wait_for(lambda: model_control("/control/state")["held"] >= 1, 30)
    ready = out / "b7-lock.ready"
    release = out / "b7-lock.release"
    lock = start_lock_helper(root, db_path, "exclusive", ready, release, 90, out / "b7-lock.log")
    ctx.lock_helpers.append(lock)
    wait_for(lambda: ready.exists(), 20)
    matrix.phase("B7_lock_held", lock_pid=lock.pid, run_id=holder.get("run_id"))
    model_control("/control/release", {})
    thread.join(90)
    release.write_text("release", encoding="utf-8")
    lock.wait(timeout=15)
    def settled_run() -> dict | None:
        row = latest_run(db_path, fault_session)
        if row is None or (row["status"] == "running" and not row["stop_diagnostic"]):
            return None
        return row

    fault_run = wait_for(settled_run, 45)
    matrix.phase(
        "B7_fault_closed", session=fault_session, run_id=holder.get("run_id"),
        stream_events=len(events), has_terminal=any(
            item["event"] in {"done", "error"} for item in events
        ),
        fault_run=fault_run,
    )
    browser.call("network", "requests", "--clear", tolerant=True)
    session_id = new_session(browser)
    ui_send(browser, "故障封闭探针")
    blocked = wait_until_ui(
        browser, lambda s: any(FAULT_MESSAGE in item for item in s["alerts"]), 45,
        "故障封闭拒绝提示",
    )
    requests = browser_run_requests(browser)
    shot = browser.screenshot("b7-fault-closed")
    read_only = {}
    for name, path in (
        ("sessions", "/api/sessions"),
        ("profile", "/api/profile"),
        ("history", f"/api/sessions/{fault_session}/history"),
        ("fault_run", f"/api/sessions/{fault_session}/runs/{holder.get('run_id')}"),
        ("runs", f"/api/sessions/{fault_session}/runs"),
    ):
        read_only[name] = client.get(path).status_code
    ensure_page(browser, f"{url}/profile", ready=profile_page_ready)
    profile_body = page_text(browser)
    profile_view = browser.ui_state()
    profile_shot = browser.screenshot("b7-read-only-profile")
    ensure_page(browser, f"{url}/sessions/{session_id}")
    chat_view = wait_until_ui(browser, lambda s: s["has_textarea"], 40, "会话视图加载")
    matrix.check(
        scenario, "界面呈现 503 service_unavailable 拒绝",
        any(FAULT_MESSAGE in item for item in blocked["alerts"]),
        {"alerts": blocked["alerts"], "screenshot": shot},
    )
    matrix.check(scenario, "故障封闭拒绝不自动重试", requests["post_count"] == 1, requests)
    matrix.check(
        scenario, "只读接口仍可用", all(value == 200 for value in read_only.values()), read_only
    )
    matrix.check(
        scenario, "只读视图在浏览器正常渲染",
        profile_rendered(profile_body)
        and not profile_view["alerts"]
        and chat_view["has_textarea"],
        {"profile_text_head": profile_body[:160],
         "profile_alerts": profile_view["alerts"], "screenshot": profile_shot},
    )
    matrix.check(
        scenario, "故障运行保留未提交终态诊断",
        fault_run["stop_diagnostic"] in {"worker_stuck", "terminal_persist_failed"}, fault_run,
    )
    matrix.phase(
        "B7_fault_closure", session=session_id, alerts=blocked["alerts"], requests=requests,
        read_only=read_only, fault_run=fault_run,
        profile_view_text=profile_body[:200], screenshot=shot,
        profile_screenshot=profile_shot,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--frontend-port", type=int, default=15173)
    parser.add_argument("--scenarios", default="B1,B2,B3,B4,B5,B6,B7")
    args = parser.parse_args()

    # 日志重定向到文件时仍统一写 UTF-8，避免中文证据在控制台编码下不可读。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    root = Path(args.root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if root.name == "fit-agent-task5":
        shutil.rmtree(root / "backend" / "data", ignore_errors=True)
        shutil.rmtree(root / "backend" / "temp", ignore_errors=True)
        shutil.rmtree(root / "tmp" / "sessions", ignore_errors=True)

    matrix = Matrix(out)
    process_script = root / "scripts" / "stability-task5" / "process.ps1"
    db_path = root / "backend" / "data" / "fit-agent.db"
    tree_ports = [8000, args.frontend_port]
    probe_ports = [8000, args.frontend_port, 18081]
    wanted = [item.strip().upper() for item in args.scenarios.split(",") if item.strip()]

    env = dict(os.environ)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["FIT_AGENT_FRONTEND_PORT"] = str(args.frontend_port)
    env["FIT_AGENT_RUN_BUDGET_SECONDS"] = "45"
    env["FIT_AGENT_SQLITE_TIMEOUT_SECONDS"] = "2"
    env["PYTHONUNBUFFERED"] = "1"
    (root / "backend" / ".env").write_text(
        "FIT_AGENT_RUN_BUDGET_SECONDS=45\nFIT_AGENT_SQLITE_TIMEOUT_SECONDS=2\n", encoding="utf-8"
    )

    before = netstat_capture(out, "before", probe_ports)
    matrix.phase(
        "preflight", root=str(root), db=str(db_path), scenarios=wanted,
        run_budget_s=45, sqlite_timeout_s=2, netstat_before=before, browser=str(BROWSER_EXE)
    )
    for port, count in before["listeners"].items():
        if count:
            raise RuntimeError(f"端口 {port} 已被占用（监听 {count}），停止验收")

    browser = Browser(out)
    model = None
    app = None
    client = None
    lock_helpers: list = []
    try:
        model = start_model_server(root / "backend", out / "model.ready", out / "model.log")
        wait_for(lambda: (out / "model.ready").exists(), 20)
        matrix.phase("model_server", pid=model.pid, state=model_control("/control/state"))

        app = start_app(root, env, out / "app-first.log")
        wait_backend()
        wait_frontend(args.frontend_port)
        tree = snapshot(process_script, app.pid, tree_ports, out / "process-start.json")
        matrix.phase(
            "app_started", root_pid=app.pid, process_count=len(tree["processes"]),
            listeners=tree["listeners"],
        )

        client = http_client()
        configure_model(client)
        set_mode("final")
        warmup_session = create_session(client, "任务六预热")
        warmup_started = time.monotonic()
        warmup_holder, warmup_events = run_stream(client, warmup_session, "预热")
        matrix.phase(
            "warmup", session=warmup_session, run_id=warmup_holder.get("run_id"),
            elapsed_s=round(time.monotonic() - warmup_started, 2),
            terminal=warmup_events[-1] if warmup_events else None,
        )

        url = f"http://localhost:{args.frontend_port}"
        browser.call("tab", "list", tolerant=True)
        ensure_page(browser, url)
        background = browser.evaluate(JS_BACKGROUND) or {}
        browser.screenshot("b0-initial")
        matrix.phase("browser_open", url=url, background=background)
        matrix.check(
            "B0", "headless 页面处于可渲染状态",
            background.get("visibility") == "visible", background,
        )

        ctx = SimpleNamespace(
            browser=browser, client=client, db=db_path, root=root, out=out,
            process_script=process_script, ports=tree_ports, env=env,
            frontend_port=args.frontend_port, url=url, app=app,
            lock_helpers=lock_helpers,
        )

        scenarios = {
            "B1": scenario_b1, "B2": scenario_b2, "B3": scenario_b3, "B4": scenario_b4,
            "B5": scenario_b5, "B6": scenario_b6, "B7": scenario_b7,
        }
        for name in wanted:
            if name not in scenarios:
                raise RuntimeError(f"未知场景 {name}")
            scenarios[name](matrix, ctx)
            app = ctx.app

        matrix.boundary(
            "真实慢客户端/背压用例未在本浏览器矩阵取证：agent-browser 0.25.4 无网络节流能力"
            "（仅 set offline、tab close、network route --abort），由 test_stability_task3.py "
            "与 verify:stability 覆盖。"
        )
        matrix.boundary(
            "只剥离自定义响应头并保留 SSE 正文的故障无法通过 network route 构造"
            "（route 仅支持 --abort/--body），由 test_stability_task3.py 与 verify:stability 覆盖。"
        )
        matrix.phase(
            "completed", scenarios=wanted, assertions=len(matrix.data["assertions"]),
            failures=len(matrix.failed),
        )
    except Exception as error:
        matrix.phase("aborted", error=type(error).__name__, message=str(error))
        raise
    finally:
        browser.close()
        matrix.phase("browser_closed")
        if app is not None:
            try:
                final_snapshot = snapshot(process_script, app.pid, tree_ports,
                                          out / "process-final.json")
                stop_result = stop_tree(process_script, out / "process-final.json", tree_ports,
                                        out / "process-final-stop.json")
                matrix.phase(
                    "final_stop", process_count=len(final_snapshot["processes"]),
                    stopped=stop_result["results"],
                    remaining_pids=stop_result["remaining_pids"],
                    listeners=stop_result["listeners"],
                )
            except Exception as error:
                matrix.phase("final_stop_error", error=type(error).__name__, message=str(error))
            terminate(app)
        for helper in lock_helpers:
            terminate(helper)
        terminate(model)
        after = netstat_capture(out, "after", probe_ports)
        matrix.phase(
            "netstat_after", netstat_after=after, evidence=str(matrix.path),
            browser_command_log=str(browser.log_path),
        )
        matrix.write()

    failures = matrix.failed
    if failures:
        print(
            "FAIL: 浏览器故障矩阵未达标断言 "
            + json.dumps(
                [{"scenario": item["scenario"], "point": item["point"]} for item in failures],
                ensure_ascii=False,
            ),
            flush=True,
        )
        sys.exit(1)
    print(
        f"PASS: 浏览器故障矩阵全部断言达标（{len(matrix.data['assertions'])} 项，"
        f"场景 {'、'.join(wanted)}）", flush=True
    )


if __name__ == "__main__":
    main()
