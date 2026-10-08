import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.business.models import PlanContent
from test.check_plan_tools import (
    batch,
    body,
    ensure_profile,
    exercise_update,
    invocation,
)
from test.check_profile_confirmation import Fixture
from test.regression_support import temporary_root

# 验证层级：真实 SQLite + 真实业务服务 + 真实目录 + 真实 Harness + 正常消息链，0 次模型网络请求。
# suggested_fields 标记助手补充或修改的字段，沿用 RFC 6901 JSON Pointer；校验位于 PlanContent 共享
# 边界，直接模型校验与 prepare_plan 工具输入一致。语法交给成熟库 jsonpointer 完整解析，
# 非法转义、非法起始与悬空 ~ 就地失败。
LEVEL = "suggested_fields 必须为 RFC 6901 JSON Pointer"
ROOT = temporary_root("plan-pointer-gap") / uuid4().hex
ROOT.mkdir()

LEGAL = [["/repeat"], ["/days", "/repeat", "/notes"], ["/days/0/exercises/0/sets"], ["/days/0/notes"],
         ["/notes"], []]
ILLEGAL = [["weights"], ["rest_seconds"], ["weights", "rest_seconds"], ["days/0/exercises/0/sets"],
           ["notes"], [" "]]
# 合法转义：~0 表示 ~，~1 表示 /；合法取值原值与数组顺序保持不变。
ESCAPE_LEGAL = [["/a~0b"], ["/a~1b"], ["/~0", "/~1"], ["/days~0name", "/days~1name"]]
# 非法转义：~ 只能后接 0 或 1；~2 与尾部悬空 ~ 均非法。
ESCAPE_ILLEGAL = [["/a~2b"], ["/tail~"], ["/~"], ["/~~0"], ["~0"], ["/a~"]]
# 成熟库 jsonpointer 3.2.0 的边界：根指针与空 member 名合法，非 / 起始非法。
BOUNDARY_LEGAL = [[""], ["/"], ["/-"], ["/01"], ["/a~01b"], ["/a~10b"]]
BOUNDARY_ILLEGAL = [["a/b"], ["0"], ["/a~2"], ["~"], [" /repeat"]]

TABLES = ("plan_snapshots", "plans", "plan_save_records")


async def plan_tables(database):
    # 完整比较三张计划业务表的全部行与列，而非仅比较行数。
    async def read(connection):
        dump = {}
        for name in TABLES:
            cursor = await connection.execute(f"SELECT * FROM {name} ORDER BY 1")
            dump[name] = [tuple(row) for row in await cursor.fetchall()]
            await cursor.close()
        return dump

    return await database.read(read)


def pointer_content(payload, fields):
    return {**payload, "suggested_fields": fields}


async def prepare_pointer(f, session, arguments, payload, fields):
    call = invocation("prepare_plan", {**arguments, "payload": pointer_content(payload, fields)})
    return call, await batch(f, session, [call])


async def check():
    f = await Fixture(ROOT / "pointer.db").seeded()
    try:
        session = await f.session("建议来源路径校验")
        await ensure_profile(f, session)
        real = f.catalog.all()[0]
        payload = exercise_update(exercise_id=real.id, name=real.name)
        arguments = {"base_profile_version": 1, "base_plan_id": None, "payload": payload}

        # 合法取值：真实工具接受，返回值原值与数组顺序不变，直接模型校验同时通过。
        accepted = []
        for fields in LEGAL + ESCAPE_LEGAL + BOUNDARY_LEGAL:
            _, got = await prepare_pointer(f, session, arguments, payload, fields)
            message = got.messages[0]
            assert not message.is_error, (fields, message.content[0].text)
            assert json.loads(message.content[0].text)["payload"]["suggested_fields"] == fields, fields
            assert PlanContent.model_validate(pointer_content(payload, fields)).suggested_fields == fields
            accepted.append(fields)
        assert len((await plan_tables(f.database))["plan_snapshots"]) == len(accepted)

        # 建立已保存版本与保存幂等记录，并保留一个待确认快照作为拒绝调用的对照基线。
        saved_call = invocation("prepare_plan", {**arguments, "payload": pointer_content(payload, ["/repeat"])})
        got = await batch(f, session, [saved_call])
        assert not got.messages[0].is_error, got.messages[0].content[0].text
        saved_proposal = body(got.messages[0])["proposal_id"]
        _, messages, _, _, _, _, _, _ = await batch(f, session, [],
            confirmation={"proposal_id": saved_proposal, "display_entry_id": got.displays[saved_call.id]})
        assert not messages[0].is_error, messages[0].content[0].text
        saved = body(messages[0])

        pending_call, got = await prepare_pointer(f, session,
            {"base_profile_version": 1, "base_plan_id": saved["id"], "payload": payload}, payload, ["/days", "/notes"])
        assert not got.messages[0].is_error, got.messages[0].content[0].text
        pending = body(got.messages[0])
        baseline_snapshot = await f.repository.get_plan_snapshot(pending["proposal_id"])
        assert baseline_snapshot.status == "pending"
        assert baseline_snapshot.display_entry_id == got.displays[pending_call.id]
        assert baseline_snapshot.confirmation_entry_id is None

        before_tables = await plan_tables(f.database)
        before_bindings = await f.business.list_plan_display_bindings(session)
        assert len(before_tables["plans"]) == 1 and len(before_tables["plan_save_records"]) == 1
        assert len(before_bindings) == 2 and baseline_snapshot.display_entry_id in before_bindings

        # 非法取值：当前基础计划 ID 有效，若校验缺失即会写入新快照并失效待确认快照，因此必须拒绝。
        illegal = ILLEGAL + ESCAPE_ILLEGAL + BOUNDARY_ILLEGAL
        calls = [invocation("prepare_plan", {"base_profile_version": 1, "base_plan_id": saved["id"],
            "payload": pointer_content(payload, fields)}) for fields in illegal]
        rejected = await batch(f, session, calls)
        assert rejected.registrations["plan"] == {} and rejected.displays == {}
        for fields, message in zip(illegal, rejected.messages):
            text = message.content[0].text
            assert message.is_error, (fields, text)
            assert text.startswith('工具 "prepare_plan" 参数校验失败'), (fields, text)
            assert "收到的参数" in text and "payload.suggested_fields" in text, (fields, text)
            with pytest.raises(ValidationError):
                PlanContent.model_validate(pointer_content(payload, fields))
        assert await plan_tables(f.database) == before_tables
        assert await f.business.list_plan_display_bindings(session) == before_bindings
        assert await f.repository.get_plan_snapshot(pending["proposal_id"]) == baseline_snapshot
        return {"level": LEVEL, "legal_pointers": LEGAL, "escaped_pointers": ESCAPE_LEGAL,
                "boundary_legal": BOUNDARY_LEGAL, "illegal_paths": ILLEGAL,
                "escaped_illegal": ESCAPE_ILLEGAL, "boundary_illegal": BOUNDARY_ILLEGAL,
                "accepted": len(accepted), "rejected": len(illegal),
                "saved_plan": saved["id"], "pending_snapshot": pending["proposal_id"],
                "preserved_tables": {name: len(rows) for name, rows in before_tables.items()}}
    finally:
        await f.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(check()), ensure_ascii=False, indent=2))
