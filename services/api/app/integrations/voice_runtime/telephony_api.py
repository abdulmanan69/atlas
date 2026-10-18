"""Runtime calls for calling providers and phone numbers.

Thin async functions over ``VoiceRuntimeClient``. Payload shapes were verified against
the runtime source (routes/organization.py, schemas/telephony_config.py,
schemas/telephony_phone_number.py); nothing here is invented. Credentials returned by
the runtime are already masked (routes/organization.py ``_credentials_for_display``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.integrations.voice_runtime.client import (
    HttpMethod,
    RuntimeRejectedError,
    VoiceRuntimeClient,
)

CONFIGS = "/organizations/telephony-configs"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TelephonyConfig(_Payload):
    """One row of ``GET /organizations/telephony-configs`` (TelephonyConfigurationListItem)."""

    id: int
    name: str
    provider: str
    connectivity: str = "api"
    is_default_outbound: bool = False
    inactive: bool = False
    inactive_reason: str | None = None
    phone_number_count: int = 0
    supports_outbound: bool = True
    is_ready_for_outbound: bool = True
    outbound_blocked_reason: str | None = None


class TelephonyConfigDetail(_Payload):
    """``GET /organizations/telephony-configs/{id}`` (TelephonyConfigurationDetail)."""

    id: int
    name: str
    provider: str
    connectivity: str = "api"
    is_default_outbound: bool = False
    inactive: bool = False
    inactive_reason: str | None = None
    credentials: dict[str, Any] = {}
    supports_outbound: bool = True
    supports_phone_number_sync: bool = False


class ProviderSync(_Payload):
    """Outcome of the runtime pushing the inbound webhook to the provider (ok=False warns)."""

    ok: bool
    message: str | None = None


class PhoneNumber(_Payload):
    """PhoneNumberResponse (schemas/telephony_phone_number.py)."""

    id: int
    telephony_configuration_id: int
    address: str
    address_normalized: str
    label: str | None = None
    inbound_workflow_id: int | None = None
    is_active: bool = True
    is_default_caller_id: bool = False
    provider_sync: ProviderSync | None = None


class SkippedNumber(_Payload):
    address: str
    reason: str


class NumberSync(_Payload):
    """PhoneNumberSyncResponse: rows are only ever added or refreshed, never removed."""

    created: list[PhoneNumber] = []
    updated: list[PhoneNumber] = []
    skipped: list[SkippedNumber] = []
    missing_at_provider: list[PhoneNumber] = []


async def _provider_backed(
    client: VoiceRuntimeClient, method: HttpMethod, path: str, *, json: Any = None
) -> Any:
    """Call a route that talks to the customer's provider account on the way.

    The runtime answers 502 when the *provider* refuses (number not owned, detach
    rejected). That is the customer's problem to fix, not an outage, so it is mapped to
    a rejection instead of the generic "voice service unavailable".
    """
    resp = await client._request(method, path, json=json)
    if resp.status_code == 502:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        message = detail if isinstance(detail, str) and detail.strip() else "request refused"
        raise RuntimeRejectedError(
            f"The calling provider refused the request: {message[:300]}", runtime_status=502
        )
    return client._parse(method, path, resp)


# ---------------------------------------------------------------- configurations


async def list_configs(client: VoiceRuntimeClient) -> list[TelephonyConfig]:
    body = await client._call("GET", CONFIGS)
    return [TelephonyConfig.model_validate(c) for c in body.get("configurations", [])]


async def get_config(client: VoiceRuntimeClient, config_id: int) -> TelephonyConfigDetail:
    body = await client._call("GET", f"{CONFIGS}/{config_id}")
    return TelephonyConfigDetail.model_validate(body)


async def create_config(
    client: VoiceRuntimeClient,
    *,
    name: str,
    provider: str,
    credentials: dict[str, str],
    is_default_outbound: bool,
) -> TelephonyConfigDetail:
    """``POST /organizations/telephony-configs`` {name, is_default_outbound, config}."""
    body = await client._call(
        "POST",
        CONFIGS,
        json={
            "name": name,
            "is_default_outbound": is_default_outbound,
            "config": {"provider": provider, **credentials},
        },
    )
    return TelephonyConfigDetail.model_validate(body)


async def update_config(
    client: VoiceRuntimeClient,
    config_id: int,
    *,
    name: str | None,
    provider: str,
    credentials: dict[str, str] | None,
) -> TelephonyConfigDetail:
    """``PUT /organizations/telephony-configs/{id}`` {name?, config?}.

    ``config`` must carry the provider's full credential set; values equal to the mask
    the runtime handed out are restored from the stored row (preserve_masked_fields).
    """
    patch: dict[str, Any] = {}
    if name is not None:
        patch["name"] = name
    if credentials is not None:
        patch["config"] = {"provider": provider, **credentials}
    body = await client._call("PUT", f"{CONFIGS}/{config_id}", json=patch)
    return TelephonyConfigDetail.model_validate(body)


async def delete_config(client: VoiceRuntimeClient, config_id: int) -> None:
    await client._call("DELETE", f"{CONFIGS}/{config_id}")


async def set_default_outbound(client: VoiceRuntimeClient, config_id: int) -> TelephonyConfigDetail:
    body = await client._call("POST", f"{CONFIGS}/{config_id}/set-default-outbound")
    return TelephonyConfigDetail.model_validate(body)


# ---------------------------------------------------------------- phone numbers


async def list_numbers(client: VoiceRuntimeClient, config_id: int) -> list[PhoneNumber]:
    body = await client._call("GET", f"{CONFIGS}/{config_id}/phone-numbers")
    return [PhoneNumber.model_validate(n) for n in body.get("phone_numbers", [])]


async def create_number(
    client: VoiceRuntimeClient,
    config_id: int,
    *,
    address: str,
    label: str | None,
) -> PhoneNumber:
    """``POST .../phone-numbers`` (PhoneNumberCreateRequest). ``address`` must be E.164.

    The runtime confirms the number is owned by the provider account before saving.
    """
    body = await _provider_backed(
        client,
        "POST",
        f"{CONFIGS}/{config_id}/phone-numbers",
        json={"address": address, "label": label, "is_active": True},
    )
    return PhoneNumber.model_validate(body)


async def set_inbound_workflow(
    client: VoiceRuntimeClient,
    config_id: int,
    number_id: int,
    *,
    workflow_id: int | None,
) -> PhoneNumber:
    """``PUT .../phone-numbers/{id}`` with inbound_workflow_id or clear_inbound_workflow.

    Inbound routing is per number: the dispatcher at ``/api/v1/telephony/inbound/run``
    resolves the workflow from the called number's ``inbound_workflow_id``.
    """
    patch: dict[str, Any] = (
        {"inbound_workflow_id": workflow_id}
        if workflow_id is not None
        else {"clear_inbound_workflow": True}
    )
    body = await client._call("PUT", f"{CONFIGS}/{config_id}/phone-numbers/{number_id}", json=patch)
    return PhoneNumber.model_validate(body)


async def delete_number(client: VoiceRuntimeClient, config_id: int, number_id: int) -> None:
    """The runtime detaches the inbound webhook at the provider first; 502 if that fails."""
    await _provider_backed(client, "DELETE", f"{CONFIGS}/{config_id}/phone-numbers/{number_id}")


async def sync_numbers(client: VoiceRuntimeClient, config_id: int) -> NumberSync:
    body = await _provider_backed(client, "POST", f"{CONFIGS}/{config_id}/phone-numbers/sync")
    return NumberSync.model_validate(body)
