"""AI providers: Pocket TTS and expressive voices, including voices cloned from a consenting
speaker, turning voice cloning on or off on the connection's server, and the sounds an
expressive voice makes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Path, UploadFile, status

from app.api.v1.deps import DB, AgentsManage, IntegrationsManage, OrgManage
from app.schemas.common import ERROR_RESPONSES, ErrorResponse
from app.schemas.voice_cloning import (
    CONSENT_ATTESTATION,
    PocketVoice,
    PocketVoiceList,
    SoundTags,
    VoiceCloningEnable,
    VoiceCloningStatus,
)
from app.services import natural_sounds
from app.services import voice_cloning as service

router = APIRouter(prefix="/ai", tags=["AI providers"], responses=ERROR_RESPONSES)

ConnectionId = Annotated[str, Path(max_length=64)]
CLONING_RULES = (
    "Only clone the voice of a speaker who gave permission. Do not use a cloned voice to "
    "deceive anyone, for fraudulent calls, or to present generated speech as the real person. "
    "The sample is forwarded to the Pocket TTS server, which keeps only the computed voice; "
    "it is not stored by this platform. The consent attestation is recorded in the audit log."
)
SERVER_WIDE = (
    "Voice cloning is a setting of the Pocket TTS server, so it applies to every organization "
    "whose connections use that server."
)
UNAVAILABLE = {"model": ErrorResponse, "description": "Pocket TTS server unreachable"}
TOO_OLD = {
    "model": ErrorResponse,
    "description": "The Pocket TTS server is too old to turn voice cloning on or off; update it",
}


@router.get(
    "/connections/{connection_id}/voices",
    response_model=PocketVoiceList,
    summary="Voices of a Pocket TTS or expressive connection and this organization's cloned voices",
    description=(
        "Built-in voices, and the voices this organization cloned on the connection's server. "
        "`sound_tags` lists the sounds the voices make, for an expressive voice. "
        "`cloning_available` is false (with the setup steps) when the server has no "
        "voice-cloning weights; `cloning_state` says whether it is off, being turned on, or "
        "failed to turn on."
    ),
)
async def list_pocket_voices(
    connection_id: ConnectionId, ctx: AgentsManage, db: DB
) -> PocketVoiceList:
    return await service.list_voices(db, ctx, connection_id)


@router.get(
    "/connections/{connection_id}/sound-tags",
    response_model=SoundTags,
    summary="Whether a voice connection makes sounds such as [laugh], and which",
    description=(
        "An expressive voice (for example Chatterbox-Turbo) laughs, sighs or gasps where a "
        "reply contains [laugh], [sigh] or [gasp]; other voices would read the words. "
        "`expressive` is false for every other voice, and when the voice server cannot be "
        "reached. Agents with the Natural sounds call setting use `prompt_tags`. The answer "
        "is kept for a minute."
    ),
)
async def get_sound_tags(connection_id: ConnectionId, ctx: AgentsManage, db: DB) -> SoundTags:
    return await natural_sounds.connection_sound_tags(db, ctx, connection_id)


@router.post(
    "/connections/{connection_id}/voices",
    response_model=PocketVoice,
    status_code=status.HTTP_201_CREATED,
    summary="Clone a voice from a short sample of a speaker who gave permission",
    description=(
        "Multipart upload: `name`, `audio` and `consent_attestation=true`. The sample may be "
        "WAV, MP3, M4A (AAC), OGG (Opus or Vorbis), WebM (Opus) or FLAC, at most 10 MB, and "
        "must be 5 to 30 seconds long; it is converted to mono 24 kHz WAV before it is sent to "
        "the server. Use a clear recording of one person speaking. " + CLONING_RULES
    ),
    responses={
        413: {"model": ErrorResponse, "description": "Sample larger than the limit"},
        415: {
            "model": ErrorResponse,
            "description": "Sample is not in a supported audio format, or could not be read",
        },
        422: {
            "model": ErrorResponse,
            "description": "Validation failed, for example a sample shorter than 5 or longer "
            "than 30 seconds",
        },
        501: {
            "model": ErrorResponse,
            "description": "Voice cloning is not set up on the Pocket TTS server",
        },
        503: UNAVAILABLE,
    },
)
async def clone_voice(
    connection_id: ConnectionId,
    name: Annotated[str, Form(max_length=200, description="Shown in voice pickers")],
    audio: Annotated[
        UploadFile,
        File(description="WAV, MP3, M4A, OGG, WebM or FLAC; 5 to 30 seconds, at most 10 MB"),
    ],
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


@router.get(
    "/connections/{connection_id}/voice-cloning",
    response_model=VoiceCloningStatus,
    summary="Whether voice cloning is on for a Pocket TTS connection's server",
    description=(
        "`state` is `off`, `enabling`, `on`, `failed` or `unsupported` (a server too old to be "
        "managed from here). Poll this while `state` is `enabling`. `can_manage` is true for "
        "organization owners, who may turn voice cloning on or off. " + SERVER_WIDE
    ),
    responses={503: UNAVAILABLE},
)
async def get_voice_cloning(
    connection_id: ConnectionId, ctx: AgentsManage, db: DB
) -> VoiceCloningStatus:
    return await service.cloning_status(db, ctx, connection_id)


@router.put(
    "/connections/{connection_id}/voice-cloning",
    response_model=VoiceCloningStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Turn voice cloning on with a Hugging Face token (organization owners)",
    description=(
        "Gives the Pocket TTS server a Hugging Face read token of an account that accepted the "
        "terms of kyutai/pocket-tts (`terms_url`), which forbid cloning anyone's voice without "
        "their consent. The server checks the token, then downloads the voice-cloning weights "
        "in the background: the answer is `state: enabling`; poll "
        "`GET .../voice-cloning` until it is `on` or `failed`. The token is passed to the "
        "server only; this platform never stores, logs or returns it. Limited to 5 changes a "
        "minute per organization. " + SERVER_WIDE
    ),
    responses={
        409: {
            "model": ErrorResponse,
            "description": "Voice cloning is already being turned on, or the server's token is "
            "set where the server runs",
        },
        422: {
            "model": ErrorResponse,
            "description": "Terms not confirmed, or Hugging Face refused the token (for example, "
            "its account has not accepted the terms: `details.terms_url` links to them)",
        },
        429: {"model": ErrorResponse, "description": "Too many changes; try again in a minute"},
        501: TOO_OLD,
        503: {
            "model": ErrorResponse,
            "description": "Pocket TTS server or Hugging Face unreachable",
        },
    },
)
async def enable_voice_cloning(
    connection_id: ConnectionId, data: VoiceCloningEnable, ctx: OrgManage, db: DB
) -> VoiceCloningStatus:
    return await service.enable_cloning(db, ctx, connection_id, data)


@router.delete(
    "/connections/{connection_id}/voice-cloning",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Turn voice cloning off (organization owners)",
    description=(
        "The server forgets the token it was given and stops cloning new voices; voices "
        "already cloned are kept. Refused (409) when the server's token is set where the "
        "server runs (`token_source: environment`). Limited to 5 changes a minute per "
        "organization. " + SERVER_WIDE
    ),
    responses={
        409: {"model": ErrorResponse, "description": "The token is set where the server runs"},
        429: {"model": ErrorResponse, "description": "Too many changes; try again in a minute"},
        501: TOO_OLD,
        503: UNAVAILABLE,
    },
)
async def disable_voice_cloning(connection_id: ConnectionId, ctx: OrgManage, db: DB) -> None:
    await service.disable_cloning(db, ctx, connection_id)
