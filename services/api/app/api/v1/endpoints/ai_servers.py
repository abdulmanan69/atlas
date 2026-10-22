"""AI servers: self-hosted model hosts reached through an AI gateway (ADR 0007).

An owner or admin registers a models host once (its gateway address and token); ATLAS
creates the model connections for it, keeps the token encrypted, checks the server every
two minutes and refuses to remove it while it is in use.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.v1.deps import DB, AgentsManage, IntegrationsManage
from app.schemas.ai_servers import (
    AiServerCreate,
    AiServerOut,
    AiServerRegistered,
    AiServerUpdate,
    AiServerUse,
)
from app.schemas.common import ERROR_RESPONSES, ErrorResponse
from app.schemas.voice import ModelSelection
from app.services import ai_servers as service

router = APIRouter(prefix="/ai/servers", tags=["AI servers"], responses=ERROR_RESPONSES)

ServerId = Annotated[uuid.UUID, Path(description="The AI server's id")]
SECRETS = (
    "The token, the live speech-to-text secret and the Deepgram key are never returned, "
    "logged or recorded in the audit log."
)


@router.get(
    "",
    response_model=list[AiServerOut],
    summary="AI servers of this organization, with the result of their last check",
)
async def list_servers(ctx: AgentsManage, db: DB) -> list[AiServerOut]:
    return await service.list_servers(db, ctx)


@router.post(
    "",
    response_model=AiServerRegistered,
    status_code=status.HTTP_201_CREATED,
    summary="Register an AI server and create its model connections",
    description=(
        "Checks `GET {url}/health` with the token, then creates a model connection for each "
        "service the server reports as up: '<name> · Language model' (`{url}/llm/v1`), "
        "'<name> · Whisper' (`{url}/whisper/v1`), '<name> · Pocket TTS' (`{url}/tts/v1`) and "
        "'<name> · Expressive voice' (`{url}/voice/v1`, a GPU server's voice that laughs and "
        "sighs), self-hosted connections that send the token as their API key. Live speech-to-text "
        "becomes '<name> · Live speech', a Deepgram connection with the base URL "
        "`{url}/live-stt/<secret>`, when the server offers it and both `live_stt_secret` and "
        "`deepgram_key` are given: the voice service checks every Deepgram connection's key "
        "with Deepgram's cloud when the connection is saved, whatever its address, so a real "
        "Deepgram key is needed once; no audio goes to Deepgram. `setup` says, per service, "
        "which connection was created or why none was. The address must use https, unless "
        "the operator allows private model addresses (ATLAS_ALLOW_PRIVATE_MODEL_URLS) and the "
        "server is on a private network. " + SECRETS
    ),
    responses={
        409: {"model": ErrorResponse, "description": "This address is already registered"},
        422: {
            "model": ErrorResponse,
            "description": (
                "The address is not allowed or not reachable, the server refused the token or "
                "the live speech-to-text secret, or the voice service refused a connection. "
                "`details.field` names the field to correct"
            ),
        },
    },
)
async def register_server(
    data: AiServerCreate, ctx: IntegrationsManage, db: DB
) -> AiServerRegistered:
    return await service.register(db, ctx, data)


@router.get("/{server_id}", response_model=AiServerOut, summary="One AI server")
async def get_server(server_id: ServerId, ctx: AgentsManage, db: DB) -> AiServerOut:
    return await service.get_server(db, ctx, server_id)


@router.post(
    "/{server_id}/check",
    response_model=AiServerOut,
    summary="Check the server now",
    description=(
        "Asks the server's /health and stores its status, round-trip time and services. "
        "A language model, Whisper or voice that was down when the server was "
        "registered gets its connection now if it is up. Servers are also checked every "
        "two minutes; a change of status is recorded in the audit log."
    ),
)
async def check_server(server_id: ServerId, ctx: IntegrationsManage, db: DB) -> AiServerOut:
    return await service.check_now(db, ctx, server_id)


@router.patch(
    "/{server_id}",
    response_model=AiServerOut,
    summary="Rename the server, or replace its token or live speech-to-text details",
    description=(
        "A new token is checked with the server first and refused (422) before anything "
        "changes; then every connection of this server uses it. A new live speech-to-text "
        "secret changes the live speech connection's address. A Deepgram key (with a secret "
        "given now or before) creates the live speech connection when the server offers it, "
        "or replaces the key of the existing one. A new name renames the connections too. "
        + SECRETS
    ),
)
async def update_server(
    server_id: ServerId, data: AiServerUpdate, ctx: IntegrationsManage, db: DB
) -> AiServerOut:
    return await service.update(db, ctx, server_id, data)


@router.post(
    "/{server_id}/use",
    response_model=ModelSelection,
    summary="Use this server for the organization's calls",
    description=(
        "Sets the organization's models to this server's connections: its language model "
        "(`llm_model`, default the first model it lists) at temperature 0.3, live "
        "speech-to-text when it has a live speech connection and `use_live_stt` is true "
        "(otherwise Whisper; a server without speech-to-text keeps the organization's current "
        "one), and its expressive voice when it has one, else Pocket TTS "
        "(`voice` chooses when it has both), with the organization's current voice when this "
        "server has it (otherwise the server's default voice). Knowledge search is kept. "
        "With `fallback_llm_connection_id`, another language model answers a turn when this "
        "server has given no output after `fallback_after_ms` (default 2500) or fails; without "
        "it, any fallback is removed. Agents with their own models keep them; an agent with its "
        "own language model does not use the fallback."
    ),
)
async def use_server(
    server_id: ServerId, data: AiServerUse, ctx: IntegrationsManage, db: DB
) -> ModelSelection:
    return await service.use(db, ctx, server_id, data)


@router.delete(
    "/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove the server and its connections",
    description=(
        "Refused (409) while the organization's models for calls, or an agent's own models, "
        "use one of its connections; the message says where (`details.in_use`). Voices "
        "cloned on the server stay on it."
    ),
    responses={409: {"model": ErrorResponse, "description": "The server is in use"}},
)
async def delete_server(server_id: ServerId, ctx: IntegrationsManage, db: DB) -> None:
    await service.delete(db, ctx, server_id)
