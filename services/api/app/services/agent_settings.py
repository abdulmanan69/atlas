"""Agent sound files and setting secrets, and handing both to the voice service on publish.

Sound files (custom background noise, voicemail and screening recordings) are validated
by content and size, kept by ATLAS so they can be previewed and so older versions stay
restorable, and referenced from the agent configuration by id. Publishing hands each file
to the voice service the way its settings expect, once:

- background noise → its ambient-noise upload (signed URL scoped to the workflow); the
  workflow's `ambient_noise_configuration` then carries `storage_key`/`storage_backend`
  (services/pipecat/audio_mixer.py reads them, falling back to the office ambience);
- recordings → its recordings upload + create; messages refer to the `recording_id`
  (schemas/answer_supervisor.py `AnswerMessage`).

A separate voicemail-detection model needs its API key inline in the workflow (the voice
service builds that classifier from provider, model and key). ATLAS cannot read keys back
from the voice service, so the key is entered once per agent, stored encrypted and only
ever sent to the voice service.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import uuid
import wave
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import UploadFile
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.api.v1.deps import TenantContext
from app.core.exceptions import (
    AppError,
    ConflictError,
    IntegrationUnavailableError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.security import get_secret_box
from app.integrations.voice_runtime.client import ProviderConnection, VoiceRuntimeClient
from app.integrations.voice_runtime.workflow_builder import (
    ClassifierModel,
    PublishAssets,
    RuntimeAudio,
)
from app.models.agent_settings import AgentAudio, AgentSecret
from app.models.voice import Agent, AgentVersion
from app.schemas.agent_settings import AudioPurpose, ClassifierKeyIn
from app.schemas.voice import CLASSIFIER_PROVIDERS, AgentConfig
from app.services import audit
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

MB = 1024 * 1024
# The voice service's own limits: ambient-noise uploads 10 MB, recordings 5 MB.
AUDIO_LIMITS: dict[str, int] = {"ambient": 10 * MB, "message": 5 * MB}
MAX_AUDIO_PER_AGENT = 25
CLASSIFIER_KIND = "voicemail_classifier"
_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_UPLOAD_TIMEOUT = 60.0


class FileTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


# ---------------------------------------------------------------- validation


def safe_audio_filename(raw: str | None) -> str:
    """A storage-safe name with its extension; only .wav and .mp3 are accepted."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    stem, ext = PurePosixPath(name).stem, PurePosixPath(name).suffix.lower()
    if ext not in _MIME:
        raise ValidationFailedError("Upload a WAV or MP3 file.")
    stem = _UNSAFE.sub("_", stem).strip("._")[:100] or "audio"
    return f"{stem}{ext}"


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def _is_mp3(data: bytes) -> bool:
    if data[:3] == b"ID3":
        return True
    # An MPEG audio frame starts with 11 set sync bits.
    return len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


def sniff_audio(filename: str, data: bytes) -> str:
    """The MIME type for `filename`, after checking the bytes really are that format."""
    if len(data) < 128:
        raise ValidationFailedError("The file is empty or too short to be audio.")
    ext = PurePosixPath(filename).suffix.lower()
    ok = _is_wav(data) if ext == ".wav" else _is_mp3(data)
    if not ok:
        raise ValidationFailedError(
            f"The file does not look like {'a WAV' if ext == '.wav' else 'an MP3'} file."
        )
    return _MIME[ext]


def wav_duration(data: bytes) -> float | None:
    """Seconds of a PCM WAV file; None for other encodings (still accepted)."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            rate = w.getframerate()
            return round(w.getnframes() / rate, 2) if rate else None
    except (wave.Error, EOFError):
        return None


async def read_audio_upload(file: UploadFile, purpose: AudioPurpose) -> tuple[str, bytes]:
    """Read at most the limit for `purpose` (+1 byte to detect overflow) and validate."""
    limit = AUDIO_LIMITS[purpose]
    name = safe_audio_filename(file.filename)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise FileTooLargeError(f"Audio files must be {limit // MB} MB or smaller")
    return name, data


# ---------------------------------------------------------------- audio files


async def _agent(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.organization_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    return agent


async def upload_audio(
    db: AsyncSession,
    ctx: TenantContext,
    agent_id: uuid.UUID,
    *,
    purpose: AudioPurpose,
    filename: str,
    data: bytes,
) -> AgentAudio:
    agent = await _agent(db, ctx, agent_id)
    mime = sniff_audio(filename, data)
    count = await db.scalar(
        select(func.count()).select_from(AgentAudio).where(AgentAudio.agent_id == agent.id)
    )
    if (count or 0) >= MAX_AUDIO_PER_AGENT:
        raise ConflictError(
            f"An agent can keep {MAX_AUDIO_PER_AGENT} sound files. Delete unused ones first."
        )
    row = AgentAudio(
        organization_id=ctx.org_id,
        agent_id=agent.id,
        purpose=purpose,
        filename=filename,
        content_type=mime,
        size_bytes=len(data),
        duration_seconds=wav_duration(data) if mime == "audio/wav" else None,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        created_by_user_id=ctx.user_id,
    )
    db.add(row)
    await db.flush()
    await audit.record(
        db,
        action="agent.audio_uploaded",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={"audio_id": str(row.id), "purpose": purpose, "size_bytes": len(data)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.refresh(row)
    return row


async def list_audio(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> list[AgentAudio]:
    agent = await _agent(db, ctx, agent_id)
    rows = await db.execute(
        select(AgentAudio)
        .where(AgentAudio.agent_id == agent.id, AgentAudio.organization_id == ctx.org_id)
        .order_by(AgentAudio.created_at.desc())
    )
    return list(rows.scalars().all())


async def audio_file(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, audio_id: uuid.UUID
) -> AgentAudio:
    row = (
        await db.execute(
            select(AgentAudio)
            .options(undefer(AgentAudio.data))
            .where(
                AgentAudio.id == audio_id,
                AgentAudio.agent_id == agent_id,
                AgentAudio.organization_id == ctx.org_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Sound file not found")
    return row


async def delete_audio(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, audio_id: uuid.UUID
) -> None:
    row = await audio_file(db, ctx, agent_id, audio_id)
    # Versions are immutable and restorable, so a file any version uses is kept.
    used = await db.scalar(
        select(func.count())
        .select_from(AgentVersion)
        .where(
            AgentVersion.agent_id == agent_id,
            cast(AgentVersion.config, String).contains(str(audio_id)),
        )
    )
    if used:
        raise ConflictError("A saved version of this agent uses this sound file, so it is kept.")
    await db.delete(row)
    await audit.record(
        db,
        action="agent.audio_deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent_id,
        changes={"audio_id": str(audio_id)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


def referenced_audio(config: AgentConfig) -> dict[uuid.UUID, tuple[AudioPurpose, str]]:
    """Every sound file the configuration points at: id → (purpose, setting name)."""
    cs = config.call_settings
    refs: dict[uuid.UUID, tuple[AudioPurpose, str]] = {}
    if cs.ambient.audio_id is not None:
        refs[cs.ambient.audio_id] = ("ambient", "Background noise")
    if cs.voicemail.message_audio_id is not None:
        refs[cs.voicemail.message_audio_id] = ("message", "Voicemail recording")
    if cs.voicemail.screening_audio_id is not None:
        refs[cs.voicemail.screening_audio_id] = ("message", "Screening recording")
    return refs


async def check_audio_references(
    db: AsyncSession, org_id: uuid.UUID, agent_id: uuid.UUID, config: AgentConfig
) -> None:
    """Refuse a configuration that points at sound files this agent does not have."""
    refs = referenced_audio(config)
    if not refs:
        return
    rows = await db.execute(
        select(AgentAudio.id, AgentAudio.purpose).where(
            AgentAudio.id.in_(list(refs)),
            AgentAudio.agent_id == agent_id,
            AgentAudio.organization_id == org_id,
        )
    )
    found = {row.id: row.purpose for row in rows}
    for audio_id, (purpose, label) in refs.items():
        if found.get(audio_id) != purpose:
            raise ValidationFailedError(f"{label}: the sound file was not found. Upload it again.")


# ---------------------------------------------------------------- classifier key


async def _connections(db: AsyncSession, org_id: uuid.UUID) -> list[ProviderConnection]:
    async with runtime_client(db, org_id) as client:
        return await client.list_connections()


def classifier_connection_problem(conn: ProviderConnection | None) -> str | None:
    if conn is None or not conn.is_active:
        return "The connection was not found under AI providers"
    if conn.provider not in CLASSIFIER_PROVIDERS:
        return (
            "Voicemail detection can use OpenAI, Groq, Google or OpenRouter connections; "
            "choose one of those or the agent's own model"
        )
    if (conn.connection_settings or {}).get("base_url"):
        return "Connections with a custom base URL cannot be used for voicemail detection"
    return None


async def get_classifier_key(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID
) -> AgentSecret | None:
    agent = await _agent(db, ctx, agent_id)
    return (
        await db.execute(
            select(AgentSecret).where(
                AgentSecret.agent_id == agent.id,
                AgentSecret.organization_id == ctx.org_id,
                AgentSecret.kind == CLASSIFIER_KIND,
            )
        )
    ).scalar_one_or_none()


async def set_classifier_key(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, data: ClassifierKeyIn
) -> AgentSecret:
    agent = await _agent(db, ctx, agent_id)
    conn = next(
        (c for c in await _connections(db, ctx.org_id) if c.uuid == data.connection_id), None
    )
    if problem := classifier_connection_problem(conn):
        raise ValidationFailedError(problem)
    encrypted = get_secret_box().encrypt(data.api_key.get_secret_value())
    row = await get_classifier_key(db, ctx, agent.id)
    if row is None:
        row = AgentSecret(
            organization_id=ctx.org_id,
            agent_id=agent.id,
            kind=CLASSIFIER_KIND,
            connection_id=data.connection_id,
            value_encrypted=encrypted,
        )
        db.add(row)
    else:
        row.connection_id = data.connection_id
        row.value_encrypted = encrypted
    await audit.record(
        db,
        action="agent.classifier_key_set",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={"connection_id": data.connection_id},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(row)
    return row


async def delete_classifier_key(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> None:
    row = await get_classifier_key(db, ctx, agent_id)
    if row is None:
        return
    await db.delete(row)
    await audit.record(
        db,
        action="agent.classifier_key_deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent_id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


# ---------------------------------------------------------------- publishing


class AssetProblem(ValidationFailedError):
    """A setting cannot be published as it stands; shown in the publish issue list."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(
            "Fix the issues below before publishing.",
            details=[
                {
                    "section": "Call settings",
                    "field": field,
                    "message": message,
                    "target": "flow",
                    "id": "",
                }
            ],
        )


async def _put_bytes(upload_url: str, data: bytes, mime: str) -> None:
    """PUT a file to the signed storage URL the voice service issued (never logged)."""
    parts = urlsplit(upload_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        log.error("agent_audio_upload_url_invalid", extra={"scheme": parts.scheme})
        raise IntegrationUnavailableError("Audio storage is not available. Try again shortly.")
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_UPLOAD_TIMEOUT, connect=5.0), follow_redirects=False
        ) as http:
            resp = await http.put(upload_url, content=data, headers={"Content-Type": mime})
    except httpx.HTTPError as exc:
        log.warning(
            "agent_audio_upload_failed",
            extra={"host": parts.hostname, "error_type": type(exc).__name__},
        )
        raise IntegrationUnavailableError(
            "Audio storage is not available. Try again shortly."
        ) from exc
    if not resp.is_success:
        log.warning(
            "agent_audio_upload_rejected",
            extra={"host": parts.hostname, "status": resp.status_code},
        )
        raise IntegrationUnavailableError("Audio storage did not accept the file. Try again.")


async def _load(db: AsyncSession, agent: Agent, audio_id: uuid.UUID) -> AgentAudio | None:
    return (
        await db.execute(
            select(AgentAudio)
            .options(undefer(AgentAudio.data))
            .where(
                AgentAudio.id == audio_id,
                AgentAudio.agent_id == agent.id,
                AgentAudio.organization_id == agent.organization_id,
            )
        )
    ).scalar_one_or_none()


async def _ambient_asset(
    db: AsyncSession, client: VoiceRuntimeClient, agent: Agent, audio_id: uuid.UUID
) -> RuntimeAudio:
    row = await _load(db, agent, audio_id)
    if row is None or row.purpose != "ambient":
        raise AssetProblem("ambient", "The custom background sound was not found. Upload it again.")
    if not (row.runtime_storage_key and row.runtime_storage_backend):
        assert agent.runtime_workflow_id  # created before assets are handed over
        target = await client.ambient_upload_url(
            agent.runtime_workflow_id,
            filename=row.filename,
            mime_type=row.content_type,
            size=row.size_bytes,
        )
        await _put_bytes(target.upload_url, row.data, row.content_type)
        row.runtime_storage_key = target.storage_key
        row.runtime_storage_backend = target.storage_backend
    return RuntimeAudio(
        storage_key=row.runtime_storage_key,
        storage_backend=row.runtime_storage_backend,
        filename=row.filename,
    )


async def _recording_asset(
    db: AsyncSession,
    client: VoiceRuntimeClient,
    agent: Agent,
    audio_id: uuid.UUID,
    *,
    field: str,
    transcript: str | None,
) -> str:
    row = await _load(db, agent, audio_id)
    if row is None or row.purpose != "message":
        raise AssetProblem(field, "The recording was not found. Upload it again.")
    if not row.runtime_recording_id:
        target = await client.recording_upload_url(
            filename=row.filename, mime_type=row.content_type, size=row.size_bytes
        )
        await _put_bytes(target.upload_url, row.data, row.content_type)
        row.runtime_recording_id = await client.create_recording(
            recording_id=target.recording_id,
            storage_key=target.storage_key,
            transcript=(transcript or "").strip() or f"Recorded message ({row.filename})",
            metadata={"file_size": row.size_bytes, "source": "agent_settings"},
        )
        row.runtime_storage_key = target.storage_key
    return row.runtime_recording_id


async def classifier_for_publish(
    db: AsyncSession, agent: Agent, config: AgentConfig, connections: list[ProviderConnection]
) -> ClassifierModel | None:
    """The separate voicemail-detection model, checked before anything is written."""
    vm = config.call_settings.voicemail
    if not vm.enabled or vm.classifier.model != "connection":
        return None
    conn = next((c for c in connections if c.uuid == vm.classifier.connection_id), None)
    if problem := classifier_connection_problem(conn):
        raise AssetProblem("voicemail.classifier", problem)
    secret = (
        await db.execute(
            select(AgentSecret).where(
                AgentSecret.agent_id == agent.id, AgentSecret.kind == CLASSIFIER_KIND
            )
        )
    ).scalar_one_or_none()
    if secret is None or secret.connection_id != vm.classifier.connection_id:
        raise AssetProblem(
            "voicemail.classifier",
            "Enter the API key for the voicemail detection model (Call settings → Voicemail).",
        )
    assert conn is not None and vm.classifier.model_name
    return ClassifierModel(
        provider=conn.provider,
        model=vm.classifier.model_name,
        api_key=get_secret_box().decrypt(secret.value_encrypted),
    )


async def publish_assets(
    db: AsyncSession,
    client: VoiceRuntimeClient,
    agent: Agent,
    config: AgentConfig,
    classifier: ClassifierModel | None,
) -> PublishAssets:
    """Hand the files this configuration uses to the voice service (each only once)."""
    cs = config.call_settings
    vm = cs.voicemail
    ambient = (
        await _ambient_asset(db, client, agent, cs.ambient.audio_id)
        if cs.ambient.enabled and cs.ambient.audio_id is not None
        else None
    )
    recordings: dict[str, str] = {}
    wanted: list[tuple[uuid.UUID, str, str | None]] = []
    if vm.enabled and vm.action == "leave_message" and vm.message_format == "recording":
        assert vm.message_audio_id is not None
        wanted.append((vm.message_audio_id, "voicemail.message", vm.message))
    if vm.enabled and vm.screening_format == "recording":
        assert vm.screening_audio_id is not None
        wanted.append((vm.screening_audio_id, "voicemail.screening", vm.screening_message))
    for audio_id, field, transcript in wanted:
        recordings[str(audio_id)] = await _recording_asset(
            db, client, agent, audio_id, field=field, transcript=transcript
        )
    await db.flush()
    return PublishAssets(ambient=ambient, recordings=recordings, classifier=classifier)


def audio_kind(row: AgentAudio) -> Literal["ambient", "message"]:
    return "ambient" if row.purpose == "ambient" else "message"


def runtime_safe(row: AgentAudio) -> dict[str, Any]:
    """Fields of a sound file that may be returned to tenants."""
    return {
        "id": row.id,
        "purpose": audio_kind(row),
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "duration_seconds": row.duration_seconds,
        "created_at": row.created_at,
    }
