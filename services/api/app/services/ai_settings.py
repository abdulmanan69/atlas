"""AI provider connections and the organization's model selection.

The runtime is the source of truth for credentials (it validates them live and
stores them); ATLAS curates which providers are offered, keeps secrets out of
its own logs and responses, and audits every change.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.integrations.voice_runtime.client import RuntimeRejectedError, VoiceRuntimeClient
from app.models.voice import RuntimeAccount
from app.schemas.voice import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionUpdate,
    ModelSelection,
    ModelSelectionUpdate,
    ProviderCatalog,
    ProviderCheck,
    ProviderOption,
    ServiceChoice,
    ServiceKind,
    VoiceOption,
)
from app.services import audit
from app.services.runtime_accounts import ensure_account, runtime_client

log = logging.getLogger(__name__)

# Providers ATLAS supports, by service. Ids are the runtime's provider ids.
SUPPORTED: dict[ServiceKind, tuple[str, ...]] = {
    "llm": ("openai", "groq", "google", "azure", "openrouter", "aws_bedrock", "speaches"),
    "stt": (
        "deepgram",
        "openai",
        "assemblyai",
        "speechmatics",
        "google",
        "azure_speech",
        "elevenlabs",
        "cartesia",
        "gladia",
        "soniox",
        "speaches",
    ),
    "tts": (
        "elevenlabs",
        "cartesia",
        "openai",
        "deepgram",
        "google",
        "azure_speech",
        "rime",
        "inworld",
        "speechify",
        "speaches",
    ),
    "embeddings": ("openai", "azure", "openrouter"),
}
SELF_HOSTED = frozenset({"speaches"})
SELF_HOSTED_TITLES: dict[ServiceKind, str] = {
    "llm": "Self-hosted LLM (Ollama, vLLM, LM Studio)",
    "stt": "Self-hosted speech-to-text (Whisper)",
    "tts": "Self-hosted text-to-speech (Kokoro, Piper)",
    "embeddings": "Self-hosted embeddings",
}
SECRET_FIELDS = frozenset(
    {"api_key", "credentials", "aws_access_key", "aws_secret_key", "aws_session_token"}
)
CONNECTION_FIELDS = frozenset(
    {
        "base_url",
        "endpoint",
        "project_id",
        "location",
        "aws_region",
        "region",
        "api_version",
        "group_id",
        "bill_to",
    }
)
HOSTED_TRIAL_TITLE = "Hosted trial models"
MANAGED_CONFIG_NAME = "Organization default"
VOICE_CATALOG_PROVIDERS = frozenset({"elevenlabs", "deepgram", "cartesia", "rime", "sarvam"})

_catalog_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
_CATALOG_TTL = 600.0


async def _raw_catalog(client: VoiceRuntimeClient) -> dict[str, dict[str, Any]]:
    global _catalog_cache
    now = time.monotonic()
    if _catalog_cache and now - _catalog_cache[0] < _CATALOG_TTL:
        return _catalog_cache[1]
    services = await client.catalog()
    _catalog_cache = (now, services)
    return services


def _option(kind: ServiceKind, pid: str, raw: dict[str, Any]) -> ProviderOption:
    return ProviderOption(
        id=pid,
        title=SELF_HOSTED_TITLES[kind] if pid in SELF_HOSTED else str(raw.get("title") or pid),
        self_hosted=pid in SELF_HOSTED,
        credential_fields=[f for f in raw.get("credential_fields", []) if f in SECRET_FIELDS],
        connection_fields=[f for f in raw.get("connection_fields", []) if f in CONNECTION_FIELDS],
        credential_required=bool(raw.get("credential_required")),
        settings_schema=raw.get("settings_schema") or {},
    )


async def catalog(db: AsyncSession, ctx: TenantContext) -> ProviderCatalog:
    async with runtime_client(db, ctx.org_id) as client:
        services = await _raw_catalog(client)
    out: dict[str, list[ProviderOption]] = {}
    for kind, allowed in SUPPORTED.items():
        available = services.get(kind, {})
        out[kind] = [_option(kind, pid, available[pid]) for pid in allowed if pid in available]
    return ProviderCatalog(**out)


def _provider_title(pid: str) -> str:
    if pid in SELF_HOSTED:
        return "Self-hosted"
    return (
        pid.replace("_", " ")
        .title()
        .replace("Openai", "OpenAI")
        .replace("Elevenlabs", "ElevenLabs")
    )


async def list_connections(db: AsyncSession, ctx: TenantContext) -> list[ConnectionOut]:
    account = await ensure_account(db, ctx.org_id)
    supported = {p for providers in SUPPORTED.values() for p in providers}
    async with runtime_client(db, ctx.org_id) as client:
        connections = await client.list_connections()
    out: list[ConnectionOut] = []
    for c in connections:
        trial = c.uuid == account.hosted_trial_connection_uuid
        if not c.is_active or (c.provider not in supported and not trial):
            continue
        if c.uuid == account.bootstrap_connection_uuid and not trial:
            continue
        out.append(
            ConnectionOut(
                id=c.uuid,
                name=HOSTED_TRIAL_TITLE if trial else c.name,
                provider="hosted_trial" if trial else c.provider,
                provider_title=HOSTED_TRIAL_TITLE if trial else _provider_title(c.provider),
                connection_settings={
                    k: v for k, v in c.connection_settings.items() if k in CONNECTION_FIELDS
                },
                configured_credentials=[k for k in c.configured_credentials if k in SECRET_FIELDS],
                hosted_trial=trial,
            )
        )
    return out


def _clean_fields(data: dict[str, str], allowed: frozenset[str], label: str) -> dict[str, str]:
    unknown = set(data) - allowed
    if unknown:
        raise ValidationFailedError(f"Unsupported {label} fields: {', '.join(sorted(unknown))}")
    return {k: v.strip() for k, v in data.items() if v and v.strip()}


async def create_connection(
    db: AsyncSession, ctx: TenantContext, data: ConnectionCreate
) -> ConnectionOut:
    supported = {p for providers in SUPPORTED.values() for p in providers}
    if data.provider not in supported:
        raise ValidationFailedError("This provider is not supported")
    credentials = _clean_fields(data.credentials, SECRET_FIELDS, "credential")
    settings = _clean_fields(data.connection_settings, CONNECTION_FIELDS, "connection")
    async with runtime_client(db, ctx.org_id) as client:
        # The runtime checks the credentials against the provider before saving.
        conn = await client.create_connection(
            name=data.name,
            provider=data.provider,
            credentials=credentials,
            connection_settings=settings,
        )
    await audit.record(
        db,
        action="ai.connection_created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_connection",
        resource_id=conn.uuid,
        changes={
            "provider": data.provider,
            "name": data.name,
            "credential_fields": sorted(credentials),
            "connection_settings": settings,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return next(c for c in await list_connections(db, ctx) if c.id == conn.uuid)


async def update_connection(
    db: AsyncSession, ctx: TenantContext, connection_id: str, data: ConnectionUpdate
) -> ConnectionOut:
    await _assert_connection(db, ctx, connection_id)
    credentials = _clean_fields(data.credentials or {}, SECRET_FIELDS, "credential")
    settings = (
        _clean_fields(data.connection_settings, CONNECTION_FIELDS, "connection")
        if data.connection_settings is not None
        else None
    )
    async with runtime_client(db, ctx.org_id) as client:
        await client.update_connection(
            connection_id,
            name=data.name,
            credentials=credentials or None,
            connection_settings=settings,
        )
    await audit.record(
        db,
        action="ai.connection_updated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_connection",
        resource_id=connection_id,
        changes={
            "name": data.name,
            "rotated_credentials": sorted(credentials),
            "connection_settings": settings,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return next(c for c in await list_connections(db, ctx) if c.id == connection_id)


async def _assert_connection(
    db: AsyncSession, ctx: TenantContext, connection_id: str
) -> ConnectionOut:
    for c in await list_connections(db, ctx):
        if c.id == connection_id:
            return c
    raise NotFoundError("Connection not found")


async def delete_connection(db: AsyncSession, ctx: TenantContext, connection_id: str) -> None:
    conn = await _assert_connection(db, ctx, connection_id)
    selection = await get_selection(db, ctx)
    in_use = [
        kind
        for kind in ("llm", "stt", "tts", "embeddings")
        if (choice := getattr(selection, kind)) is not None
        and choice.connection_id == connection_id
    ]
    if in_use:
        raise ValidationFailedError(
            f"This connection is in use for: {', '.join(k.upper() for k in in_use)}. "
            "Choose another provider for those first."
        )
    async with runtime_client(db, ctx.org_id) as client:
        await client.delete_connection(connection_id)
    await audit.record(
        db,
        action="ai.connection_deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_connection",
        resource_id=connection_id,
        changes={"provider": conn.provider},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


def _choice(raw: Any) -> ServiceChoice | None:
    if not isinstance(raw, dict) or not raw.get("provider_connection_uuid"):
        return None
    return ServiceChoice(
        connection_id=str(raw["provider_connection_uuid"]), settings=raw.get("settings") or {}
    )


async def get_selection(db: AsyncSession, ctx: TenantContext) -> ModelSelection:
    return await get_selection_for_org(db, ctx.org_id)


async def get_selection_for_org(db: AsyncSession, org_id: uuid.UUID) -> ModelSelection:
    account = await ensure_account(db, org_id)
    async with runtime_client(db, org_id) as client:
        default_uuid = await client.get_default_configuration()
        # The runtime's bootstrap default is not the tenant's choice: report "not set up".
        if not default_uuid or (
            default_uuid == account.bootstrap_model_config_uuid
            and not get_settings().voice_allow_hosted_trial_models
        ):
            return ModelSelection()
        try:
            cfg = await client.get_model_configuration(default_uuid)
        except RuntimeRejectedError:
            return ModelSelection()
    spec = cfg.configuration
    return ModelSelection(
        llm=_choice(spec.get("llm")),
        stt=_choice(spec.get("stt")),
        tts=_choice(spec.get("tts")),
        embeddings=_choice(spec.get("embeddings")),
    )


async def save_selection(
    db: AsyncSession, ctx: TenantContext, data: ModelSelectionUpdate
) -> ModelSelection:
    account = await ensure_account(db, ctx.org_id)
    known = {c.id for c in await list_connections(db, ctx)}
    spec: dict[str, Any] = {"version": 3, "mode": "pipeline"}
    for kind in ("llm", "stt", "tts", "embeddings"):
        choice: ServiceChoice | None = getattr(data, kind)
        if choice is None:
            continue
        if choice.connection_id not in known:
            raise ValidationFailedError(f"Unknown connection for {kind.upper()}")
        if kind == "tts":
            # Cloned voices live in one shared store on the Pocket TTS server.
            from app.services.voice_cloning import assert_voice_owned

            await assert_voice_owned(db, ctx.org_id, choice.settings.get("voice"))
        spec[kind] = {
            "provider_connection_uuid": choice.connection_id,
            "settings": {k: v for k, v in choice.settings.items() if v not in (None, "")},
        }
    async with runtime_client(db, ctx.org_id) as client:
        cfg_uuid = account.managed_model_config_uuid
        if cfg_uuid:
            try:
                await client.update_model_configuration(cfg_uuid, spec)
            except RuntimeRejectedError as exc:
                if exc.runtime_status != 404:
                    raise
                cfg_uuid = None
        if not cfg_uuid:
            created = await client.create_model_configuration(MANAGED_CONFIG_NAME, spec)
            cfg_uuid = created.uuid
            account.managed_model_config_uuid = cfg_uuid
        await client.set_default_configuration(cfg_uuid)
        if not get_settings().voice_allow_hosted_trial_models:
            await _retire_bootstrap_models(client, account)
    await audit.record(
        db,
        action="ai.models_updated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_models",
        changes={"configuration": spec},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return await get_selection(db, ctx)


async def _retire_bootstrap_models(client: VoiceRuntimeClient, account: RuntimeAccount) -> None:
    """Archive the runtime's vendor-hosted bootstrap models once they are no longer default."""
    if account.bootstrap_model_config_uuid:
        try:
            await client.delete_model_configuration(account.bootstrap_model_config_uuid)
            account.bootstrap_model_config_uuid = None
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 404:
                account.bootstrap_model_config_uuid = None
            else:
                log.warning("bootstrap_model_retire_failed", extra={"kind": "configuration"})
    if account.bootstrap_connection_uuid and not account.bootstrap_model_config_uuid:
        try:
            await client.delete_connection(account.bootstrap_connection_uuid)
            account.bootstrap_connection_uuid = None
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 404:
                account.bootstrap_connection_uuid = None
            else:
                log.warning("bootstrap_model_retire_failed", extra={"kind": "connection"})


async def check(db: AsyncSession, ctx: TenantContext) -> ProviderCheck:
    selection = await get_selection(db, ctx)
    if not (selection.llm and selection.stt and selection.tts):
        return ProviderCheck(
            ok=False, problems=["Choose a language model, speech-to-text and text-to-speech."]
        )
    async with runtime_client(db, ctx.org_id) as client:
        problems = await client.validate_default_configuration()
    messages = [
        f"{str(p.get('model', '')).upper()}: {p.get('message', 'not working')}" for p in problems
    ]
    return ProviderCheck(ok=not messages, problems=messages)


async def voices(
    db: AsyncSession,
    ctx: TenantContext,
    *,
    provider: str,
    connection_id: str,
    query: str | None,
    language: str | None,
    model: str | None,
) -> list[VoiceOption]:
    if provider not in VOICE_CATALOG_PROVIDERS:
        return []
    await _assert_connection(db, ctx, connection_id)
    params = {k: v for k, v in {"q": query, "language": language, "model": model}.items() if v}
    async with runtime_client(db, ctx.org_id) as client:
        body = await client.voices(provider, connection_uuid=connection_id, params=params)
    return [
        VoiceOption(
            id=str(v.get("voice_id")),
            name=str(v.get("name") or v.get("voice_id")),
            gender=v.get("gender"),
            accent=v.get("accent"),
            language=v.get("language"),
            description=v.get("description"),
            preview_url=v.get("preview_url"),
        )
        for v in body.get("voices", [])
        if v.get("voice_id")
    ][:200]


def hosted_trial_enabled() -> bool:
    return get_settings().voice_allow_hosted_trial_models
