"""Tenant isolation and role-based authorization."""

from __future__ import annotations

import uuid

import httpx

from tests.conftest import Account, invite_and_join, signup

LEAD = {"first_name": "Ayesha", "last_name": "Khan", "phone": "+44 20 7946 0001"}


async def _lead(client: httpx.AsyncClient, acct: Account, **extra: object) -> dict[str, object]:
    r = await client.post("/api/v1/leads", headers=acct.headers, json={**LEAD, **extra})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_other_tenant_cannot_see_or_touch_records(client: httpx.AsyncClient) -> None:
    a = await signup(client, "a@example.com", "Tenant A")
    b = await signup(client, "b@example.com", "Tenant B")
    lead = await _lead(client, a)
    lead_url = f"/api/v1/leads/{lead['id']}"

    # B, within its own org, cannot address A's lead by id.
    assert (await client.get(lead_url, headers=b.headers)).status_code == 404
    assert (
        await client.patch(lead_url, headers=b.headers, json={"first_name": "X"})
    ).status_code == 404
    assert (await client.delete(lead_url, headers=b.headers)).status_code == 404
    assert (await client.get(f"{lead_url}/activities", headers=b.headers)).status_code == 404
    assert (
        await client.post(f"{lead_url}/notes", headers=b.headers, json={"body": "x"})
    ).status_code == 404
    listing = (await client.get("/api/v1/leads", headers=b.headers)).json()
    assert listing["total"] == 0

    # B cannot claim A's organization via the header.
    spoofed = {**b.headers, "X-Organization-Id": a.org_id}
    r = await client.get("/api/v1/leads", headers=spoofed)
    assert r.status_code == 403
    # Unknown org gives the same answer as a foreign org (no enumeration).
    unknown = {**b.headers, "X-Organization-Id": str(uuid.uuid4())}
    r_unknown = await client.get("/api/v1/leads", headers=unknown)
    assert r_unknown.status_code == 403
    assert r_unknown.json()["error"]["message"] == r.json()["error"]["message"]

    # Bulk actions silently ignore foreign ids.
    r = await client.post(
        "/api/v1/leads/bulk",
        headers=b.headers,
        json={"lead_ids": [lead["id"]], "action": "add_tags", "tags": ["x"]},
    )
    assert r.json() == {"affected": 0}

    # A's lead is untouched and the same phone is free to use in B (dedup is per tenant).
    assert (await client.get(lead_url, headers=a.headers)).json()["tags"] == []
    await _lead(client, b)

    # Tasks and audit logs are scoped too.
    r = await client.post(
        "/api/v1/tasks", headers=b.headers, json={"title": "Call", "lead_id": lead["id"]}
    )
    assert r.status_code == 404
    audit_b = (await client.get("/api/v1/audit-logs", headers=b.headers)).json()
    assert all(item["resource_id"] != lead["id"] for item in audit_b["items"])


async def test_missing_org_header_is_rejected(client: httpx.AsyncClient, owner: Account) -> None:
    r = await client.get("/api/v1/leads", headers={"Authorization": owner.headers["Authorization"]})
    assert r.status_code == 422


async def test_viewer_is_read_only(client: httpx.AsyncClient, owner: Account) -> None:
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    await _lead(client, owner)
    assert (await client.get("/api/v1/leads", headers=viewer.headers)).json()["total"] == 1
    r = await client.post("/api/v1/leads", headers=viewer.headers, json={"first_name": "No"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    assert (await client.get("/api/v1/audit-logs", headers=viewer.headers)).status_code == 403
    assert (await client.get("/api/v1/leads/export", headers=viewer.headers)).status_code == 403


async def test_agent_can_work_leads_but_not_import_or_delete(
    client: httpx.AsyncClient, owner: Account
) -> None:
    agent = await invite_and_join(client, owner, "agent@example.com", "agent")
    lead = await _lead(client, agent)
    r = await client.delete(f"/api/v1/leads/{lead['id']}", headers=agent.headers)
    assert r.status_code == 403
    files = {"file": ("l.csv", b"first_name,phone\nA,+442079460002\n", "text/csv")}
    assert (
        await client.post("/api/v1/leads/import", headers=agent.headers, files=files)
    ).status_code == 403
    r = await client.post(
        "/api/v1/leads/bulk",
        headers=agent.headers,
        json={"lead_ids": [lead["id"]], "action": "delete"},
    )
    assert r.status_code == 403


async def test_role_escalation_is_blocked(client: httpx.AsyncClient, owner: Account) -> None:
    admin = await invite_and_join(client, owner, "admin@example.com", "admin")
    # Admin may invite a manager, but not an owner.
    r = await client.post(
        "/api/v1/invitations",
        headers=admin.headers,
        json={"email": "boss@example.com", "role": "owner"},
    )
    assert r.status_code == 403
    r = await client.post(
        "/api/v1/invitations",
        headers=admin.headers,
        json={"email": "mgr@example.com", "role": "manager"},
    )
    assert r.status_code == 201

    members = (await client.get("/api/v1/members", headers=admin.headers)).json()
    owner_member = next(m for m in members if m["role"] == "owner")
    r = await client.patch(
        f"/api/v1/members/{owner_member['id']}", headers=admin.headers, json={"role": "viewer"}
    )
    assert r.status_code == 403


async def test_last_owner_cannot_be_demoted(client: httpx.AsyncClient, owner: Account) -> None:
    members = (await client.get("/api/v1/members", headers=owner.headers)).json()
    r = await client.patch(
        f"/api/v1/members/{members[0]['id']}", headers=owner.headers, json={"role": "admin"}
    )
    assert r.status_code == 422


async def test_invitation_single_use_and_revocable(
    client: httpx.AsyncClient, owner: Account
) -> None:
    r = await client.post(
        "/api/v1/invitations",
        headers=owner.headers,
        json={"email": "late@example.com", "role": "agent"},
    )
    inv = r.json()
    dup = await client.post(
        "/api/v1/invitations",
        headers=owner.headers,
        json={"email": "LATE@example.com", "role": "agent"},
    )
    assert dup.status_code == 409
    assert (
        await client.delete(f"/api/v1/invitations/{inv['id']}", headers=owner.headers)
    ).status_code == 204
    r = await client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": inv["token"], "full_name": "L", "password": "x" * 14},
    )
    assert r.status_code == 422


async def test_user_in_two_orgs_switches_by_header(client: httpx.AsyncClient) -> None:
    a = await signup(client, "multi@example.com", "First Org")
    r = await client.post(
        "/api/v1/organizations",
        headers={"Authorization": f"Bearer {a.token}"},
        json={"name": "Second Org", "timezone": "Asia/Karachi", "default_country": "PK"},
    )
    assert r.status_code == 201
    second = r.json()["organization"]["id"]
    await _lead(client, a)
    hdrs = {"Authorization": f"Bearer {a.token}", "X-Organization-Id": second}
    assert (await client.get("/api/v1/leads", headers=hdrs)).json()["total"] == 0
    assert (await client.get("/api/v1/organization", headers=hdrs)).json()[
        "timezone"
    ] == "Asia/Karachi"
