import asyncio
import json
from uuid import uuid4

from app.ai.messages import SystemMessage, TextContent, ToolResultMessage
from app.application.business.service import BusinessService, business_date
from app.application.session.service import SessionService
from app.domain.business.models import (
    BusinessContext,
    ProfileProposalArguments,
    ProfileSaveArguments,
)
from app.domain.session.models import SendCommand, SendRequest, SessionMessageEntry
from app.interfaces.http import app
from app.model_config import load_model_config
from test.check_profile_confirmation import assistant_prepare_call, payload
from test.regression_support import (
    Server,
    client,
    patch_default_database,
    temporary_root,
)

EVIDENCE = temporary_root("business-http")
SYSTEM = SystemMessage(role="system", content="系统提示词", tools_added=[], timestamp=100)
CALL_ID = "call-http-prepare"


def prepare_arguments(base, data: dict) -> dict:
    return {"profile_id": 1, "base_profile_version": base, "payload": data}


async def build_pending(
    service: SessionService, business: BusinessService, data: dict, title: str
) -> dict:
    # 真实服务链路：助手工具调用节点 -> 待确认快照 -> 结果节点持久化 -> 展示绑定 -> 用户确认节点。
    profile = await business.get_profile()
    session_id = str(uuid4())
    created, _ = await service.create_session_result(session_id, title)
    assert created
    outcome = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="请整理我的画像"),
        ),
        system_message=SYSTEM,
    )
    run = outcome.run
    request_id = run.request_entry_id
    arguments = prepare_arguments(profile.version, data)
    source_id = str(uuid4())
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=source_id,
            parent_id=request_id,
            run_id=run.id,
            type="message",
            messages=[assistant_prepare_call(CALL_ID, arguments, 200)],
            created_at=200,
        )
    )
    context = BusinessContext(
        timezone="Asia/Shanghai",
        business_date=business_date(200),
        session_id=session_id,
        run_id=run.id,
        request_entry_id=request_id,
        source_entry_id=source_id,
    )
    proposal = await business.prepare_profile_update(
        context, ProfileProposalArguments.model_validate(arguments)
    )
    display_id = str(uuid4())
    await service.append_entry(
        SessionMessageEntry(
            session_id=session_id,
            id=display_id,
            parent_id=source_id,
            run_id=run.id,
            type="message",
            messages=[
                ToolResultMessage(
                    role="toolResult",
                    tool_call_id=CALL_ID,
                    tool_name="prepare_profile_update",
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(proposal.model_dump(), ensure_ascii=False),
                        )
                    ],
                    is_error=False,
                    timestamp=201,
                )
            ],
            created_at=201,
        )
    )
    await business.bind_display_entry(proposal.proposal_id, display_id)
    await service.finish_run(session_id, run.id, "completed")
    confirmation = await service.accept_send(
        SendCommand(
            operation_id=str(uuid4()),
            session_id=session_id,
            request=SendRequest(text="确认保存这份画像"),
        ),
        system_message=SYSTEM,
    )
    await service.finish_run(session_id, confirmation.run.id, "completed")
    return {
        "session": session_id,
        "request": request_id,
        "source": source_id,
        "display": display_id,
        "confirmation": confirmation.run.request_entry_id,
        "proposal": proposal.proposal_id,
        "context": context,
        "payload": data,
    }


async def save(business: BusinessService, ids: dict):
    return await business.save_profile_update(
        ids["context"],
        ProfileSaveArguments(
            proposal_id=ids["proposal"],
            display_entry_id=ids["display"],
            confirmation_entry_id=ids["confirmation"],
        ),
    )


def check() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    patch_default_database("business-http")
    evidence: dict = {}
    with Server(app) as server:
        loop = app.state.loop
        service: SessionService = app.state.session_service
        business: BusinessService = app.state.business

        def run(coro):
            return asyncio.run_coroutine_threadsafe(coro, loop).result()

        ids = run(build_pending(service, business, payload(), "画像 HTTP 会话"))

        with client(server.base_url) as http:
            # 未建档。
            empty = http.get("/api/profile")
            assert empty.status_code == 200
            assert empty.json() == {"version": None, "content": None}
            assert empty.headers["cache-control"] == "no-store"

            # 旧卡片接口不再注册。
            legacy_get = http.get(f"/api/confirmations/{uuid4()}")
            legacy_post = http.post(f"/api/confirmations/{uuid4()}/commit")
            assert legacy_get.status_code == 404, legacy_get.text
            assert legacy_post.status_code == 404, legacy_post.text
            assert legacy_get.json() == {"detail": "Not Found"}, legacy_get.json()
            assert legacy_post.json() == {"detail": "Not Found"}, legacy_post.json()

            # 历史接口只返回会话、消息、运行与输入四类数据。
            history = http.get(f"/api/sessions/{ids['session']}/history")
            assert history.status_code == 200, history.text
            body = history.json()
            assert set(body) == {"session", "entries", "runs", "steering"}, set(body)
            assert "confirmation" not in json.dumps(body, ensure_ascii=False).lower()
            roles = [item["message"]["role"] for item in body["entries"]]
            assert roles == ["system", "user", "assistant", "toolResult", "user"], roles
            display = next(
                item for item in body["entries"] if item["entry_id"] == ids["display"]
            )
            assert display["message"]["tool_name"] == "prepare_profile_update"
            assert display["message"]["is_error"] is False
            assert json.loads(display["message"]["content"])["payload"] == payload()

            # 保存后画像接口返回新版本与完整内容。
            saved = run(save(business, ids))
            assert saved.version == 1 and saved.profile_id == 1
            profile = http.get("/api/profile")
            assert profile.status_code == 200
            assert profile.json() == {"version": 1, "content": payload()}

            # 缺失会话历史。
            missing = http.get(f"/api/sessions/{uuid4()}/history")
            assert missing.status_code == 404
            assert missing.json()["detail"]["code"] == "session_not_found"

            # 凭据保护：画像内容命中模型凭据时拒绝公开。
            secret = load_model_config().OPENAI_API_KEY
            leaky = run(
                build_pending(
                    service, business, payload(environment=f"环境 {secret}"), "凭据会话"
                )
            )
            leaked = run(save(business, leaky))
            assert leaked.version == 2
            blocked = http.get("/api/profile")
            assert blocked.status_code == 422, blocked.text
            assert blocked.json()["detail"]["code"] == "credential_detected"
            blocked_history = http.get(f"/api/sessions/{leaky['session']}/history")
            assert blocked_history.status_code == 422
            assert blocked_history.json()["detail"]["code"] == "credential_detected"

            clean = run(build_pending(service, business, payload(goal="减脂"), "更新会话"))
            assert run(save(business, clean)).version == 3
            restored = http.get("/api/profile")
            assert restored.status_code == 200
            assert restored.json() == {"version": 3, "content": payload(goal="减脂")}

            # Host / Origin 边界。
            foreign = http.get(
                "/api/profile",
                headers={"Host": "evil.example", "Origin": "http://evil.example"},
            )
            assert foreign.status_code == 403
            assert foreign.json()["detail"]["code"] == "host_forbidden"
            foreign_origin = http.get(
                "/api/profile", headers={"Origin": "http://evil.example"}
            )
            assert foreign_origin.status_code == 403
            assert foreign_origin.json()["detail"]["code"] == "origin_forbidden"

            evidence = {
                "profile": restored.json(),
                "history_keys": sorted(body),
                "legacy": {
                    "get": [legacy_get.status_code, legacy_get.json()],
                    "post": [legacy_post.status_code, legacy_post.json()],
                },
                "credential_blocked": blocked.json(),
            }
    (EVIDENCE / "business-http.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        "PASS: GET /api/profile 版本与内容、历史接口仅会话消息运行输入四类数据、"
        "旧 /api/confirmations 接口移除、凭据保护、Host/Origin 边界"
    )


if __name__ == "__main__":
    check()
