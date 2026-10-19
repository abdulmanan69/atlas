"""AI providers: Pocket TTS voices, including voices cloned from a consenting speaker."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Path, UploadFile, status

from app.api.v1.deps import DB, AgentsManage, IntegrationsManage
from app.schemas.common import ERROR_RESPONSES, ErrorResponse
from app.schemas.voice_cloning import CONSENT_ATTESTATION, PocketVoice, PocketVoiceList
from app.services import voice_cloning as service

router = APIRouter(prefix="/ai", tags=["AI providers"], responses=ERROR_RESPONSES)

ConnectionId = Annotated[str, Path(max_length=64)]
CLONING_RULES = (
    "Only clone the voice of a speaker who gave permission. Do not use a cloned voice to "
    "deceive anyone, for fraudulent calls, or to present generated speech as the real person. "
    "The sample is forwarded to the Pocket TTS server, which keeps only the computed voice; "
    "it is not stored by this platform. The consent attestation is recorded in the audit log."
)


@router.get(
    "/connections/{connection_id}/voices",
    response_model=PocketVoiceList,
    summary="Pocket TTS voices of a connection, with this organization's cloned voices",
    description=(
        "Built-in voices, and the voices this organization cloned on the connection's server. "
        "`cloning_available` is false (with the setup steps) when the server has no "
        "voice-cloning weights."
    ),
)
async def list_pocket_voices(
    connection_id: ConnectionId, ctx: AgentsManage, db: DB
) -> PocketVoiceList:
    return await service.list_voices(db, ctx, connection_id)


@router.post(
    "/connections/{connection_id}/voices",
    response_model=PocketVoice,
    status_code=status.HTTP_201_CREATED,
    summary="Clone a voice from a short sample of a speaker who gave permission",
    description=(
        "Multipart upload: `name`, `audio` (WAV, 5 to 30 seconds after decoding, at most "
        "10 MB) and `consent_attestation=true`. " + CLONING_RULES
    ),
    responses={
        413: {"model": ErrorResponse, "description": "Sample larger than the limit"},
        415: {"model": ErrorResponse, "description": "Sample is not a WAV file"},
        501: {
            "model": ErrorResponse,
            "description": "Voice cloning is not set up on the Pocket TTS server",
        },
        503: {"model": ErrorResponse, "description": "Pocket TTS server unreachable"},
    },
)
async def clone_voice(
    connection_id: ConnectionId,
    name: Annotated[str, Form(max_length=200, description="Shown in voice pickers")],
    audio: Annotated[UploadFile, File(description="WAV sample, 5 to 30 seconds, at most 10 MB")],
    ctx: IntegrationsManage,
    db: DB,
    consent_attestation: Annotated[
        bool, Form(description=f'"{CONSENT_ATTESTATION}" Must be true.')
    ] = False,
) -> PocketVoice:
    return await service.create_voice(
        db,
        ctx,
        connection_id,
        name=name,
        consent_attestation=consent_attestation,
        audio=audio,
    )


@router.delete(
    "/connections/{connection_id}/voices/{voice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a cloned voice",
    description="Refused while the organization's models or an agent use the voice.",
)
async def delete_cloned_voice(
    connection_id: ConnectionId,
    voice_id: Annotated[str, Path(max_length=80)],
    ctx: IntegrationsManage,
    db: DB,
) -> None:
    await service.delete_voice(db, ctx, connection_id, voice_id)
