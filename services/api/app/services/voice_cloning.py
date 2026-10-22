"""Pocket TTS voice cloning: list, clone and delete voices on a tenant's Pocket TTS server.

A cloned voice is computed by the Pocket TTS server (infra/docker/pocket-tts) from a short
sample of a speaker who gave permission. ATLAS:

- requires an explicit consent attestation and records it in the audit log (who, when,
  which voice, the attestation; never the audio);
- checks size, format and length before anything is forwarded (WAV, MP3, M4A, OGG, WebM
  and FLAC are decoded to WAV by voice_samples.py); the sample is not stored here;
- records which organization owns each voice (`custom_voices`), because the server keeps
  every cloned voice in one shared store. Voice ids carry a random 64-bit suffix, and an
  organization only ever sees, selects or deletes its own.

Organization owners turn voice cloning on (with a Hugging Face token of an account that
accepted the gated weights' terms) or off. The setting belongs to the server, so it applies
to every organization using it. The token is passed straight through to the server: it is
never stored, logged, audited or returned here, and is scrubbed from the server's replies.

ATLAS reaches the server at ATLAS_LOCAL_TTS_URL when set (an operator setting, so it may be
private), otherwise at the connection's base URL under the outbound destination policy. The
Pocket TTS connection of a registered AI server (ADR 0007) is always reached at that server's
own address, {url}/tts/v1, with its token: the local setting is for another server. So is
any voice connection whose base URL is on a registered AI server's address (for example the
expressive voice at {url}/voice/v1).

The expressive voice server (Chatterbox-Turbo, infra/docker/expressive-tts) has the same
voice and cloning API, so everything here works for it too: its connections are named by
"expressive" or "chatterbox" (or answer GET {base}/audio/sound-tags, see `sound_tags`), and
ATLAS reaches it at ATLAS_EXPRESSIVE_TTS_URL when set.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

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
    PermissionDeniedError,
    RateLimitedError,
    ValidationFailedError,
)
from app.core.net_policy import validate_destination_url
from app.core.permissions import Permission, has_permission
from app.core.redis import enforce_rate_limit
from app.core.security import get_secret_box
from app.models.ai_servers import AiServer
from app.models.voice import Agent, AgentStatus, AgentVersion
from app.models.voice_cloning import CustomVoice
from app.schemas.voice import ConnectionOut
from app.schemas.voice_cloning import (
    CONSENT_ATTESTATION,
    CloningState,
    PocketVoice,
    PocketVoiceList,
    VoiceCloningEnable,
    VoiceCloningStatus,
)
from app.services import ai_servers, ai_settings, audit, voice_samples

log = logging.getLogger(__name__)

# A Pocket TTS connection is a self-hosted voice connection whose name or address names the
# server (same hints as the speed presets in ai_speed.py; /tts/v1 is its path behind an AI
# gateway). The expressive voice server has the same voice API (/voice/v1 behind a gateway).
POCKET_HINTS = ("local-tts", "pocket", "/tts/v1")
EXPRESSIVE_HINTS = ("expressive", "chatterbox", "/voice/v1")
SETUP_URL = "https://huggingface.co/kyutai/pocket-tts"
VOICE_ID = re.compile(r"^custom-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{16}$")
# Anything in a server reply that looks like a Hugging Face token.
HF_TOKEN = re.compile(r"hf_[A-Za-z0-9_-]{16,}")
TERMS_URL = re.compile(r"https://huggingface\.co/[A-Za-z0-9_.\-/]{1,150}")
MAX_NAME_CHARS = 60
MAX_LISTED = 500
CLONING_CHANGES_PER_MINUTE = 5
NOT_POCKET = (
    "Voice cloning works with Pocket TTS and expressive voice connections: a self-hosted "
    "voice connection whose name or address contains 'pocket', 'local-tts', 'expressive' or "
    "'chatterbox'."
)
OLD_SERVER = (
    "This Pocket TTS server has no voice cloning: rebuild it with the current image "
    "(docker compose --profile local-ai build local-tts, then restart local-tts)."
)
UPDATE_SERVER = (
    "This Pocket TTS server is too old to turn voice cloning on or off from ATLAS: update it "
    "(docker compose --profile local-ai build local-tts, then restart local-tts)."
)
CONSENT_REQUIRED = (
    "Confirm that you have the speaker's permission to clone their voice (consent_attestation)."
)
TERMS_REQUIRED = (
    "Confirm that the token's Hugging Face account accepted the terms of kyutai/pocket-tts "
    "(terms_accepted)."
)
BAD_CREDENTIAL = "The voice server did not accept this token: paste a Hugging Face read token."
# The server's answer when ATLAS_LOCAL_TTS_API_KEY does not match (not a token problem).
SERVER_KEY_REJECTED = "Invalid API key"


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


def _haystack(conn: ConnectionOut) -> str:
    return " ".join([conn.name, *map(str, conn.connection_settings.values())]).lower()


def _self_hosted_voice(conn: ConnectionOut) -> bool:
    return conn.provider == "speaches" and not conn.hosted_trial


def is_expressive_connection(conn: ConnectionOut) -> bool:
    """Named as the expressive voice server (by name or address; `sound_tags` asks it)."""
    return _self_hosted_voice(conn) and any(h in _haystack(conn) for h in EXPRESSIVE_HINTS)


def is_pocket_connection(conn: ConnectionOut) -> bool:
    """A voice server with the Pocket TTS voice API: Pocket TTS or the expressive server."""
    if not _self_hosted_voice(conn):
        return False
    haystack = _haystack(conn)
    return any(h in haystack for h in (*POCKET_HINTS, *EXPRESSIVE_HINTS))


async def _pocket_connection(
    db: AsyncSession, ctx: TenantContext, connection_id: str
) -> ConnectionOut:
    for conn in await ai_settings.list_connections(db, ctx):
        if conn.id == connection_id:
            # Not named as either server: an expressive server is still known by its answer.
            if is_pocket_connection(conn) or await sound_tags(db, ctx.org_id, conn):
                return conn
            raise ValidationFailedError(NOT_POCKET)
    raise NotFoundError("Connection not found")


@dataclass(frozen=True, slots=True)
class PocketServer:
    """Where ATLAS reaches a Pocket TTS server, and the key it sends there."""

    base: str  # OpenAI-style base URL (".../v1")
    api_key: str | None = field(default=None, repr=False)
    ai_server: str | None = None  # a registered AI server's name: its gateway checks the key


def _normalised(url: str) -> str:
    """Scheme and host in lower case, without a trailing slash (as AI servers are stored)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


async def _registered_server(
    db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut
) -> ai_servers.Gateway | None:
    """The AI server behind a connection: one ATLAS created, or one at its address."""
    gateway = await ai_servers.gateway_for(db, org_id, conn.id, "tts")
    if gateway is not None:
        return gateway
    url = _normalised(str(conn.connection_settings.get("base_url") or ""))
    if not url:
        return None
    for server in await db.scalars(select(AiServer).where(AiServer.organization_id == org_id)):
        if url.startswith(server.base_url + "/"):
            return ai_servers.Gateway(
                server_name=server.name,
                base_url=url,
                token=get_secret_box().decrypt(server.token_encrypted),
            )
    return None


async def _server(db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut) -> PocketServer:
    """The server's OpenAI-style base URL and key, after the destination policy."""
    gateway = await _registered_server(db, org_id, conn)
    if gateway is not None:
        try:
            url = await ai_servers.check_address(gateway.base_url)
        except ValidationFailedError as exc:
            raise IntegrationUnavailableError(
                f"ATLAS cannot use the AI server {gateway.server_name}: {exc.message}."
            ) from None
        return PocketServer(url.rstrip("/"), gateway.token, gateway.server_name)
    url, key = await _base_url(conn)
    return PocketServer(url, key)


@dataclass(frozen=True, slots=True)
class _Local:
    """The operator's setting for where ATLAS reaches one kind of voice server."""

    url_setting: str
    url: str
    key: str | None = field(repr=False)
    server: str
    example: str


def _local(conn: ConnectionOut) -> _Local | None:
    settings = get_settings()
    if is_expressive_connection(conn):
        return _Local(
            "ATLAS_EXPRESSIVE_TTS_URL",
            (settings.expressive_tts_url or "").strip(),
            settings.expressive_tts_api_key.get_secret_value() or None,
            "expressive voice server",
            "http://expressive-tts:8000/v1 for an API container",
        )
    if is_pocket_connection(conn):
        return _Local(
            "ATLAS_LOCAL_TTS_URL",
            (settings.local_tts_url or "").strip(),
            settings.local_tts_api_key.get_secret_value() or None,
            "Pocket TTS server",
            "http://127.0.0.1:8002/v1 when the API runs outside Docker",
        )
    return None  # another self-hosted voice: only its own address


async def _base_url(conn: ConnectionOut) -> tuple[str, str | None]:
    """The server's OpenAI-style base URL (".../v1") after the destination policy, and key."""
    settings = get_settings()
    local = _local(conn)
    override = local.url if local else ""
    url = override or str(conn.connection_settings.get("base_url") or "").strip()
    if not url:
        where = (
            f" Set {local.url_setting} to where ATLAS reaches the {local.server}." if local else ""
        )
        raise IntegrationUnavailableError(f"This connection has no base URL.{where}")
    try:
        url = await validate_destination_url(
            url,
            label=local.server[0].upper() + local.server[1:] if local else "Voice server",
            allow_private=bool(override) or settings.allow_private_model_urls,
            # The operator's own URL may be plain http to a server on the same Docker network.
            allow_internal_http=bool(override),
        )
    except ValidationFailedError as exc:
        where = (
            f" Set {local.url_setting} to where ATLAS reaches the {local.server} (for example "
            f"{local.example})."
            if local
            else ""
        )
        raise IntegrationUnavailableError(
            f"ATLAS cannot use {url} to manage voices: {exc.message}.{where}"
        ) from None
    return url.rstrip("/"), local.key if local else None


# ---------------------------------------------------------------- talking to the server


def _gateway_refused(response: httpx.Response) -> bool:
    """The AI gateway's own answer to a wrong token (not the Pocket TTS server's)."""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "unauthorized"


async def _send(
    method: str, server: PocketServer, path: str, *, read_seconds: float = 15.0, **kwargs: Any
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {server.api_key}"} if server.api_key else {}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(read_seconds, connect=5.0),
            follow_redirects=False,
            headers=headers,
        ) as client:
            response = await client.request(method, f"{server.base}{path}", **kwargs)
    except httpx.TimeoutException:
        raise IntegrationUnavailableError(
            "The Pocket TTS server did not answer in time. Try again in a moment."
        ) from None
    except httpx.HTTPError:
        if server.ai_server:
            raise IntegrationUnavailableError(
                f"ATLAS cannot reach the AI server {server.ai_server}. Check that it is running "
                "(AI providers, AI servers)."
            ) from None
        raise IntegrationUnavailableError(
            f"ATLAS cannot reach the Pocket TTS server at {server.base}. Check "
            "that local-tts is running; if the API runs outside Docker, set "
            "ATLAS_LOCAL_TTS_URL (for example http://127.0.0.1:8002/v1)."
        ) from None
    if server.ai_server and response.status_code == 401 and _gateway_refused(response):
        raise IntegrationUnavailableError(
            f"The AI server {server.ai_server} refused its token. Replace the token under AI "
            "providers, AI servers."
        )
    return response


def _scrub(text: str, secret: str | None = None) -> str:
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return HF_TOKEN.sub("[REDACTED]", text)


def _plain_detail(response: httpx.Response, secret: str | None = None) -> str | None:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, str) and detail.strip():
        return _scrub(detail.strip(), secret)[:500]
    return None


def _detail(response: httpx.Response, secret: str | None = None) -> str:
    return (
        _plain_detail(response, secret)
        or f"The Pocket TTS server answered HTTP {response.status_code}."
    )


def _raise_for(response: httpx.Response, secret: str | None = None) -> NoReturn:
    status = response.status_code
    detail = _detail(response, secret)
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


# ---------------------------------------------------------------- sounds an expressive voice makes

# An expressive voice server lists the sound cues it performs at GET {base}/audio/sound-tags
# as {"tags": ["[laugh]", "[clear throat]", ...]} (written with or without the brackets). The
# answer is kept briefly, per connection and address, so publishing, previews and pickers
# do not ask for every request.
SOUND_TAGS_PATH = "/audio/sound-tags"
SOUND_TAGS_TTL_SECONDS = 60.0
SOUND_TAGS_TIMEOUT = httpx.Timeout(4.0, connect=3.0)
MAX_SOUND_TAGS = 20
_MAX_CACHED = 512
# The tags reach agents' prompts: plain lower-case words only, never the server's free text.
_SOUND_TAG = re.compile(r"[a-z]+(?: [a-z]+){0,2}")
_sound_tags_cache: dict[tuple[str, str, str], tuple[float, tuple[str, ...]]] = {}


def reset_sound_tags_cache() -> None:
    """Forget every server's answer (tests)."""
    _sound_tags_cache.clear()


def clean_sound_tags(raw: Any) -> tuple[str, ...]:
    """The server's tags that are plain words ("[laugh]" -> "laugh"), in its order, once."""
    if not isinstance(raw, list):
        return ()
    tags: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        bare = item.strip()
        if bare.startswith("[") and bare.endswith("]"):
            bare = bare[1:-1]
        tag = " ".join(bare.split()).lower()
        if len(tag) <= 24 and _SOUND_TAG.fullmatch(tag) and tag not in tags:
            tags.append(tag)
    return tuple(tags[:MAX_SOUND_TAGS])


async def _fetch_sound_tags(server: PocketServer) -> tuple[str, ...]:
    headers = {"Authorization": f"Bearer {server.api_key}"} if server.api_key else {}
    async with httpx.AsyncClient(
        timeout=SOUND_TAGS_TIMEOUT, follow_redirects=False, headers=headers
    ) as client:
        response = await client.get(f"{server.base}{SOUND_TAGS_PATH}")
    if response.status_code != 200:
        return ()
    try:
        body = response.json()
    except ValueError:
        return ()
    return clean_sound_tags(body.get("tags")) if isinstance(body, dict) else ()


async def sound_tags(db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut) -> tuple[str, ...]:
    """The sound cues the connection's voice server performs; empty when it performs none.

    A voice connection is expressive when its server answers GET {base}/audio/sound-tags
    with at least one tag. Reached like the voice and cloning API (registered AI server,
    operator setting, or the connection's own address under the destination policy).
    Never raises: a server that cannot be asked counts as not expressive.
    """
    if not _self_hosted_voice(conn):
        return ()
    key = (str(org_id), conn.id, repr(sorted(conn.connection_settings.items())))
    now = time.monotonic()
    cached = _sound_tags_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        tags = await _fetch_sound_tags(await _server(db, org_id, conn))
    except Exception as exc:  # unreachable, refused by the policy, or a bad answer
        log.info("sound_tags_unavailable", extra={"error_type": type(exc).__name__})
        tags = ()
    if len(_sound_tags_cache) >= _MAX_CACHED:
        for stale in [k for k, (until, _) in _sound_tags_cache.items() if until <= now] or [
            next(iter(_sound_tags_cache))
        ]:
            _sound_tags_cache.pop(stale, None)
    _sound_tags_cache[key] = (now + SOUND_TAGS_TTL_SECONDS, tags)
    return tags


async def default_voice(db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut) -> str | None:
    """The built-in voice the server marks as its default (else its first); None if unknown."""
    try:
        response = await _send("GET", await _server(db, org_id, conn), "/audio/voices")
    except AppError:
        return None
    if not response.is_success:
        return None
    voices = [v for raw in _data(response) if (v := _builtin(raw))]
    chosen = next((v for v in voices if v.default), voices[0] if voices else None)
    return chosen.id if chosen else None


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
    """Read at most the limit (+1 byte to detect overflow)."""
    limit = get_settings().voice_clone_max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise SampleTooLargeError(f"Voice samples must be {limit // (1024 * 1024)} MB or smaller.")
    if not data:
        raise ValidationFailedError("The voice sample is empty.", details={"field": "audio"})
    return data


async def prepare_sample(data: bytes) -> voice_samples.Sample:
    """Any accepted audio format, as mono 16-bit 24 kHz WAV of 5 to 30 seconds."""
    try:
        return await voice_samples.normalize(data)
    except voice_samples.SampleError as exc:
        if exc.status == 415:
            raise UnsupportedSampleError(exc.message) from None
        raise ValidationFailedError(exc.message, details={"field": "audio"}) from None


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
    server = await _server(db, ctx.org_id, conn)
    tags = list(await sound_tags(db, ctx.org_id, conn))
    response = await _send("GET", server, "/voices")
    if response.status_code in (404, 501):
        # Cloning is off (or an older server): built-in voices still come from the speech API.
        if response.status_code == 404:
            state, message = "unsupported", OLD_SERVER
        else:
            state, message = await _state_while_off(server, _detail(response))
        builtins = await _send("GET", server, "/audio/voices")
        if not builtins.is_success:
            _raise_for(builtins)
        return PocketVoiceList(
            voices=[v for raw in _data(builtins) if (v := _builtin(raw))],
            cloning_available=False,
            cloning_state=state,
            setup_message=message,
            setup_url=SETUP_URL,
            sound_tags=tags,
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
    return PocketVoiceList(
        voices=voices, cloning_available=True, cloning_state="on", sound_tags=tags
    )


async def _state_while_off(server: PocketServer, message: str) -> tuple[CloningState, str]:
    """Whether a server without working voice cloning is turning it on, or why it failed.

    Best effort: the voice list must not fail because of it.
    """
    try:
        response = await _send("GET", server, "/cloning")
    except AppError:
        return "off", message
    if response.status_code == 404:
        return "unsupported", message  # cloning can only be set up on the server itself
    if response.status_code != 200:
        return "off", message
    try:
        status = _status(response, can_manage=False)
    except AppError:
        return "off", message
    # "on" here means it came on between the two requests: the next list shows it.
    state: CloningState = "enabling" if status.state == "on" else status.state
    return state, status.message or message


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
    sample = await prepare_sample(data)
    server = await _server(db, ctx.org_id, conn)
    now = datetime.now(UTC)
    # Kept by the server with the voice, so the store itself shows the attestation's source.
    statement = (
        f"{CONSENT_ATTESTATION} Attested in ATLAS by user {ctx.user_id} of organization "
        f"{ctx.org_id} on {now.date().isoformat()}."
    )
    response = await _send(
        "POST",
        server,
        "/voices",
        read_seconds=get_settings().local_tts_timeout_seconds,
        data={"name": clean, "consent": "true", "consent_statement": statement},
        files={"audio": ("sample.wav", sample.wav, "audio/wav")},
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
        await _delete_on_server(server, voice_id, quiet=True)
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
            "sample_format": sample.source_format,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _cloned(row, seconds)


async def _delete_on_server(server: PocketServer, voice_id: str, *, quiet: bool = False) -> None:
    try:
        response = await _send("DELETE", server, f"/voices/{voice_id}")
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
    server = await _server(db, ctx.org_id, conn)
    await _delete_on_server(server, voice_id)
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


# ---------------------------------------------------------------- turning cloning on and off


def _status(
    response: httpx.Response, *, can_manage: bool, secret: str | None = None
) -> VoiceCloningStatus:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("state") not in ("off", "enabling", "on", "failed"):
        raise IntegrationUnavailableError(
            "The Pocket TTS server returned an unexpected voice cloning status."
        )
    message, terms, source = body.get("message"), body.get("terms_url"), body.get("token_source")
    text = _scrub(message.strip(), secret)[:500] if isinstance(message, str) else ""
    return VoiceCloningStatus(
        state=body["state"],
        message=text or None,
        # Shown as a link, so only ever a Hugging Face page.
        terms_url=terms if isinstance(terms, str) and TERMS_URL.fullmatch(terms) else SETUP_URL,
        token_source=source if source in ("environment", "saved") else None,
        can_manage=can_manage,
    )


OPERATOR_MANAGED = (
    "Voice cloning on this platform is turned on or off by its operator. Ask them to set it up "
    "on the voice server."
)


def _can_manage(ctx: TenantContext) -> bool:
    return get_settings().voice_cloning_switch == "owners" and has_permission(
        ctx.role, Permission.ORG_MANAGE
    )


def _require_switch() -> None:
    if get_settings().voice_cloning_switch != "owners":
        raise PermissionDeniedError(OPERATOR_MANAGED)


async def cloning_status(
    db: AsyncSession, ctx: TenantContext, connection_id: str
) -> VoiceCloningStatus:
    conn = await _pocket_connection(db, ctx, connection_id)
    server = await _server(db, ctx.org_id, conn)
    response = await _send("GET", server, "/cloning")
    if response.status_code == 404:
        return VoiceCloningStatus(
            state="unsupported",
            message=UPDATE_SERVER,
            terms_url=SETUP_URL,
            can_manage=_can_manage(ctx),
        )
    if response.status_code != 200:
        _raise_for(response)
    return _status(response, can_manage=_can_manage(ctx))


def _raise_cloning(response: httpx.Response, *, token: str | None = None) -> NoReturn:
    status = response.status_code
    if status == 404:
        raise VoiceCloningUnavailableError(UPDATE_SERVER, details={"setup_url": SETUP_URL})
    if token is not None:
        detail = _detail(response, token)
        if status == 422:
            # Plain messages only: a structured validation error could quote the token back.
            message = _plain_detail(response, token) or BAD_CREDENTIAL
            raise ValidationFailedError(message, details={"field": "hf_token"})
        # 401: Hugging Face rejected the token (unless the server refused ATLAS's own API key);
        # 403: the token's account has not accepted the terms of the gated weights.
        if status == 403 or (status == 401 and detail != SERVER_KEY_REJECTED):
            raise ValidationFailedError(
                detail, details={"field": "hf_token", "terms_url": SETUP_URL}
            )
    _raise_for(response, token)


async def enable_cloning(
    db: AsyncSession, ctx: TenantContext, connection_id: str, data: VoiceCloningEnable
) -> VoiceCloningStatus:
    if data.terms_accepted is not True:
        raise ValidationFailedError(
            TERMS_REQUIRED, details={"field": "terms_accepted", "terms_url": SETUP_URL}
        )
    _require_switch()
    await enforce_rate_limit(f"voice-cloning:{ctx.org_id}", CLONING_CHANGES_PER_MINUTE)
    conn = await _pocket_connection(db, ctx, connection_id)
    server = await _server(db, ctx.org_id, conn)
    token = data.hf_token.get_secret_value()
    # The server checks the token with Hugging Face before answering.
    response = await _send("PUT", server, "/cloning", read_seconds=30.0, json={"hf_token": token})
    if response.status_code not in (200, 202):
        _raise_cloning(response, token=token)
    try:
        status = _status(response, can_manage=_can_manage(ctx), secret=token)
    except IntegrationUnavailableError:
        # Accepted, with a body this version does not understand: it is being turned on.
        status = VoiceCloningStatus(
            state="enabling", terms_url=SETUP_URL, can_manage=_can_manage(ctx)
        )
    await audit.record(
        db,
        action="voice_cloning.enable_requested",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_connection",
        resource_id=connection_id,
        changes={"connection_id": connection_id, "terms_accepted": True, "state": status.state},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return status


async def disable_cloning(db: AsyncSession, ctx: TenantContext, connection_id: str) -> None:
    _require_switch()
    await enforce_rate_limit(f"voice-cloning:{ctx.org_id}", CLONING_CHANGES_PER_MINUTE)
    conn = await _pocket_connection(db, ctx, connection_id)
    server = await _server(db, ctx.org_id, conn)
    response = await _send("DELETE", server, "/cloning")
    if response.status_code not in (200, 204):
        _raise_cloning(response)
    await audit.record(
        db,
        action="voice_cloning.disabled",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_connection",
        resource_id=connection_id,
        changes={"connection_id": connection_id},
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
