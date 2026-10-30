"""Voice design: a new voice from a written description, and what a voice server can do.

The voice design server (infra/docker/voice-design, Qwen3-TTS) makes a voice from a plain
description ("a warm, unhurried woman in her forties with a soft Irish accent") instead of
a recording, so no speaker and no consent are involved. It has the Pocket TTS voice API, so
voice_cloning.py lists and deletes its voices and reaches it the same way (a registered AI
server's gateway, the operator's address, or the connection's own address under the
destination policy); this module is its companion and shares those helpers.

Every voice server states what it can do at GET {base}/capabilities:

    {"model": "...", "cloning": true, "design": true, "instruct": true,
     "sound_tags": ["laugh", ...], "languages": ["en", ...], "streaming": "sentence",
     "reference_seconds": {"min": 3, "max": 30}}

For a server from before that endpoint ATLAS derives the answer from what it has (cloning
from GET /cloning, the sounds from GET /audio/sound-tags; no design, no instructions, an
unknown streaming mode), so older servers keep working. Answers are kept briefly, per
connection and address, like the sound tags.

A designed voice is the organization's like a cloned one (`custom_voices`, voice_cloning.py):
its id starts with "design-", only its organization sees, selects or deletes it, and the
audit log records who designed which voice and when, never the description's text. The
server keeps a short preview of each voice (GET /voices/{id}/sample) that ATLAS streams to
the browser.
"""

from __future__ import annotations

import logging
import time
import unicodedata
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import IntegrationUnavailableError, NotFoundError, ValidationFailedError
from app.models.voice_cloning import CustomVoice
from app.schemas.voice import ConnectionOut
from app.schemas.voice_cloning import (
    PocketVoice,
    ReferenceSeconds,
    VoiceCapabilities,
    VoiceDesignCreate,
)
from app.services import audit, voice_cloning
from app.services.voice_cloning import PocketServer

log = logging.getLogger(__name__)

CAPABILITIES_PATH = "/capabilities"
DESIGN_PATH = "/voices/design"
CAPABILITIES_TTL_SECONDS = 60.0
CAPABILITIES_TIMEOUT = httpx.Timeout(4.0, connect=3.0)
SAMPLE_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
SAMPLE_CHUNK_BYTES = 64 * 1024
MAX_SAMPLE_BYTES = 10 * 1024 * 1024
MAX_LANGUAGES = 50
MAX_LANGUAGE_CHARS = 32
MAX_MODEL_CHARS = 80
MIN_DESCRIPTION_CHARS = 10
MAX_DESCRIPTION_CHARS = 300
MAX_VOICE_ID_CHARS = 80
STREAMING_MODES = ("sentence", "chunk", "none")
_MAX_CACHED = 512
NO_DESIGN = (
    "This voice server cannot design a voice from a description. Voice design needs the "
    "voice design server; on this one, clone a voice from a recording instead."
)
NO_SAMPLE = "This voice has no preview."

_capabilities_cache: dict[tuple[str, str, str, str], tuple[float, VoiceCapabilities | None]] = {}


def reset_capabilities_cache() -> None:
    """Forget every server's answer (tests)."""
    _capabilities_cache.clear()


# ---------------------------------------------------------------- what a server can do


def _plain(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _words(value: Any, limit: int, count: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if (word := _plain(item, limit)) and word not in out:
            out.append(word)
    return out[:count]


def _reference_seconds(raw: Any) -> ReferenceSeconds | None:
    if not isinstance(raw, dict):
        return None
    low, high = _seconds(raw.get("min")), _seconds(raw.get("max"))
    if low is None or high is None or low > high:
        return None
    return ReferenceSeconds(min=low, max=high)


def _seconds(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3600:
        return value
    return None


def clean_capabilities(raw: Any) -> VoiceCapabilities | None:
    """A server's capabilities with only the fields ATLAS knows, bounded; None if not one."""
    if not isinstance(raw, dict) or not all(
        isinstance(raw.get(key), bool) for key in ("cloning", "design", "instruct")
    ):
        return None
    streaming = raw.get("streaming")
    return VoiceCapabilities(
        model=_plain(raw.get("model"), MAX_MODEL_CHARS),
        cloning=raw["cloning"],
        design=raw["design"],
        instruct=raw["instruct"],
        sound_tags=list(voice_cloning.clean_sound_tags(raw.get("sound_tags"))),
        languages=_words(raw.get("languages"), MAX_LANGUAGE_CHARS, MAX_LANGUAGES),
        streaming=streaming if streaming in STREAMING_MODES else "unknown",
        reference_seconds=_reference_seconds(raw.get("reference_seconds")),
    )


def _headers(server: PocketServer) -> dict[str, str]:
    return {"Authorization": f"Bearer {server.api_key}"} if server.api_key else {}


async def _fetch_capabilities(server: PocketServer) -> VoiceCapabilities | None:
    """GET {base}/capabilities; None for a server without it (or an answer that is not one)."""
    async with httpx.AsyncClient(
        timeout=CAPABILITIES_TIMEOUT, follow_redirects=False, headers=_headers(server)
    ) as client:
        response = await client.get(f"{server.base}{CAPABILITIES_PATH}")
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return clean_capabilities(body)


def _remember(key: tuple[str, str, str, str], value: VoiceCapabilities | None) -> None:
    now = time.monotonic()
    if len(_capabilities_cache) >= _MAX_CACHED:
        for stale in [k for k, (until, _) in _capabilities_cache.items() if until <= now] or [
            next(iter(_capabilities_cache))
        ]:
            _capabilities_cache.pop(stale, None)
    _capabilities_cache[key] = (now + CAPABILITIES_TTL_SECONDS, value)


def _key(org_id: uuid.UUID, conn: ConnectionOut, kind: str) -> tuple[str, str, str, str]:
    return (str(org_id), conn.id, repr(sorted(conn.connection_settings.items())), kind)


async def server_capabilities(
    db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut
) -> VoiceCapabilities | None:
    """What the connection's voice server states about itself; None when it has no
    capabilities endpoint, or cannot be asked. Never raises; the answer is kept a minute."""
    if not voice_cloning._self_hosted_voice(conn):
        return None
    key = _key(org_id, conn, "server")
    cached = _capabilities_cache.get(key)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    try:
        caps = await _fetch_capabilities(await voice_cloning._server(db, org_id, conn))
    except Exception as exc:  # unreachable, refused by the policy, or a bad answer
        log.info("voice_capabilities_unavailable", extra={"error_type": type(exc).__name__})
        caps = None
    _remember(key, caps)
    return caps


async def _derive_capabilities(server: PocketServer) -> VoiceCapabilities:
    """What an older server (no /capabilities) can do, from the endpoints it has.

    Cloning is on when GET /cloning says so; a server from before /cloning clones when
    GET /voices answers. The sounds come from GET /audio/sound-tags. The rest is unknown.
    """
    response = await voice_cloning._send("GET", server, "/cloning")
    if response.status_code == 200:
        try:
            cloning = response.json().get("state") == "on"
        except (ValueError, AttributeError):
            cloning = False
    elif response.status_code == 404:
        cloning = (await voice_cloning._send("GET", server, "/voices")).status_code == 200
    else:
        cloning = False
    try:
        tags = await voice_cloning._fetch_sound_tags(server)
    except httpx.HTTPError:
        tags = ()
    return VoiceCapabilities(
        cloning=cloning,
        design=False,
        instruct=False,
        sound_tags=list(tags),
        languages=[],
        streaming="unknown",
    )


async def _capabilities_of(
    db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut
) -> VoiceCapabilities:
    """The server's own statement, else the derived one. Raises when the server cannot be
    reached at all (the derived answer needs it)."""
    stated = await server_capabilities(db, org_id, conn)
    if stated is not None:
        return stated
    key = _key(org_id, conn, "derived")
    cached = _capabilities_cache.get(key)
    if cached is not None and cached[0] > time.monotonic() and cached[1] is not None:
        return cached[1]
    derived = await _derive_capabilities(await voice_cloning._server(db, org_id, conn))
    _remember(key, derived)
    return derived


async def capabilities(
    db: AsyncSession, ctx: TenantContext, connection_id: str
) -> VoiceCapabilities:
    conn = await voice_cloning._pocket_connection(db, ctx, connection_id)
    return await _capabilities_of(db, ctx.org_id, conn)


async def sound_tags(db: AsyncSession, org_id: uuid.UUID, conn: ConnectionOut) -> tuple[str, ...]:
    """The sounds the connection's voice server performs: as its capabilities state them
    when it has them, else as `voice_cloning.sound_tags` finds out. Never raises."""
    stated = await server_capabilities(db, org_id, conn)
    if stated is not None:
        return tuple(stated.sound_tags)
    return await voice_cloning.sound_tags(db, org_id, conn)


# ---------------------------------------------------------------- designing a voice


def clean_description(raw: str) -> str:
    """The description as plain text: one line of 10 to 300 characters, no control codes."""
    text = " ".join(raw.split())
    if len(text) < MIN_DESCRIPTION_CHARS:
        raise ValidationFailedError(
            f"Describe the voice in at least {MIN_DESCRIPTION_CHARS} characters: who is "
            "speaking and how.",
            details={"field": "description"},
        )
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise ValidationFailedError(
            f"Descriptions are at most {MAX_DESCRIPTION_CHARS} characters.",
            details={"field": "description"},
        )
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise ValidationFailedError(
            "The description contains control characters.", details={"field": "description"}
        )
    return text


def clean_language(raw: str | None, offered: list[str]) -> str | None:
    """One of the server's languages, or none (the server's default)."""
    language = _plain(raw, MAX_LANGUAGE_CHARS)
    if language is None:
        return None
    if offered and language not in offered:
        raise ValidationFailedError(
            f"This voice server does not speak {language}. It offers: {', '.join(offered[:10])}.",
            details={"field": "language"},
        )
    return language


def _sample_text(raw: str | None) -> str | None:
    text = _plain(raw, 300)
    if text and any(unicodedata.category(c).startswith("C") for c in text):
        raise ValidationFailedError(
            "The sample text contains control characters.", details={"field": "sample_text"}
        )
    return text


async def design_voice(
    db: AsyncSession, ctx: TenantContext, connection_id: str, data: VoiceDesignCreate
) -> PocketVoice:
    name = voice_cloning.clean_name(data.name)
    description = clean_description(data.description)
    sample_text = _sample_text(data.sample_text)
    conn = await voice_cloning._pocket_connection(db, ctx, connection_id)
    caps = await _capabilities_of(db, ctx.org_id, conn)
    if not caps.design:
        raise ValidationFailedError(NO_DESIGN)
    language = clean_language(data.language, caps.languages)
    server = await voice_cloning._server(db, ctx.org_id, conn)
    body: dict[str, Any] = {"name": name, "description": description}
    if language is not None:
        body["language"] = language
    if sample_text is not None:
        body["sample_text"] = sample_text
    if data.seed is not None:
        body["seed"] = data.seed
    now = datetime.now(UTC)
    response = await voice_cloning._send(
        "POST",
        server,
        DESIGN_PATH,
        read_seconds=get_settings().local_tts_timeout_seconds,
        json=body,
    )
    if response.status_code == 404:
        raise ValidationFailedError(NO_DESIGN)
    if response.status_code != 201:
        voice_cloning._raise_for(response)
    try:
        reply = response.json()
    except ValueError:
        reply = {}
    voice_id = reply.get("id") if isinstance(reply, dict) else None
    if (
        not isinstance(voice_id, str)
        or len(voice_id) > MAX_VOICE_ID_CHARS
        or not voice_cloning.DESIGN_ID.match(voice_id)
    ):
        raise IntegrationUnavailableError("The voice server returned an unexpected voice id.")
    row = CustomVoice(
        organization_id=ctx.org_id,
        connection_id=connection_id,
        voice_id=voice_id,
        name=name,
        created_by=ctx.user_id,
        created_at=now,
    )
    db.add(row)
    try:
        await db.flush()
    except Exception:
        # Do not leave an unowned voice on the server.
        await voice_cloning._delete_on_server(server, voice_id, quiet=True)
        raise
    await audit.record(
        db,
        action="ai.voice_designed",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="custom_voice",
        resource_id=voice_id,
        changes={
            "voice_id": voice_id,
            "name": name,
            "connection_id": connection_id,
            "language": language,
            "description_chars": len(description),  # the text itself is only on the server
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return voice_cloning._designed(row, reply | {"description": description})


# ---------------------------------------------------------------- the preview


@dataclass(slots=True)
class SampleStream:
    content_type: str
    content_length: int | None
    chunks: AsyncGenerator[bytes, None]


def _audio_type(declared: str | None) -> str:
    kind = (declared or "").split(";")[0].strip().lower()
    return kind if kind.startswith("audio/") else "audio/wav"


async def open_sample(
    db: AsyncSession, ctx: TenantContext, connection_id: str, voice_id: str
) -> SampleStream:
    """Start streaming the preview of one of the organization's voices. The caller must
    consume or close `chunks`."""
    if not (voice_cloning.DESIGN_ID.match(voice_id) or voice_cloning.VOICE_ID.match(voice_id)):
        raise NotFoundError("Voice not found")
    owned = await db.scalar(
        select(CustomVoice.id).where(
            CustomVoice.organization_id == ctx.org_id,
            CustomVoice.connection_id == connection_id,
            CustomVoice.voice_id == voice_id,
        )
    )
    if owned is None:
        raise NotFoundError("Voice not found")
    conn = await voice_cloning._pocket_connection(db, ctx, connection_id)
    server = await voice_cloning._server(db, ctx.org_id, conn)
    client = httpx.AsyncClient(
        timeout=SAMPLE_TIMEOUT, follow_redirects=False, headers=_headers(server)
    )
    try:
        request = client.build_request("GET", f"{server.base}/voices/{voice_id}/sample")
        response = await client.send(request, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        raise IntegrationUnavailableError(
            "The voice server did not answer in time. Try again in a moment."
        ) from None
    except httpx.HTTPError:
        await client.aclose()
        raise IntegrationUnavailableError(
            f"ATLAS cannot reach the AI server {server.ai_server}. Check that it is running "
            "(AI providers, AI servers)."
            if server.ai_server
            else f"ATLAS cannot reach the voice server at {server.base}."
        ) from None
    if response.status_code != 200:
        await response.aread()
        await response.aclose()
        await client.aclose()
        if (
            server.ai_server
            and response.status_code == 401
            and voice_cloning._gateway_refused(response)
        ):
            raise IntegrationUnavailableError(
                f"The AI server {server.ai_server} refused its token. Replace the token under "
                "AI providers, AI servers."
            )
        if response.status_code == 404:
            raise NotFoundError(NO_SAMPLE)
        voice_cloning._raise_for(response)

    async def _chunks() -> AsyncGenerator[bytes, None]:
        sent = 0
        try:
            async for chunk in response.aiter_bytes(SAMPLE_CHUNK_BYTES):
                sent += len(chunk)
                if sent > MAX_SAMPLE_BYTES:
                    log.warning("voice_sample_too_large", extra={"voice_id": voice_id})
                    break
                yield chunk
        except httpx.HTTPError as exc:
            log.warning("voice_sample_interrupted", extra={"error_type": type(exc).__name__})
        finally:
            await response.aclose()
            await client.aclose()

    declared = response.headers.get("content-length")
    length = int(declared) if declared and declared.isdigit() else None
    return SampleStream(
        content_type=_audio_type(response.headers.get("content-type")),
        content_length=length if length is not None and length <= MAX_SAMPLE_BYTES else None,
        chunks=_chunks(),
    )
