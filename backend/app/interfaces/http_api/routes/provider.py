from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.agent.diagnostic import run_diagnostic
from app.interfaces.http_api.contracts import ProviderWriteBody
from app.interfaces.http_api.errors import NO_STORE
from app.model_config import (
    clear_provider,
    effective_provider,
    load_provider_status,
    save_provider,
)

router = APIRouter()


@router.get("/api/provider")
async def get_provider():
    return JSONResponse(load_provider_status().model_dump(), headers=NO_STORE)


@router.put("/api/provider")
async def put_provider(payload: ProviderWriteBody):
    status = save_provider(
        api=payload.api,
        base_url=payload.base_url,
        model=payload.model,
        provider=payload.provider,
        api_key=payload.api_key,
    )
    return JSONResponse(status.model_dump(), headers=NO_STORE)


@router.delete("/api/provider")
async def delete_provider():
    return JSONResponse(clear_provider().model_dump(), headers=NO_STORE)


@router.post("/api/provider/test")
async def test_provider(payload: ProviderWriteBody):
    result = await run_diagnostic(
        api=payload.api,
        base_url=payload.base_url,
        model=payload.model,
        provider=effective_provider(payload.api, payload.provider),
        api_key=payload.api_key,
    )
    return JSONResponse(result, headers=NO_STORE)
