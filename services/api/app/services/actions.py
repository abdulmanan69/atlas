"""Actions: HTTP calls an agent can make during a conversation.

The voice service stores and executes actions as `http_api` tools; ATLAS owns the
contract the product exposes, validates every destination URL before anything reaches
the service, keeps credentials write-only and audits each change. Nothing is stored in
the ATLAS database: the runtime tool uuid is the action id, and tenant isolation comes
from each organization having its own runtime account.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import AppError, NotFoundError, ValidationFailedError
from app.core.net_policy import validate_destination_url
from app.integrations.voice_runtime import tools_api
from app.integrations.voice_runtime.client import RuntimeRejectedError, VoiceRuntimeClient
from app.integrations.voice_runtime.tools_api import RuntimeCredential, RuntimeTool
from app.schemas.actions import (
    ActionAuth,
    ActionAuthOut,
    ActionCreate,
    ActionOut,
    ActionParameter,
    ActionSpec,
    ActionTestRequest,
    ActionTestResult,
    ActionUpdate,
    AuthType,
    PresetParameter,
)
from app.services import audit
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

MAX_ACTIONS = 100
EXCERPT_CHARS = 1000
CREDENTIAL_PREFIX = "atlas-action-"
# Headers that carry authentication or frame the request; the credential owns these.
RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
    }
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")  # RFC 7230 token

# ATLAS auth type -> runtime credential_type (api/enums.py:216-223).
_RUNTIME_CREDENTIAL_TYPE: dict[AuthType, str] = {
    "api_key": "api_key",
    "bearer": "bearer_token",
    "basic": "basic_auth",
    "custom_header": "custom_header",
}
_AUTH_TYPE: dict[str, AuthType] = {v: k for k, v in _RUNTIME_CREDENTIAL_TYPE.items()}


def function_name(name: str) -> str:
    """The function name the model calls; mirrors the runtime's rule exactly.

    Verified: `api/services/workflow/tools/custom_tool.py:41-44`.
    """
    lowered = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return re.sub(r"_+", "_", lowered).strip("_")


# ------------------------------------------------------------------ validation


async def validate_action_url(url: str) -> str:
    return await validate_destination_url(
        url, label="Action", allow_private=get_settings().allow_private_action_urls
    )


def auth_header_name(auth: ActionAuth) -> str:
    """Header the runtime sends the credential in (api/utils/credential_auth.py:34-52)."""
    if auth.type in ("bearer", "basic"):
        return "Authorization"
    return (auth.header_name or "").strip()


def clean_headers(headers: dict[str, str], credential_header: str | None) -> dict[str, str]:
    """Static headers: valid names, no auth/framing headers, no clash with the credential."""

    def reject(message: str) -> ValidationFailedError:
        return ValidationFailedError(message, details={"field": "headers"})

    out: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = raw_name.strip()
        if not name or not _HEADER_NAME.match(name):
            raise reject(f"Invalid header name: {raw_name!r}")
        if name.lower() in RESERVED_HEADERS:
            raise reject(f"The {name} header is managed by the credential and cannot be set")
        if credential_header and name.lower() == credential_header.lower():
            raise reject(f"The {name} header is already used by the action's credential")
        if name.lower() in {k.lower() for k in out}:
            raise reject(f"Duplicate header: {name}")
        value = raw_value.strip()
        if "\r" in value or "\n" in value or len(value) > 2000:
            raise reject(f"Invalid value for header {name}")
        out[name] = value
    return out


def _validate_credential_header(auth: ActionAuth) -> None:
    header = auth_header_name(auth)
    if not _HEADER_NAME.match(header):
        raise ValidationFailedError(
            f"Invalid header name: {header!r}", details={"field": "auth.header_name"}
        )


# ------------------------------------------------------------------ mapping


def _definition(
    spec: ActionSpec, *, url: str, headers: dict[str, str], credential_uuid: str | None
) -> dict[str, Any]:
    """The runtime's HttpApiToolDefinition (api/schemas/tool.py:92-176, 534-562, 573-577)."""
    config: dict[str, Any] = {
        "method": spec.method,
        "url": url,
        "headers": headers or None,
        "credential_uuid": credential_uuid,
        "parameters": [p.model_dump() for p in spec.parameters] or None,
        "preset_parameters": [
            {
                "name": p.name,
                "type": p.type,
                "value_template": p.value,
                "required": p.required,
            }
            for p in spec.preset_parameters
        ]
        or None,
        "timeout_ms": spec.timeout_ms,
        "body_format": spec.body_format,
    }
    messages: list[dict[str, Any]] = []
    say = (spec.say_while_running or "").strip()
    if say:
        messages.append({"event": "start", "type": "text", "text": say})
    return {"schema_version": 1, "type": "http_api", "config": config, "messages": messages}


def _config(tool: RuntimeTool) -> dict[str, Any]:
    config = tool.definition.get("config")
    return dict(config) if isinstance(config, dict) else {}


def _is_action(tool: RuntimeTool) -> bool:
    return (
        tool.category == tools_api.HTTP_API
        and tool.definition.get("type") == tools_api.HTTP_API
        and tool.status != "archived"
    )


def _to_out(tool: RuntimeTool, credentials: dict[str, RuntimeCredential]) -> ActionOut:
    config = _config(tool)
    auth: ActionAuthOut | None = None
    cred = credentials.get(str(config.get("credential_uuid") or ""))
    if cred is not None and cred.credential_type in _AUTH_TYPE:
        auth = ActionAuthOut(
            type=_AUTH_TYPE[cred.credential_type], header_name=cred.description or None
        )
    say = next(
        (
            str(m.get("text"))
            for m in tool.definition.get("messages") or []
            if isinstance(m, dict) and m.get("event") == "start" and m.get("type") == "text"
        ),
        None,
    )
    return ActionOut(
        id=tool.tool_uuid,
        name=tool.name,
        function_name=function_name(tool.name),
        description=tool.description or "",
        method=config.get("method") or "POST",
        url=str(config.get("url") or ""),
        headers=dict(config.get("headers") or {}),
        parameters=[ActionParameter.model_validate(p) for p in config.get("parameters") or []],
        preset_parameters=[
            PresetParameter(
                name=p["name"],
                type=p.get("type", "string"),
                value=p.get("value_template", ""),
                required=p.get("required", True),
            )
            for p in config.get("preset_parameters") or []
        ],
        timeout_ms=int(config.get("timeout_ms") or 5000),
        body_format=config.get("body_format") or "json",
        say_while_running=say,
        auth=auth,
        created_at=tool.created_at,
        updated_at=tool.updated_at,
    )


# ------------------------------------------------------------------ runtime helpers


async def _fetch(client: VoiceRuntimeClient, action_id: str) -> RuntimeTool:
    """The organization's action, or 404. Other categories and archived tools do not exist."""
    try:
        uuid.UUID(action_id)
    except ValueError as exc:
        raise NotFoundError("Action not found") from exc
    try:
        tool = await tools_api.get_tool(client, action_id)
    except RuntimeRejectedError as exc:
        if exc.runtime_status == 404:
            raise NotFoundError("Action not found") from exc
        raise
    if not _is_action(tool):
        raise NotFoundError("Action not found")
    return tool


async def _credentials(
    client: VoiceRuntimeClient, tools: list[RuntimeTool]
) -> dict[str, RuntimeCredential]:
    if not any(_config(t).get("credential_uuid") for t in tools):
        return {}
    return {c.uuid: c for c in await tools_api.list_credentials(client)}


async def _store_credential(client: VoiceRuntimeClient, auth: ActionAuth) -> RuntimeCredential:
    """Store the secret with the runtime (required fields: api/routes/credentials.py:63-96)."""
    header = auth_header_name(auth)
    data: dict[str, str] = {
        "api_key": {"header_name": header, "api_key": auth.api_key or ""},
        "bearer": {"token": auth.token or ""},
        "basic": {"username": auth.username or "", "password": auth.password or ""},
        "custom_header": {"header_name": header, "header_value": auth.header_value or ""},
    }[auth.type]
    return await tools_api.create_credential(
        client,
        name=f"{CREDENTIAL_PREFIX}{secrets.token_hex(8)}",
        description=header,  # Read back to show which header carries the credential.
        credential_type=_RUNTIME_CREDENTIAL_TYPE[auth.type],
        credential_data=data,
    )


async def _drop_credential(
    client: VoiceRuntimeClient, credential_uuid: str, known: dict[str, RuntimeCredential]
) -> None:
    """Best effort: only credentials ATLAS created for actions are ever removed."""
    cred = known.get(credential_uuid)
    if cred is None or not cred.name.startswith(CREDENTIAL_PREFIX):
        return
    try:
        await tools_api.delete_credential(client, credential_uuid)
    except AppError as exc:
        if not (isinstance(exc, RuntimeRejectedError) and exc.runtime_status == 404):
            log.warning("action_credential_cleanup_failed", extra={"error": exc.message})


def _audit_changes(
    spec: ActionSpec, headers: dict[str, str], auth: ActionAuth | None
) -> dict[str, Any]:
    return {
        "name": spec.name,
        "function_name": function_name(spec.name),
        "method": spec.method,
        "url": spec.url,
        "header_names": sorted(headers),
        "parameters": [p.name for p in spec.parameters],
        "preset_parameters": [p.name for p in spec.preset_parameters],
        "timeout_ms": spec.timeout_ms,
        "auth_type": auth.type if auth else None,
    }


def _prepare(spec: ActionSpec, credential_header: str | None) -> dict[str, str]:
    if not function_name(spec.name):
        raise ValidationFailedError(
            "Name must contain at least one letter or digit", details={"field": "name"}
        )
    return clean_headers(spec.headers, credential_header)


# ------------------------------------------------------------------ public API


async def list_actions(db: AsyncSession, ctx: TenantContext) -> list[ActionOut]:
    async with runtime_client(db, ctx.org_id) as client:
        tools = [t for t in await tools_api.list_http_tools(client) if _is_action(t)]
        credentials = await _credentials(client, tools)
    return [_to_out(t, credentials) for t in tools]


async def get_action(db: AsyncSession, ctx: TenantContext, action_id: str) -> ActionOut:
    async with runtime_client(db, ctx.org_id) as client:
        tool = await _fetch(client, action_id)
        credentials = await _credentials(client, [tool])
    return _to_out(tool, credentials)


async def create_action(db: AsyncSession, ctx: TenantContext, data: ActionCreate) -> ActionOut:
    if data.auth is not None:
        _validate_credential_header(data.auth)
    headers = _prepare(data, auth_header_name(data.auth) if data.auth else None)
    url = await validate_action_url(data.url)
    async with runtime_client(db, ctx.org_id) as client:
        existing = [t for t in await tools_api.list_http_tools(client) if _is_action(t)]
        if len(existing) >= MAX_ACTIONS:
            raise ValidationFailedError(f"An organization may have at most {MAX_ACTIONS} actions")
        credentials: dict[str, RuntimeCredential] = {}
        credential_uuid: str | None = None
        if data.auth is not None:
            cred = await _store_credential(client, data.auth)
            credentials[cred.uuid] = cred
            credential_uuid = cred.uuid
        try:
            tool = await tools_api.create_http_tool(
                client,
                name=data.name,
                description=data.description,
                definition=_definition(
                    data, url=url, headers=headers, credential_uuid=credential_uuid
                ),
            )
        except AppError:
            if credential_uuid:
                await _drop_credential(client, credential_uuid, credentials)
            raise
    await audit.record(
        db,
        action="action.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="action",
        resource_id=tool.tool_uuid,
        changes=_audit_changes(data, headers, data.auth),
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _to_out(tool, credentials)


async def update_action(
    db: AsyncSession, ctx: TenantContext, action_id: str, data: ActionUpdate
) -> ActionOut:
    changes = data.model_dump(exclude_unset=True, exclude={"auth"})
    auth_changed = "auth" in data.model_fields_set
    if auth_changed and data.auth is not None:
        _validate_credential_header(data.auth)
    async with runtime_client(db, ctx.org_id) as client:
        tool = await _fetch(client, action_id)
        credentials = await _credentials(client, [tool])
        current = _to_out(tool, credentials)
        spec = ActionSpec.model_validate(
            {**current.model_dump(include=set(ActionSpec.model_fields)), **changes}
        )
        if auth_changed:
            credential_header = auth_header_name(data.auth) if data.auth else None
        else:
            credential_header = current.auth.header_name if current.auth else None
        headers = _prepare(spec, credential_header)
        url = await validate_action_url(spec.url)
        old_credential = str(_config(tool).get("credential_uuid") or "") or None
        credential_uuid = old_credential
        if auth_changed:
            credential_uuid = None
            if data.auth is not None:
                cred = await _store_credential(client, data.auth)
                credentials[cred.uuid] = cred
                credential_uuid = cred.uuid
        try:
            tool = await tools_api.update_http_tool(
                client,
                action_id,
                name=spec.name,
                description=spec.description,
                definition=_definition(
                    spec, url=url, headers=headers, credential_uuid=credential_uuid
                ),
            )
        except AppError:
            if auth_changed and credential_uuid:
                await _drop_credential(client, credential_uuid, credentials)
            raise
        if auth_changed and old_credential and old_credential != credential_uuid:
            await _drop_credential(client, old_credential, credentials)
    await audit.record(
        db,
        action="action.updated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="action",
        resource_id=action_id,
        changes={
            "fields": sorted(changes),
            "auth": ("rotated" if data.auth else "removed") if auth_changed else "unchanged",
            **_audit_changes(spec, headers, data.auth if auth_changed else None),
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return _to_out(tool, credentials)


async def delete_action(db: AsyncSession, ctx: TenantContext, action_id: str) -> None:
    async with runtime_client(db, ctx.org_id) as client:
        tool = await _fetch(client, action_id)
        credentials = await _credentials(client, [tool])
        try:
            await tools_api.archive_tool(client, action_id)
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 404:
                raise NotFoundError("Action not found") from exc
            raise
        credential_uuid = str(_config(tool).get("credential_uuid") or "")
        if credential_uuid:
            await _drop_credential(client, credential_uuid, credentials)
    await audit.record(
        db,
        action="action.deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="action",
        resource_id=action_id,
        changes={"name": tool.name, "function_name": function_name(tool.name)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


def _excerpt(data: Any) -> str | None:
    if data is None:
        return None
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return text[:EXCERPT_CHARS]


async def test_action(
    db: AsyncSession, ctx: TenantContext, action_id: str, data: ActionTestRequest
) -> ActionTestResult:
    """Run the action once with sample values. The runtime makes the HTTP call."""
    async with runtime_client(db, ctx.org_id) as client:
        tool = await _fetch(client, action_id)
        # DNS answers change: re-check right before the runtime connects.
        await validate_action_url(str(_config(tool).get("url") or ""))
        try:
            result = await tools_api.test_tool(
                client,
                action_id,
                llm_params=data.parameters,
                preset_params=data.preset_parameters,
            )
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 404:
                raise NotFoundError("Action not found") from exc
            raise
    ok = result.status == "success" and (result.status_code is None or result.status_code < 400)
    out = ActionTestResult(
        ok=ok,
        status_code=result.status_code,
        latency_ms=max(0, result.duration_ms),
        request_method=result.request_method,
        request_url=result.request_url,
        response_excerpt=_excerpt(result.data),
        error=result.error,
        hint=result.hint,
    )
    await audit.record(
        db,
        action="action.tested",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="action",
        resource_id=action_id,
        changes={"ok": out.ok, "status_code": out.status_code, "latency_ms": out.latency_ms},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return out
