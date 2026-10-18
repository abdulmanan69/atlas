"""Phone numbers: calling providers, the numbers on them, and which agent answers each."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.v1.deps import DB, AgentsManage, AgentsRead, IntegrationsManage
from app.schemas.common import ERROR_RESPONSES
from app.schemas.telephony import (
    InboundAgentAssignment,
    NumberCreate,
    NumberOut,
    NumberSyncResult,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
    SetupInstructions,
    TelephonyProviderOption,
)
from app.services import telephony as service

router = APIRouter(prefix="/telephony", tags=["Phone numbers"], responses=ERROR_RESPONSES)


# ---------------------------------------------------------------- providers


@router.get(
    "/providers/catalog",
    response_model=list[TelephonyProviderOption],
    summary="Calling providers you can connect, with the credential fields each needs",
)
async def provider_catalog(_: AgentsRead) -> list[TelephonyProviderOption]:
    return service.catalog()


@router.get("/providers", response_model=list[ProviderOut], summary="Connected calling providers")
async def list_providers(ctx: AgentsRead, db: DB) -> list[ProviderOut]:
    return await service.list_providers(db, ctx)


@router.post(
    "/providers",
    response_model=ProviderOut,
    status_code=status.HTTP_201_CREATED,
    summary="Connect a calling provider (credentials are stored by the voice service)",
)
async def create_provider(data: ProviderCreate, ctx: IntegrationsManage, db: DB) -> ProviderOut:
    return await service.create_provider(db, ctx, data)


@router.get("/providers/{provider_id}", response_model=ProviderOut)
async def get_provider(provider_id: str, ctx: AgentsRead, db: DB) -> ProviderOut:
    return await service.get_provider(db, ctx, provider_id)


@router.patch(
    "/providers/{provider_id}",
    response_model=ProviderOut,
    summary="Rename a provider or replace some of its credentials",
)
async def update_provider(
    provider_id: str, data: ProviderUpdate, ctx: IntegrationsManage, db: DB
) -> ProviderOut:
    return await service.update_provider(db, ctx, provider_id, data)


@router.delete(
    "/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect a provider (refused while calls still depend on it)",
)
async def delete_provider(provider_id: str, ctx: IntegrationsManage, db: DB) -> None:
    await service.delete_provider(db, ctx, provider_id)


@router.post(
    "/providers/{provider_id}/set-default-outbound",
    response_model=ProviderOut,
    summary="Use this provider for outbound calls",
)
async def set_default_outbound(provider_id: str, ctx: IntegrationsManage, db: DB) -> ProviderOut:
    return await service.make_default_outbound(db, ctx, provider_id)


@router.get(
    "/setup-instructions/{provider}",
    response_model=SetupInstructions,
    summary="What to paste into the provider console so calls reach your agents",
)
async def setup_instructions(provider: str, _: AgentsRead) -> SetupInstructions:
    return service.setup_instructions(provider)


# ---------------------------------------------------------------- numbers


@router.get(
    "/numbers",
    response_model=list[NumberOut],
    summary="Phone numbers across all providers, with the agent answering each",
)
async def list_numbers(
    ctx: AgentsRead,
    db: DB,
    provider_id: Annotated[str | None, Query(max_length=32)] = None,
) -> list[NumberOut]:
    return await service.list_numbers(db, ctx, provider_id=provider_id)


@router.get("/providers/{provider_id}/numbers", response_model=list[NumberOut])
async def list_provider_numbers(provider_id: str, ctx: AgentsRead, db: DB) -> list[NumberOut]:
    return await service.list_numbers(db, ctx, provider_id=provider_id)


@router.post(
    "/providers/{provider_id}/numbers",
    response_model=NumberOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a number the provider account owns",
)
async def add_number(
    provider_id: str, data: NumberCreate, ctx: IntegrationsManage, db: DB
) -> NumberOut:
    return await service.add_number(db, ctx, provider_id, data)


@router.post(
    "/providers/{provider_id}/numbers/sync",
    response_model=NumberSyncResult,
    summary="Import the numbers the provider account holds",
)
async def sync_numbers(provider_id: str, ctx: IntegrationsManage, db: DB) -> NumberSyncResult:
    return await service.sync_numbers(db, ctx, provider_id)


@router.delete("/numbers/{number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_number(number_id: str, ctx: IntegrationsManage, db: DB) -> None:
    await service.remove_number(db, ctx, number_id)


@router.put(
    "/numbers/{number_id}/inbound-agent",
    response_model=NumberOut,
    summary="Choose the agent that answers inbound calls on this number",
)
async def assign_inbound_agent(
    number_id: str, data: InboundAgentAssignment, ctx: AgentsManage, db: DB
) -> NumberOut:
    return await service.assign_inbound_agent(db, ctx, number_id, data)
