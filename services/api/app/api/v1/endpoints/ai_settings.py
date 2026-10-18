"""AI providers: connections (API keys or self-hosted endpoints) and model selection."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.v1.deps import DB, AgentsManage, IntegrationsManage
from app.schemas.common import ERROR_RESPONSES
from app.schemas.voice import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionUpdate,
    ModelSelection,
    ModelSelectionUpdate,
    ProviderCatalog,
    ProviderCheck,
    VoiceOption,
)
from app.services import ai_settings as service

router = APIRouter(prefix="/ai", tags=["AI providers"], responses=ERROR_RESPONSES)


@router.get("/catalog", response_model=ProviderCatalog, summary="Supported providers per service")
async def get_catalog(ctx: AgentsManage, db: DB) -> ProviderCatalog:
    return await service.catalog(db, ctx)


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(ctx: AgentsManage, db: DB) -> list[ConnectionOut]:
    return await service.list_connections(db, ctx)


@router.post(
    "/connections",
    response_model=ConnectionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a provider connection (credentials are verified with the provider first)",
)
async def create_connection(
    data: ConnectionCreate, ctx: IntegrationsManage, db: DB
) -> ConnectionOut:
    return await service.create_connection(db, ctx, data)


@router.patch(
    "/connections/{connection_id}",
    response_model=ConnectionOut,
    summary="Rename, rotate credentials, or change the endpoint",
)
async def update_connection(
    connection_id: str, data: ConnectionUpdate, ctx: IntegrationsManage, db: DB
) -> ConnectionOut:
    return await service.update_connection(db, ctx, connection_id, data)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(connection_id: str, ctx: IntegrationsManage, db: DB) -> None:
    await service.delete_connection(db, ctx, connection_id)


@router.get(
    "/models", response_model=ModelSelection, summary="Models used for this organization's calls"
)
async def get_models(ctx: AgentsManage, db: DB) -> ModelSelection:
    return await service.get_selection(db, ctx)


@router.put("/models", response_model=ModelSelection)
async def save_models(
    data: ModelSelectionUpdate, ctx: IntegrationsManage, db: DB
) -> ModelSelection:
    return await service.save_selection(db, ctx, data)


@router.post(
    "/models/check",
    response_model=ProviderCheck,
    summary="Check that the selected providers respond",
)
async def check_models(ctx: AgentsManage, db: DB) -> ProviderCheck:
    return await service.check(db, ctx)


@router.get(
    "/voices", response_model=list[VoiceOption], summary="Voices offered by a TTS connection"
)
async def list_voices(
    ctx: AgentsManage,
    db: DB,
    provider: Annotated[str, Query(max_length=64)],
    connection_id: Annotated[str, Query(max_length=64)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    language: Annotated[str | None, Query(max_length=16)] = None,
    model: Annotated[str | None, Query(max_length=100)] = None,
) -> list[VoiceOption]:
    return await service.voices(
        db,
        ctx,
        provider=provider,
        connection_id=connection_id,
        query=q,
        language=language,
        model=model,
    )
