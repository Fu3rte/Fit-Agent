import argparse
import copy
import json
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

from test import plan_adjustment_e2e_support as support
from app.domain.business.models import BusinessContext, WorkoutListArguments
from app.infrastructure.persistence.sqlite import database as database_module
from uuid import uuid4

RUN_ID = "run-2026-10-09-a"
EVIDENCE = support.REPO / "tmp" / "plan-adjustment-e2e-acceptance"
STATE_NAME = "state.json"


def evidence_root(run_id: str) -> Path:
    root = EVIDENCE / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_path(root: Path) -> Path:
    return root / STATE_NAME


def port_free(port: int) -> bool:
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        probe.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def baseline_snapshot(http) -> dict:
    return {
        "profile": http.get("/api/profile").json(),
        "current": support.current_plan(http),
        "plans": support.list_plans(http),
        "workouts": support.list_workouts(http),
    }


def stage_setup(root: Path, state: dict, http) -> None:
    seeded = support.seed_baseline(http)
    if seeded:
        state["baseline"] = seeded
    assert state.get("baseline"), "缺少基线种子信息"
    baseline = state["baseline"]

    profile = http.get("/api/profile").json()
    assert profile["version"] == 1, profile
    assert profile["content"]["goal"] == "增肌与一般力量提升"

    current = support.current_plan(http)
    assert current["id"] == baseline["plan_id"], current
    plans = support.list_plans(http)
    assert len(plans) == 1 and plans[0]["is_current"] is True, plans

    workouts = support.list_workouts(http)
    assert workouts["total"] == 12 and len(workouts["items"]) == 12, workouts
    today = support.business_today()
    assert {record["performed_on"] for record in workouts["items"]} == {
        (today - timedelta(days=offset)).isoformat() for offset in range(12)
    }

    arguments = WorkoutListArguments(
        date_from=None,
        date_to=baseline["business_date"],
        page=1,
        page_size=10,
    )
    result = support.call(support.interface.app.state.business.list_workouts(arguments))
    items = [record.model_dump() for record in result.items]
    assert result.total == 12 and result.page == 1 and result.page_size == 10, result
    assert len(items) == 10, items
    expected = sorted(
        workouts["items"], key=lambda record: (record["performed_on"], record["id"]), reverse=True
    )[:10]
    assert items == expected, "最近 10 条取数与完整记录不一致"
    keys = [(record["performed_on"], record["id"]) for record in items]
    assert keys == sorted(keys, reverse=True), keys

    session = state.get("session_a")
    if not session:
        session = support.create_session(http, "调整验收-主线")
        state["session_a"] = session
    support.open_session(session)
    support.screenshot(root / "setup-chat.png")
    assert support.js("document.querySelectorAll('textarea').length >= 1")

    support.open_path("/plans")
    support.until("document.body.innerText.includes('当前计划')")
    support.until("document.body.innerText.includes('已保存版本')")
    text = support.js("document.body.innerText")
    assert "当前计划" in text and "已保存版本" in text
    support.screenshot(root / "setup-plans.png")

    state["baseline_snapshot"] = baseline_snapshot(http)
    support.dump(root, "setup.json", state["baseline_snapshot"])
    print("PASS setup: 隔离库基线（画像 v1、当前计划、12 条训练记录、最近 10 条取数）与浏览器可访问")


def exercise_signature(exercise: dict) -> dict:
    return {
        key: exercise[key]
        for key in (
            "exercise_id",
            "name",
            "reps",
            "duration_seconds",
            "weight_kg",
            "load_convention",
            "rest_seconds",
        )
    }


def collect_notes(payload: dict) -> str:
    parts = [payload.get("notes") or ""]
    for day in payload["days"]:
        parts.append(day.get("notes") or "")
    return "\n".join(parts)


def success_named(run: dict, name: str) -> list[dict]:
    return [item for item in support.named(run, name) if item["is_error"] is False]


def prepare_success(run: dict) -> tuple[dict, dict]:
    items = success_named(run, "prepare_plan_adjustment")
    assert len(items) == 1, [(item["name"], item["is_error"]) for item in support.tool_calls(run)]
    return json.loads(items[0]["content"]), items[0]


def display_message(session: str, entry_id: str):
    return entry_message(session, entry_id)


def entry_message(session: str, entry_id: str):
    entries = support.call(support.interface.app.state.session_service.get_current_branch(session))
    matched = [entry for entry in entries if entry.id == entry_id]
    assert len(matched) == 1, entry_id
    return matched[0].messages[0]


def plan_status(session: str, proposal_id: str) -> dict:
    context = BusinessContext(
        timezone="Asia/Shanghai",
        business_date=support.business_today().isoformat(),
        session_id=session,
        run_id=str(uuid4()),
        request_entry_id=str(uuid4()),
        source_entry_id=str(uuid4()),
    )
    return support.call(
        support.interface.app.state.business.get_plan_save_status(context, proposal_id)
    ).model_dump()


def display_for(session: str, proposal_id: str) -> str:
    bindings = support.call(support.interface.app.state.business.list_plan_display_bindings(session))
    matches = [entry for entry, value in bindings.items() if value == proposal_id]
    assert len(matches) == 1, bindings
    return matches[0]


def stage_A(root: Path, state: dict, http) -> None:
    # 每次以新会话运行：同一会话存在待确认快照时模型按契约不再重复准备。
    session = support.create_session(http, "调整验收-主线")
    state["session_a"] = session
    state.pop("proposal_a", None)
    state.pop("display_a", None)
    baseline = state["baseline_snapshot"]["current"]["content"]
    business_date = state["baseline"]["business_date"]
    support.open_session(session)

    before_current = support.current_plan(http)
    before_plans = support.list_plans(http)

    prompt = (
        "请调整我当前的训练计划：把第 1 天（推日）的第一个动作「杠铃卧推」由 4 组改成 5 组，"
        "其余动作、重量、休息时间、训练日和休息日全部保持不变。先读取真实画像状态与版本、当前计划的完整内容，"
        "并固定查询最近 10 条实际训练记录（date_from=null、date_to 用后端 business_date、page=1、page_size=10），"
        "结合读到的记录说明本次改动的理由。完整展示调整后的计划、全部字段、notes 与建议来源，等待我确认，先不要保存。"
    )
    run = support.send_and_wait(support.OBS, prompt)
    support.dump(root, "A-run.json", run)

    assert success_named(run, "get_profile"), "缺少 get_profile 成功结果"
    assert success_named(run, "get_current_plan"), "缺少 get_current_plan 成功结果"
    queries = success_named(run, "list_workouts")
    assert queries, "缺少 list_workouts 成功结果"
    for item in queries:
        arguments = item["arguments"]
        assert arguments.get("date_from") is None, arguments
        assert arguments.get("date_to") == business_date, arguments
        assert arguments.get("page", 1) == 1 and arguments.get("page_size", 10) == 10, arguments

    listing = json.loads(queries[0]["content"])
    expected_items = sorted(
        state["baseline_snapshot"]["workouts"]["items"],
        key=lambda record: (record["performed_on"], record["id"]),
        reverse=True,
    )[:10]
    assert listing["total"] == 12 and listing["page"] == 1 and listing["page_size"] == 10, listing
    assert len(listing["items"]) == 10, listing
    assert listing["items"] == expected_items, "最近 10 条取数与数据库完整记录不一致"

    proposal, prepare_item = prepare_success(run)
    assert proposal["preparation_kind"] == "adjustment", proposal["preparation_kind"]
    assert proposal["base_profile_version"] == 1, proposal["base_profile_version"]
    assert proposal["base_plan_id"] == state["baseline"]["plan_id"], proposal["base_plan_id"]
    payload = proposal["payload"]

    assert payload["repeat"] is True, payload["repeat"]
    modified = payload["days"][0]["exercises"][0]
    assert modified["name"] == "杠铃卧推", modified
    assert modified["sets"] == 5, modified
    original = baseline["days"][0]["exercises"][0]
    assert exercise_signature(modified) == exercise_signature(original), (modified, original)

    for index, day in enumerate(payload["days"]):
        source = baseline["days"][index]
        assert day["kind"] == source["kind"] and day["focus"] == source["focus"], (day, source)
        assert len(day["exercises"]) == len(source["exercises"]), (day, source)
        if index == 0:
            for position in range(1, len(source["exercises"])):
                assert exercise_signature(day["exercises"][position]) == exercise_signature(
                    source["exercises"][position]
                )
        else:
            for position, exercise in enumerate(day["exercises"]):
                assert exercise_signature(exercise) == exercise_signature(
                    source["exercises"][position]
                )

    assert "/days/1/exercises/0/rest_seconds" in payload["suggested_fields"], payload["suggested_fields"]
    assert "/days/2/exercises/0/reps" in payload["suggested_fields"], payload["suggested_fields"]
    assert any(
        pointer.startswith("/days/0/exercises/0") for pointer in payload["suggested_fields"]
    ), payload["suggested_fields"]

    notes = collect_notes(payload)
    facts = {record["performed_on"] for record in expected_items}
    assert any(day in notes for day in facts), (notes, facts)
    assert "5" in notes and "4" in notes, notes

    assert support.current_plan(http) == before_current, "确认前当前计划发生变化"
    assert support.list_plans(http) == before_plans, "确认前计划版本数发生变化"
    assert not success_named(run, "save_plan"), "未确认即出现保存"
    assert not success_named(run, "prepare_plan") and not success_named(run, "prepare_plan_import")

    display = prepare_item["entry_id"]
    bindings = support.call(support.interface.app.state.business.list_plan_display_bindings(session))
    matches = [entry for entry, proposal_id in bindings.items() if proposal_id == proposal["proposal_id"]]
    assert len(matches) == 1, bindings
    assert matches[0] == display, (matches[0], display)
    persisted = display_message(session, display)
    from app.ai.messages import text_projection

    assert persisted.is_error is False and persisted.tool_name == "prepare_plan_adjustment"
    assert json.loads(text_projection(persisted.content)) == proposal, "展示节点与准备结果不一致"

    transcript = support.js(support.TRACE)
    assert "杠铃卧推" in transcript and "建议" in transcript, transcript[:500]
    assert "5 组" in transcript, transcript[:500]
    support.screenshot(root / "A-chat.png")

    state["proposal_a"] = proposal["proposal_id"]
    state["display_a"] = display
    support.dump(
        root,
        "A.json",
        {
            "prompt": prompt,
            "tool_calls": support.tool_calls(run),
            "proposal": proposal,
            "display_entry_id": display,
            "recent10": listing,
            "notes": notes,
            "transcript": transcript,
        },
    )
    print("PASS A: 浏览器调整当前计划；真实工具取数与最近 10 条、完整 payload、来源延续与展示绑定一致")


def stage_B(root: Path, state: dict, http) -> None:
    session = state["session_a"]
    support.open_session(session)
    before_plans = support.list_plans(http)
    before_current = support.current_plan(http)
    workouts_before = support.list_workouts(http)

    run1 = support.send_and_wait(
        support.OBS,
        "确认，但把第 1 天（推日）第一个动作「杠铃卧推」再改成 6 组，其余动作、重量、休息、"
        "训练日和休息日保持不变；请重新完整展示，等待我再次确认，先不要保存。",
    )
    support.dump(root, "B-modify.json", run1)
    proposal, _ = prepare_success(run1)
    assert proposal["proposal_id"] != state["proposal_a"], "修改式确认未创建新快照"
    assert proposal["payload"]["days"][0]["exercises"][0]["sets"] == 6, proposal["payload"]["days"][0]
    assert plan_status(session, state["proposal_a"])["status"] == "invalidated", "原 pending 快照未失效"
    assert support.current_plan(http) == before_current, "修改式确认改动了当前计划"
    assert [record["id"] for record in support.list_plans(http)] == [
        record["id"] for record in before_plans
    ], "修改式确认新增了计划版本"
    assert not success_named(run1, "save_plan"), "修改式确认出现保存"

    run2 = support.send_and_wait(
        support.OBS, "确认无误，采纳并保存刚才最后完整展示的这份计划。"
    )
    support.dump(root, "B-save.json", run2)
    saves = success_named(run2, "save_plan")
    assert len(saves) == 1, [(call["name"], call["is_error"]) for call in support.tool_calls(run2)]
    saved = json.loads(saves[0]["content"])
    arguments = saves[0]["arguments"]
    assert saved["proposal_id"] == proposal["proposal_id"], (saved, proposal["proposal_id"])
    assert saved["content"] == proposal["payload"], "保存内容与准备 payload 不一致"
    assert saved["created_at"] == saved["saved_at"], saved
    assert arguments["proposal_id"] == proposal["proposal_id"], arguments
    display = display_for(session, proposal["proposal_id"])
    assert arguments["display_entry_id"] == display, arguments

    after_plans = support.list_plans(http)
    assert len(after_plans) == len(before_plans) + 1, after_plans
    assert after_plans[0]["id"] == saved["id"] and after_plans[0]["is_current"] is True, after_plans[0]
    assert after_plans[0]["content"] == saved["content"]
    assert [record["id"] for record in after_plans[1:]] == [record["id"] for record in before_plans]
    assert support.current_plan(http) == {"id": saved["id"], "content": saved["content"]}
    assert support.list_workouts(http) == workouts_before, "保存改动了训练记录"
    status = plan_status(session, proposal["proposal_id"])
    assert status["status"] == "saved" and status["result"] == saved, status

    entries = support.call(support.interface.app.state.session_service.get_current_branch(session))
    ids = [entry.id for entry in entries]
    confirmation = arguments["confirmation_entry_id"]
    assert display in ids and confirmation in ids, (display, confirmation)
    assert ids.index(display) < ids.index(confirmation), "确认节点不在展示之后"
    confirmation_message = entry_message(session, confirmation)
    assert confirmation_message.role == "user", confirmation_message.role

    support.open_path("/plans")
    support.until("document.body.innerText.includes('已保存版本')")
    support.until("document.body.innerText.includes('当前')")
    page_text = support.js("document.body.innerText")
    badges = support.js(
        "Array.from(document.querySelectorAll('*'))."
        "filter(el => el.children.length === 0 && el.textContent.trim() === '当前').length"
    )
    assert badges == 1, (badges, page_text[:400])
    support.screenshot(root / "B-plans.png")

    state["proposal_b"] = proposal["proposal_id"]
    state["display_b"] = display
    state["saved_b"] = saved
    support.dump(
        root,
        "B.json",
        {
            "modify_calls": support.tool_calls(run1),
            "save_calls": support.tool_calls(run2),
            "saved": saved,
            "page_text": page_text,
        },
    )
    print("PASS B: 修改式确认使旧快照失效且当前计划保持原值；自然语言确认只新增一个完整版本并切换当前计划")


def stage_C(root: Path, state: dict, http) -> None:
    session = state["session_a"]
    support.open_session(session)
    plan_before = support.current_plan(http)
    plans_before = support.list_plans(http)
    today = support.business_today().isoformat()

    run1 = support.send_and_wait(
        support.OBS,
        "请再调整当前计划：把第 3 天（腿日）的杠铃高杠深蹲由 4 组改成 5 组，其余全部保持不变；"
        "先读取画像与当前计划，固定查询最近 10 条实际训练记录作为依据，完整展示调整后的计划与理由，"
        "等待我确认，先不要保存。",
    )
    support.dump(root, "C-adjust.json", run1)
    proposal_c1, _ = prepare_success(run1)
    assert proposal_c1["payload"]["days"][2]["exercises"][0]["sets"] == 5, proposal_c1["payload"]["days"][2]
    assert support.current_plan(http) == plan_before
    assert [record["id"] for record in support.list_plans(http)] == [record["id"] for record in plans_before]
    queries = success_named(run1, "list_workouts")
    facts_before = json.loads(queries[0]["content"]) if queries else None

    run2 = support.send_and_wait(
        support.OBS,
        f"纠正今天的训练记录：今天完成的杠铃卧推实际是 4 组，分别是 8 次 62.5 kg、8 次 65 kg、"
        f"7 次 67.5 kg、6 次 70 kg。今天是 {today}。请先查询该日记录，完整展示待确认内容。",
    )
    support.dump(root, "C-workout-prepare.json", run2)
    prepared_workout = success_named(run2, "prepare_workout")
    assert len(prepared_workout) == 1, [call["name"] for call in support.tool_calls(run2)]

    run3 = support.send_and_wait(support.OBS, "确认保存这份训练记录。")
    support.dump(root, "C-workout-save.json", run3)
    saved_workout = success_named(run3, "save_workout") + success_named(run3, "update_workout")
    assert len(saved_workout) == 1, [call["name"] for call in support.tool_calls(run3)]
    workout_result = json.loads(saved_workout[0]["content"])
    assert workout_result["performed_on"] == today, workout_result

    today_records = support.list_workouts(http, query=f"date_from={today}&date_to={today}")["items"]
    assert any(
        len(exercise["sets"]) == 4
        for record in today_records
        for exercise in record["content"]["exercises"]
    ), today_records
    assert plan_status(session, proposal_c1["proposal_id"])["status"] == "pending", "训练记录变化改动了原待确认计划"
    assert plan_status(session, proposal_c1["proposal_id"])["result"] is None

    run4 = support.send_and_wait(
        support.OBS,
        "现在采用刚才新增/修正的这条训练事实，重新调整刚才那份计划：以新的实际记录为依据重新查询并准备完整计划，"
        "完整展示新的依据与改动，等待我再次确认。",
    )
    support.dump(root, "C-readjust.json", run4)
    proposal_c2, _ = prepare_success(run4)
    assert proposal_c2["proposal_id"] != proposal_c1["proposal_id"], "未重新准备新快照"
    assert plan_status(session, proposal_c1["proposal_id"])["status"] == "invalidated", "原快照未失效"
    assert proposal_c2["payload"]["days"][2]["exercises"][0]["sets"] == 5
    notes = collect_notes(proposal_c2["payload"])
    assert "4" in notes, notes
    assert support.current_plan(http) == plan_before

    run5 = support.send_and_wait(support.OBS, "确认，保存重新调整后的这份计划。")
    support.dump(root, "C-save.json", run5)
    saves = success_named(run5, "save_plan")
    assert len(saves) == 1, [call["name"] for call in support.tool_calls(run5)]
    saved = json.loads(saves[0]["content"])
    assert saved["proposal_id"] == proposal_c2["proposal_id"]
    assert saved["content"] == proposal_c2["payload"], "保存内容与新展示不一致"
    after_plans = support.list_plans(http)
    assert len(after_plans) == len(plans_before) + 1
    assert after_plans[0]["id"] == saved["id"] and after_plans[0]["is_current"] is True

    state["proposal_c"] = proposal_c2["proposal_id"]
    state["saved_c"] = saved
    support.dump(
        root,
        "C.json",
        {
            "facts_before": facts_before,
            "workout_result": workout_result,
            "today_records": today_records,
            "proposal_c1": proposal_c1,
            "proposal_c2": proposal_c2,
            "saved": saved,
        },
    )
    print("PASS C: 修正训练事实后原待确认计划保持原值；采用新事实重新准备、重新展示并确认保存一致")


PLAN_MARKDOWN = """# 我的循环安排

推、拉、腿、休，四天一个循环。

## 推日
（动作与参数待定）

## 拉日
（动作与参数待定）

## 腿日
（动作与参数待定）

## 休息日
"""


def upload_plan_file(root: Path) -> Path:
    directory = root / "upload"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plan-cycle.md"
    path.write_bytes(PLAN_MARKDOWN.encode("utf-8"))
    return path


def session_history(http, session: str) -> dict:
    response = http.get(f"/api/sessions/{session}/history")
    assert response.status_code == 200, response.text
    return response.json()


def stage_D1(root: Path, state: dict, http) -> None:
    file_path = upload_plan_file(root)
    session = support.create_session(http, "调整验收-上传")
    state["session_d"] = session
    support.open_session(session)
    plan_before = support.current_plan(http)
    plans_before = support.list_plans(http)

    run = support.send_attachment(
        support.OBS,
        "这是我上传的训练循环文件。请直接把它作为调整对象：保持推、拉、腿、休四日循环，"
        "训练日的具体动作与参数按文件保持未知，整理成完整计划并完整展示，等待我确认，先不要保存。",
        file_path,
    )
    support.dump(root, "D-upload.json", run)

    history = session_history(http, session)
    user_entries = [entry for entry in history["entries"] if entry["message"]["role"] == "user"]
    attachments = [
        item for entry in user_entries for item in entry["message"].get("attachments", [])
    ]
    assert len(attachments) == 1, attachments
    attachment = attachments[0]
    assert attachment["file_name"] == "plan-cycle.md", attachment
    assert attachment["size_bytes"] == len(PLAN_MARKDOWN.encode("utf-8")), attachment

    storage_path = f"sessions/{session}/attachments/{attachment['attachment_id']}.md"
    reads = [call for call in support.named(run, "read") if call["is_error"] is False]
    assert any(
        call["arguments"].get("path") == storage_path for call in reads
    ), [call["arguments"] for call in reads]
    assert any("推、拉、腿、休" in (call["content"] or "") for call in reads), "未从真实附件读取全文"

    fetched = http.get(f"/api/sessions/{session}/attachments/{attachment['attachment_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["text"].encode("utf-8") == PLAN_MARKDOWN.encode("utf-8"), "原附件内容被改动"

    proposal, prepare_item = prepare_success(run)
    payload = proposal["payload"]
    assert proposal["preparation_kind"] == "adjustment", proposal["preparation_kind"]
    kinds = [day["kind"] for day in payload["days"]]
    assert kinds[:4] == ["training", "training", "training", "rest"], kinds
    for day in payload["days"][:3]:
        assert day["exercises"] == [], day
    assert payload["days"][3]["exercises"] == []
    assert support.current_plan(http) == plan_before, "上传直接调整改动了当前计划"
    assert [record["id"] for record in support.list_plans(http)] == [
        record["id"] for record in plans_before
    ]

    state["proposal_d"] = proposal["proposal_id"]
    state["display_d"] = prepare_item["entry_id"]
    support.dump(
        root,
        "D1.json",
        {
            "attachment": attachment,
            "storage_path": storage_path,
            "read_calls": [call["arguments"] for call in reads],
            "proposal": proposal,
            "history": history,
        },
    )
    print("PASS D1: 真实附件入口受理；Agent 按真实路径读取原文；四日循环与未知动作详情按文件保留；当前计划不变")


def stage_D2(root: Path, state: dict, http) -> None:
    session = state["session_d"]
    support.open_session(session)
    plans_before = support.list_plans(http)
    run = support.send_and_wait(support.OBS, "确认，保存这份最终计划。")
    support.dump(root, "D-save.json", run)
    saves = success_named(run, "save_plan")
    assert len(saves) == 1, [call["name"] for call in support.tool_calls(run)]
    saved = json.loads(saves[0]["content"])
    assert saved["proposal_id"] == state["proposal_d"]
    after = support.list_plans(http)
    assert len(after) == len(plans_before) + 1, "上传安排额外产生了历史版本"
    assert after[0]["id"] == saved["id"] and after[0]["is_current"] is True
    assert after[0]["content"]["days"][0]["exercises"] == []
    history = session_history(http, session)
    assert any(
        item["file_name"] == "plan-cycle.md"
        for entry in history["entries"]
        for item in entry["message"].get("attachments", [])
    )
    state["saved_d"] = saved
    support.dump(root, "D2.json", {"saved": saved, "history": history})
    print("PASS D2: 确认后仅保存最终调整版本，原上传安排未成为额外历史版本")


def stage_D_check(root: Path, state: dict, http) -> None:
    session = state["session_d"]
    history = session_history(http, session)
    entries = history["entries"]
    attachments = [
        item for entry in entries for item in entry["message"].get("attachments", [])
    ]
    assert len(attachments) == 1, attachments
    attachment = attachments[0]
    assert attachment["file_name"] == "plan-cycle.md", attachment
    storage = support.REPO / "tmp" / f"sessions/{session}/attachments/{attachment['attachment_id']}.md"
    assert storage.exists(), storage
    stored_bytes = storage.read_bytes()
    assert attachment["size_bytes"] == len(stored_bytes), attachment
    fetched = http.get(f"/api/sessions/{session}/attachments/{attachment['attachment_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["text"].replace("\r\n", "\n") == PLAN_MARKDOWN.replace("\r\n", "\n")

    run = json.loads((root / "D-upload.json").read_text(encoding="utf-8"))
    prepared = success_named(run, "prepare_plan_adjustment")
    reads = [call for call in support.named(run, "read") if call["is_error"] is False]
    support.open_session(session)
    button = "button[aria-label='查看 plan-cycle.md 原文']"
    support.until(f"!!document.querySelector({json.dumps(button)})", timeout=90)
    support.js(f"document.querySelector({json.dumps(button)}).click()")
    support.until("document.body.innerText.includes('推、拉、腿、休')", timeout=60)
    support.screenshot(root / "D-attachment.png")

    support.dump(
        root,
        "D-check.json",
        {
            "attachment": attachment,
            "storage": str(storage),
            "read_calls": [call["arguments"] for call in reads],
            "prepare_success_count": len(prepared),
            "entries": len(entries),
        },
    )
    print(
        "PASS D-check: 附件经真实浏览器入口受理并持久化，原文件字节不变，Agent 按真实路径读取全文；"
        f"prepare_plan_adjustment 成功数={len(prepared)}（预算耗尽未完成准备与保存）"
    )


def stage_F(root: Path, state: dict, http) -> None:
    from app.domain.business.models import PlanSaveArguments

    session = state["session_a"]
    saved = state["saved_c"]
    save_run = json.loads((root / "C-save.json").read_text(encoding="utf-8"))
    save_calls = [
        call
        for call in support.tool_calls(save_run)
        if call["name"] == "save_plan" and call["is_error"] is False
    ]
    assert save_calls, "缺少原保存调用"
    arguments = save_calls[0]["arguments"]
    context = BusinessContext(
        timezone="Asia/Shanghai",
        business_date=support.business_today().isoformat(),
        session_id=session,
        run_id=str(uuid4()),
        request_entry_id=str(uuid4()),
        source_entry_id=str(uuid4()),
    )
    plans_before = support.list_plans(http)
    repeat = support.call(
        support.interface.app.state.business.save_plan(
            context, PlanSaveArguments.model_validate(arguments)
        )
    ).model_dump()
    assert repeat == saved, (repeat, saved)
    assert len(support.list_plans(http)) == len(plans_before), "重复保存新增了计划版本"
    status = plan_status(session, saved["proposal_id"])
    assert status["status"] == "saved" and status["result"] == saved, status
    support.dump(root, "F.json", {"repeat": repeat, "status": status, "arguments": arguments})
    print("PASS F: 同一快照重复保存返回原固定结果，计划版本数不变，状态查询返回 saved 与原完整结果")


def stage_G(root: Path, state: dict, http) -> None:
    session = state["session_a"]
    support.open_session(session)
    support.until("document.body.innerText.includes('依据与注意事项')", timeout=120)
    before = support.js(support.TRACE)
    assert "第 1 天 · 训练日" in before, before[:400]
    support.screenshot(root / "G-chat-before.png")

    support.browser("reload")
    support.until("!!document.querySelector(\"textarea[aria-label='消息']\")", timeout=120)
    support.until("document.body.innerText.includes('依据与注意事项')", timeout=120)
    after = support.js(support.TRACE)
    assert after == before, "刷新后历史投影与实时不一致"

    support.open_path("/plans")
    support.until("document.body.innerText.includes('已保存版本')")
    plans = support.list_plans(http)
    assert plans[0]["is_current"] is True, plans[0]
    assert plans[0]["id"] == support.current_plan(http)["id"], plans[0]
    badges = support.js(
        "Array.from(document.querySelectorAll('*'))."
        "filter(el => el.children.length === 0 && el.textContent.trim() === '当前').length"
    )
    assert badges == 1, badges
    marker = next(
        (
            line.strip()
            for line in (plans[0]["content"]["notes"] or "").splitlines()
            if line.strip()
        ),
        "",
    )[:16]
    assert marker, plans[0]["content"]["notes"]
    before_fields = support.js("(document.body.innerText.match(/循环方式/g) || []).length")
    support.js(
        "(() => { const button = [...document.querySelectorAll('button')]"
        ".find(el => el.textContent.trim() === '查看完整内容');"
        " if (!button) return false; button.click(); return true; })()"
    )
    support.until(
        f"(document.body.innerText.match(/循环方式/g) || []).length > {before_fields}",
        timeout=60,
    )
    page_text = support.js("document.body.innerText")
    assert marker in page_text, (marker, page_text[:400])
    detail = http.get(f"/api/plans/{plans[0]['id']}")
    assert detail.status_code == 200 and detail.json()["content"] == plans[0]["content"]
    support.screenshot(root / "G-plans.png")

    errors = support.browser("errors")
    support.dump(
        root,
        "G.json",
        {
            "transcript_before": before,
            "transcript_after": after,
            "plans": plans,
            "page_text": page_text,
            "browser_errors": errors,
        },
    )
    print("PASS G: 会话历史恢复与刷新投影一致；计划页当前标记唯一；版本详情展开与实时查询一致")


def stage_D1_check(root: Path, state: dict, http) -> None:
    from app.ai.messages import text_projection

    run = json.loads((root / "D-upload.json").read_text(encoding="utf-8"))
    session = state["session_d"]
    history = session_history(http, session)
    attachments = [
        item for entry in history["entries"] for item in entry["message"].get("attachments", [])
    ]
    assert len(attachments) == 1, attachments
    attachment = attachments[0]
    storage_path = f"sessions/{session}/attachments/{attachment['attachment_id']}.md"
    reads = [call for call in support.named(run, "read") if call["is_error"] is False]
    assert any(call["arguments"].get("path") == storage_path for call in reads), [
        call["arguments"] for call in reads
    ]
    assert any("推、拉、腿、休" in (call["content"] or "") for call in reads), "未读取附件全文"

    proposal, prepare_item = prepare_success(run)
    payload = proposal["payload"]
    assert proposal["preparation_kind"] == "adjustment", proposal["preparation_kind"]
    kinds = [day["kind"] for day in payload["days"]]
    assert kinds[:4] == ["training", "training", "training", "rest"], kinds
    for day in payload["days"][:3]:
        assert day["exercises"] == [], day
    assert payload["days"][3]["exercises"] == [], payload["days"][3]

    assert support.current_plan(http)["id"] == state["saved_c"]["id"], "上传直接调整改动了当前计划"
    assert len(support.list_plans(http)) == 3, "上传直接调整新增了计划版本"
    display = display_for(session, proposal["proposal_id"])
    assert display == prepare_item["entry_id"], (display, prepare_item["entry_id"])
    persisted = entry_message(session, display)
    assert persisted.tool_name == "prepare_plan_adjustment" and persisted.is_error is False
    assert json.loads(text_projection(persisted.content)) == proposal

    state["proposal_d"] = proposal["proposal_id"]
    state["display_d"] = display
    support.dump(
        root,
        "D1.json",
        {
            "attachment": attachment,
            "storage_path": storage_path,
            "read_calls": [call["arguments"] for call in reads],
            "proposal": proposal,
        },
    )
    print("PASS D1: 附件经真实入口受理；Agent 按真实路径读取原文；四日循环与未知动作详情保留；当前计划不变；展示绑定一致")


def stage_D2_check(root: Path, state: dict, http) -> None:
    session = state["session_d"]
    saved = state["saved_d"]
    assert saved["proposal_id"] == state["proposal_d"]
    after = support.list_plans(http)
    assert len(after) == 4, after
    assert after[0]["id"] == saved["id"] and after[0]["is_current"] is True
    assert after[0]["content"]["days"][0]["exercises"] == []
    support.open_path("/plans")
    support.until("document.body.innerText.includes('已保存版本')")
    badges = support.js(
        "Array.from(document.querySelectorAll('*'))."
        "filter(el => el.children.length === 0 && el.textContent.trim() === '当前').length"
    )
    assert badges == 1, badges
    support.dump(root, "D2.json", {"saved": saved, "plans": after})
    print("PASS D2: 确认后仅保存最终调整版本，原上传安排未成为额外历史版本")


def business_error_codes(run: dict) -> list[tuple[str, str, str]]:
    found = []
    for item in support.tool_calls(run):
        if item["is_error"] is not True or not item["content"]:
            continue
        try:
            payload = json.loads(item["content"])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("code"):
            found.append((item["name"], payload["code"], item["content"]))
    return found


def profile_status(http) -> dict:
    return http.get("/api/profile").json()


def stage_E1_prepare(root: Path, state: dict, http) -> None:
    session = support.create_session(http, "调整验收-依据-画像")
    state["session_e1"] = session
    support.open_session(session)
    before = support.current_plan(http)
    plans = support.list_plans(http)
    profile = profile_status(http)
    run = support.send_and_wait(
        support.OBS,
        "请调整当前计划：把第 3 天（腿日）的 focus 改为「腿与臀」，其余保持不变；"
        "先读取画像状态与版本、当前计划，固定查询最近 10 条训练记录，完整展示等待确认，先不要保存。",
    )
    support.dump(root, "E1-prepare.json", run)
    proposal, _ = prepare_success(run)
    assert proposal["base_profile_version"] == profile["version"], proposal
    assert proposal["base_plan_id"] == before["id"], proposal
    assert support.current_plan(http) == before
    assert [r["id"] for r in support.list_plans(http)] == [r["id"] for r in plans]
    state["proposal_e1"] = proposal["proposal_id"]
    support.dump(root, "E1.json", {"prepare": proposal, "profile": profile})
    print(f"PASS E1-prepare: 以画像 v{profile['version']} 与当前计划准备待确认方案")


def stage_E1_profile(root: Path, state: dict, http) -> None:
    session = support.create_session(http, "调整验收-画像流程")
    state["session_e2"] = session
    support.open_session(session)
    profile_before = profile_status(http)
    run1 = support.send_and_wait(
        support.OBS,
        "请修改我的画像：把训练目标改为「提升力量与围度」，其余字段保持不变，"
        "先完整展示待确认内容，等待我确认，不要保存。",
    )
    support.dump(root, "E1-profile-prepare.json", run1)
    assert success_named(run1, "prepare_profile_update"), [
        call["name"] for call in support.tool_calls(run1)
    ]
    run2 = support.send_and_wait(support.OBS, "确认，保存画像。")
    support.dump(root, "E1-profile-save.json", run2)
    assert success_named(run2, "save_profile_update"), [
        call["name"] for call in support.tool_calls(run2)
    ]
    profile_after = profile_status(http)
    assert profile_after["version"] == profile_before["version"] + 1, profile_after
    assert profile_after["content"]["goal"] == "提升力量与围度", profile_after["content"]
    state["profile_version_after"] = profile_after["version"]
    support.dump(
        root, "E1.json", {"prepare": state.get("proposal_e1"), "profile": profile_after}
    )
    print(f"PASS E1-profile: 真实画像流程保存为新版本 v{profile_after['version']}")


def stage_E1_confirm(root: Path, state: dict, http) -> None:
    session = state["session_e1"]
    support.open_session(session)
    before = support.current_plan(http)
    plans = support.list_plans(http)
    profile = profile_status(http)
    run = support.send_and_wait(support.OBS, "确认，采纳并保存这份计划。")
    support.dump(root, "E1-confirm.json", run)
    codes = business_error_codes(run)
    reprepared = success_named(run, "prepare_plan_adjustment")
    saved = success_named(run, "save_plan")
    assert not saved, "依据变化后仍直接保存"
    conflict = next((entry for entry in codes if entry[1] == "profile_version_conflict"), None)
    if conflict is not None:
        outcome = {"kind": "conflict", "code": conflict[1], "text": conflict[2]}
    else:
        assert reprepared, (codes, [call["name"] for call in support.tool_calls(run)])
        proposal = json.loads(reprepared[0]["content"])
        assert proposal["proposal_id"] != state["proposal_e1"], "未产生新方案"
        assert plan_status(session, state["proposal_e1"])["status"] == "invalidated"
        assert proposal["base_profile_version"] == profile["version"], proposal
        outcome = {"kind": "reprepared", "proposal_id": proposal["proposal_id"]}
    assert support.current_plan(http) == before, "依据变化后覆盖了当前计划"
    assert [r["id"] for r in support.list_plans(http)] == [r["id"] for r in plans], "依据变化后新增了版本"
    assert profile_status(http)["version"] == profile["version"], "确认过程改动了画像"
    state["e1_outcome"] = outcome
    support.dump(root, "E1.json", {"detail": outcome, "codes": codes, "profile": profile})
    print(f"PASS E1-confirm: 画像依据变化后未覆盖当前业务数据；实际行为={outcome['kind']}")


def stage_E2_prepare(root: Path, state: dict, http) -> None:
    session = support.create_session(http, "调整验收-依据-计划")
    state["session_e3"] = session
    support.open_session(session)
    before = support.current_plan(http)
    plans = support.list_plans(http)
    profile = profile_status(http)
    run = support.send_and_wait(
        support.OBS,
        "请调整当前计划：把第 2 天（拉日）的 focus 改为「背与二头」，其余保持不变；"
        "先读取画像与当前计划，固定查询最近 10 条训练记录，完整展示等待确认，先不要保存。",
    )
    support.dump(root, "E2-prepare.json", run)
    proposal, _ = prepare_success(run)
    assert proposal["base_plan_id"] == before["id"], proposal
    assert proposal["base_profile_version"] == profile["version"], proposal
    state["proposal_e2"] = proposal["proposal_id"]
    support.dump(root, "E2.json", {"prepare": proposal, "plan": before})
    print("PASS E2-prepare: 以当前计划与画像版本准备待确认方案")


def stage_E2_plan(root: Path, state: dict, http) -> None:
    session = support.create_session(http, "调整验收-新当前计划")
    state["session_e4"] = session
    support.open_session(session)
    plans = support.list_plans(http)
    run1 = support.send_and_wait(
        support.OBS,
        "请调整当前计划：把第 1 天（推日）的 focus 改为「上肢推」，其余保持不变；"
        "完整展示等待确认，先不要保存。",
    )
    support.dump(root, "E2-plan-prepare.json", run1)
    proposal, _ = prepare_success(run1)
    run2 = support.send_and_wait(support.OBS, "确认，保存这份计划。")
    support.dump(root, "E2-plan-save.json", run2)
    saves = success_named(run2, "save_plan")
    assert len(saves) == 1, [call["name"] for call in support.tool_calls(run2)]
    saved = json.loads(saves[0]["content"])
    assert saved["proposal_id"] == proposal["proposal_id"]
    after = support.list_plans(http)
    assert len(after) == len(plans) + 1 and after[0]["id"] == saved["id"], after
    state["saved_e2_plan"] = saved
    support.dump(root, "E2.json", {"prepare": proposal, "saved": saved})
    print("PASS E2-plan: 另一会话真实保存新的当前计划")


def stage_E2_confirm(root: Path, state: dict, http) -> None:
    session = state["session_e3"]
    support.open_session(session)
    before = support.current_plan(http)
    plans = support.list_plans(http)
    run = support.send_and_wait(support.OBS, "确认，采纳并保存这份计划。")
    support.dump(root, "E2-confirm.json", run)
    codes = business_error_codes(run)
    reprepared = success_named(run, "prepare_plan_adjustment")
    saved = success_named(run, "save_plan")
    assert not saved, "依据变化后仍直接保存"
    conflict = next((entry for entry in codes if entry[1] == "plan_version_conflict"), None)
    if conflict is not None:
        outcome = {"kind": "conflict", "code": conflict[1], "text": conflict[2]}
    else:
        assert reprepared, (codes, [call["name"] for call in support.tool_calls(run)])
        proposal = json.loads(reprepared[0]["content"])
        assert proposal["proposal_id"] != state["proposal_e2"], "未产生新方案"
        assert plan_status(session, state["proposal_e2"])["status"] == "invalidated"
        assert proposal["base_plan_id"] == before["id"], proposal
        outcome = {"kind": "reprepared", "proposal_id": proposal["proposal_id"]}
    assert support.current_plan(http) == before, "依据变化后覆盖了当前计划"
    assert [r["id"] for r in support.list_plans(http)] == [r["id"] for r in plans], "依据变化后新增了版本"
    state["e2_outcome"] = outcome
    support.dump(root, "E2.json", {"detail": outcome, "codes": codes})
    print(f"PASS E2-confirm: 当前计划依据变化后未覆盖业务数据；实际行为={outcome['kind']}")


def stage_F_model(root: Path, state: dict, http) -> None:
    session = state["session_a"]
    saved = state["saved_c"]
    support.open_session(session)
    current = support.current_plan(http)
    assert current["id"] != saved["id"], "当前计划未变化，无法验证原结果保持"
    run = support.send_and_wait(
        support.OBS, "我之前保存的那份计划现在是什么状态？请核对保存状态并说明。"
    )
    support.dump(root, "F-model.json", run)
    statuses = success_named(run, "get_plan_save_status")
    assert statuses, [call["name"] for call in support.tool_calls(run)]
    status = json.loads(statuses[0]["content"])
    assert status["status"] == "saved" and status["result"] == saved, status
    transcript = support.js(support.TRACE)
    assert "saved" in transcript or "已保存" in transcript or "保存" in transcript, transcript[-800:]
    after = plan_status(session, saved["proposal_id"])
    assert after["status"] == "saved" and after["result"] == saved, after
    assert support.current_plan(http) == current, "状态查询改动了当前计划"
    support.dump(
        root,
        "F-model.json",
        {
            "status": status,
            "transcript": transcript,
            "current_plan_id": current["id"],
            "saved_c_id": saved["id"],
        },
    )
    print("PASS F-model: Agent 依据真实状态查询说明已保存；当前计划变化后原保存结果保持原值")


def stage_G_errors(root: Path, state: dict, http) -> None:
    from app.ai.messages import text_projection

    session = support.create_session(http, "调整验收-错误展示")
    state["session_g"] = session
    support.open_session(session)
    run = support.send_and_wait(
        support.OBS,
        "请调用 list_workouts 工具：date_from 省略、date_to=2026-10-09、page=1、page_size=0；"
        "不要修改参数，把工具返回的内容原样告诉我。",
    )
    support.dump(root, "G-harness.json", run)
    failed = [
        call
        for call in support.tool_calls(run)
        if call["is_error"] is True and call["content"]
    ]
    harness = [
        call
        for call in failed
        if not (call["content"].lstrip().startswith("{") and '"code"' in call["content"])
    ]
    assert harness, [call["content"][:120] for call in failed]
    error_item = harness[0]
    assert error_item["entry_id"], error_item
    persisted = entry_message(session, error_item["entry_id"])
    assert persisted.is_error is True, persisted
    error_text = error_item["content"]
    snippet = error_text.strip()[:24]
    transcript = support.js(support.TRACE)
    assert snippet in transcript, (snippet, transcript[-1000:])
    support.browser("reload")
    support.until("!!document.querySelector(\"textarea[aria-label='消息']\")", timeout=120)
    support.until(f"document.body.innerText.includes({json.dumps(snippet)})", timeout=120)
    support.screenshot(root / "G-harness.png")

    business = None
    for key, session_key in (("e1_outcome", "session_e1"), ("e2_outcome", "session_e3")):
        outcome = state.get(key, {})
        if outcome.get("kind") == "conflict":
            business = {"session": state[session_key], "outcome": outcome}
            break
    business_detail = None
    if business is not None:
        support.open_session(business["session"])
        support.until("document.body.innerText.includes('计划')", timeout=120)
        business_text = business["outcome"]["text"][:24]
        support.until(f"document.body.innerText.includes({json.dumps(business_text)})", timeout=120)
        business_transcript = support.js(support.TRACE)
        assert business_text in business_transcript
        support.browser("reload")
        support.until("!!document.querySelector(\"textarea[aria-label='消息']\")", timeout=120)
        support.until(
            f"document.body.innerText.includes({json.dumps(business_text)})", timeout=120
        )
        business_detail = {"marker": business_text, "session": business["session"]}
        support.screenshot(root / "G-business-error.png")
    support.dump(
        root,
        "G-errors.json",
        {
            "harness_error": error_text,
            "harness_transcript": transcript,
            "business_error": business_detail,
            "e1_outcome": state.get("e1_outcome"),
            "e2_outcome": state.get("e2_outcome"),
        },
    )
    print(
        "PASS G-errors: Harness 文本错误在浏览器展示且刷新后仍展示；"
        f"业务错误路径={'已覆盖' if business_detail else '本次未出现'}"
    )


def dispatch(stage: str, root: Path, state: dict, http) -> None:
    stages = {
        "setup": stage_setup,
        "A": stage_A,
        "B": stage_B,
        "C": stage_C,
        "D1": stage_D1,
        "D2": stage_D2,
        "D1-check": stage_D1_check,
        "D2-check": stage_D2_check,
        "D-check": stage_D_check,
        "E1-prepare": stage_E1_prepare,
        "E1-profile": stage_E1_profile,
        "E1-confirm": stage_E1_confirm,
        "E2-prepare": stage_E2_prepare,
        "E2-plan": stage_E2_plan,
        "E2-confirm": stage_E2_confirm,
        "F": stage_F,
        "F-model": stage_F_model,
        "G": stage_G,
        "G-errors": stage_G_errors,
    }
    if stage not in stages:
        raise SystemExit(f"未实现的阶段：{stage}")
    stages[stage](root, state, http)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--run-id", default=RUN_ID)
    parser.add_argument("--budget", type=int, default=48)
    arguments = parser.parse_args()
    support.MODEL_LIMIT = arguments.budget

    root = evidence_root(arguments.run_id)
    support.BROWSER_PROFILE = root / "browser-profile"
    support.BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)
    support.BROWSER_IO = root / "browser-io"
    support.BROWSER_IO.mkdir(parents=True, exist_ok=True)
    database = root / "isolated.db"
    database_module.default_database_path = lambda: database

    # 每次阶段以独立进程运行：状态跨阶段经 state.json 传递，模型用量累计进入预算。
    path = state_path(root)
    state = support.read_state(path) if path.exists() else {}
    support.install_observer(state)

    if not port_free(support.BACKEND_PORT):
        raise SystemExit(f"端口 {support.BACKEND_PORT} 被占用，请先停止既有后端")
    if not port_free(support.FRONTEND_PORT):
        raise SystemExit(f"端口 {support.FRONTEND_PORT} 被占用，无法启动隔离前端")

    failure: BaseException | None = None
    with support.Server(support.interface.app):
        frontend = support.start_frontend(root / "frontend.log")
        try:
            with support.http_client() as http:
                dispatch(arguments.stage, root, state, http)
        except BaseException as error:
            failure = error
        finally:
            support.stop_frontend(frontend)
            state.setdefault("requests", []).extend(support.OBS.requests)
            state.setdefault("usage", []).extend(support.OBS.usage)
            state["credential_hit"] = state.get("credential_hit", False) or support.OBS.credential_hit
            state.setdefault("stages", []).append(arguments.stage)
            support.write_state(path, state)

    if support.OBS.credential_hit:
        raise SystemExit("凭据保护命中，立即停止验收")
    if failure is not None:
        raise failure
    print(json.dumps({"stage": arguments.stage, "requests": len(state.get("requests", [])),
                      "budget": support.MODEL_LIMIT}, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
