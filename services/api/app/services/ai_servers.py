"""AI servers: register a self-hosted models host once; ATLAS sets up and watches its connections.

A models host publishes its model servers through one AI gateway address with a token
(ADR 0007, infra/deploy/oci/caddy/ai-gateway.caddy): the language model at /llm/v1, Whisper
at /whisper/v1, Pocket TTS at /tts/v1, live speech-to-text at /live-stt/<secret>/v1/listen
and a health report at /health. ATLAS:

- checks /health with the token before anything is saved, and creates the voice service's
  model connections (through ai_settings, so they are audited and listed like any other)
  only for the services that are up;
- keeps the token and the live speech-to-text secret encrypted; they, and the Deepgram key
  (kept by the voice service only), are never logged, audited or returned, and are scrubbed
  from upstream messages;
- replaces the token on every connection of the server in one step;
- checks every server every two minutes (app/workers/ai_servers.py) and audits status
  changes;
- refuses to remove a server whose connections the organization's models or an agent use.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationFailedError
from app.core.net_policy import validate_destination_url
from app.core.security import get_secret_box
from app.integrations.voice_runtime.client import RuntimeRejectedError
from app.models.ai_servers import AiServer
from app.models.voice import Agent, AgentStatus, AgentVersion
from app.models.voice_cloning import CustomVoice
from app.schemas.ai_servers import (
    AiServerCreate,
    AiServerOut,
    AiServerRegistered,
    AiServerService,
    AiServerSetup,
    AiServerUpdate,
    AiServerUse,
    AiService,
)
from app.schemas.voice import (
    ConnectionCreate,
    ConnectionUpdate,
    LlmFallback,
    ModelSelection,
    ModelSelectionUpdate,
    ServiceChoice,
)
from app.services import ai_settings, audit
from app.services.ai_speed import SELF_HOSTED_STT_MODEL, STREAMING_STT_MODEL
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

SERVICES: tuple[AiService, ...] = ("llm", "whisper", "tts", "live_stt")
TITLES: dict[AiService, str] = {
    "llm": "Language model",
    "whisper": "Whisper",
    "tts": "Pocket TTS",  # "Pocket" in the name: voice cloning and the presets look for it
    "live_stt": "Live speech",
}
# The OpenAI-compatible model servers, and where each one is behind the gateway.
OPENAI_SERVICES: tuple[AiService, ...] = ("llm", "whisper", "tts")
PATHS: dict[str, str] = {"llm": "/llm/v1", "whisper": "/whisper/v1", "tts": "/tts/v1"}
# The gateway gives each model server 3 seconds and asks them all at once.
HEALTH_TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MONITOR_CONCURRENCY = 8
# Like the self-hosted presets (ai_speed.SELF_HOSTED_LLM_SETTINGS).
LLM_TEMPERATURE = 0.3
POCKET_MODEL = "pocket-tts"
DEFAULT_POCKET_VOICE = "alba"
MAX_TEXT = 200
MAX_ITEMS = 100

REFUSED_TOKEN = "The server refused this token."  # noqa: S105 - a message
STORED_TOKEN_REFUSED = "The server refused its token. Replace the token."  # noqa: S105
LIVE_STT_REFUSED = (
    "The server did not accept this live speech-to-text secret. Copy the part after "
    "/live-stt/ from the live speech-to-text address."
)


# ------------------------------------------------------------------ talking to the gateway


async def check_address(url: str) -> str:
    """The outbound destination policy for an AI server (app/core/net_policy.py).

    The same as for a tenant's own model URL: https to a public address. When the operator
    allows private model addresses (ATLAS_ALLOW_PRIVATE_MODEL_URLS) private addresses are
    accepted too, and plain http on a private network (docs/deployment-layouts.md).
    """
    allow = get_settings().allow_private_model_urls
    return await validate_destination_url(
        url, label="AI server", allow_private=allow, allow_internal_http=allow
    )


def _normalise(url: str) -> str:
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        raise ValidationFailedError(
            "Enter the server's address without ? or #.", details={"field": "url"}
        )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


@dataclass(slots=True)
class Probe:
    """One health check: what the gateway reported, or why it could not be asked."""

    status: str  # ok | degraded | down
    services: dict[str, dict[str, Any]]
    latency_ms: int | None
    error: str | None = None
    refused: bool = False  # the token was refused


def _causes(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 6:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _transport_reason(exc: httpx.HTTPError) -> str:
    """A plain reason for a failed connection; never the exception's own text."""
    chain = _causes(exc)
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain) or any(
        "CERTIFICATE_VERIFY_FAILED" in str(e) for e in chain
    ):
        return (
            "The server's TLS certificate was not accepted (expired, self-signed, or issued "
            "for another name)."
        )
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return "The secure (TLS) connection to the server failed."
    if isinstance(exc, httpx.ConnectError):
        return (
            "The server could not be reached: its name does not resolve, or nothing accepts "
            "connections at that address."
        )
    return "The connection to the server failed."


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _names(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [v[:MAX_TEXT] for v in value if isinstance(v, str)][:MAX_ITEMS]


def _clean_service(raw: Any) -> dict[str, Any]:
    """Only the fields the gateway documents, with bounded sizes."""
    if not isinstance(raw, dict):
        return {"up": False, "state": "down", "error": "invalid response"}
    up = raw.get("up") is True
    state = raw.get("state")
    out: dict[str, Any] = {
        "up": up,
        "state": state if state in ("up", "down", "loading") else ("up" if up else "down"),
    }
    for key in ("latency_ms", "voices", "sessions", "max_sessions"):
        if (number := _count(raw.get(key))) is not None:
            out[key] = number
    for key in ("version", "error", "default_voice", "cloning", "language", "model"):
        if (text := _text(raw.get(key))) is not None:
            out[key] = text
    for key in ("models", "loaded"):
        if (names := _names(raw.get(key))) is not None:
            out[key] = names
    return out


async def probe(
    url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> Probe:
    """GET {url}/health with the token. Never raises and never returns upstream text."""
    try:
        await check_address(url)
    except ValidationFailedError as exc:
        return Probe("down", {}, None, error=exc.message)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=HEALTH_TIMEOUT, follow_redirects=False, transport=transport
        ) as http:
            response = await http.get(
                f"{url}/health",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.TimeoutException:
        return Probe("down", {}, None, error="The server did not answer within 8 seconds.")
    except httpx.HTTPError as exc:
        return Probe("down", {}, None, error=_transport_reason(exc))
    except Exception as exc:  # a check must never fail its caller (the monitor checks many)
        log.warning("ai_server_check_failed", extra={"error_type": type(exc).__name__})
        return Probe("down", {}, None, error="The check failed.")
    latency = round((time.perf_counter() - started) * 1000)
    if response.status_code == 401:
        return Probe("down", {}, latency, error=REFUSED_TOKEN, refused=True)
    try:
        body = response.json()
    except ValueError:
        body = None
    if (
        response.status_code in (200, 503)
        and isinstance(body, dict)
        and isinstance(body.get("services"), dict)
    ):
        services = {
            name: _clean_service(raw) for name, raw in body["services"].items() if name in SERVICES
        }
        up = sum(1 for s in services.values() if s["up"])
        if services and up == len(services):
            return Probe("ok", services, latency)
        if up:
            return Probe("degraded", services, latency)
        return Probe("down", services, latency, error="None of the model servers answers.")
    if response.status_code == 404:
        error = "No AI gateway answers at this address (it has no /health)."
    elif response.is_redirect:
        error = "The address redirects elsewhere. Enter the AI gateway's own address."
    elif response.is_success:
        error = "The address answered, but not like an AI gateway."
    else:
        error = f"The server answered HTTP {response.status_code} instead of its health report."
    return Probe("down", {}, latency, error=error)


async def live_secret_accepted(
    url: str, secret: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> bool | None:
    """Whether the gateway knows this live speech-to-text secret; None when it cannot tell.

    A plain (not WebSocket) request for the listen path: with a wrong secret the path is
    unknown to the gateway (404 {"error": "not_found"}); with the right one the speech
    server itself answers, here refusing a deliberately unusable query.
    """
    try:
        async with httpx.AsyncClient(
            timeout=HEALTH_TIMEOUT, follow_redirects=False, transport=transport
        ) as http:
            response = await http.get(
                f"{url}/live-stt/{secret}/v1/listen", params={"encoding": "atlas-check"}
            )
    except httpx.HTTPError:
        return None
    if response.status_code != 404:
        return True
    try:
        body = response.json()
    except ValueError:
        return True
    return not (isinstance(body, dict) and body.get("error") == "not_found")


# ------------------------------------------------------------------ helpers


@dataclass(frozen=True, slots=True)
class Secrets:
    token: str = field(repr=False)
    live_stt_secret: str | None = field(default=None, repr=False)
    deepgram_key: str | None = field(default=None, repr=False)

    def scrub(self, text: str) -> str:
        for value in (self.token, self.live_stt_secret, self.deepgram_key):
            if value:
                text = text.replace(value, "[REDACTED]")
        return text


def _stored_secrets(server: AiServer) -> Secrets:
    box = get_secret_box()
    secret = server.live_stt_secret_encrypted
    return Secrets(
        token=box.decrypt(server.token_encrypted),
        live_stt_secret=box.decrypt(secret) if secret else None,
    )


def _ids(server: AiServer) -> dict[AiService, str | None]:
    return {
        "llm": server.llm_connection_id,
        "whisper": server.whisper_connection_id,
        "tts": server.tts_connection_id,
        "live_stt": server.live_stt_connection_id,
    }


def _set_id(server: AiServer, service: AiService, connection_id: str | None) -> None:
    setattr(server, f"{service}_connection_id", connection_id)


def _health(server: AiServer) -> dict[str, dict[str, Any]]:
    services = (server.last_health or {}).get("services")
    return services if isinstance(services, dict) else {}


def _live_url(server_url: str, secret: str) -> str:
    return f"{server_url}/live-stt/{secret}"


def _connection_name(server_name: str, service: AiService) -> str:
    return f"{server_name} · {TITLES[service]}"


def _out(server: AiServer) -> AiServerOut:
    health = _health(server)
    ids = _ids(server)
    services: list[AiServerService] = []
    for name in SERVICES:
        raw = health.get(name)
        if raw is None:
            services.append(
                AiServerService(
                    service=name,
                    title=TITLES[name],
                    state="not_offered",
                    up=False,
                    connection_id=ids[name],
                )
            )
            continue
        models = raw.get("models") or ([raw["model"]] if raw.get("model") else [])
        services.append(
            AiServerService(
                service=name,
                title=TITLES[name],
                state=raw.get("state", "down"),
                up=bool(raw.get("up")),
                latency_ms=raw.get("latency_ms"),
                version=raw.get("version"),
                models=models,
                loaded=raw.get("loaded"),
                error=raw.get("error"),
                voices=raw.get("voices"),
                default_voice=raw.get("default_voice"),
                cloning=raw.get("cloning"),
                sessions=raw.get("sessions"),
                max_sessions=raw.get("max_sessions"),
                connection_id=ids[name],
            )
        )
    return AiServerOut(
        id=server.id,
        name=server.name,
        url=server.base_url,
        status=server.status,
        last_checked_at=server.last_checked_at,
        last_latency_ms=server.last_latency_ms,
        last_error=server.last_error,
        services=services,
        live_stt_secret_set=server.live_stt_secret_encrypted is not None,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _apply(server: AiServer, result: Probe) -> None:
    server.last_checked_at = datetime.now(UTC)
    server.last_latency_ms = result.latency_ms
    server.last_error = result.error[:300] if result.error else None
    server.status = result.status
    if result.services:
        server.last_health = {"services": result.services}
    elif known := _health(server):
        # Not reachable: what it offered, all marked down.
        reason = "token refused" if result.refused else "unreachable"
        server.last_health = {
            "services": {
                name: {
                    **{k: v for k, v in raw.items() if k in ("version", "models", "model")},
                    "up": False,
                    "state": "down",
                    "error": reason,
                }
                for name, raw in known.items()
            }
        }


async def _record(
    db: AsyncSession, server: AiServer, result: Probe, ctx: TenantContext | None = None
) -> bool:
    """Store a check's result; audit and log a change of status. Returns whether it changed."""
    before = server.status
    _apply(server, result)
    if before == server.status or before == "unchecked":
        return False
    await audit.record(
        db,
        action="ai_server.status_changed",
        organization_id=server.organization_id,
        actor_user_id=ctx.user_id if ctx else None,
        resource_type="ai_server",
        resource_id=server.id,
        changes={"from": before, "to": server.status, "reason": server.last_error},
        ip=ctx.meta.ip if ctx else None,
        user_agent=ctx.meta.user_agent if ctx else None,
    )
    log.info(
        "ai_server_status_changed",
        extra={"ai_server_id": str(server.id), "from": before, "to": server.status},
    )
    return True


def _refuse_unusable(result: Probe) -> None:
    if result.refused:
        raise ValidationFailedError(REFUSED_TOKEN, details={"field": "token"})
    if not result.services:
        raise ValidationFailedError(
            result.error or "The server could not be checked.", details={"field": "url"}
        )


async def _check_live_secret(url: str, secret: str, result: Probe) -> None:
    live = result.services.get("live_stt")
    if live is None or not live.get("up"):
        return  # nothing answers there to tell a right secret from a wrong one
    if await live_secret_accepted(url, secret) is False:
        raise ValidationFailedError(LIVE_STT_REFUSED, details={"field": "live_stt_secret"})


async def _get(db: AsyncSession, ctx: TenantContext, server_id: uuid.UUID) -> AiServer:
    server = await db.scalar(
        select(AiServer).where(AiServer.id == server_id, AiServer.organization_id == ctx.org_id)
    )
    if server is None:
        raise NotFoundError("AI server not found")
    return server


async def _present_ids(
    db: AsyncSession, ctx: TenantContext, server: AiServer
) -> dict[AiService, str | None]:
    """The server's connections that still exist; ids of removed ones are forgotten."""
    existing = {c.id for c in await ai_settings.list_connections(db, ctx)}
    for service, connection_id in _ids(server).items():
        if connection_id and connection_id not in existing:
            _set_id(server, service, None)
    return _ids(server)


def _skip_reason(service: AiService, raw: dict[str, Any] | None, secrets: Secrets) -> str | None:
    if raw is None:
        return "The server does not offer it."
    if raw.get("state") == "loading":
        return "It is still loading its model. Check the server again in a few minutes."
    if not raw.get("up"):
        return f"The server reports it down ({raw.get('error') or 'no answer'})."
    if service == "live_stt":
        missing = [
            label
            for label, value in (
                ("the live speech-to-text secret", secrets.live_stt_secret),
                ("a Deepgram key", secrets.deepgram_key),
            )
            if not value
        ]
        if missing:
            return f"Give {' and '.join(missing)} to use it."
    return None


def _connection_data(server: AiServer, service: AiService, secrets: Secrets) -> ConnectionCreate:
    name = _connection_name(server.name, service)
    if service == "live_stt":
        # Deepgram's protocol: the voice service appends /v1/listen to this base URL and
        # sends the key as "Authorization: Token <key>", which the gateway does not check.
        return ConnectionCreate(
            name=name,
            provider="deepgram",
            credentials={"api_key": secrets.deepgram_key or ""},
            connection_settings={
                "base_url": _live_url(server.base_url, secrets.live_stt_secret or "")
            },
        )
    return ConnectionCreate(
        name=name,
        provider="speaches",
        credentials={"api_key": secrets.token},
        connection_settings={"base_url": server.base_url + PATHS[service]},
    )


def _refused_change(service: AiService, exc: AppError, secrets: Secrets) -> ValidationFailedError:
    reason = secrets.scrub(exc.message)
    return ValidationFailedError(
        f"The voice service could not use the server's {TITLES[service].lower()}: {reason}",
        details={"field": "deepgram_key" if service == "live_stt" else "url"},
    )


async def _delete_connections(db: AsyncSession, ctx: TenantContext, ids: list[str]) -> None:
    async with runtime_client(db, ctx.org_id) as client:
        for connection_id in ids:
            try:
                await client.delete_connection(connection_id)
            except AppError:
                log.warning("ai_server_connection_cleanup_failed")


async def _setup(
    db: AsyncSession,
    ctx: TenantContext,
    server: AiServer,
    result: Probe,
    secrets: Secrets,
    *,
    strict: bool,
    services: tuple[AiService, ...] = SERVICES,
) -> list[AiServerSetup]:
    """Create a connection for each service that is up and has none yet.

    strict: a connection the voice service refuses undoes the ones just created and fails
    with the reason. Otherwise it is reported as skipped.
    """
    setup: list[AiServerSetup] = []
    created: list[str] = []
    for service in services:
        if _ids(server)[service]:
            continue
        reason = _skip_reason(service, result.services.get(service), secrets)
        if reason is not None:
            setup.append(AiServerSetup(service=service, created=False, reason=reason))
            continue
        data = _connection_data(server, service, secrets)
        try:
            # The voice service checks the connection with the server before saving it.
            conn = await ai_settings.create_connection(db, ctx, data)
        except RuntimeRejectedError as exc:
            if strict:
                await _delete_connections(db, ctx, created)
                raise _refused_change(service, exc, secrets) from None
            setup.append(
                AiServerSetup(service=service, created=False, reason=secrets.scrub(exc.message))
            )
            continue
        except AppError:
            if strict:
                await _delete_connections(db, ctx, created)
            raise
        created.append(conn.id)
        _set_id(server, service, conn.id)
        setup.append(
            AiServerSetup(
                service=service, created=True, connection_id=conn.id, connection_name=conn.name
            )
        )
    return setup


async def _update_connection(
    db: AsyncSession,
    ctx: TenantContext,
    service: AiService,
    connection_id: str,
    data: ConnectionUpdate,
    secrets: Secrets,
) -> None:
    try:
        await ai_settings.update_connection(db, ctx, connection_id, data)
    except RuntimeRejectedError as exc:
        raise _refused_change(service, exc, secrets) from None


# ------------------------------------------------------------------ reads


async def list_servers(db: AsyncSession, ctx: TenantContext) -> list[AiServerOut]:
    rows = await db.scalars(
        select(AiServer)
        .where(AiServer.organization_id == ctx.org_id)
        .order_by(AiServer.created_at, AiServer.id)
    )
    return [_out(s) for s in rows]


async def get_server(db: AsyncSession, ctx: TenantContext, server_id: uuid.UUID) -> AiServerOut:
    return _out(await _get(db, ctx, server_id))


# ------------------------------------------------------------------ register, check, update


async def register(
    db: AsyncSession, ctx: TenantContext, data: AiServerCreate
) -> AiServerRegistered:
    url = _normalise(await check_address(data.url))
    taken = await db.scalar(
        select(AiServer.name).where(
            AiServer.organization_id == ctx.org_id, AiServer.base_url == url
        )
    )
    if taken is not None:
        raise ConflictError(
            f"This AI server is already registered as {taken}.", details={"field": "url"}
        )
    secrets = Secrets(
        token=data.token.get_secret_value(),
        live_stt_secret=data.live_stt_secret.get_secret_value() if data.live_stt_secret else None,
        deepgram_key=data.deepgram_key.get_secret_value() if data.deepgram_key else None,
    )
    result = await probe(url, secrets.token)
    _refuse_unusable(result)
    if secrets.live_stt_secret:
        await _check_live_secret(url, secrets.live_stt_secret, result)
    box = get_secret_box()
    server = AiServer(
        organization_id=ctx.org_id,
        name=data.name,
        base_url=url,
        token_encrypted=box.encrypt(secrets.token),
        live_stt_secret_encrypted=(
            box.encrypt(secrets.live_stt_secret) if secrets.live_stt_secret else None
        ),
        last_health={},
        created_by=ctx.user_id,
    )
    _apply(server, result)
    setup = await _setup(db, ctx, server, result, secrets, strict=True)
    try:
        async with db.begin_nested():
            db.add(server)
            await db.flush()
    except IntegrityError:
        await _delete_connections(db, ctx, [s.connection_id for s in setup if s.connection_id])
        raise ConflictError(
            "This AI server was just registered by someone else.", details={"field": "url"}
        ) from None
    await db.refresh(server)
    await audit.record(
        db,
        action="ai_server.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_server",
        resource_id=server.id,
        changes={
            "name": server.name,
            "url": server.base_url,
            "status": server.status,
            "connections": {s.service: s.connection_id for s in setup if s.created},
            "skipped": {s.service: s.reason for s in setup if not s.created},
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return AiServerRegistered(**_out(server).model_dump(), setup=setup)


async def check_now(db: AsyncSession, ctx: TenantContext, server_id: uuid.UUID) -> AiServerOut:
    server = await _get(db, ctx, server_id)
    secrets = _stored_secrets(server)
    result = await probe(server.base_url, secrets.token)
    await _record(db, server, result, ctx)
    if result.services:
        # A service that was down or loading when the server was registered gets its
        # connection once it is up (live speech-to-text needs the Deepgram key: PATCH).
        setup = await _setup(
            db, ctx, server, result, secrets, strict=False, services=OPENAI_SERVICES
        )
        added = {s.service: s.connection_id for s in setup if s.created}
        if added:
            await audit.record(
                db,
                action="ai_server.updated",
                organization_id=ctx.org_id,
                actor_user_id=ctx.user_id,
                resource_type="ai_server",
                resource_id=server.id,
                changes={"fields": ["connections"], "connections": added},
                ip=ctx.meta.ip,
                user_agent=ctx.meta.user_agent,
            )
    await db.flush()
    await db.refresh(server)
    return _out(server)


async def update(
    db: AsyncSession, ctx: TenantContext, server_id: uuid.UUID, data: AiServerUpdate
) -> AiServerOut:
    server = await _get(db, ctx, server_id)
    stored = _stored_secrets(server)
    token = data.token.get_secret_value() if data.token else None
    secret = data.live_stt_secret.get_secret_value() if data.live_stt_secret else None
    key = data.deepgram_key.get_secret_value() if data.deepgram_key else None
    secrets = Secrets(
        token=token or stored.token,
        live_stt_secret=secret or stored.live_stt_secret,
        deepgram_key=key,
    )
    if key and not secrets.live_stt_secret:
        raise ValidationFailedError(
            "Give the live speech-to-text secret too: the Deepgram key is only used for live "
            "speech-to-text.",
            details={"field": "live_stt_secret"},
        )
    result: Probe | None = None
    if token or secret or key:
        # Checked first: a wrong new token or secret is refused before anything changes.
        result = await probe(server.base_url, secrets.token)
        if result.refused and not token:
            raise ValidationFailedError(STORED_TOKEN_REFUSED, details={"field": "token"})
        _refuse_unusable(result)
        if secret:
            await _check_live_secret(server.base_url, secret, result)
    present = await _present_ids(db, ctx, server)
    live_id = present["live_stt"]
    add_live = result is not None and live_id is None and bool(key)
    if result is not None and add_live:
        reason = _skip_reason("live_stt", result.services.get("live_stt"), secrets)
        if reason is not None:
            raise ValidationFailedError(
                f"Live speech-to-text was not set up: {reason}", details={"field": "deepgram_key"}
            )
    box = get_secret_box()
    if data.name:
        server.name = data.name
    created: list[str] = []
    try:
        if result is not None and add_live:
            setup = await _setup(
                db, ctx, server, result, secrets, strict=True, services=("live_stt",)
            )
            created = [s.connection_id for s in setup if s.connection_id]
        elif live_id and (secret or key):
            await _update_connection(
                db,
                ctx,
                "live_stt",
                live_id,
                ConnectionUpdate(
                    credentials={"api_key": key} if key else None,
                    connection_settings=(
                        {"base_url": _live_url(server.base_url, secret)} if secret else None
                    ),
                ),
                secrets,
            )
        if token:
            for service in OPENAI_SERVICES:
                if connection_id := present[service]:
                    await _update_connection(
                        db,
                        ctx,
                        service,
                        connection_id,
                        ConnectionUpdate(credentials={"api_key": token}),
                        secrets,
                    )
        if data.name:
            for service, connection_id in present.items():
                if connection_id and connection_id not in created:
                    await _update_connection(
                        db,
                        ctx,
                        service,
                        connection_id,
                        ConnectionUpdate(name=_connection_name(data.name, service)),
                        secrets,
                    )
    except Exception:
        # Nothing is saved here when a step fails: do not leave a connection behind.
        await _delete_connections(db, ctx, created)
        raise
    if token:
        server.token_encrypted = box.encrypt(token)
    if secret:
        server.live_stt_secret_encrypted = box.encrypt(secret)
    if result is not None:
        await _record(db, server, result, ctx)
    if token:
        await audit.record(
            db,
            action="ai_server.token_rotated",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="ai_server",
            resource_id=server.id,
            changes={
                "connections": sorted(s for s in OPENAI_SERVICES if present[s]),
            },
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
    fields = [f for f in ("name", "live_stt_secret", "deepgram_key") if getattr(data, f)]
    if fields:
        await audit.record(
            db,
            action="ai_server.updated",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="ai_server",
            resource_id=server.id,
            changes={"fields": fields},
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
    await db.flush()
    await db.refresh(server)
    return _out(server)


# ------------------------------------------------------------------ use for the organization


def _pick(models: list[str], preferred: str) -> str:
    return preferred if preferred in models or not models else models[0]


async def _voice(
    db: AsyncSession,
    ctx: TenantContext,
    current: ModelSelection,
    tts_connection_id: str,
    default: str,
) -> str:
    """The organization's current voice when this server has it, else the server's default."""
    from app.services.voice_cloning import (  # imports this module
        is_expressive_connection,
        is_pocket_connection,
    )

    if current.tts is None:
        return default
    voice = current.tts.settings.get("voice")
    if not isinstance(voice, str) or not voice:
        return default
    if voice.startswith("custom-"):
        # Cloned voices live on one server: only the voice cloned on this one is here.
        owner_connection = await db.scalar(
            select(CustomVoice.connection_id).where(
                CustomVoice.organization_id == ctx.org_id, CustomVoice.voice_id == voice
            )
        )
        return voice if owner_connection == tts_connection_id else default
    connections = {c.id: c for c in await ai_settings.list_connections(db, ctx)}
    in_use = connections.get(current.tts.connection_id)
    # Built-in voices are the same on every Pocket TTS server (an expressive one has others).
    if in_use is None or not is_pocket_connection(in_use) or is_expressive_connection(in_use):
        return default
    return voice


async def use(
    db: AsyncSession, ctx: TenantContext, server_id: uuid.UUID, data: AiServerUse
) -> ModelSelection:
    server = await _get(db, ctx, server_id)
    present = await _present_ids(db, ctx, server)
    health = _health(server)
    llm_id, tts_id = present["llm"], present["tts"]
    stt_id = (present["live_stt"] if data.use_live_stt else None) or present["whisper"]
    if not (llm_id and stt_id and tts_id):
        missing = [
            label
            for label, connection_id in (
                ("language model", llm_id),
                ("speech-to-text", stt_id),
                ("Pocket TTS", tts_id),
            )
            if not connection_id
        ]
        raise ValidationFailedError(
            f"This server has no connection for: {', '.join(missing)}. Check the server: "
            "ATLAS adds a connection for each service that is up."
        )
    llm_models = (health.get("llm") or {}).get("models") or []
    model = data.llm_model or (llm_models[0] if llm_models else None)
    if model is None:
        raise ValidationFailedError(
            "Choose the language model: the server did not list its models.",
            details={"field": "llm_model"},
        )
    if llm_models and model not in llm_models:
        raise ValidationFailedError(
            f"This server does not offer {model}. It offers: {', '.join(llm_models[:10])}.",
            details={"field": "llm_model"},
        )
    if stt_id == present["live_stt"]:
        stt = ServiceChoice(
            connection_id=stt_id, settings={"model": STREAMING_STT_MODEL, "language": "en"}
        )
    else:
        whisper_models = (health.get("whisper") or {}).get("models") or []
        stt = ServiceChoice(
            connection_id=stt_id, settings={"model": _pick(whisper_models, SELF_HOSTED_STT_MODEL)}
        )
    current = await ai_settings.get_selection(db, ctx)
    tts_health = health.get("tts") or {}
    voice = await _voice(
        db, ctx, current, tts_id, tts_health.get("default_voice") or DEFAULT_POCKET_VOICE
    )
    tts = ServiceChoice(
        connection_id=tts_id,
        settings={"model": _pick(tts_health.get("models") or [], POCKET_MODEL), "voice": voice},
    )
    llm = ServiceChoice(
        connection_id=llm_id, settings={"model": model, "temperature": LLM_TEMPERATURE}
    )
    fallback: LlmFallback | None = None
    if data.fallback_llm_connection_id:
        fallback_id = data.fallback_llm_connection_id
        connections = {c.id: c for c in await ai_settings.list_connections(db, ctx)}
        target = connections.get(fallback_id)
        if fallback_id in {c for c in present.values() if c}:
            raise ValidationFailedError(
                "Choose a language model on another connection as the fallback.",
                details={"field": "fallback_llm_connection_id"},
            )
        if target is None or target.provider not in ai_settings.SUPPORTED["llm"]:
            raise ValidationFailedError(
                "Unknown language model connection for the fallback.",
                details={"field": "fallback_llm_connection_id"},
            )
        if data.fallback_llm_model:
            fallback_settings: dict[str, Any] = {"model": data.fallback_llm_model}
        elif current.llm and current.llm.connection_id == fallback_id:
            fallback_settings = dict(current.llm.settings)
        else:
            fallback_settings = {}
        fallback = LlmFallback(
            connection_id=fallback_id,
            settings=fallback_settings,
            after_ms=data.fallback_after_ms,
            on_error=True,
        )
    selection = await ai_settings.save_selection(
        db,
        ctx,
        ModelSelectionUpdate(
            llm=llm, stt=stt, tts=tts, embeddings=current.embeddings, llm_fallback=fallback
        ),
    )
    await audit.record(
        db,
        action="ai_server.used",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_server",
        resource_id=server.id,
        changes={
            "llm_model": model,
            "speech_to_text": "live_stt" if stt_id == present["live_stt"] else "whisper",
            "voice": voice,
            "fallback": (
                {"connection_id": fallback.connection_id, "after_ms": fallback.after_ms}
                if fallback
                else None
            ),
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return selection


# ------------------------------------------------------------------ remove


async def _agents_using(db: AsyncSession, org_id: uuid.UUID, ids: list[str]) -> list[str]:
    def override(kind: str) -> Any:
        return func.jsonb_extract_path_text(
            AgentVersion.config, "model_override", kind, "connection_id"
        )

    names = await db.scalars(
        select(Agent.name)
        .join(AgentVersion, AgentVersion.agent_id == Agent.id)
        .where(
            Agent.organization_id == org_id,
            AgentVersion.organization_id == org_id,
            Agent.status != AgentStatus.ARCHIVED,
            or_(
                AgentVersion.version == Agent.latest_version,
                AgentVersion.version == Agent.published_version,
            ),
            or_(override("llm").in_(ids), override("tts").in_(ids)),
        )
        .distinct()
        .order_by(Agent.name)
        .limit(5)
    )
    return list(names)


async def _uses(db: AsyncSession, ctx: TenantContext, ids: list[str]) -> list[str]:
    if not ids:
        return []
    selection = await ai_settings.get_selection(db, ctx)
    labels = {
        "llm": "language model",
        "stt": "speech-to-text",
        "tts": "voice",
        "embeddings": "knowledge search",
    }
    kinds = [
        label
        for kind, label in labels.items()
        if (choice := getattr(selection, kind)) is not None and choice.connection_id in ids
    ]
    if selection.llm_fallback is not None and selection.llm_fallback.connection_id in ids:
        kinds.append("fallback language model")
    places: list[str] = []
    if kinds:
        places.append(f"the organization's models for calls ({', '.join(kinds)})")
    agents = await _agents_using(db, ctx.org_id, ids)
    if agents:
        places.append(f"{'agent' if len(agents) == 1 else 'agents'} {', '.join(agents)}")
    return places


async def delete(db: AsyncSession, ctx: TenantContext, server_id: uuid.UUID) -> None:
    server = await _get(db, ctx, server_id)
    ids = [c for c in _ids(server).values() if c]
    places = await _uses(db, ctx, ids)
    if places:
        raise ConflictError(
            f"This AI server is in use by {' and '.join(places)}. Choose other models there "
            "first, then remove the server.",
            details={"in_use": places},
        )
    existing = {c.id: c for c in await ai_settings.list_connections(db, ctx)}
    removed = [c for c in ids if c in existing]
    async with runtime_client(db, ctx.org_id) as client:
        for connection_id in removed:
            try:
                await client.delete_connection(connection_id)
            except RuntimeRejectedError as exc:
                if exc.runtime_status != 404:
                    raise
            await audit.record(
                db,
                action="ai.connection_deleted",
                organization_id=ctx.org_id,
                actor_user_id=ctx.user_id,
                resource_type="ai_connection",
                resource_id=connection_id,
                changes={"provider": existing[connection_id].provider, "ai_server": str(server.id)},
                ip=ctx.meta.ip,
                user_agent=ctx.meta.user_agent,
            )
    await audit.record(
        db,
        action="ai_server.deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_server",
        resource_id=server.id,
        changes={"name": server.name, "url": server.base_url, "connections_removed": len(removed)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.delete(server)


# ------------------------------------------------------------------ monitor


async def check_all(
    *, transport: httpx.AsyncBaseTransport | None = None, concurrency: int = MONITOR_CONCURRENCY
) -> dict[str, int]:
    """Check every registered server, a few at a time; audit status changes. Never raises
    for one server's failure."""
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as db:
        servers = list(await db.scalars(select(AiServer).order_by(AiServer.created_at)))
        if not servers:
            return {"checked": 0, "changed": 0}
        tokens: dict[uuid.UUID, str | None] = {}
        for server in servers:
            try:
                tokens[server.id] = _stored_secrets(server).token
            except RuntimeError:  # encrypted with another key
                tokens[server.id] = None
        gate = asyncio.Semaphore(concurrency)

        async def one(server: AiServer) -> Probe:
            token = tokens[server.id]
            if token is None:
                return Probe(
                    "down", {}, None, error="The stored token cannot be read. Replace the token."
                )
            async with gate:
                return await probe(server.base_url, token, transport=transport)

        results = await asyncio.gather(*(one(s) for s in servers))
        changed = 0
        for server, result in zip(servers, results, strict=True):
            changed += await _record(db, server, result)
        await db.commit()
    return {"checked": len(servers), "changed": changed}


# ------------------------------------------------------------------ for warm-up and cloning


@dataclass(frozen=True, slots=True)
class Gateway:
    """Where ATLAS itself reaches one service of a registered AI server, with its token."""

    server_name: str
    base_url: str  # the service's OpenAI-style base, e.g. https://ai.example.com/tts/v1
    token: str = field(repr=False)


async def gateway_for(
    db: AsyncSession, org_id: uuid.UUID, connection_id: str, service: Literal["llm", "tts"]
) -> Gateway | None:
    """The registered AI server behind a connection ATLAS created for it, if any."""
    column = AiServer.llm_connection_id if service == "llm" else AiServer.tts_connection_id
    server = await db.scalar(
        select(AiServer).where(AiServer.organization_id == org_id, column == connection_id)
    )
    if server is None:
        return None
    return Gateway(
        server_name=server.name,
        base_url=server.base_url + PATHS[service],
        token=get_secret_box().decrypt(server.token_encrypted),
    )
