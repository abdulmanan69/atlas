"""Tenant API keys, the transactional outbox and outbound webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import types
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.sql import Executable

import app.core.redis as redis_module
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from app.models.developer import (
    ApiKey,
    DeliveryStatus,
    OutboxEvent,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services import api_keys, events
from app.services import outbound_webhooks as webhooks
from app.services.events import EVENT_TYPES
from app.workers.outbound_webhooks import deliver_due, fan_out, purge
from tests.conftest import Account, invite_and_join, signup

HOOK_URL = "https://hooks.example.com/atlas"
LEAD = {
    "first_name": "Ayesha",
    "last_name": "Khan",
    "phone": "+44 20 7946 0001",
    "email": "ayesha@example.com",
}
PUBLIC_IP = ip_address("93.184.216.34")
DEV = "/api/v1/developer"


# ---------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch real DNS: hosts resolve to a public address unless named otherwise."""

    async def resolve(host: str, port: int) -> list[IPv4Address | IPv6Address]:
        if host.endswith(".internal.example"):
            return [ip_address("10.0.0.5")]
        if host == "nowhere.example":
            return []
        return [PUBLIC_IP]

    monkeypatch.setattr(webhooks, "_resolve_host", resolve)


@pytest.fixture
def hooks() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def key_headers(key: str, org_id: str | None = None) -> dict[str, str]:
    headers = {"X-API-Key": key}
    if org_id:
        headers["X-Organization-Id"] = org_id
    return headers


async def make_key(
    client: httpx.AsyncClient, acct: Account, role: str = "manager", **extra: Any
) -> tuple[dict[str, Any], str]:
    r = await client.post(
        f"{DEV}/api-keys", headers=acct.headers, json={"name": "CI", "role": role, **extra}
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body, body["key"]


async def make_webhook(
    client: httpx.AsyncClient,
    acct: Account,
    event_types: tuple[str, ...] = ("lead.created",),
    url: str = HOOK_URL,
) -> dict[str, Any]:
    r = await client.post(
        f"{DEV}/webhooks",
        headers=acct.headers,
        json={"url": url, "events": list(event_types), "description": "CRM sync"},
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def make_lead(client: httpx.AsyncClient, acct: Account, **extra: Any) -> dict[str, Any]:
    r = await client.post("/api/v1/leads", headers=acct.headers, json={**LEAD, **extra})
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def stage_ids(client: httpx.AsyncClient, acct: Account) -> dict[str, str]:
    r = await client.get("/api/v1/pipeline-stages", headers=acct.headers)
    return {s["key"]: s["id"] for s in r.json()}


def verify_signature(secret: str, timestamp: str, body: bytes, header: str) -> bool:
    """Reference verifier, as documented for customers."""
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(header, f"v1={expected}")


async def db_exec(stmt: Executable) -> None:
    async with get_sessionmaker()() as db:
        await db.execute(stmt)
        await db.commit()


async def db_outbox() -> list[OutboxEvent]:
    async with get_sessionmaker()() as db:
        rows = await db.execute(select(OutboxEvent).order_by(OutboxEvent.id))
        return list(rows.scalars().all())


async def db_deliveries(endpoint_id: str | None = None) -> list[WebhookDelivery]:
    async with get_sessionmaker()() as db:
        # Deliveries are created in outbox order; `created_at` can tie on Windows clocks,
        # so order by the outbox sequence instead of the timestamp.
        stmt = (
            select(WebhookDelivery)
            .join(OutboxEvent, OutboxEvent.event_id == WebhookDelivery.event_id)
            .order_by(OutboxEvent.id, WebhookDelivery.id)
        )
        if endpoint_id:
            stmt = stmt.where(WebhookDelivery.endpoint_id == uuid.UUID(endpoint_id))
        return list((await db.execute(stmt)).scalars().all())


async def db_endpoint(endpoint_id: str) -> WebhookEndpoint:
    async with get_sessionmaker()() as db:
        return (await db.get(WebhookEndpoint, uuid.UUID(endpoint_id))) or pytest.fail("missing")


async def db_audit(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at)
        )
        return list(rows.scalars().all())


def now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- API keys


async def test_api_key_lifecycle(client: httpx.AsyncClient, owner: Account) -> None:
    body, key = await make_key(client, owner, role="manager")
    assert key.startswith("atlas_") and len(key) > 40
    assert body["key_prefix"] == key[:14] and body["role"] == "manager"
    assert body["created_by_user_id"] == owner.user_id

    listing = (await client.get(f"{DEV}/api-keys", headers=owner.headers)).json()
    assert [k["id"] for k in listing] == [body["id"]]
    assert "key" not in listing[0] and listing[0]["key_prefix"] == key[:14]

    # The key works without X-Organization-Id: the tenant is the key's organization.
    assert (await client.get("/api/v1/leads", headers=key_headers(key))).status_code == 200
    r = await client.post("/api/v1/leads", headers=key_headers(key), json=LEAD)
    assert r.status_code == 201, r.text

    # Audit: the creator is the actor, and the key is recorded too.
    (entry,) = await db_audit("lead.created")
    assert entry.actor_user_id == uuid.UUID(owner.user_id)
    assert entry.changes["actor"] == {"type": "api_key", "id": body["id"]}
    # ...but a normal session request is not attributed to a key.
    await make_lead(client, owner, phone="+44 20 7946 0002")
    assert "actor" not in (await db_audit("lead.created"))[-1].changes

    listing = (await client.get(f"{DEV}/api-keys", headers=owner.headers)).json()
    assert listing[0]["last_used_at"] is not None

    r = await client.delete(f"{DEV}/api-keys/{body['id']}", headers=owner.headers)
    assert r.status_code == 204
    r = await client.get("/api/v1/leads", headers=key_headers(key))
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "API key has been revoked"
    assert (await client.get(f"{DEV}/api-keys", headers=owner.headers)).json() == []
    revoked = (
        await client.get(f"{DEV}/api-keys", headers=owner.headers, params={"include_revoked": 1})
    ).json()
    assert revoked[0]["revoked_at"] is not None
    assert len(await db_audit("api_key.created")) == 1
    assert len(await db_audit("api_key.revoked")) == 1

    for bad in ("atlas_nope", "sk_live_" + "x" * 40, key[:-1] + ("a" if key[-1] != "a" else "b")):
        r = await client.get("/api/v1/leads", headers=key_headers(bad))
        assert r.status_code == 401, bad


async def test_api_key_role_is_capped_and_enforced(
    client: httpx.AsyncClient, owner: Account
) -> None:
    admin = await invite_and_join(client, owner, "admin@example.com", "admin")
    r = await client.post(
        f"{DEV}/api-keys", headers=admin.headers, json={"name": "too much", "role": "owner"}
    )
    assert r.status_code == 403
    _, admin_key = await make_key(client, admin, role="admin")
    _, viewer_key = await make_key(client, owner, role="viewer")

    # A viewer key reads but never writes.
    assert (await client.get("/api/v1/leads", headers=key_headers(viewer_key))).status_code == 200
    r = await client.post("/api/v1/leads", headers=key_headers(viewer_key), json=LEAD)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"

    # An admin key has admin rights (audit) but not owner rights (org settings)...
    assert (
        await client.get("/api/v1/audit-logs", headers=key_headers(admin_key))
    ).status_code == 200
    r = await client.patch(
        "/api/v1/organization", headers=key_headers(admin_key), json={"name": "Renamed"}
    )
    assert r.status_code == 403
    # ...and keys it creates are capped at admin as well.
    r = await client.post(
        f"{DEV}/api-keys", headers=key_headers(admin_key), json={"name": "esc", "role": "owner"}
    )
    assert r.status_code == 403
    r = await client.post(
        f"{DEV}/api-keys", headers=key_headers(admin_key), json={"name": "ok", "role": "manager"}
    )
    assert r.status_code == 201


async def test_api_key_follows_its_creator_membership(
    client: httpx.AsyncClient, owner: Account
) -> None:
    admin = await invite_and_join(client, owner, "admin@example.com", "admin")
    _, key = await make_key(client, admin, role="admin")
    assert (await client.get("/api/v1/audit-logs", headers=key_headers(key))).status_code == 200

    members = (await client.get("/api/v1/members", headers=owner.headers)).json()
    member_id = next(m["id"] for m in members if m["user"]["id"] == admin.user_id)
    # Demoting the creator caps the key at the new role.
    r = await client.patch(
        f"/api/v1/members/{member_id}", headers=owner.headers, json={"role": "viewer"}
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/audit-logs", headers=key_headers(key))).status_code == 403
    assert (await client.get("/api/v1/leads", headers=key_headers(key))).status_code == 200
    # Removing the creator stops the key.
    r = await client.delete(f"/api/v1/members/{member_id}", headers=owner.headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/leads", headers=key_headers(key))).status_code == 401


async def test_api_key_expiry(client: httpx.AsyncClient, owner: Account) -> None:
    past = (now() - timedelta(hours=1)).isoformat()
    r = await client.post(
        f"{DEV}/api-keys",
        headers=owner.headers,
        json={"name": "old", "role": "viewer", "expires_at": past},
    )
    assert r.status_code == 422
    body, key = await make_key(
        client, owner, role="viewer", expires_at=(now() + timedelta(days=1)).isoformat()
    )
    assert body["expires_at"] is not None
    assert (await client.get("/api/v1/leads", headers=key_headers(key))).status_code == 200
    await db_exec(
        update(ApiKey)
        .where(ApiKey.id == uuid.UUID(body["id"]))
        .values(expires_at=now() - timedelta(seconds=1))
    )
    r = await client.get("/api/v1/leads", headers=key_headers(key))
    assert r.status_code == 401 and r.json()["error"]["message"] == "API key has expired"


async def test_api_key_org_header_must_match(client: httpx.AsyncClient, owner: Account) -> None:
    other = await signup(client, "other@example.com", "Other Co")
    _, key = await make_key(client, owner)
    r = await client.get("/api/v1/leads", headers=key_headers(key, other.org_id))
    assert r.status_code == 403
    r_unknown = await client.get("/api/v1/leads", headers=key_headers(key, str(uuid.uuid4())))
    assert r_unknown.status_code == 403
    assert r_unknown.json()["error"]["message"] == r.json()["error"]["message"]
    assert (
        await client.get("/api/v1/leads", headers=key_headers(key, owner.org_id))
    ).status_code == 200
    assert (await client.get("/api/v1/leads", headers=key_headers(key, "nope"))).status_code == 422
    # A key never sees another tenant's data even when both are "in the same request".
    await make_lead(client, other)
    assert (await client.get("/api/v1/leads", headers=key_headers(key))).json()["total"] == 0


async def test_api_key_rejected_on_auth_and_user_endpoints(
    client: httpx.AsyncClient, owner: Account
) -> None:
    _, key = await make_key(client, owner, role="owner")
    for path in ("/api/v1/auth/me", "/api/v1/auth/sessions"):
        assert (await client.get(path, headers=key_headers(key))).status_code == 401, path
    r = await client.post(
        "/api/v1/organizations",
        headers=key_headers(key),
        json={"name": "Side org", "timezone": "UTC", "default_country": "US"},
    )
    assert r.status_code == 401


async def test_api_key_rate_limit(
    client: httpx.AsyncClient, owner: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_api_key_per_minute", 3)
    # Freeze the limiter's clock so the test cannot straddle a window boundary.
    monkeypatch.setattr(redis_module, "time", types.SimpleNamespace(time=lambda: 1_700_000_000.0))
    _, key = await make_key(client, owner)
    codes = [
        (await client.get("/api/v1/leads", headers=key_headers(key))).status_code for _ in range(4)
    ]
    assert codes == [200, 200, 200, 429]
    _, other_key = await make_key(client, owner)
    assert (await client.get("/api/v1/leads", headers=key_headers(other_key))).status_code == 200
    assert (await client.get("/api/v1/leads", headers=owner.headers)).status_code == 200


async def test_lower_roles_cannot_manage_developer_resources(
    client: httpx.AsyncClient, owner: Account
) -> None:
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    manager = await invite_and_join(client, owner, "manager@example.com", "manager")
    for acct in (viewer, manager):
        assert (await client.get(f"{DEV}/api-keys", headers=acct.headers)).status_code == 403
        r = await client.post(
            f"{DEV}/api-keys", headers=acct.headers, json={"name": "x", "role": "viewer"}
        )
        assert r.status_code == 403
        assert (await client.get(f"{DEV}/webhooks", headers=acct.headers)).status_code == 403
        r = await client.post(
            f"{DEV}/webhooks", headers=acct.headers, json={"url": HOOK_URL, "events": ["*"]}
        )
        assert r.status_code == 403
        assert (await client.get(f"{DEV}/events", headers=acct.headers)).status_code == 403


def test_api_key_format_and_hashing() -> None:
    key = api_keys.generate_key()
    assert key.startswith("atlas_") and len(key) == len("atlas_") + 43
    assert api_keys.shown_prefix(key) == key[:14]
    assert api_keys.hash_key(key) == hashlib.sha256(key.encode()).hexdigest()
    assert api_keys.generate_key() != key


# ---------------------------------------------------------------- webhooks


async def test_webhook_crud_and_secret_rotation(client: httpx.AsyncClient, owner: Account) -> None:
    created = await make_webhook(client, owner, ("lead.created", " lead.created", "*"))
    assert created["secret"].startswith("whsec_")
    assert created["events"] == ["*", "lead.created"]
    assert created["is_active"] is True and created["failure_count"] == 0
    endpoint_id = created["id"]

    listing = (await client.get(f"{DEV}/webhooks", headers=owner.headers)).json()
    assert [e["id"] for e in listing] == [endpoint_id] and "secret" not in listing[0]
    assert (
        await client.get(f"{DEV}/webhooks/{endpoint_id}", headers=owner.headers)
    ).status_code == 200

    r = await client.patch(
        f"{DEV}/webhooks/{endpoint_id}",
        headers=owner.headers,
        json={"url": "https://hooks.example.com/v2", "events": ["lead.stage_changed"]},
    )
    assert r.status_code == 200 and r.json()["url"] == "https://hooks.example.com/v2"
    assert r.json()["events"] == ["lead.stage_changed"]
    r = await client.patch(
        f"{DEV}/webhooks/{endpoint_id}", headers=owner.headers, json={"events": ["lead.exploded"]}
    )
    assert r.status_code == 422
    r = await client.patch(
        f"{DEV}/webhooks/{endpoint_id}", headers=owner.headers, json={"is_active": False}
    )
    assert r.json()["is_active"] is False

    r = await client.post(f"{DEV}/webhooks/{endpoint_id}/rotate-secret", headers=owner.headers)
    assert r.status_code == 200
    assert r.json()["secret"].startswith("whsec_") and r.json()["secret"] != created["secret"]

    catalog = (await client.get(f"{DEV}/events", headers=owner.headers)).json()
    assert {e["type"] for e in catalog} == {*EVENT_TYPES, "*"}

    assert (
        await client.delete(f"{DEV}/webhooks/{endpoint_id}", headers=owner.headers)
    ).status_code == 204
    assert (
        await client.get(f"{DEV}/webhooks/{endpoint_id}", headers=owner.headers)
    ).status_code == 404
    for action in (
        "webhook.created",
        "webhook.updated",
        "webhook.secret_rotated",
        "webhook.deleted",
    ):
        assert await db_audit(action), action


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",  # https only outside local development
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://192.168.1.1/x",
        "https://169.254.169.254/latest/meta-data/",
        "https://100.64.0.1/x",
        "https://0.0.0.0/x",
        "https://[::1]/x",
        "https://[::ffff:10.0.0.1]/x",
        "https://[fd00::1]/x",
        "https://user:pw@hooks.example.com/x",
        "https://intranet.internal.example/x",  # resolves to 10.0.0.5
        "https://nowhere.example/x",  # does not resolve
        "ftp://hooks.example.com/x",
        "https://hooks.example.com:99999/x",
    ],
)
async def test_webhook_url_policy_rejects(
    client: httpx.AsyncClient, owner: Account, url: str
) -> None:
    r = await client.post(
        f"{DEV}/webhooks", headers=owner.headers, json={"url": url, "events": ["*"]}
    )
    assert r.status_code == 422, (url, r.text)
    assert r.json()["error"]["code"] == "validation_failed"


async def test_webhook_url_policy_private_opt_in(
    client: httpx.AsyncClient, owner: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "allow_private_webhook_urls", True)
    r = await client.post(
        f"{DEV}/webhooks",
        headers=owner.headers,
        json={"url": "https://10.1.2.3/x", "events": ["*"]},
    )
    assert r.status_code == 201, r.text
    # The scheme rule is not relaxed by the private-address opt-in.
    r = await client.post(
        f"{DEV}/webhooks", headers=owner.headers, json={"url": "http://10.1.2.3/x", "events": ["*"]}
    )
    assert r.status_code == 422


async def test_lead_changes_write_outbox_events(client: httpx.AsyncClient, owner: Account) -> None:
    lead = await make_lead(client, owner, company="Acme")
    stages = await stage_ids(client, owner)
    r = await client.patch(
        f"/api/v1/leads/{lead['id']}", headers=owner.headers, json={"company": "Acme Ltd"}
    )
    assert r.status_code == 200
    r = await client.patch(
        f"/api/v1/leads/{lead['id']}", headers=owner.headers, json={"stage_id": stages["qualified"]}
    )
    assert r.status_code == 200

    outbox = await db_outbox()
    assert [e.event_type for e in outbox] == ["lead.created", "lead.updated", "lead.stage_changed"]
    assert all(e.processed_at is None for e in outbox)
    assert len({e.event_id for e in outbox}) == 3
    created = outbox[0].data
    assert created["lead_id"] == lead["id"] and created["stage"] == "new"
    assert created["first_name"] == "Ayesha" and created["company"] == "Acme"
    assert outbox[1].data["changed_fields"] == ["company"]
    assert outbox[2].data["from_stage"] == "new" and outbox[2].data["to_stage"] == "qualified"
    # No contact details leave the platform through events.
    serialized = json.dumps([e.data for e in outbox])
    assert "7946" not in serialized and "ayesha@example.com" not in serialized

    async with get_sessionmaker()() as db:
        with pytest.raises(ValueError, match="Unknown event type"):
            await events.emit(db, uuid.UUID(owner.org_id), "lead.exploded", {})
        await events.emit(
            db,
            uuid.UUID(owner.org_id),
            "lead.updated",
            {"lead_id": uuid.UUID(lead["id"]), "api_token": "s3cret", "when": now()},
        )
        await db.commit()
    last = (await db_outbox())[-1].data
    assert last["api_token"] == "[REDACTED]" and last["lead_id"] == lead["id"]
    assert isinstance(last["when"], str)


async def test_fan_out_creates_deliveries_for_subscribed_endpoints(
    client: httpx.AsyncClient, owner: Account
) -> None:
    only_created = await make_webhook(client, owner, ("lead.created",))
    everything = await make_webhook(client, owner, ("*",))
    inactive = await make_webhook(client, owner, ("*",))
    await client.patch(
        f"{DEV}/webhooks/{inactive['id']}", headers=owner.headers, json={"is_active": False}
    )
    other = await signup(client, "other@example.com", "Other Co")
    foreign = await make_webhook(client, other, ("*",))

    lead = await make_lead(client, owner)
    await client.patch(f"/api/v1/leads/{lead['id']}", headers=owner.headers, json={"company": "X"})

    assert await fan_out(get_sessionmaker(), now=now()) == 3
    assert [d.event_type for d in await db_deliveries(only_created["id"])] == ["lead.created"]
    assert [d.event_type for d in await db_deliveries(everything["id"])] == [
        "lead.created",
        "lead.updated",
    ]
    assert await db_deliveries(inactive["id"]) == []
    assert await db_deliveries(foreign["id"]) == []

    delivery = (await db_deliveries(only_created["id"]))[0]
    assert delivery.status == DeliveryStatus.PENDING and delivery.attempts == 0
    assert delivery.payload["type"] == "lead.created"
    assert delivery.payload["id"] == str(delivery.event_id)
    assert delivery.payload["organization_id"] == owner.org_id
    assert delivery.payload["data"]["lead_id"] == lead["id"]
    assert all(e.processed_at is not None for e in await db_outbox())
    assert await fan_out(get_sessionmaker(), now=now()) == 0


async def test_delivery_is_signed_and_verifiable(
    client: httpx.AsyncClient, owner: Account, hooks: respx.MockRouter
) -> None:
    created = await make_webhook(client, owner, ("lead.created",))
    route = hooks.post(HOOK_URL).mock(return_value=httpx.Response(200, text="ok"))
    lead = await make_lead(client, owner)
    clock = now()
    await fan_out(get_sessionmaker(), now=clock)
    assert await deliver_due(get_sessionmaker(), now=clock) == 1
    assert route.call_count == 1

    request = route.calls.last.request
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("ATLAS-Webhooks/")
    assert request.headers["x-atlas-event"] == "lead.created"
    timestamp = request.headers["x-atlas-timestamp"]
    assert abs(int(timestamp) - time.time()) < 60
    signature = request.headers["x-atlas-signature"]
    assert signature.startswith("v1=") and len(signature) == 3 + 64
    assert verify_signature(created["secret"], timestamp, request.content, signature)
    assert not verify_signature("whsec_wrong", timestamp, request.content, signature)
    assert not verify_signature(created["secret"], timestamp, request.content + b" ", signature)

    payload = json.loads(request.content)
    assert payload["id"] == request.headers["x-atlas-event-id"]
    assert payload["type"] == "lead.created" and payload["data"]["lead_id"] == lead["id"]
    assert request.headers["x-atlas-delivery-id"]

    (delivery,) = await db_deliveries(created["id"])
    assert delivery.status == DeliveryStatus.DELIVERED and delivery.attempts == 1
    assert delivery.last_status_code == 200 and delivery.delivered_at is not None
    assert delivery.next_attempt_at is None and delivery.last_error is None
    assert (await db_endpoint(created["id"])).failure_count == 0

    page = (
        await client.get(f"{DEV}/webhooks/{created['id']}/deliveries", headers=owner.headers)
    ).json()
    assert page["total"] == 1 and page["items"][0]["status"] == "delivered"
    assert page["items"][0]["payload"]["data"]["lead_id"] == lead["id"]


def test_signature_reference_implementation() -> None:
    body = webhooks.canonical_body({"b": 1, "a": {"z": "ü"}})
    assert body == '{"a":{"z":"ü"},"b":1}'
    expected = hmac.new(b"whsec_k", b"1700000000." + body.encode(), hashlib.sha256).hexdigest()
    assert webhooks.sign("whsec_k", "1700000000", body) == f"v1={expected}"


def test_backoff_schedule_doubles_with_jitter_and_caps() -> None:
    for attempt in range(1, 9):
        base = min(60 * 2 ** (attempt - 1), 7200)
        seconds = webhooks.backoff_seconds(attempt)
        assert base * 0.8 - 1 <= seconds <= base * 1.2 + 1, attempt
    assert webhooks.backoff_seconds(50) <= 7200 * 1.2


async def test_retries_back_off_then_dead_letter_and_manual_retry(
    client: httpx.AsyncClient, owner: Account, hooks: respx.MockRouter
) -> None:
    created = await make_webhook(client, owner, ("lead.created",))
    route = hooks.post(HOOK_URL).mock(return_value=httpx.Response(500))
    await make_lead(client, owner)
    clock = now()
    await fan_out(get_sessionmaker(), now=clock)

    assert await deliver_due(get_sessionmaker(), now=clock) == 1
    (delivery,) = await db_deliveries(created["id"])
    assert delivery.status == DeliveryStatus.FAILED and delivery.attempts == 1
    assert delivery.last_status_code == 500 and "HTTP 500" in (delivery.last_error or "")
    assert delivery.next_attempt_at is not None
    assert (
        clock + timedelta(seconds=47) <= delivery.next_attempt_at <= clock + timedelta(seconds=73)
    )
    # Not due yet: nothing is sent.
    assert await deliver_due(get_sessionmaker(), now=clock + timedelta(seconds=10)) == 0

    previous_gap = timedelta(0)
    for attempt in range(2, 9):
        assert delivery.next_attempt_at is not None
        gap = delivery.next_attempt_at - clock
        assert gap > previous_gap, "backoff must grow"
        previous_gap, clock = gap, delivery.next_attempt_at + timedelta(seconds=1)
        assert await deliver_due(get_sessionmaker(), now=clock) == 1
        (delivery,) = await db_deliveries(created["id"])
        assert delivery.attempts == attempt
        if attempt < 8:
            assert delivery.status == DeliveryStatus.FAILED
    assert delivery.status == DeliveryStatus.DEAD and delivery.next_attempt_at is None
    assert route.call_count == 8
    assert await deliver_due(get_sessionmaker(), now=clock + timedelta(days=1)) == 0

    page = (
        await client.get(
            f"{DEV}/webhooks/{created['id']}/deliveries",
            headers=owner.headers,
            params={"status": "dead"},
        )
    ).json()
    assert page["total"] == 1 and page["items"][0]["attempts"] == 8

    route.mock(return_value=httpx.Response(204))
    r = await client.post(f"{DEV}/deliveries/{delivery.id}/retry", headers=owner.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending" and r.json()["attempts"] == 0
    assert await deliver_due(get_sessionmaker(), now=clock + timedelta(days=1)) == 1
    (delivery,) = await db_deliveries(created["id"])
    assert delivery.status == DeliveryStatus.DELIVERED and delivery.last_status_code == 204
    r = await client.post(f"{DEV}/deliveries/{delivery.id}/retry", headers=owner.headers)
    assert r.status_code == 409
    assert await db_audit("webhook.delivery_retried")


async def test_endpoint_auto_disables_after_consecutive_failures(
    client: httpx.AsyncClient, owner: Account, hooks: respx.MockRouter
) -> None:
    created = await make_webhook(client, owner, ("*",))
    route = hooks.post(HOOK_URL).mock(return_value=httpx.Response(503))
    await db_exec(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.id == uuid.UUID(created["id"]))
        .values(failure_count=49)
    )
    lead = await make_lead(client, owner)
    clock = now()
    await fan_out(get_sessionmaker(), now=clock)
    assert await deliver_due(get_sessionmaker(), now=clock) == 1

    endpoint = await db_endpoint(created["id"])
    assert endpoint.is_active is False and endpoint.failure_count == 50
    assert endpoint.disabled_at is not None
    assert "50 consecutive" in (endpoint.disabled_reason or "")
    shown = (await client.get(f"{DEV}/webhooks/{created['id']}", headers=owner.headers)).json()
    assert shown["is_active"] is False and shown["disabled_reason"] == endpoint.disabled_reason
    (entry,) = await db_audit("webhook.auto_disabled")
    assert entry.actor_user_id is None and entry.changes["failure_count"] == 50
    assert entry.organization_id == uuid.UUID(owner.org_id)

    # Disabled endpoints receive nothing: no fan-out, and the failed delivery stays parked.
    await client.patch(f"/api/v1/leads/{lead['id']}", headers=owner.headers, json={"company": "Y"})
    assert await fan_out(get_sessionmaker(), now=clock) == 0
    assert await deliver_due(get_sessionmaker(), now=clock + timedelta(days=1)) == 0
    assert route.call_count == 1

    # Re-enabling clears the counter and the parked delivery goes out.
    route.mock(return_value=httpx.Response(200))
    r = await client.patch(
        f"{DEV}/webhooks/{created['id']}", headers=owner.headers, json={"is_active": True}
    )
    assert r.json()["is_active"] is True and r.json()["failure_count"] == 0
    assert r.json()["disabled_reason"] is None
    assert await deliver_due(get_sessionmaker(), now=clock + timedelta(days=1)) == 1
    (delivery,) = await db_deliveries(created["id"])
    assert delivery.status == DeliveryStatus.DELIVERED


async def test_test_endpoint_sends_signed_ping(
    client: httpx.AsyncClient, owner: Account, hooks: respx.MockRouter
) -> None:
    created = await make_webhook(client, owner)
    route = hooks.post(HOOK_URL).mock(return_value=httpx.Response(204))
    r = await client.post(f"{DEV}/webhooks/{created['id']}/test", headers=owner.headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["status_code"] == 204
    assert r.json()["latency_ms"] >= 0 and r.json()["error"] is None
    request = route.calls.last.request
    assert request.headers["x-atlas-event"] == "ping"
    assert verify_signature(
        created["secret"],
        request.headers["x-atlas-timestamp"],
        request.content,
        request.headers["x-atlas-signature"],
    )
    ping = json.loads(request.content)
    assert ping["type"] == "ping" and ping["data"]["endpoint_id"] == created["id"]
    assert ping["id"] == request.headers["x-atlas-event-id"]

    route.mock(return_value=httpx.Response(500))
    body = (await client.post(f"{DEV}/webhooks/{created['id']}/test", headers=owner.headers)).json()
    assert body["ok"] is False and body["status_code"] == 500 and "500" in body["error"]

    route.mock(side_effect=httpx.ConnectTimeout("slow"))
    body = (await client.post(f"{DEV}/webhooks/{created['id']}/test", headers=owner.headers)).json()
    assert body["ok"] is False and body["status_code"] is None
    assert "ConnectTimeout" in body["error"]

    # Redirects are never followed.
    route.mock(return_value=httpx.Response(302, headers={"location": "https://elsewhere.example/"}))
    body = (await client.post(f"{DEV}/webhooks/{created['id']}/test", headers=owner.headers)).json()
    assert body["ok"] is False and body["status_code"] == 302
    assert route.call_count == 4
    assert len(await db_audit("webhook.tested")) == 4


async def test_delivery_revalidates_url_before_sending(
    client: httpx.AsyncClient,
    owner: Account,
    hooks: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await make_webhook(client, owner, ("lead.created",))
    route = hooks.post(HOOK_URL).mock(return_value=httpx.Response(200))
    await make_lead(client, owner)
    clock = now()
    await fan_out(get_sessionmaker(), now=clock)

    async def rebinding(host: str, port: int) -> list[IPv4Address | IPv6Address]:
        return [ip_address("10.0.0.9")]

    monkeypatch.setattr(webhooks, "_resolve_host", rebinding)
    assert await deliver_due(get_sessionmaker(), now=clock) == 1
    assert route.call_count == 0
    (delivery,) = await db_deliveries(created["id"])
    assert delivery.status == DeliveryStatus.FAILED and delivery.last_status_code is None
    assert "security policy" in (delivery.last_error or "")


async def test_delivery_listing_redacts_and_filters(
    client: httpx.AsyncClient, owner: Account
) -> None:
    created = await make_webhook(client, owner, ("lead.updated",))
    async with get_sessionmaker()() as db:
        await events.emit(
            db, uuid.UUID(owner.org_id), "lead.updated", {"lead_id": "x", "password": "hunter2"}
        )
        await db.commit()
    await fan_out(get_sessionmaker(), now=now())
    url = f"{DEV}/webhooks/{created['id']}/deliveries"
    page = (await client.get(url, headers=owner.headers)).json()
    assert page["total"] == 1
    assert page["items"][0]["payload"]["data"]["password"] == "[REDACTED]"
    assert (await client.get(url, headers=owner.headers, params={"status": "delivered"})).json()[
        "total"
    ] == 0
    assert (await client.get(url, headers=owner.headers, params={"status": "pending"})).json()[
        "total"
    ] == 1
    assert (
        await client.get(url, headers=owner.headers, params={"status": "bogus"})
    ).status_code == 422


async def test_developer_resources_are_tenant_scoped(
    client: httpx.AsyncClient, owner: Account
) -> None:
    other = await signup(client, "other@example.com", "Other Co")
    key_body, _ = await make_key(client, owner)
    hook = await make_webhook(client, owner, ("*",))
    await make_lead(client, owner)
    await fan_out(get_sessionmaker(), now=now())
    (delivery,) = await db_deliveries(hook["id"])

    assert (await client.get(f"{DEV}/api-keys", headers=other.headers)).json() == []
    assert (
        await client.delete(f"{DEV}/api-keys/{key_body['id']}", headers=other.headers)
    ).status_code == 404
    assert (await client.get(f"{DEV}/webhooks", headers=other.headers)).json() == []
    hook_url = f"{DEV}/webhooks/{hook['id']}"
    assert (await client.get(hook_url, headers=other.headers)).status_code == 404
    assert (
        await client.patch(hook_url, headers=other.headers, json={"description": "x"})
    ).status_code == 404
    assert (await client.delete(hook_url, headers=other.headers)).status_code == 404
    assert (await client.post(f"{hook_url}/test", headers=other.headers)).status_code == 404
    assert (
        await client.post(f"{hook_url}/rotate-secret", headers=other.headers)
    ).status_code == 404
    assert (await client.get(f"{hook_url}/deliveries", headers=other.headers)).status_code == 404
    r = await client.post(f"{DEV}/deliveries/{delivery.id}/retry", headers=other.headers)
    assert r.status_code == 404
    # Nothing was changed by the foreign attempts.
    assert (await client.get(f"{DEV}/api-keys", headers=owner.headers)).json()[0][
        "revoked_at"
    ] is None
    assert (await client.get(hook_url, headers=owner.headers)).status_code == 200


async def test_purge_drops_old_history(client: httpx.AsyncClient, owner: Account) -> None:
    hook = await make_webhook(client, owner, ("*",))
    await make_lead(client, owner)
    clock = now()
    await fan_out(get_sessionmaker(), now=clock)
    assert await purge(get_sessionmaker(), now=clock) == 0
    await db_exec(update(OutboxEvent).values(processed_at=clock - timedelta(days=8)))
    await db_exec(
        update(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == uuid.UUID(hook["id"]))
        .values(status=DeliveryStatus.DELIVERED, created_at=clock - timedelta(days=31))
    )
    assert await purge(get_sessionmaker(), now=clock) == 2
    assert await db_outbox() == [] and await db_deliveries() == []


def test_worker_discovers_webhook_jobs() -> None:
    from app.workers.main import WorkerSettings

    names = {f.__name__ for f in WorkerSettings.functions}
    assert {"fan_out_outbox", "deliver_pending", "purge_webhook_history"} <= names
    cron_names = {c.name for c in WorkerSettings.cron_jobs}
    assert {"cron:fan_out_outbox", "cron:deliver_pending"} <= cron_names
