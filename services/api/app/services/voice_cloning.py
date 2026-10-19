"""Pocket TTS voice cloning: list, clone and delete voices on a tenant's Pocket TTS server.

A cloned voice is computed by the Pocket TTS server (infra/docker/pocket-tts) from a short
WAV sample of a speaker who gave permission. ATLAS:

- requires an explicit consent attestation and records it in the audit log (who, when,
  which voice, the attestation; never the audio);
- checks size and type before anything is forwarded; the sample is not stored here;
- records which organization owns each voice (`custom_voices`), because the server keeps
  every cloned voice in one shared store. Voice ids carry a random 64-bit suffix, and an
  organization only ever sees, selects or deletes its own.

ATLAS reaches the server at ATLAS_LOCAL_TTS_URL when set (an operator setting, so it may be
private), otherwise at the connection's base URL under the outbound destination policy.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import UploadFile
from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    IntegrationUnavailableError,
    NotFoundError,
    RateLimitedError,
    ValidationFailedError,
)
from app.core.net_policy import validate_destination_url
from app.models.voice import Agent, AgentStatus, AgentVersion
from app.models.voice_cloning import CustomVoice
from app.schemas.voice import ConnectionOut
from app.schemas.voice_cloning import CONSENT_ATTESTATION, PocketVoice, PocketVoiceList
from app.services import ai_settings, audit

log = logging.getLogger(__name__)

# A Pocket TTS connection is a self-hosted voice connection whose name or address names the
# server (same hints as the speed presets in ai_speed.py).
POCKET_HINTS = ("local-tts", "pocket")
SETUP_URL = "https://huggingface.co/kyutai/pocket-tts"
VOICE_ID = re.compile(r"^custom-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{16}$")
MAX_NAME_CHARS = 60
MAX_LISTED = 500
NOT_POCKET = (
    "Voice cloning works with Pocket TTS connections: a self-hosted voice connection whose "
    "name or address contains 'pocket' or 'local-tts'."
)
OLD_SERVER = (
    "This Pocket TTS server has no voice cloning: rebuild it with the current image "
    "(docker compose --profile local-ai build local-tts, then restart local-tts)."
)
CONSENT_REQUIRED = (
    "Confirm that you have the speaker's permission to clone their voice (consent_attestation)."
)


class VoiceCloningUnavailableError(AppError):
    status_code = 501
    code = "voice_cloning_unavailable"


class SampleTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class UnsupportedSampleError(AppError):
    status_code = 415
    code = "unsupported_media_type"


# ---------------------------------------------------------------- connection and target


def is_pocket_connection(conn: ConnectionOut) -> bool:
    if conn.provider != "speaches" or conn.hosted_trial:
        return False
    haystack = " ".join([conn.name, *map(str, conn.connection_settings.values())]).lower()
    return any(h in haystack for h in POCKET_HINTS)


async def _pocket_connection(
    db: AsyncSession, ctx: TenantContext, connection_id: str
) -> ConnectionOut:
    for conn in await ai_settings.list_connections(db, ctx):
        if conn.id == connection_id:
            if not is_pocket_connection(conn):
                raise ValidationFailedError(NOT_POCKET)
            return conn
    raise NotFoundError("Connection not found")


async def _base_url(conn: ConnectionOut) -> str:
    """The server's OpenAI-style base URL (".../v1"), after the destination policy."""
    settings = get_settings()
    override = (settings.local_tts_url or "").strip()
    url = override or str(conn.connection_settings.get("base_url") or "").strip()
    if not url:
        raise IntegrationUnavailableError(
            "This connection has no base URL. Set ATLAS_LOCAL_TTS_URL to where ATLAS reaches "
            "the Pocket TTS server."
        )
    try:
        url = await validate_destination_url(
            url,
            label="Pocket TTS server",
            allow_private=bool(override) or settings.allow_private_model_urls,
            # The operator's own URL may be plain http to a server on the same Docker network.
            allow_internal_http=bool(override),
        )
    except ValidationFailedError as exc:
        raise IntegrationUnavailableError(
            f"ATLAS cannot use {url} to manage voices: {exc.message}. Set ATLAS_LOCAL_TTS_URL "
            "to where ATLAS reaches the Pocket TTS server (for example "
            "http://127.0.0.1:8002/v1 when the API runs outside Docker)."
        ) from None
    return url.rstrip("/")


# ---------------------------------------------------------------- talking to the server


async def _send(
    method: str, base: str, path: str, *, read_seconds: float = 15.0, **kwargs: Any
) -> httpx.Response:
    headers = {}
    if key := get_settings().local_tts_api_key.get_secret_value():
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(read_seconds, connect=5.0),
            follow_redirects=False,
            headers=headers,
        ) as client:
            return await client.request(method, f"{base}{path}", **kwargs)
    except httpx.TimeoutException:
        raise IntegrationUnavailableError(
            "The Pocket TTS server did not answer in time. Try again in a moment."
        ) from None
    except httpx.HTTPError:
        raise IntegrationUnavailableError(
            f"ATLAS cannot reach the Pocket TTS server at {base}. Check "
            "that local-tts is running; if the API runs outside Docker, set "
            "ATLAS_LOCAL_TTS_URL (for example http://127.0.0.1:8002/v1)."
        ) from None


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:500]
    return f"The Pocket TTS server answered HTTP {response.status_code}."


def _raise_for(response: httpx.Response) -> None:
    status = response.status_code
    detail = _detail(response)
    if status == 501:
        raise VoiceCloningUnavailableError(detail, details={"setup_url": SETUP_URL})
    if status == 401:
        raise IntegrationUnavailableError(
            "The Pocket TTS server requires an API key: set ATLAS_LOCAL_TTS_API_KEY to its "
            "POCKET_TTS_API_KEY."
        )
    if status == 413:
        raise SampleTooLargeError(detail)
    if status == 415:
        raise UnsupportedSampleError(detail)
    if status in (400, 411, 422):
        raise ValidationFailedError(detail)
    if status == 409:
        raise ConflictError(detail)
    if status == 429:
        raise RateLimitedError(detail)
    raise IntegrationUnavailableError(detail)


# ---------------------------------------------------------------- validation


def clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise ValidationFailedError("Give the voice a name.", details={"field": "name"})
    if len(name) > MAX_NAME_CHARS:
        raise ValidationFailedError(
            f"Names are at most {MAX_NAME_CHARS} characters.", details={"field": "name"}
        )
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise ValidationFailedError(
            "The name contains control characters.", details={"field": "name"}
        )
    return name


async def read_sample(file: UploadFile) -> bytes:
    """Read at most the limit (+1 byte to detect overflow); WAV only, checked by content."""
    limit = get_settings().voice_clone_max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise SampleTooLargeError(f"Voice samples must be {limit // (1024 * 1024)} MB or smaller.")
    if not data:
        raise ValidationFailedError("The voice sample is empty.", details={"field": "audio"})
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise UnsupportedSampleError(
            "Upload the sample as a WAV file. The ATLAS web app converts MP3, M4A and browser "
            "recordings to WAV before uploading."
        )
    return data


# ---------------------------------------------------------------- reads


async def _owned(db: AsyncSession, org_id: uuid.UUID, connection_id: str) -> dict[str, CustomVoice]:
    rows = await db.scalars(
        select(CustomVoice).where(
            CustomVoice.organization_id == org_id, CustomVoice.connection_id == connection_id
        )
    )
    return {r.voice_id: r for r in rows}


def _builtin(raw: dict[str, Any]) -> PocketVoice | None:
    vid = raw.get("id")
    if not isinstance(vid, str) or not vid or vid.startswith("custom-"):
        return None
    return PocketVoice(
        id=vid[:80],
        name=str(raw.get("name") or vid)[:80],
        cloned=False,
        license=str(raw["license"])[:40] if raw.get("license") else None,
        default=raw.get("default") is True,
    )


def _sample_seconds(raw: dict[str, Any]) -> float | None:
    value = raw.get("sample_seconds")
    return float(value) if isinstance(value, int | float) else None


async def list_voices(db: AsyncSession, ctx: TenantContext, connection_id: str) -> PocketVoiceList:
    conn = await _pocket_connection(db, ctx, connection_id)
    base = await _base_url(conn)
    response = await _send("GET", base, "/voices")
    if response.status_code in (404, 501):
        # Cloning is off (or an older server): built-in voices still come from the speech API.
        message = OLD_SERVER if response.status_code == 404 else _detail(response)
        builtins = await _send("GET", base, "/audio/voices")
        if not builtins.is_success:
            _raise_for(builtins)
        return PocketVoiceList(
            voices=[v for raw in _data(builtins) if (v := _builtin(raw))],
            cloning_available=False,
            setup_message=message,
            setup_url=SETUP_URL,
        )
    if not response.is_success:
        _raise_for(response)
    owned = await _owned(db, ctx.org_id, connection_id)
    voices: list[PocketVoice] = []
    for raw in _data(response):
        if raw.get("type") == "custom":
            row = owned.get(str(raw.get("id")))
            if row is not None:  # other organizations' voices are never shown
                voices.append(_cloned(row, _sample_seconds(raw)))
        elif voice := _builtin(raw):
            voices.append(voice)
    return PocketVoiceList(voices=voices, cloning_available=True)


def _data(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        data = response.json().get("data")
    except (ValueError, AttributeError):
        return []
    return [d for d in data[:MAX_LISTED] if isinstance(d, dict)] if isinstance(data, list) else []


def _cloned(row: CustomVoice, sample_seconds: float | None) -> PocketVoice:
    return PocketVoice(
        id=row.voice_id,
        name=row.name,
        cloned=True,
        created_at=row.created_at,
        created_by=row.created_by,
        sample_seconds=sample_seconds,
    )


# ---------------------------------------------------------------- writes


async def create_voice(
    db: AsyncSession,
    ctx: TenantContext,
    connection_id: str,
    *,
    name: str,
    consent_attestation: bool,
    audio: UploadFile,
) -> PocketVoice:
    if consent_attestation is not True:
        raise ValidationFailedError(CONSENT_REQUIRED, details={"field": "consent_attestation"})
    clean = clean_name(name)
    conn = await _pocket_connection(db, ctx, connection_id)
    data = await read_sample(audio)
    base = await _base_url(conn)
    now = datetime.now(UTC)
    # Kept by the server with the voice, so the store itself shows the attestation's source.
    statement = (
        f"{CONSENT_ATTESTATION} Attested in ATLAS by user {ctx.user_id} of organization "
        f"{ctx.org_id} on {now.date().isoformat()}."
    )
    response = await _send(
        "POST",
        base,
        "/voices",
        read_seconds=get_settings().local_tts_timeout_seconds,
        data={"name": clean, "consent": "true", "consent_statement": statement},
        files={"audio": ("sample.wav", data, "audio/wav")},
    )
    if response.status_code == 404:
        raise VoiceCloningUnavailableError(OLD_SERVER, details={"setup_url": SETUP_URL})
    if response.status_code != 201:
        _raise_for(response)
    try:
        body = response.json()
    except ValueError:
        body = {}
    voice_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(voice_id, str) or not VOICE_ID.match(voice_id):
        raise IntegrationUnavailableError("The Pocket TTS server returned an unexpected voice id.")
    seconds = _sample_seconds(body)
    row = CustomVoice(
        organization_id=ctx.org_id,
        connection_id=connection_id,
        voice_id=voice_id,
        name=clean,
        created_by=ctx.user_id,
        created_at=now,
    )
    db.add(row)
    try:
        await db.flush()
    except Exception:
        # Do not leave an unowned voice on the server.
        await _delete_on_server(base, voice_id, quiet=True)
        raise
    await audit.record(
        db,
        action="ai.voice_cloned",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="custom_voice",
        resource_id=voice_id,
        changes={
            "voice_id": voice_id,
            "name": clean,
            "connection_id": connection_id,
            "consent_attestation": CONSENT_ATTESTATION,
            "consent_confirmed": True,
            "sample_seconds": seconds,
            "sample_bytes": len(data),
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _cloned(row, seconds)


async def _delete_on_server(base: str, voice_id: str, *, quiet: bool = False) -> None:
    try:
        response = await _send("DELETE", base, f"/voices/{voice_id}")
    except AppError:
        if quiet:
            log.warning("voice_clone_cleanup_failed", extra={"voice_id": voice_id})
            return
        raise
    if response.status_code in (204, 404):
        return
    if quiet:
        log.warning("voice_clone_cleanup_failed", extra={"voice_id": voice_id})
        return
    _raise_for(response)


async def _in_use(db: AsyncSession, ctx: TenantContext, voice_id: str) -> str | None:
    selection = await ai_settings.get_selection(db, ctx)
    if selection.tts is not None and selection.tts.settings.get("voice") == voice_id:
        return "the organization's models for calls (AI providers)"
    # Ids are random and contain only [a-z0-9-], so a text search of the config is exact.
    names = await db.scalars(
        select(Agent.name)
        .join(AgentVersion, AgentVersion.agent_id == Agent.id)
        .where(
            Agent.organization_id == ctx.org_id,
            AgentVersion.organization_id == ctx.org_id,
            Agent.status != AgentStatus.ARCHIVED,
            or_(
                AgentVersion.version == Agent.latest_version,
                AgentVersion.version == Agent.published_version,
            ),
            cast(AgentVersion.config, Text).contains(voice_id),
        )
        .distinct()
        .limit(3)
    )
    found = list(names)
    return f"agent {', '.join(found)}" if found else None


async def delete_voice(
    db: AsyncSession, ctx: TenantContext, connection_id: str, voice_id: str
) -> None:
    if not VOICE_ID.match(voice_id):
        raise NotFoundError("Voice not found")
    row = await db.scalar(
        select(CustomVoice).where(
            CustomVoice.organization_id == ctx.org_id,
            CustomVoice.connection_id == connection_id,
            CustomVoice.voice_id == voice_id,
        )
    )
    if row is None:
        raise NotFoundError("Voice not found")
    if user := await _in_use(db, ctx, voice_id):
        raise ValidationFailedError(
            f"This voice is used by {user}. Choose another voice there first."
        )
    conn = await _pocket_connection(db, ctx, connection_id)
    base = await _base_url(conn)
    await _delete_on_server(base, voice_id)
    await db.delete(row)
    await audit.record(
        db,
        action="ai.voice_deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="custom_voice",
        resource_id=voice_id,
        changes={"voice_id": voice_id, "name": row.name, "connection_id": connection_id},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


async def assert_voice_owned(db: AsyncSession, org_id: uuid.UUID, voice: object) -> None:
    """A cloned voice id may only be selected by the organization that created it."""
    if not isinstance(voice, str) or not voice.startswith("custom-"):
        return
    owner = await db.scalar(
        select(CustomVoice.organization_id).where(CustomVoice.voice_id == voice)
    )
    if owner != org_id:
        raise ValidationFailedError("Unknown cloned voice. Choose a voice from the list.")
