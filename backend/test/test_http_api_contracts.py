import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.interfaces.http import app
from app.interfaces.http_api.contracts import (
    EditPayload,
    ProviderWriteBody,
    RunRequest,
    SaveConfirmationWire,
    path_uuid,
)
from app.interfaces.http_api.routes import provider


def test_provider_router_entrypoint_contract():
    expected = {
        ("/api/provider", "GET"): provider.get_provider,
        ("/api/provider", "PUT"): provider.put_provider,
        ("/api/provider", "DELETE"): provider.delete_provider,
        ("/api/provider/test", "POST"): provider.test_provider,
    }
    routes = [
        route for route in provider.router.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/provider")
    ]
    assert len(routes) == len(expected)
    for route in routes:
        assert route.methods is not None and len(route.methods) == 1
        assert route.endpoint is expected[(route.path, next(iter(route.methods)))]
    async def check_boundary():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://forbidden.example"
        ) as client:
            for path, method in expected:
                response = await client.request(method, path)
                assert response.status_code == 403
                assert response.json()["detail"]["code"] == "host_forbidden"
                assert response.headers["cache-control"] == "no-store"

    asyncio.run(check_boundary())
    schema = app.openapi()
    assert schema["paths"]["/api/provider"]["put"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProviderWriteBody"
    }


def test_request_contracts_preserve_validation():
    identifier = str(uuid4())
    assert path_uuid(identifier.upper()) == identifier
    for value in (identifier.replace("-", ""), "{" + identifier + "}", 1):
        with pytest.raises(ValueError):
            path_uuid(value)
    with pytest.raises(ValidationError):
        RunRequest(session_id=identifier, operation_id=identifier, request=" ")
    edit = EditPayload(
        session_id=identifier, operation_id=identifier,
        target_entry_id=identifier, request="",
    )
    assert "attachments" not in edit.model_fields_set
    with pytest.raises(ValidationError):
        EditPayload(**edit.model_dump())
    assert set(SaveConfirmationWire.model_fields) == {
        "operation_id", "business_kind", "proposal_id", "display_entry_id"
    }
    body = ProviderWriteBody(
        api="openai-completions", base_url="http://localhost:11434/v1",
        model="model", api_key="key",
    )
    assert body.provider == ""
    with pytest.raises(ValidationError):
        ProviderWriteBody(**{**body.model_dump(), "api_key": " "})
