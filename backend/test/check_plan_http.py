import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from dotenv import set_key

from app.infrastructure.persistence.sqlite import database as database_module
from app.interfaces.http import app
from test.check_plan_core import prepare, profile
from test.check_profile_confirmation import Fixture
from test.regression_support import Server, client, temporary_root

ROOT = temporary_root("plan-http") / uuid4().hex
ROOT.mkdir()


async def seed(path):
    f = await Fixture(path).seeded()
    try:
        session = await f.session("计划HTTP查询")
        await profile(f, session)
        context, arguments = await prepare(f, session)
        first = await f.business.save_plan(context, arguments)
        context2, arguments2 = await prepare(f, session, base_plan_id=first.id)
        second = await f.business.save_plan(context2, arguments2)
        interrupted, pending = await prepare(f, session, base_plan_id=second.id)
        async with f.repository.transaction():
            await f.business._begin_plan_save(interrupted, pending)
        assert (await f.repository.get_plan_snapshot(pending.proposal_id)).status == "processing"
        return first, second, interrupted, pending
    finally:
        await f.close()


def call(coro):
    return asyncio.run_coroutine_threadsafe(coro, app.state.loop).result(timeout=30)


def query(http, path, status=200):
    response = http.get(path)
    assert response.status_code == status, (path, response.status_code)
    assert response.headers["cache-control"] == "no-store"
    return response.json()


@contextmanager
def credential_test_token():
    from app.model_config import load_model_config

    source = Path(__file__).resolve().parents[1] / ".env"
    original = load_model_config().OPENAI_API_KEY
    token = "plan-test-" + uuid4().hex
    try:
        set_key(source, "OPENAI_API_KEY", token)
        if load_model_config().OPENAI_API_KEY != token:
            raise RuntimeError("隔离凭据测试配置未生效")
        yield token
    finally:
        set_key(source, "OPENAI_API_KEY", original)
        if load_model_config().OPENAI_API_KEY != original:
            raise RuntimeError("隔离凭据测试配置恢复失败")


def check():
    empty = ROOT / "empty.db"
    database_module.default_database_path = lambda: empty
    with Server(app) as server, client(server.base_url) as http:
        assert query(http, "/api/plans/current") == {"id": None, "content": None}
        assert query(http, "/api/plans") == []
        assert query(http, "/api/plans/" + str(uuid4()), 404)["detail"]["code"] == "plan_not_found"
        for path in ("/api/plans", "/api/plans/current", "/api/plans/" + str(uuid4())):
            for suffix in ("?unknown=1", "?page=1", "?page=1&page=2"):
                assert query(http, path + suffix, 422)["detail"]["code"] == "invalid_request"
            assert http.get(path, headers={"Host": "invalid.example"}).status_code == 403
            assert http.get(path, headers={"Origin": "http://invalid.example"}).status_code == 403
        for value in ("invalid", uuid4().hex, "{" + str(uuid4()) + "}"):
            query(http, "/api/plans/" + value, 422)
    path = ROOT / "saved.db"
    first, second, context, pending = asyncio.run(seed(path))
    database_module.default_database_path = lambda: path
    with Server(app) as server, client(server.base_url) as http:
        assert query(http, "/api/plans/current") == {"id": second.id, "content": second.content.model_dump()}
        records = query(http, "/api/plans")
        assert len(records) == 2 and records[0]["id"] == second.id
        assert sum(record["is_current"] for record in records) == 1
        # HTTP 查询与业务服务读取保持同一形状与顺序。
        assert records == [item.model_dump() for item in call(app.state.business.list_plans())]
        assert query(http, "/api/plans/current") == call(app.state.business.get_current_plan()).model_dump()
        assert query(http, "/api/plans/" + first.id) == call(app.state.business.get_plan(first.id)).model_dump()
        missing = query(http, "/api/plans/" + str(uuid4()), 404)
        assert set(missing["detail"]) == {"code", "message"} and missing["detail"]["code"] == "plan_not_found"
        assert query(http, "/api/plans/" + first.id)["is_current"] is False
        assert query(http, "/api/plans/" + second.id)["is_current"] is True
        status = call(app.state.business.get_plan_save_status(context, pending.proposal_id))
        assert status.status == "pending" and status.result is None
        saved = call(app.state.business.save_plan(context, pending))
        assert call(app.state.business.save_plan(context, pending)) == saved
        assert len(query(http, "/api/plans")) == 3
        assert query(http, "/api/plans/current")["id"] == saved.id
        # 专用无生产权限token通过真实loader及HTTP过滤链路核实。
        from app.domain.business.models import PlanRecord

        with credential_test_token() as token:
            content = saved.content.model_copy(update={"notes": token})
            sensitive = PlanRecord(id=str(uuid4()), is_current=True, created_at=saved.created_at + 1, content=content)

            async def insert():
                await app.state.business._repository.insert_plan(sensitive)

            call(insert())
            for endpoint in ("/api/plans", "/api/plans/current", "/api/plans/" + sensitive.id):
                body = query(http, endpoint, 422)
                assert body["detail"]["code"] == "credential_detected"
                assert token not in json.dumps(body)

            async def remove_sensitive():
                repository = app.state.business._repository
                async with repository.transaction():
                    await repository._write("DELETE FROM plans WHERE id = ?", (sensitive.id,))
                    await repository._write("UPDATE plans SET is_current = 1 WHERE id = ?", (saved.id,))

            call(remove_sensitive())
        assert query(http, "/api/plans/current")["id"] == saved.id
    with Server(app) as server, client(server.base_url) as http:
        assert query(http, "/api/plans/current")["id"] == saved.id
        assert len(query(http, "/api/plans")) == 3
    (ROOT / "evidence.json").write_text(json.dumps({"database": str(path), "first": first.id,
        "second": second.id, "recovered": saved.id, "checks": ["empty", "shape", "query rejection",
        "UUID", "404", "boundary", "no-store", "credential rejection", "lifespan recovery",
        "fixed idempotency", "restart"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS: plan real HTTP/SQLite queries, boundary, no-store, credentials, lifespan recovery, idempotency, restart")
    print("Evidence:", ROOT)


if __name__ == "__main__":
    check()
