import asyncio
from uuid import uuid4

import pytest

from app.agent.tool import CredentialDetectedError
from app.ai.messages import AssistantMessage, TextContent, Usage
from app.application.session.credentials import CredentialFilter
from app.interfaces.http_api.events import HttpRunEvents
from app.interfaces.http_api.stream import RunState


def test_http_run_output_uses_accepted_run_identity():
    run_id = uuid4()
    state = RunState(uuid4(), run_id=run_id)
    assert state.run_id == state.output.run_id == run_id
    state.publish("custom_message", {"entry_id": "node"})
    state.publish_terminal("done", {"status": "completed"})
    assert state.events.get_nowait().data["run_id"] == str(run_id)
    assert state.events.get_nowait().data["run_id"] == str(run_id)


def test_http_run_events_shared_parent_and_first_public_text():
    published = []
    parents = {}
    events = HttpRunEvents(
        lambda kind, data: published.append((kind, data)),
        CredentialFilter(("secret",)), parents,
        lambda: published.append(("first_text", None)),
    )
    message = AssistantMessage(
        role="assistant", content=[TextContent(type="text", text="OK")],
        api="test", provider="test", model="test",
        usage=Usage(input=1, output=1, cache_read=0, cache_write=0, total_tokens=2),
        stop_reason="stop", timestamp=0,
    )
    asyncio.run(events.emit({
        "type": "message_start", "message_id": "node", "message": message,
    }))
    assert published[0][1]["content"][0]["text"] == ""
    parents["node"] = "committed-parent"
    asyncio.run(events.emit({
        "type": "message_end", "message_id": "node", "message": message,
    }))
    assert [kind for kind, _ in published] == ["message_start", "first_text", "message_end"]
    assert published[-1][1]["parent_id"] == "committed-parent"
    message.content[0].text = "secret"
    with pytest.raises(CredentialDetectedError):
        asyncio.run(events.emit({
            "type": "message_end", "message_id": "node", "message": message,
        }))
    assert len(published) == 3
