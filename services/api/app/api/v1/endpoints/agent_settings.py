"""Agent settings beside the versioned configuration: sound files, a voicemail-detection
model's API key, and the website widget."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile, status

from app.api.v1.deps import DB, AgentsManage, AgentsRead
from app.schemas.agent_settings import (
    AgentAudioOut,
    AudioPurpose,
    ClassifierKeyIn,
    ClassifierKeyOut,
    WebEmbedOut,
    WebEmbedUpdate,
)
from app.schemas.common import ERROR_RESPONSES
from app.services import agent_settings as service
from app.services import web_embed

router = APIRouter(prefix="/agents", tags=["Agent settings"], responses=ERROR_RESPONSES)


# ---------------------------------------------------------------- sound files


@router.get(
    "/{agent_id}/audio",
    response_model=list[AgentAudioOut],
    summary="Sound files uploaded for this agent",
)
async def list_audio(agent_id: uuid.UUID, ctx: AgentsRead, db: DB) -> list[AgentAudioOut]:
    return [
        AgentAudioOut.model_validate(service.runtime_safe(r))
        for r in await service.list_audio(db, ctx, agent_id)
    ]


@router.post(
    "/{agent_id}/audio",
    response_model=AgentAudioOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a WAV or MP3 file for background noise or a recorded message",
    description=(
        "`purpose` `ambient`: a custom background sound, at most 10 MB. `message`: a "
        "voicemail or screening recording, at most 5 MB. The format is checked from the "
        "file's content. Reference the returned id from the agent's call settings "
        "(`ambient.audio_id`, `voicemail.message_audio_id`, `voicemail.screening_audio_id`); "
        "the file reaches the voice service when the agent is published."
    ),
    responses={413: {"description": "File is larger than the limit for its purpose"}},
)
async def upload_audio(
    agent_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="A .wav or .mp3 file")],
    purpose: Annotated[AudioPurpose, Form()],
    ctx: AgentsManage,
    db: DB,
) -> AgentAudioOut:
    filename, data = await service.read_audio_upload(file, purpose)
    row = await service.upload_audio(
        db, ctx, agent_id, purpose=purpose, filename=filename, data=data
    )
    return AgentAudioOut.model_validate(service.runtime_safe(row))


@router.get(
    "/{agent_id}/audio/{audio_id}/file",
    response_class=Response,
    summary="The sound file itself, for preview",
    responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}}}},
)
async def audio_file(agent_id: uuid.UUID, audio_id: uuid.UUID, ctx: AgentsRead, db: DB) -> Response:
    row = await service.audio_file(db, ctx, agent_id, audio_id)
    return Response(
        content=row.data,
        media_type=row.content_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "Content-Disposition": f'inline; filename="{row.filename}"',
        },
    )


@router.delete(
    "/{agent_id}/audio/{audio_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a sound file no saved version uses",
    responses={409: {"description": "A saved version of the agent uses the file"}},
)
async def delete_audio(agent_id: uuid.UUID, audio_id: uuid.UUID, ctx: AgentsManage, db: DB) -> None:
    await service.delete_audio(db, ctx, agent_id, audio_id)


# ---------------------------------------------------------------- voicemail detection key


@router.get(
    "/{agent_id}/voicemail-classifier-key",
    response_model=ClassifierKeyOut | None,
    summary="Whether a key is stored for a separate voicemail-detection model",
)
async def get_classifier_key(
    agent_id: uuid.UUID, ctx: AgentsManage, db: DB
) -> ClassifierKeyOut | None:
    row = await service.get_classifier_key(db, ctx, agent_id)
    return ClassifierKeyOut.model_validate(row) if row else None


@router.put(
    "/{agent_id}/voicemail-classifier-key",
    response_model=ClassifierKeyOut,
    summary="Store the API key for a separate voicemail-detection model",
    description=(
        "Used when `voicemail.classifier.model` is `connection`: the voice service runs that "
        "classifier with an inline key, so it is entered here once, stored encrypted, sent "
        "only to the voice service on publish, and never returned."
    ),
)
async def set_classifier_key(
    agent_id: uuid.UUID, data: ClassifierKeyIn, ctx: AgentsManage, db: DB
) -> ClassifierKeyOut:
    return ClassifierKeyOut.model_validate(
        await service.set_classifier_key(db, ctx, agent_id, data)
    )


@router.delete("/{agent_id}/voicemail-classifier-key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_classifier_key(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> None:
    await service.delete_classifier_key(db, ctx, agent_id)


# ---------------------------------------------------------------- website widget


@router.get(
    "/{agent_id}/web-embed",
    response_model=WebEmbedOut,
    summary="The agent's website widget: allowed sites, status and snippet",
)
async def get_web_embed(agent_id: uuid.UUID, ctx: AgentsRead, db: DB) -> WebEmbedOut:
    return await web_embed.get_embed(db, ctx, agent_id)


@router.put(
    "/{agent_id}/web-embed",
    response_model=WebEmbedOut,
    summary="Turn the website widget on or off and set the sites allowed to show it",
    description="Applies at once; no publish needed. Turning it on needs at least one site.",
)
async def update_web_embed(
    agent_id: uuid.UUID, data: WebEmbedUpdate, ctx: AgentsManage, db: DB
) -> WebEmbedOut:
    return await web_embed.update_embed(db, ctx, agent_id, data)


@router.post(
    "/{agent_id}/web-embed/rotate",
    response_model=WebEmbedOut,
    summary="Issue a new widget key; snippets with the old key stop working at once",
)
async def rotate_web_embed(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> WebEmbedOut:
    return await web_embed.rotate_key(db, ctx, agent_id)
