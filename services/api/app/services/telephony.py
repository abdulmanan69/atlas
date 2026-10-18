"""Calling providers and phone numbers.

The runtime stores provider credentials and talks to the carrier; ATLAS curates which
providers are offered, keeps secrets out of its responses, logs and audit trail, and maps
inbound routing onto ATLAS agents. Everything is read through from the runtime; no copy
is kept here.

Inbound routing is per phone number (verified: the runtime's dispatcher resolves the
workflow from the called number's ``inbound_workflow_id``), so "which agent answers this
number" is exactly the number's workflow, and an ATLAS agent maps to its published
workflow (``Agent.runtime_workflow_id``).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.core.phone import InvalidPhoneError, display_phone, normalize_phone
from app.integrations.voice_runtime import telephony_api as api
from app.integrations.voice_runtime.client import RuntimeRejectedError, VoiceRuntimeClient
from app.integrations.voice_runtime.scrub import scrub_text
from app.models.voice import Agent, AgentStatus
from app.schemas.telephony import (
    Connectivity,
    CredentialField,
    InboundAgentAssignment,
    NumberCreate,
    NumberOut,
    NumberSyncResult,
    ProviderCreate,
    ProviderOut,
    ProviderSyncOut,
    ProviderUpdate,
    SetupInstructions,
    SkippedNumberOut,
    TelephonyProviderOption,
)
from app.services import agents as agent_service
from app.services import audit
from app.services.runtime_accounts import runtime_client

INBOUND_PATH = "/api/v1/telephony/inbound/run"
FALLBACK_PATH = "/api/v1/telephony/inbound/fallback"
VONAGE_EVENTS_PATH = "/api/v1/telephony/vonage/events"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"})  # noqa: S104


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    title: str
    connectivity: Connectivity
    supports_outbound: bool
    fields: tuple[CredentialField, ...]
    # The runtime writes the inbound webhook into the provider when an agent is assigned.
    automatic: bool
    steps: tuple[str, ...]
    notes: tuple[str, ...]
    fallback: bool = False
    event_path: str | None = None

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields)

    @property
    def required(self) -> frozenset[str]:
        return frozenset(f.name for f in self.fields if f.required)


# Field names and which are required follow the runtime's per-provider request schemas
# exactly (providers/<name>/config.py). Labels and help are ATLAS copy.
PROVIDERS: dict[str, ProviderSpec] = {
    "twilio": ProviderSpec(
        id="twilio",
        title="Twilio",
        connectivity="api",
        supports_outbound=True,
        fields=(
            CredentialField(
                name="account_sid",
                label="Account SID",
                secret=True,
                help="Starts with AC; Twilio Console → Account Info",
            ),
            CredentialField(name="auth_token", label="Auth Token", secret=True),
        ),
        automatic=True,
        fallback=True,
        steps=(
            "In Twilio Console open Phone Numbers → Manage → Active numbers and pick the number.",
            "Under Voice & Fax → A call comes in choose Webhook, paste {inbound_url}, HTTP POST.",
            "Under Primary handler fails paste {fallback_url}, HTTP POST. Save.",
        ),
        notes=(
            "Once an agent is assigned to a number here, the voice service sets the number's "
            "webhook in Twilio for you; the manual steps are only needed if that fails.",
        ),
    ),
    "vonage": ProviderSpec(
        id="vonage",
        title="Vonage",
        connectivity="api",
        supports_outbound=True,
        fields=(
            CredentialField(name="api_key", label="API key", secret=True),
            CredentialField(name="api_secret", label="API secret", secret=True),
            CredentialField(
                name="application_id",
                label="Application ID",
                secret=False,
                help="A Vonage Voice application; its numbers are routed through it",
            ),
            CredentialField(
                name="private_key",
                label="Private key",
                secret=True,
                multiline=True,
                help="The application's private.key file contents",
            ),
            CredentialField(
                name="signature_secret",
                label="Signature secret",
                secret=True,
                required=False,
                help="Enables signed webhook verification",
            ),
        ),
        automatic=True,
        event_path=VONAGE_EVENTS_PATH,
        steps=(
            "In the Vonage dashboard open Applications and select the application above.",
            "Under Voice set Answer URL to {inbound_url} (POST) and Event URL to {event_url} "
            "(POST). Save.",
            "Under Link numbers link each number you want answered by an agent.",
        ),
        notes=(
            "All numbers linked to one application share its Answer URL; the voice service "
            "updates it when an agent is assigned.",
        ),
    ),
    "plivo": ProviderSpec(
        id="plivo",
        title="Plivo",
        connectivity="api",
        supports_outbound=True,
        fields=(
            CredentialField(name="auth_id", label="Auth ID", secret=True),
            CredentialField(name="auth_token", label="Auth Token", secret=True),
            CredentialField(
                name="application_id",
                label="Application ID",
                secret=False,
                required=False,
                help="Leave empty to have an application created for you",
            ),
        ),
        automatic=True,
        steps=(
            "In Plivo Console open Voice → Applications and select the application used here.",
            "Set Answer URL to {inbound_url}, method POST. Save.",
            "Open Phone Numbers → Your Numbers and attach each number to that application.",
        ),
        notes=(
            "All numbers attached to one application share its Answer URL; the voice service "
            "updates it when an agent is assigned.",
        ),
    ),
    "telnyx": ProviderSpec(
        id="telnyx",
        title="Telnyx",
        connectivity="api",
        supports_outbound=True,
        fields=(
            CredentialField(name="api_key", label="API key", secret=True),
            CredentialField(
                name="connection_id",
                label="Call Control App ID",
                secret=False,
                required=False,
                help="Leave empty to have a Call Control application created for you",
            ),
            CredentialField(
                name="webhook_public_key",
                label="Webhook public key",
                secret=False,
                required=False,
                help="Mission Control Portal → Keys & Credentials → Public Key; verifies "
                "webhook signatures",
            ),
        ),
        automatic=True,
        steps=(
            "In Mission Control Portal open Voice → Call Control Applications and select the "
            "application used here.",
            "Set Webhook URL to {inbound_url}. Save.",
            "Open Numbers → My Numbers and assign each number to that application.",
        ),
        notes=(
            "All numbers on one Call Control application share its webhook; the voice service "
            "updates it when an agent is assigned.",
        ),
    ),
    "ari": ProviderSpec(
        id="ari",
        title="SIP trunk (Asterisk ARI)",
        connectivity="sip",
        supports_outbound=True,
        fields=(
            CredentialField(
                name="ari_endpoint",
                label="ARI endpoint",
                secret=False,
                help="Base URL of the Asterisk REST Interface, e.g. http://pbx.example.com:8088",
            ),
            CredentialField(name="app_name", label="ARI username", secret=False),
            CredentialField(name="app_password", label="ARI password", secret=True),
            CredentialField(
                name="ws_client_name",
                label="websocket_client.conf name",
                secret=False,
                required=False,
                help="Connection name Asterisk uses for the audio stream",
            ),
            CredentialField(
                name="dial_string_template",
                label="Dial string template",
                secret=False,
                required=False,
                help="How a number becomes a dial string, e.g. PJSIP/{number}@my-trunk",
            ),
        ),
        automatic=False,
        steps=(
            "Enable ARI on your Asterisk and create a user matching the username and "
            "password above.",
            "Make the ARI endpoint reachable from the voice service and allow the voice "
            "service's address through your firewall.",
            "Route inbound DIDs from your SIP trunk into the Stasis application; add each DID "
            "as a number here and assign an agent.",
        ),
        notes=("Calls on a SIP trunk use no provider webhooks; routing happens over ARI.",),
    ),
}


def catalog() -> list[TelephonyProviderOption]:
    return [
        TelephonyProviderOption(
            id=spec.id,
            title=spec.title,
            connectivity=spec.connectivity,
            supports_outbound=spec.supports_outbound,
            fields=list(spec.fields),
        )
        for spec in PROVIDERS.values()
    ]


def _spec(provider: str) -> ProviderSpec:
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ValidationFailedError("This calling provider is not supported")
    return spec


def _require_supported(detail: api.TelephonyConfigDetail) -> api.TelephonyConfigDetail:
    """Configurations the runtime creates on its own (vendor-managed connectivity) are
    not ATLAS providers: they are hidden from lists and unreachable by id."""
    if detail.provider not in PROVIDERS:
        raise NotFoundError("Calling provider not found")
    return detail


def _runtime_id(value: str, what: str) -> int:
    if not value.isdigit():
        raise NotFoundError(f"{what} not found")
    return int(value)


def _clean_credentials(spec: ProviderSpec, data: dict[str, str]) -> dict[str, str]:
    unknown = set(data) - spec.field_names
    if unknown:
        raise ValidationFailedError(f"Unsupported credential fields: {', '.join(sorted(unknown))}")
    return {k: v.strip() for k, v in data.items() if v and v.strip()}


def _require_complete(spec: ProviderSpec, credentials: dict[str, str]) -> None:
    missing = sorted(spec.required - set(credentials))
    if missing:
        labels = [f.label for f in spec.fields if f.name in missing]
        raise ValidationFailedError(f"Missing required fields: {', '.join(labels)}")


def _provider_out(
    detail: api.TelephonyConfigDetail, summary: api.TelephonyConfig | None
) -> ProviderOut:
    spec = PROVIDERS.get(detail.provider)
    configured = sorted(
        k for k, v in detail.credentials.items() if v and (spec is None or k in spec.field_names)
    )
    return ProviderOut(
        id=str(detail.id),
        name=detail.name,
        provider=detail.provider,
        provider_title=spec.title if spec else detail.provider.title(),
        connectivity="sip" if detail.connectivity == "sip" else "api",
        configured_fields=configured,
        is_default_outbound=detail.is_default_outbound,
        inactive=detail.inactive,
        inactive_reason=scrub_text(detail.inactive_reason),
        number_count=summary.phone_number_count if summary else 0,
        supports_outbound=detail.supports_outbound,
        ready_for_outbound=summary.is_ready_for_outbound if summary else detail.supports_outbound,
        outbound_blocked_reason=scrub_text(summary.outbound_blocked_reason) if summary else None,
        supports_number_sync=detail.supports_phone_number_sync,
    )


async def _summary(client: VoiceRuntimeClient, config_id: int) -> api.TelephonyConfig | None:
    return next((c for c in await api.list_configs(client) if c.id == config_id), None)


def _map_rejection(exc: RuntimeRejectedError, *, what: str) -> Exception:
    if exc.runtime_status == 404:
        return NotFoundError(f"{what} not found")
    if exc.runtime_status == 409:
        return ConflictError(exc.message)
    return exc


# ---------------------------------------------------------------- providers


async def list_providers(db: AsyncSession, ctx: TenantContext) -> list[ProviderOut]:
    async with runtime_client(db, ctx.org_id) as client:
        summaries = await api.list_configs(client)
        details = [await api.get_config(client, c.id) for c in summaries]
    return [
        _provider_out(d, s)
        for d, s in zip(details, summaries, strict=True)
        if d.provider in PROVIDERS
    ]


async def get_provider(db: AsyncSession, ctx: TenantContext, provider_id: str) -> ProviderOut:
    config_id = _runtime_id(provider_id, "Calling provider")
    async with runtime_client(db, ctx.org_id) as client:
        try:
            detail = _require_supported(await api.get_config(client, config_id))
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
        summary = await _summary(client, config_id)
    return _provider_out(detail, summary)


async def create_provider(
    db: AsyncSession, ctx: TenantContext, data: ProviderCreate
) -> ProviderOut:
    spec = _spec(data.provider)
    credentials = _clean_credentials(spec, data.credentials)
    _require_complete(spec, credentials)
    if data.is_default_outbound and not spec.supports_outbound:
        raise ValidationFailedError("This provider receives calls only")
    async with runtime_client(db, ctx.org_id) as client:
        try:
            detail = await api.create_config(
                client,
                name=data.name,
                provider=spec.id,
                credentials=credentials,
                is_default_outbound=data.is_default_outbound,
            )
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
        summary = await _summary(client, detail.id)
    await audit.record(
        db,
        action="telephony.provider_created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="telephony_provider",
        resource_id=str(detail.id),
        changes={
            "provider": spec.id,
            "name": data.name,
            "credential_fields": sorted(credentials),
            "is_default_outbound": data.is_default_outbound,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _provider_out(detail, summary)


async def update_provider(
    db: AsyncSession, ctx: TenantContext, provider_id: str, data: ProviderUpdate
) -> ProviderOut:
    config_id = _runtime_id(provider_id, "Calling provider")
    async with runtime_client(db, ctx.org_id) as client:
        try:
            current = _require_supported(await api.get_config(client, config_id))
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
        spec = _spec(current.provider)
        merged: dict[str, str] | None = None
        rotated: list[str] = []
        if data.credentials is not None:
            supplied = _clean_credentials(spec, data.credentials)
            # Fields not being replaced are sent back as the mask the runtime handed out;
            # the runtime restores the stored value for those. Secrets never pass through
            # ATLAS unless the customer just typed them.
            merged = {
                k: str(v)
                for k, v in current.credentials.items()
                if k in spec.field_names and v not in (None, "")
            }
            merged.update(supplied)
            _require_complete(spec, merged)
            rotated = sorted(supplied)
        try:
            detail = await api.update_config(
                client, config_id, name=data.name, provider=spec.id, credentials=merged
            )
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
        summary = await _summary(client, config_id)
    await audit.record(
        db,
        action="telephony.provider_updated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="telephony_provider",
        resource_id=str(config_id),
        changes={"name": data.name, "rotated_credentials": rotated},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _provider_out(detail, summary)


async def delete_provider(db: AsyncSession, ctx: TenantContext, provider_id: str) -> None:
    config_id = _runtime_id(provider_id, "Calling provider")
    async with runtime_client(db, ctx.org_id) as client:
        try:
            current = _require_supported(await api.get_config(client, config_id))
            await api.delete_config(client, config_id)
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
    await audit.record(
        db,
        action="telephony.provider_deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="telephony_provider",
        resource_id=str(config_id),
        changes={"provider": current.provider, "name": current.name},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


async def make_default_outbound(
    db: AsyncSession, ctx: TenantContext, provider_id: str
) -> ProviderOut:
    config_id = _runtime_id(provider_id, "Calling provider")
    async with runtime_client(db, ctx.org_id) as client:
        try:
            detail = await api.set_default_outbound(client, config_id)
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
        summary = await _summary(client, config_id)
    await audit.record(
        db,
        action="telephony.default_outbound_set",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="telephony_provider",
        resource_id=str(config_id),
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _provider_out(detail, summary)


# ---------------------------------------------------------------- numbers


async def _agents_by_workflow(
    db: AsyncSession, ctx: TenantContext, numbers: list[api.PhoneNumber]
) -> dict[int, Agent]:
    ids = {n.inbound_workflow_id for n in numbers if n.inbound_workflow_id is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Agent).where(
                Agent.organization_id == ctx.org_id,
                Agent.runtime_workflow_id.in_([str(i) for i in ids]),
            )
        )
    ).scalars()
    return {int(a.runtime_workflow_id): a for a in rows if a.runtime_workflow_id}


def _number_out(
    n: api.PhoneNumber, cfg: api.TelephonyConfig, agents: dict[int, Agent]
) -> NumberOut:
    agent = agents.get(n.inbound_workflow_id) if n.inbound_workflow_id is not None else None
    e164 = n.address_normalized or n.address
    return NumberOut(
        id=str(n.id),
        provider_id=str(cfg.id),
        provider=cfg.provider,
        provider_name=cfg.name,
        phone_number=e164,
        display=display_phone(e164) or e164,
        label=n.label,
        inbound_agent_id=agent.id if agent else None,
        inbound_agent_name=agent.name if agent else None,
        is_active=n.is_active,
        is_default_caller_id=n.is_default_caller_id,
        provider_sync=(
            ProviderSyncOut(ok=n.provider_sync.ok, message=scrub_text(n.provider_sync.message))
            if n.provider_sync
            else None
        ),
    )


async def _config_summary(client: VoiceRuntimeClient, provider_id: str) -> api.TelephonyConfig:
    config_id = _runtime_id(provider_id, "Calling provider")
    summary = await _summary(client, config_id)
    if summary is None:
        raise NotFoundError("Calling provider not found")
    return summary


async def _locate_number(
    client: VoiceRuntimeClient, number_id: str
) -> tuple[api.TelephonyConfig, api.PhoneNumber]:
    nid = _runtime_id(number_id, "Phone number")
    for cfg in await api.list_configs(client):
        for n in await api.list_numbers(client, cfg.id):
            if n.id == nid:
                return cfg, n
    raise NotFoundError("Phone number not found")


async def list_numbers(
    db: AsyncSession, ctx: TenantContext, *, provider_id: str | None = None
) -> list[NumberOut]:
    async with runtime_client(db, ctx.org_id) as client:
        configs = (
            [await _config_summary(client, provider_id)]
            if provider_id is not None
            else await api.list_configs(client)
        )
        found = [(cfg, n) for cfg in configs for n in await api.list_numbers(client, cfg.id)]
    agents = await _agents_by_workflow(db, ctx, [n for _, n in found])
    return [_number_out(n, cfg, agents) for cfg, n in found]


async def add_number(
    db: AsyncSession, ctx: TenantContext, provider_id: str, data: NumberCreate
) -> NumberOut:
    try:
        e164 = normalize_phone(data.phone_number, data.default_region)
    except InvalidPhoneError as exc:
        raise ValidationFailedError(str(exc)) from exc
    async with runtime_client(db, ctx.org_id) as client:
        cfg = await _config_summary(client, provider_id)
        try:
            number = await api.create_number(client, cfg.id, address=e164, label=data.label)
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
    await audit.record(
        db,
        action="telephony.number_added",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="phone_number",
        resource_id=str(number.id),
        changes={"provider_id": str(cfg.id), "phone_number": e164, "label": data.label},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _number_out(number, cfg, {})


async def remove_number(db: AsyncSession, ctx: TenantContext, number_id: str) -> None:
    async with runtime_client(db, ctx.org_id) as client:
        cfg, number = await _locate_number(client, number_id)
        try:
            await api.delete_number(client, cfg.id, number.id)
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Phone number") from exc
    await audit.record(
        db,
        action="telephony.number_removed",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="phone_number",
        resource_id=str(number.id),
        changes={"provider_id": str(cfg.id), "phone_number": number.address_normalized},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


async def assign_inbound_agent(
    db: AsyncSession, ctx: TenantContext, number_id: str, data: InboundAgentAssignment
) -> NumberOut:
    agent: Agent | None = None
    workflow_id: int | None = None
    if data.agent_id is not None:
        agent = await agent_service.get_agent(db, ctx, data.agent_id)
        if agent.status == AgentStatus.ARCHIVED:
            raise ValidationFailedError("An archived agent cannot answer calls")
        if not agent.runtime_workflow_id or agent.published_version is None:
            raise ValidationFailedError("Publish the agent before assigning it to a number")
        workflow_id = int(agent.runtime_workflow_id)
    async with runtime_client(db, ctx.org_id) as client:
        cfg, number = await _locate_number(client, number_id)
        try:
            updated = await api.set_inbound_workflow(
                client, cfg.id, number.id, workflow_id=workflow_id
            )
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Phone number") from exc
    await audit.record(
        db,
        action="telephony.inbound_agent_assigned",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="phone_number",
        resource_id=str(number.id),
        changes={
            "phone_number": number.address_normalized,
            "agent_id": str(agent.id) if agent else None,
            "provider_sync_ok": updated.provider_sync.ok if updated.provider_sync else None,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    agents = {workflow_id: agent} if agent and workflow_id is not None else {}
    return _number_out(updated, cfg, agents)


async def sync_numbers(db: AsyncSession, ctx: TenantContext, provider_id: str) -> NumberSyncResult:
    async with runtime_client(db, ctx.org_id) as client:
        cfg = await _config_summary(client, provider_id)
        try:
            result = await api.sync_numbers(client, cfg.id)
        except RuntimeRejectedError as exc:
            raise _map_rejection(exc, what="Calling provider") from exc
    agents = await _agents_by_workflow(
        db, ctx, [*result.created, *result.updated, *result.missing_at_provider]
    )
    await audit.record(
        db,
        action="telephony.numbers_synced",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="telephony_provider",
        resource_id=str(cfg.id),
        changes={"added": len(result.created), "updated": len(result.updated)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return NumberSyncResult(
        added=[_number_out(n, cfg, agents) for n in result.created],
        updated=[_number_out(n, cfg, agents) for n in result.updated],
        skipped=[SkippedNumberOut(phone_number=s.address, reason=s.reason) for s in result.skipped],
        missing_at_provider=[_number_out(n, cfg, agents) for n in result.missing_at_provider],
    )


# ---------------------------------------------------------------- setup instructions


def setup_instructions(provider: str) -> SetupInstructions:
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise NotFoundError("Calling provider not found")
    base = get_settings().voice_runtime_public_url.rstrip("/")
    inbound = base + INBOUND_PATH
    fallback = base + FALLBACK_PATH if spec.fallback else None
    event = base + spec.event_path if spec.event_path else None
    values = {"inbound_url": inbound, "fallback_url": fallback or "", "event_url": event or ""}
    notes = list(spec.notes)
    host = (urlsplit(base).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        notes.append(
            "The voice service address is a local one and providers on the internet cannot "
            "reach it. Set ATLAS_VOICE_RUNTIME_PUBLIC_URL to its public HTTPS address."
        )
    return SetupInstructions(
        provider=spec.id,
        title=spec.title,
        inbound_webhook_url=inbound,
        fallback_webhook_url=fallback,
        event_webhook_url=event,
        automatic=spec.automatic,
        steps=[s.format(**values) for s in spec.steps],
        notes=notes,
    )
