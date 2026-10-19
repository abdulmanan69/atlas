"""The website widget: a "talk to us" button tenants put on their own sites.

Management (agent editor): one widget per agent with a public key, the sites allowed to
show it and an on/off switch; changes apply at once, without publishing.

Visitors' browsers never talk to the voice service's API and never see its credentials or
name. The widget script (served by ATLAS) asks ATLAS for a session; ATLAS checks the key,
the page's origin against the allowed sites and per-visitor and per-widget rate limits,
records the conversation as an inbound call, opens a voice session server-side and
returns only what the browser needs for audio: a one-time signaling address and ICE
servers, exactly like the editor's browser test calls. Deployment requirement: the voice
service's signaling address (`ATLAS_VOICE_RUNTIME_PUBLIC_URL`) must be reachable by
visitors, and the voice service checks the page's origin there too, so ATLAS keeps the
widget's sites on the voice session token (`ensure_embed_token`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.redis import enforce_rate_limit
from app.integrations.voice_runtime.client import RuntimeRejectedError, RuntimeUnavailableError
from app.models.agent_settings import AgentWebEmbed
from app.models.calls import TERMINAL_STATES, Call, CallDirection, CallMode, CallState
from app.models.voice import Agent, AgentStatus
from app.schemas.agent_settings import WebCallSession, WebEmbedOut, WebEmbedUpdate
from app.schemas.voice import AgentConfig, IceServer
from app.services import audit, jobs
from app.services.calls import (
    TEST_SYNC_DELAY_SECONDS,
    _emit,
    _event,
    _event_data,
    _set_state,
)
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

SESSION_SECONDS = 3600
SESSIONS_PER_VISITOR_PER_MINUTE = 6
SESSIONS_PER_WIDGET_PER_MINUTE = 60
ENDS_PER_VISITOR_PER_MINUTE = 30
WIDGET_PATH = "/api/v1/public/embed/widget.js"
SOURCE = "website"


# ---------------------------------------------------------------- origins


def _host_port(value: str) -> tuple[str, int | None]:
    """Host and explicit port of an origin (`https://a.b:8443`) or an allowed domain."""
    raw = value.strip().lower()
    if "://" not in raw:
        raw = f"//{raw}"
    try:
        parts = urlsplit(raw)
        return (parts.hostname or "", parts.port)
    except ValueError:
        return ("", None)


def _www(host: str) -> set[str]:
    return {host, host[4:]} if host.startswith("www.") else {host, f"www.{host}"}


def origin_allowed(origin: str | None, allowed: list[str]) -> bool:
    """Whether a page origin is one of the widget's sites.

    Same rules as the voice service's own check (embed_session_service.py
    `validate_embed_origin`), so a page ATLAS admits is never refused when its audio
    connects: `www.` is optional, `*.example.com` covers example.com and its subdomains,
    and a port, when given, must match. Unlike it, an empty list allows nothing.
    """
    if not origin or not allowed:
        return False
    host, port = _host_port(origin)
    if not host:
        return False
    for entry in allowed:
        wildcard = entry.startswith("*.")
        a_host, a_port = _host_port(entry[2:] if wildcard else entry)
        if not a_host or (a_port is not None and a_port != port):
            continue
        if wildcard:
            if host == a_host or host.endswith(f".{a_host}"):
                return True
        elif _www(host) & _www(a_host):
            return True
    return False


def request_origin(origin: str | None, referer: str | None) -> str | None:
    """The page's origin: the Origin header, else the scheme and host of the Referer."""
    if origin and origin != "null":
        return origin
    if referer:
        parts = urlsplit(referer)
        if parts.scheme in ("http", "https") and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return None


# ---------------------------------------------------------------- management


def _new_key() -> str:
    return f"wk_{secrets.token_urlsafe(24)}"


def public_base_url() -> str:
    """Where browsers reach this API (the web app proxies /api when not set)."""
    settings = get_settings()
    return (settings.public_api_url or settings.web_app_origin).rstrip("/")


def snippet(key: str) -> str:
    return f'<script src="{public_base_url()}{WIDGET_PATH}" data-agent-key="{key}" async></script>'


def to_out(embed: AgentWebEmbed | None, agent: Agent) -> WebEmbedOut:
    return WebEmbedOut(
        enabled=bool(embed and embed.enabled),
        allowed_domains=list(embed.allowed_domains) if embed else [],
        key=embed.key if embed else None,
        script_url=f"{public_base_url()}{WIDGET_PATH}",
        snippet=snippet(embed.key) if embed else None,
        sessions_started=embed.sessions_started if embed else 0,
        last_used_at=embed.last_used_at if embed else None,
        rotated_at=embed.rotated_at if embed else None,
        agent_published=agent.published_version is not None and agent.status == AgentStatus.ACTIVE,
    )


async def _agent(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.organization_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    return agent


async def _embed(db: AsyncSession, agent: Agent) -> AgentWebEmbed | None:
    return (
        await db.execute(
            select(AgentWebEmbed).where(
                AgentWebEmbed.agent_id == agent.id,
                AgentWebEmbed.organization_id == agent.organization_id,
            )
        )
    ).scalar_one_or_none()


async def get_embed(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> WebEmbedOut:
    agent = await _agent(db, ctx, agent_id)
    return to_out(await _embed(db, agent), agent)


def _session_domains(embed: AgentWebEmbed | None) -> list[str]:
    """Sites the voice session token allows: the web app (test calls) plus the widget's."""
    web = urlsplit(get_settings().web_app_origin).netloc
    widget = list(embed.allowed_domains) if embed and embed.enabled else []
    return list(dict.fromkeys([web, *widget]))


async def _sync_runtime_domains(db: AsyncSession, agent: Agent, embed: AgentWebEmbed) -> None:
    """Best effort: sessions re-check, so a voice service outage never blocks a save."""
    if not agent.runtime_workflow_id:
        return
    try:
        async with runtime_client(db, agent.organization_id) as client:
            await client.ensure_embed_token(
                agent.runtime_workflow_id, _session_domains(embed), replace=True
            )
    except (RuntimeUnavailableError, RuntimeRejectedError):
        log.warning("web_embed_domains_not_synced", extra={"agent_id": str(agent.id)})


async def update_embed(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, data: WebEmbedUpdate
) -> WebEmbedOut:
    agent = await _agent(db, ctx, agent_id)
    if data.enabled and not data.allowed_domains:
        raise ValidationFailedError("Add at least one website the widget may appear on")
    embed = await _embed(db, agent)
    if embed is None:
        embed = AgentWebEmbed(
            organization_id=ctx.org_id, agent_id=agent.id, key=_new_key(), allowed_domains=[]
        )
        db.add(embed)
    before = {"enabled": embed.enabled, "allowed_domains": list(embed.allowed_domains or [])}
    embed.enabled = data.enabled
    embed.allowed_domains = data.allowed_domains
    await db.flush()
    await audit.record(
        db,
        action="agent.web_embed_updated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={
            "before": before,
            "after": {"enabled": data.enabled, "allowed_domains": data.allowed_domains},
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await _sync_runtime_domains(db, agent, embed)
    await db.refresh(embed)
    return to_out(embed, agent)


async def rotate_key(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> WebEmbedOut:
    """A new public key: snippets with the old key stop working at once."""
    agent = await _agent(db, ctx, agent_id)
    embed = await _embed(db, agent)
    if embed is None:
        raise NotFoundError("This agent has no website widget yet")
    embed.key = _new_key()
    embed.rotated_at = datetime.now(UTC)
    await audit.record(
        db,
        action="agent.web_embed_key_rotated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(embed)
    return to_out(embed, agent)


# ---------------------------------------------------------------- public sessions


def call_token(call_id: uuid.UUID) -> str:
    """Bearer proof for ending one widget call; derived, never stored."""
    secret = get_settings().jwt_secret.get_secret_value().encode()
    digest = hmac.new(secret, f"web-call:{call_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


_UNAVAILABLE = "This assistant is not available right now."


async def _public_embed(db: AsyncSession, key: str) -> AgentWebEmbed:
    embed = (
        await db.execute(select(AgentWebEmbed).where(AgentWebEmbed.key == key))
    ).scalar_one_or_none()
    if embed is None or not embed.enabled:
        raise NotFoundError(_UNAVAILABLE)
    return embed


async def start_web_call(
    db: AsyncSession,
    key: str,
    *,
    origin: str | None,
    ip: str | None,
    user_agent: str | None,
) -> WebCallSession:
    settings = get_settings()
    await enforce_rate_limit(f"web-call-ip:{ip or 'unknown'}", SESSIONS_PER_VISITOR_PER_MINUTE)
    await enforce_rate_limit(f"web-call-key:{key[:64]}", SESSIONS_PER_WIDGET_PER_MINUTE)
    embed = await _public_embed(db, key)
    if not origin_allowed(origin, list(embed.allowed_domains)):
        raise PermissionDeniedError("This website is not allowed to use this assistant.")
    agent = await db.get(Agent, embed.agent_id)
    if (
        agent is None
        or agent.organization_id != embed.organization_id
        or agent.status != AgentStatus.ACTIVE
        or agent.published_version is None
        or not agent.runtime_workflow_id
    ):
        raise InvalidStateTransitionError(_UNAVAILABLE)
    from app.services.agents import config_of  # local import: agents imports this package

    config: AgentConfig = await config_of(db, agent, agent.published_version)
    host = urlsplit(origin or "").netloc
    call = Call(
        organization_id=agent.organization_id,
        agent_id=agent.id,
        agent_version=agent.published_version,
        direction=CallDirection.INBOUND,  # the visitor calls in, from the website
        mode=CallMode.LIVE,
        state=CallState.QUEUED,
        runtime_workflow_id=agent.runtime_workflow_id,
    )
    db.add(call)
    await db.flush()
    _event(
        db,
        call,
        "created",
        {
            "direction": call.direction.value,
            "mode": call.mode.value,
            "source": SOURCE,
            "site": host,
        },
    )
    context = {"atlas_call_id": str(call.id), "source": SOURCE}
    async with runtime_client(db, agent.organization_id) as client:
        token = await client.ensure_embed_token(agent.runtime_workflow_id, _session_domains(embed))
        # Server-to-server: ATLAS's own origin is on the token; the visitor's is checked
        # by the voice service when the browser's audio connects.
        session = await client.start_embed_session(
            token, origin=settings.web_app_origin.rstrip("/"), context=context
        )
        ice = await client.turn_servers(session.session_token)
    call.runtime_run_id = str(session.workflow_run_id)
    call.started_at = datetime.now(UTC)
    _set_state(db, call, CallState.IN_PROGRESS, reason="web_session")
    await _emit(db, call, "call.started", _event_data(call))
    await db.execute(
        update(AgentWebEmbed)
        .where(AgentWebEmbed.id == embed.id)
        .values(sessions_started=AgentWebEmbed.sessions_started + 1, last_used_at=datetime.now(UTC))
    )
    await audit.record(
        db,
        action="agent.web_call_started",
        organization_id=agent.organization_id,
        actor_user_id=None,
        resource_type="call",
        resource_id=call.id,
        changes={"agent_id": str(agent.id), "site": host},
        ip=ip,
        user_agent=user_agent,
    )
    public = urlsplit(settings.voice_runtime_public_url)
    scheme = "wss" if public.scheme == "https" else "ws"
    signaling = (
        f"{scheme}://{public.netloc}{public.path.rstrip('/')}"
        f"/api/v1/ws/public/signaling/{session.session_token}"
    )
    servers = [IceServer.model_validate(s) for s in ice] or [
        IceServer(urls="stun:stun.l.google.com:19302")
    ]
    return WebCallSession(
        call_id=call.id,
        call_token=call_token(call.id),
        signaling_url=signaling,
        ice_servers=servers,
        expires_in_seconds=SESSION_SECONDS,
        agent_name=config.persona_name,
        company_name=config.company_name,
    )


async def end_web_call(
    db: AsyncSession,
    key: str,
    call_id: uuid.UUID,
    *,
    token: str,
    connected: bool,
    ip: str | None,
) -> None:
    """The widget reports that a call ended. Idempotent; a later real result still wins."""
    await enforce_rate_limit(f"web-call-end:{ip or 'unknown'}", ENDS_PER_VISITOR_PER_MINUTE)
    if not hmac.compare_digest(token, call_token(call_id)):
        raise NotFoundError("Call not found")
    embed = (
        await db.execute(select(AgentWebEmbed).where(AgentWebEmbed.key == key))
    ).scalar_one_or_none()
    call = await db.get(Call, call_id)
    if (
        embed is None
        or call is None
        or call.agent_id != embed.agent_id
        or call.organization_id != embed.organization_id
        or call.mode != CallMode.LIVE
    ):
        raise NotFoundError("Call not found")
    if call.state in TERMINAL_STATES:
        return
    if not connected:
        call.error_code = "not_connected"
        call.error_message = "The visitor's browser never connected to the agent"
        call.ended_at = call.ended_at or datetime.now(UTC)
        _set_state(db, call, CallState.CANCELED, reason="not_connected")
        return
    if call.runtime_run_id and call.runtime_workflow_id:
        try:
            await jobs.enqueue(
                "process_runtime_run",
                str(call.organization_id),
                str(call.id),
                call.runtime_workflow_id,
                call.runtime_run_id,
                job_id=f"web-call-ended:{call.id}",
                defer_by=TEST_SYNC_DELAY_SECONDS,
            )
        except jobs.QueueUnavailableError:
            log.warning("web_call_sync_not_scheduled", extra={"call_id": str(call.id)})
