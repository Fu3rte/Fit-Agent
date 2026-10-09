import asyncio
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent.tool import run_tool_batch, run_tool_call
from app.agent.tools.business import bind_business_tools, business_tool_declarations
from app.agent.tools.dates import bind_date_tools, date_tool_declarations
from app.ai.messages import ToolCall
from app.domain.business.models import BusinessContext
from app.domain.session.models import SendCommand, SendRequest
from test.check_profile_confirmation import DECLARED, SYSTEM, Fixture, new_id
from test.check_workout_service_http import CONTENT, assistant, prepare
from test.regression_support import run_tool, temporary_root, text

ROOT = temporary_root("dates") / uuid4().hex
ROOT.mkdir()
TOOL = "calculate_date"
BACKEND = Path(__file__).resolve().parents[1]

# 跨月、跨年、闰日、零偏移与正偏移的期望值按日历事实逐条写死，作为独立于实现的判据。
DATE_CASES = [
    ("2026-01-01", 0, "2026-01-01"),
    ("2026-01-01", -1, "2025-12-31"),
    ("2026-01-01", -6, "2025-12-26"),
    ("2026-01-01", -366, "2024-12-31"),
    ("2026-01-31", 1, "2026-02-01"),
    ("2026-02-28", 1, "2026-03-01"),
    ("2026-03-01", -1, "2026-02-28"),
    ("2026-03-01", -3, "2026-02-26"),
    ("2024-02-29", -1, "2024-02-28"),
    ("2024-02-29", -4, "2024-02-25"),
    ("2024-02-29", 1, "2024-03-01"),
    ("2024-03-01", -1, "2024-02-29"),
    ("2025-12-31", 1, "2026-01-01"),
    ("2025-12-31", -6, "2025-12-25"),
    ("2026-05-31", -1, "2026-05-30"),
    ("2026-06-01", -1, "2026-05-31"),
]

INVALID_ARGUMENTS = [
    ({"days_offset": "-1"}, "days_offset"),
    ({"days_offset": -1.0}, "days_offset"),
    ({"days_offset": -1.5}, "days_offset"),
    ({"days_offset": True}, "days_offset"),
    ({"days_offset": None}, "days_offset"),
    ({"days_offset": [3]}, "days_offset"),
    ({}, "days_offset"),
    ({"days_offset": -1, "business_date": "2026-01-01"}, "business_date"),
    ({"days_offset": -1, "timezone": "Asia/Shanghai"}, "timezone"),
    ({"days_offset": -1, "session_id": str(uuid4())}, "session_id"),
    ({"days_offset": -1, "run_id": str(uuid4())}, "run_id"),
    ({"days_offset": -1, "days": 1}, "days"),
]


def context_for(business_date: str) -> BusinessContext:
    return BusinessContext(
        timezone="Asia/Shanghai",
        business_date=business_date,
        session_id=str(uuid4()),
        run_id=str(uuid4()),
        request_entry_id=str(uuid4()),
        source_entry_id=str(uuid4()),
    )


def tool(business_date: str):
    return bind_date_tools(context_for(business_date))[TOOL]


def invocation(name: str, arguments: dict) -> ToolCall:
    return ToolCall(type="toolCall", id=new_id(), name=name, arguments=arguments)


def body(message) -> dict:
    assert len(message.content) == 1 and message.content[0].type == "text"
    return json.loads(message.content[0].text)


def checked(message) -> str:
    assert not message.is_error, text(message)
    return body(message)["date"]


def check_contract() -> dict:
    # 真实 harness 单工具调用：每个基准绑定各自的可信上下文，逐条核对日历事实。
    dates = []
    for base, offset, expected in DATE_CASES:
        message = run_tool(tool(base), {"days_offset": offset})
        assert checked(message) == expected, (base, offset)
        dates.append({"business_date": base, "days_offset": offset, "date": expected})

    rejected = []
    for arguments, marker in INVALID_ARGUMENTS:
        message = run_tool(tool("2026-01-01"), arguments)
        assert message.is_error is True, arguments
        assert marker in text(message), (arguments, text(message))
        with pytest.raises(ValidationError):
            tool("2026-01-01").arguments.model_validate(arguments)
        rejected.append({"arguments": arguments, "marker": marker})

    overflow = run_tool(tool("2026-01-01"), {"days_offset": 10**9})
    assert overflow.is_error and "工具执行失败" in text(overflow), text(overflow)

    cancelled = Event()
    cancelled.set()
    aborted = run_tool(tool("2026-01-01"), {"days_offset": -1}, signal=cancelled)
    assert aborted.is_error and "Operation aborted" in text(aborted), text(aborted)
    return {
        "dates": dates,
        "rejected": rejected,
        "overflow": text(overflow),
        "cancelled": text(aborted),
    }


def check_binding_and_declaration() -> dict:
    instance = tool("2026-01-01")
    assert instance.execution_mode == "parallel" and instance.max_output_chars is None
    schema = instance.definition().parameters
    assert set(schema["properties"]) == {"days_offset"}
    assert schema["required"] == ["days_offset"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["days_offset"]["type"] == "integer"
    assert date_tool_declarations()[0].name == TOOL
    assert {item.name for item in business_tool_declarations()} >= {TOOL}

    # 不同绑定各用各的基准日期；源上下文后续变化不影响已绑定实例，绑定快照不可改写。
    first, second = context_for("2026-01-01"), context_for("2024-03-01")
    bound = [bind_date_tools(first)[TOOL], bind_date_tools(second)[TOOL]]
    results = [checked(run_tool(item, {"days_offset": -1})) for item in bound]
    assert results == ["2025-12-31", "2024-02-29"], results
    first.business_date = "2030-01-01"
    assert checked(run_tool(bound[0], {"days_offset": -1})) == "2025-12-31"
    with pytest.raises(ValidationError):
        bound[0].trusted_context.business_date = "2031-01-01"

    # 同批并行调用共用一个固定基准；声明与执行注册表不一致由 harness 拒绝。
    batch = asyncio.run(
        run_tool_batch(
            [invocation(TOOL, {"days_offset": offset}) for offset in (0, -1, -6, 1)],
            tools=bind_date_tools(context_for("2026-03-01")),
            declared={TOOL: instance.definition()},
        )
    )
    assert batch.failure is None, batch.failure
    parallel = [body(message)["date"] for message in batch.messages]
    assert parallel == ["2026-03-01", "2026-02-28", "2026-02-23", "2026-03-02"], parallel

    tampered = instance.definition().model_copy(update={"description": "改写声明"})
    mismatch = asyncio.run(
        run_tool_batch(
            [invocation(TOOL, {"days_offset": -1})],
            tools=bind_date_tools(context_for("2026-01-01")),
            declared={TOOL: tampered},
        )
    )
    assert isinstance(mismatch.failure, ValueError), mismatch.failure
    assert "声明与执行注册表不一致" in str(mismatch.failure)
    return {"bound_dates": results, "parallel_batch": parallel}


async def check_business_flow() -> dict:
    # 真实 SQLite、真实服务与真实 harness：日期只来自 calculate_date 的结果。
    fixture = await Fixture(ROOT / "flow.db").seeded()
    try:
        session = await fixture.session("相对日期录入")
        accepted = await fixture.service.accept_send(
            SendCommand(
                operation_id=new_id(),
                session_id=session,
                request=SendRequest(text="按相对日期查询并记录训练"),
            ),
            system_message=SYSTEM,
        )
        run = accepted.run
        calls = [invocation(TOOL, {"days_offset": offset}) for offset in (0, -1, -6, -7, 1)]
        source = await fixture.append(
            session, run.id, run.request_entry_id,
            assistant(TOOL, calls[0].id, calls[0].arguments).model_copy(
                update={"content": calls}
            ),
        )
        context = fixture.context(session, run.request_entry_id, source)
        tools = bind_business_tools(fixture.business, context, fixture.call, {}, {})
        assert tools[TOOL].trusted_context.model_dump() == context.model_dump()
        batch = await run_tool_batch(calls, tools=tools, declared=DECLARED)
        assert batch.failure is None, batch.failure
        # 结果节点先入分支，工具链完整后才允许后续真实确认与保存。
        parent = source
        for message in batch.messages:
            parent = await fixture.append(session, run.id, parent, message)
        await fixture.service.finish_run(session, run.id, "completed")
        today, yesterday, start, boundary, tomorrow = (
            body(message)["date"] for message in batch.messages
        )
        assert today == context.business_date

        # 近七日范围按工具端点读取：范围内含昨日记录，范围外为空。
        saved_context, save_arguments, saved_run = await prepare(fixture, session, yesterday)
        record = await fixture.business.save_workout(saved_context, save_arguments)
        await fixture.service.finish_run(session, saved_run, "completed")
        assert record.performed_on == yesterday and record.version == 1

        async def listing(date_from: str, date_to: str) -> dict:
            message = await run_tool_call(
                invocation("list_workouts", {"date_from": date_from, "date_to": date_to}),
                tools=bind_business_tools(fixture.business, context, fixture.call, {}, {}),
                declared=DECLARED,
            )
            assert not message.is_error, text(message)
            return body(message)

        inside = await listing(start, today)
        assert [item["performed_on"] for item in inside["items"]] == [yesterday]
        outside = await listing(boundary, boundary)
        assert outside["total"] == 0

        future = await run_tool_call(
            invocation(
                "prepare_workout",
                {
                    "performed_on": tomorrow,
                    "base_workout_id": None,
                    "base_workout_version": None,
                    "payload": CONTENT,
                },
            ),
            tools=bind_business_tools(fixture.business, context, fixture.call, {}, {}),
            declared=DECLARED,
        )
        assert future.is_error and body(future)["code"] == "invalid_business_payload"
        return {
            "today": today,
            "yesterday": yesterday,
            "range_from": start,
            "tomorrow": tomorrow,
            "saved": {"id": record.id, "performed_on": record.performed_on,
                      "version": record.version},
            "range_total": inside["total"],
            "future_rejected": body(future)["code"],
        }
    finally:
        await fixture.close()


def check_no_host_command_entry() -> None:
    # Agent 不保留通用宿主命令执行入口，工具构建也无需寻找 Bash 解释器。
    with pytest.raises(ModuleNotFoundError):
        __import__("app.agent.tools.bash")
    sources = list((BACKEND / "app").rglob("*.py"))
    assert sources
    for path in sources:
        code = path.read_text(encoding="utf-8")
        assert "subprocess" not in code and "shutil.which" not in code, path


def check() -> None:
    check_no_host_command_entry()
    evidence = {
        "contract": check_contract(),
        "binding": check_binding_and_declaration(),
        "business_flow": asyncio.run(check_business_flow()),
    }
    (ROOT / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("PASS: calculate_date 日历边界、严格参数、可信批次绑定与隔离、声明一致性、取消与溢出、"
          "真实保存与近七日范围、无宿主命令入口:", ROOT)


if __name__ == "__main__":
    check()
