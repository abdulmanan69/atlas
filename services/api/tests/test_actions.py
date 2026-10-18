"""Actions (HTTP calls an agent can make) against a contract-faithful fake voice runtime."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core import net_policy
from app.core.config import Environment, get_settings
from app.core.net_policy import is_blocked_address
from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from app.services.actions import function_name
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import FakeRuntime, Org, _forbid_extra, _json

PUBLIC_IP = ip_address("93.184.216.34")
SECRET = "tok-super-secret-value"
HTTP_CONFIG_FIELDS = {
    "method",
    "url",
    "headers",
    "credential_uuid",
    "parameters",
    "preset_parameters",
    "timeout_ms",
    "body_template",
    "body_format",
}
CREDENTIAL_FIELDS = {
    "api_key": {"header_name", "api_key"},
    "bearer_token": {"token"},
    "basic_auth": {"username", "password"},
    "custom_header": {"header_name", "header_value"},
}


class ToolRuntime(FakeRuntime):
    """Adds the runtime's /tools and /credentials routes (api/routes/tool.py, credentials.py)."""

    def __init__(self) -> None:
        super().__init__()
        self.tools: dict[int, dict[str, dict[str, Any]]] = {}
        self.test_result: dict[str, Any] = {
            "status": "success",
            "status_code": 200,
            "data": {"slots": ["10:00", "14:30"]},
            "error": None,
            "hint": None,
        }

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if path.startswith("/tools"):
            return self._tools(org, m, path, body, request)
        if path.startswith("/credentials"):
            return self._credentials(org, m, path, body)
        return super()._authed(org, m, path, body, request)

    # -- credentials

    def _credentials(self, org: Org, m: str, path: str, body: Any) -> httpx.Response:
        if path.rstrip("/") == "/credentials":
            if m == "GET":
                return _json(
                    200,
                    [
                        {k: c[k] for k in ("uuid", "name", "description", "credential_type")}
                        for c in org.credentials.values()
                        if c.get("is_active", True)
                    ],
                )
            needed = CREDENTIAL_FIELDS[body["credential_type"]]
            if not needed <= set(body["credential_data"]):
                return _json(400, {"detail": "credential requires fields"})
            if any(c["name"] == body["name"] for c in org.credentials.values()):
                return _json(409, {"detail": "A credential with the name already exists"})
            cid = str(uuid.uuid4())
            org.credentials[cid] = {**body, "uuid": cid, "is_active": True}
            return _json(
                200,
                {
                    "uuid": cid,
                    "name": body["name"],
                    "description": body.get("description"),
                    "credential_type": body["credential_type"],
                    "created_at": "2026-10-18T00:00:00Z",
                    "updated_at": None,
                },
            )
        if mm := re.fullmatch(r"/credentials/([\w-]+)", path):
            cred = org.credentials.get(mm[1])
            if cred is None or not cred.get("is_active", True):
                return _json(404, {"detail": "Credential not found"})
            if m == "DELETE":
                cred["is_active"] = False
                return _json(200, {"status": "deleted", "uuid": mm[1]})
        return _json(404, {"detail": "Not Found"})

    # -- tools

    def _check_definition(self, org: Org, definition: dict[str, Any]) -> httpx.Response | None:
        if definition.get("type") == "http_api":
            if err := _forbid_extra(definition["config"], HTTP_CONFIG_FIELDS):
                return err
            if definition["config"]["method"] not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                return _json(422, {"detail": "method must be one of GET, POST, PUT, PATCH, DELETE"})
        cred = definition.get("config", {}).get("credential_uuid")
        if cred and not org.credentials.get(cred, {}).get("is_active"):
            return _json(404, {"detail": f"Credential '{cred}' was not found"})
        return None

    def _tool_response(self, tool: dict[str, Any]) -> httpx.Response:
        return _json(200, tool)

    def _tools(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        tools = self.tools.setdefault(org.id, {})
        if path.rstrip("/") == "/tools":
            if m == "GET":
                status = request.url.params.get("status")
                category = request.url.params.get("category")
                out = [
                    t
                    for t in tools.values()
                    if (t["status"] in status.split(",") if status else t["status"] != "archived")
                    and (not category or t["category"] == category)
                ]
                return _json(200, sorted(out, key=lambda t: t["name"]))
            category = body.get("category") or body["definition"]["type"]
            if category != body["definition"]["type"]:
                return _json(422, {"detail": "category must match definition.type"})
            if err := self._check_definition(org, body["definition"]):
                return err
            tid = str(uuid.uuid4())
            tools[tid] = {
                "id": next(self._ids),
                "tool_uuid": tid,
                "name": body["name"],
                "description": body.get("description"),
                "category": category,
                "icon": "globe",
                "icon_color": "#3B82F6",
                "status": "active",
                "definition": body["definition"],
                "created_at": "2026-10-18T00:00:00Z",
                "updated_at": None,
            }
            return self._tool_response(tools[tid])
        if not (mm := re.fullmatch(r"/tools/([\w-]+)(/test)?", path)):
            return _json(404, {"detail": "Not Found"})
        tool = tools.get(mm[1])
        if tool is None:
            return _json(404, {"detail": "Tool not found"})
        if mm[2] == "/test" and m == "POST":
            if tool["category"] != "http_api":
                return _json(400, {"detail": "Only HTTP API tools can be tested"})
            cfg = tool["definition"]["config"]
            return _json(
                200,
                {
                    **self.test_result,
                    "request_method": cfg["method"],
                    "request_url": cfg["url"],
                    "request_headers": {},
                    "request_body": {**body["preset_params"], **body["llm_params"]},
                    "request_params": None,
                    "duration_ms": 87,
                },
            )
        if m == "GET":
            return self._tool_response(tool)  # includes archived, like the runtime
        if m == "PUT":
            if body.get("definition") is not None:
                if err := self._check_definition(org, body["definition"]):
                    return err
                tool["definition"] = body["definition"]
            for key in ("name", "description", "status"):
                if body.get(key) is not None:
                    tool[key] = body[key]
            tool["updated_at"] = "2026-10-18T01:00:00Z"
            return self._tool_response(tool)
        if m == "DELETE":
            if tool["status"] == "archived":
                return _json(404, {"detail": "Tool not found"})
            tool["status"] = "archived"
            return _json(200, {"status": "archived", "tool_uuid": mm[1]})
        return _json(404, {"detail": "Not Found"})

    def seed_tool(self, org_id: int, category: str, name: str) -> str:
        tid = str(uuid.uuid4())
        self.tools.setdefault(org_id, {})[tid] = {
            "id": next(self._ids),
            "tool_uuid": tid,
            "name": name,
            "description": None,
            "category": category,
            "icon": None,
            "icon_color": None,
            "status": "active",
            "definition": {"type": category, "config": {}, "messages": []},
            "created_at": "2026-10-18T00:00:00Z",
            "updated_at": None,
        }
        return tid


@pytest.fixture
def runtime() -> Iterator[ToolRuntime]:
    fake = ToolRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        yield fake


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch real DNS: hosts resolve to a public address unless named otherwise."""

    async def resolve(host: str, port: int) -> list[IPv4Address | IPv6Address]:
        if host.endswith(".internal.example"):
            return [ip_address("10.0.0.5")]
        if host == "nowhere.example":
            return []
        return [PUBLIC_IP]

    monkeypatch.setattr(net_policy, "resolve_host", resolve)


def booking_action(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Book a viewing!",
        "description": "Books a property viewing once the caller confirmed a date and time.",
        "method": "POST",
        "url": "https://api.example.com/viewings",
        "headers": {"X-Source": "atlas"},
        "parameters": [
            {"name": "date", "type": "string", "description": "Viewing date, YYYY-MM-DD"},
            {"name": "guests", "type": "number", "description": "Party size", "required": False},
        ],
        "preset_parameters": [
            {"name": "phone", "type": "string", "value": "{{initial_context.phone_number}}"}
        ],
        "timeout_ms": 8000,
        "body_format": "json",
        "say_while_running": "Let me check that for you.",
        "auth": {"type": "bearer", "token": SECRET},
    }
    body.update(overrides)
    return body


async def create(client: httpx.AsyncClient, acct: Account, **overrides: Any) -> httpx.Response:
    return await client.post(
        "/api/v1/actions", headers=acct.headers, json=booking_action(**overrides)
    )


async def audit_entries(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# ------------------------------------------------------------------ CRUD


async def test_crud_round_trip_maps_to_runtime_http_tool(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    r = await create(client, owner)
    assert r.status_code == 201, r.text
    created = r.json()
    assert uuid.UUID(created["id"])
    assert created["function_name"] == "book_a_viewing"
    assert created["auth"] == {
        "type": "bearer",
        "header_name": "Authorization",
        "status": "configured",
    }
    assert created["say_while_running"] == "Let me check that for you."
    assert created["timeout_ms"] == 8000
    assert created["preset_parameters"][0]["value"] == "{{initial_context.phone_number}}"

    sent = runtime.last("POST", r"^/tools/?$")
    assert sent["category"] == "http_api"
    definition = sent["definition"]
    assert definition["type"] == "http_api" and definition["schema_version"] == 1
    cfg = definition["config"]
    assert cfg["method"] == "POST" and cfg["url"] == "https://api.example.com/viewings"
    assert cfg["headers"] == {"X-Source": "atlas"}
    assert cfg["parameters"] == [
        {
            "name": "date",
            "type": "string",
            "description": "Viewing date, YYYY-MM-DD",
            "required": True,
        },
        {"name": "guests", "type": "number", "description": "Party size", "required": False},
    ]
    assert cfg["preset_parameters"] == [
        {
            "name": "phone",
            "type": "string",
            "value_template": "{{initial_context.phone_number}}",
            "required": True,
        }
    ]
    assert cfg["timeout_ms"] == 8000 and cfg["body_format"] == "json"
    assert definition["messages"] == [
        {"event": "start", "type": "text", "text": "Let me check that for you."}
    ]
    org = next(iter(runtime.orgs.values()))
    cred = org.credentials[cfg["credential_uuid"]]
    assert cred["credential_type"] == "bearer_token"
    assert cred["credential_data"] == {"token": SECRET}

    listed = (await client.get("/api/v1/actions", headers=owner.headers)).json()
    assert [a["id"] for a in listed] == [created["id"]]
    assert listed[0] == created

    fetched = await client.get(f"/api/v1/actions/{created['id']}", headers=owner.headers)
    assert fetched.status_code == 200 and fetched.json() == created

    # Rename + rotate to an API key credential: the old credential is removed.
    r = await client.patch(
        f"/api/v1/actions/{created['id']}",
        headers=owner.headers,
        json={
            "name": "Book viewing v2",
            "auth": {"type": "api_key", "header_name": "X-Api-Key", "api_key": "k-123"},
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["function_name"] == "book_viewing_v2"
    assert updated["auth"] == {
        "type": "api_key",
        "header_name": "X-Api-Key",
        "status": "configured",
    }
    assert updated["url"] == created["url"] and updated["parameters"] == created["parameters"]
    assert cred["is_active"] is False
    new_cfg = runtime.tools[org.id][created["id"]]["definition"]["config"]
    assert org.credentials[new_cfg["credential_uuid"]]["credential_data"] == {
        "header_name": "X-Api-Key",
        "api_key": "k-123",
    }

    # Remove the credential explicitly; omitting `auth` keeps it.
    r = await client.patch(
        f"/api/v1/actions/{created['id']}", headers=owner.headers, json={"timeout_ms": 20000}
    )
    assert r.status_code == 422, "timeout is capped at 15000"
    r = await client.patch(
        f"/api/v1/actions/{created['id']}", headers=owner.headers, json={"timeout_ms": 15000}
    )
    assert r.status_code == 200 and r.json()["auth"]["type"] == "api_key"
    r = await client.patch(
        f"/api/v1/actions/{created['id']}", headers=owner.headers, json={"auth": None}
    )
    assert r.status_code == 200 and r.json()["auth"] is None
    assert runtime.tools[org.id][created["id"]]["definition"]["config"]["credential_uuid"] is None
    assert all(
        not c["is_active"]
        for c in org.credentials.values()
        if c["name"].startswith("atlas-action-")
    )
    assert sum(1 for c in org.credentials.values() if c["is_active"]) == 1, (
        "platform credential kept"
    )

    r = await client.delete(f"/api/v1/actions/{created['id']}", headers=owner.headers)
    assert r.status_code == 204
    assert runtime.tools[org.id][created["id"]]["status"] == "archived"
    assert (await client.get("/api/v1/actions", headers=owner.headers)).json() == []
    assert (
        await client.get(f"/api/v1/actions/{created['id']}", headers=owner.headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/actions/{created['id']}", headers=owner.headers)
    ).status_code == 404

    assert len(await audit_entries("action.created")) == 1
    assert len(await audit_entries("action.updated")) == 3
    assert len(await audit_entries("action.deleted")) == 1


async def test_name_and_parameter_validation(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    r = await create(client, owner, name="!!!")
    assert r.status_code == 422 and "letter or digit" in r.json()["error"]["message"]
    r = await create(
        client,
        owner,
        parameters=[{"name": "phone", "description": "x"}],
        preset_parameters=[{"name": "phone", "value": "1"}],
    )
    assert r.status_code == 422
    r = await create(client, owner, parameters=[{"name": "bad name", "description": "x"}])
    assert r.status_code == 422
    r = await create(client, owner, auth={"type": "api_key", "api_key": "k"})
    assert r.status_code == 422, "api_key auth needs a header name"
    assert runtime.count("POST", r"^/tools/?$") == 0


# ------------------------------------------------------------------ security


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://10.0.0.1/hook", "public address"),
        ("https://127.0.0.1:8080/hook", "public address"),
        ("https://169.254.169.254/latest/meta-data", "public address"),
        ("https://[::1]/hook", "public address"),
        ("https://[::ffff:10.0.0.1]/hook", "public address"),
        ("https://crm.internal.example/hook", "public address"),
        ("https://nowhere.example/hook", "could not be resolved"),
        ("http://api.example.com/hook", "must use https"),
        ("ftp://api.example.com/hook", "must use https"),
        ("https://user:pw@api.example.com/hook", "embed credentials"),
    ],
)
async def test_destination_policy_rejects_before_reaching_runtime(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime, url: str, reason: str
) -> None:
    r = await create(client, owner, url=url)
    assert r.status_code == 422, r.text
    assert reason in r.json()["error"]["message"]
    assert r.json()["error"]["details"] == {"field": "url"}
    assert runtime.count("POST", r"^/tools/?$") == 0
    assert runtime.count("POST", r"^/credentials/?$") == 0, "no secret stored for a rejected URL"


async def test_destination_policy_applies_on_update_and_test(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = (await create(client, owner)).json()
    r = await client.patch(
        f"/api/v1/actions/{created['id']}",
        headers=owner.headers,
        json={"url": "https://crm.internal.example/x"},
    )
    assert r.status_code == 422

    async def rebinding(host: str, port: int) -> list[IPv4Address | IPv6Address]:
        return [ip_address("10.0.0.9")]

    monkeypatch.setattr(net_policy, "resolve_host", rebinding)
    r = await client.post(f"/api/v1/actions/{created['id']}/test", headers=owner.headers, json={})
    assert r.status_code == 422 and "public address" in r.json()["error"]["message"]
    assert runtime.count("POST", r"/test$") == 0


async def test_private_urls_allowed_only_by_explicit_setting(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "allow_private_action_urls", True)
    r = await create(client, owner, url="https://crm.internal.example/hook")
    assert r.status_code == 201, r.text
    # The scheme rule still applies outside local development.
    r = await create(client, owner, url="http://crm.internal.example/hook")
    assert r.status_code == 422
    monkeypatch.setattr(get_settings(), "environment", Environment.LOCAL)
    r = await create(client, owner, url="http://api.example.com/hook")
    assert r.status_code == 201, r.text


async def test_reserved_and_conflicting_headers_are_rejected(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    for headers in (
        {"Authorization": "Bearer x"},
        {"authorization": "Bearer x"},
        {"Cookie": "a=b"},
        {"Proxy-Authorization": "x"},
        {"Host": "evil.example"},
        {"X Bad": "x"},
        {"X-Ok": "line\r\nInjected: 1"},
        {"X-Dup": "1", "x-dup": "2"},
    ):
        r = await create(client, owner, headers=headers)
        assert r.status_code == 422, headers
        assert r.json()["error"]["details"] == {"field": "headers"}
    r = await create(
        client,
        owner,
        headers={"X-Api-Key": "static"},
        auth={"type": "api_key", "header_name": "x-api-key", "api_key": "k"},
    )
    assert r.status_code == 422 and "credential" in r.json()["error"]["message"]
    assert runtime.count("POST", r"^/tools/?$") == 0
    assert runtime.count("POST", r"^/credentials/?$") == 0

    r = await create(client, owner, headers={"X-Api-Key": "static"})
    assert r.status_code == 201
    r = await client.patch(
        f"/api/v1/actions/{r.json()['id']}",
        headers=owner.headers,
        json={"auth": {"type": "custom_header", "header_name": "X-API-KEY", "header_value": "v"}},
    )
    assert r.status_code == 422, "a credential cannot take over an existing static header"


async def test_only_http_actions_exist_never_transfers(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    created = (await create(client, owner)).json()
    org = next(iter(runtime.orgs.values()))
    transfer = runtime.seed_tool(org.id, "transfer_call", "Transfer to human")
    agent_transfer = runtime.seed_tool(org.id, "transfer_agent", "Hand to billing")
    end_call = runtime.seed_tool(org.id, "end_call", "Hang up")

    listed = (await client.get("/api/v1/actions", headers=owner.headers)).json()
    assert [a["id"] for a in listed] == [created["id"]]
    for hidden in (transfer, agent_transfer, end_call):
        assert (
            await client.get(f"/api/v1/actions/{hidden}", headers=owner.headers)
        ).status_code == 404
        assert (
            await client.patch(
                f"/api/v1/actions/{hidden}", headers=owner.headers, json={"name": "x"}
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/v1/actions/{hidden}", headers=owner.headers)
        ).status_code == 404
        assert runtime.tools[org.id][hidden]["status"] == "active"
    assert (
        await client.get("/api/v1/actions/not-a-uuid", headers=owner.headers)
    ).status_code == 404

    for method, path, body in runtime.calls:
        if method in ("POST", "PUT") and path.startswith("/tools") and isinstance(body, dict):
            assert body.get("category", "http_api") == "http_api"
            assert body["definition"]["type"] == "http_api"


async def test_credentials_are_never_echoed(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    r = await create(client, owner)
    assert r.status_code == 201 and SECRET not in r.text
    aid = r.json()["id"]
    r = await client.patch(
        f"/api/v1/actions/{aid}",
        headers=owner.headers,
        json={"auth": {"type": "basic", "username": "u", "password": SECRET}},
    )
    assert r.status_code == 200 and SECRET not in r.text and "u" not in r.json()["auth"].values()
    assert r.json()["auth"] == {
        "type": "basic",
        "header_name": "Authorization",
        "status": "configured",
    }
    for resp in (
        await client.get("/api/v1/actions", headers=owner.headers),
        await client.get(f"/api/v1/actions/{aid}", headers=owner.headers),
    ):
        assert SECRET not in resp.text
    for entry in [*await audit_entries("action.created"), *await audit_entries("action.updated")]:
        assert SECRET not in str(entry.changes)
        assert entry.changes["auth_type"] in ("bearer", "basic")
        assert "header_names" in entry.changes and "headers" not in entry.changes


async def test_roles_and_tenants(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    created = (await create(client, owner)).json()
    aid = created["id"]

    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    agent = await invite_and_join(client, owner, "a@example.com", "agent")
    manager = await invite_and_join(client, owner, "m@example.com", "manager")
    for reader in (viewer, agent):
        assert (await client.get("/api/v1/actions", headers=reader.headers)).status_code == 200
        assert (
            await client.get(f"/api/v1/actions/{aid}", headers=reader.headers)
        ).status_code == 200
        assert (await create(client, reader)).status_code == 403
        assert (
            await client.patch(f"/api/v1/actions/{aid}", headers=reader.headers, json={"name": "x"})
        ).status_code == 403
        assert (
            await client.delete(f"/api/v1/actions/{aid}", headers=reader.headers)
        ).status_code == 403
        assert (
            await client.post(f"/api/v1/actions/{aid}/test", headers=reader.headers, json={})
        ).status_code == 403
    assert (await create(client, manager, name="Manager action")).status_code == 201

    other = await signup(client, "other@example.com", "Other Org")
    assert (await client.get("/api/v1/actions", headers=other.headers)).json() == []
    assert (await client.get(f"/api/v1/actions/{aid}", headers=other.headers)).status_code == 404
    assert (
        await client.patch(f"/api/v1/actions/{aid}", headers=other.headers, json={"name": "x"})
    ).status_code == 404
    assert (await client.delete(f"/api/v1/actions/{aid}", headers=other.headers)).status_code == 404
    assert (
        await client.post(f"/api/v1/actions/{aid}/test", headers=other.headers, json={})
    ).status_code == 404
    assert len(runtime.orgs) == 2
    owner_org = min(runtime.orgs.values(), key=lambda o: o.id)
    assert runtime.tools[owner_org.id][aid]["status"] == "active"


# ------------------------------------------------------------------ test endpoint


async def test_test_endpoint_runs_the_runtime_tool_test(
    client: httpx.AsyncClient, owner: Account, runtime: ToolRuntime
) -> None:
    aid = (await create(client, owner)).json()["id"]
    r = await client.post(
        f"/api/v1/actions/{aid}/test",
        headers=owner.headers,
        json={"parameters": {"date": "2026-11-02"}, "preset_parameters": {"phone": "+15550100"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status_code"] == 200 and body["latency_ms"] == 87
    assert body["request_method"] == "POST"
    assert body["request_url"] == "https://api.example.com/viewings"
    assert body["response_excerpt"] == '{"slots": ["10:00", "14:30"]}'
    assert body["error"] is None and body["hint"] is None
    sent = runtime.last("POST", r"/test$")
    assert sent == {"llm_params": {"date": "2026-11-02"}, "preset_params": {"phone": "+15550100"}}

    runtime.test_result = {
        "status": "error",
        "status_code": 401,
        "data": {"message": "bad key"},
        "error": "HTTP 401",
        "hint": "HTTP 401 Unauthorized - check the credential",
    }
    r = await client.post(f"/api/v1/actions/{aid}/test", headers=owner.headers, json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["status_code"] == 401
    assert r.json()["hint"].startswith("HTTP 401")

    runtime.test_result = {
        "status": "error",
        "status_code": None,
        "data": None,
        "error": "timed out",
    }
    r = await client.post(f"/api/v1/actions/{aid}/test", headers=owner.headers, json={})
    assert r.json() == {
        "ok": False,
        "status_code": None,
        "latency_ms": 87,
        "request_method": "POST",
        "request_url": "https://api.example.com/viewings",
        "response_excerpt": None,
        "error": "timed out",
        "hint": None,
    }
    tested = await audit_entries("action.tested")
    assert [e.changes["ok"] for e in tested] == [True, False, False]

    runtime.down = True
    r = await client.post(f"/api/v1/actions/{aid}/test", headers=owner.headers, json={})
    assert r.status_code == 503


# ------------------------------------------------------------------ units


@pytest.mark.parametrize(
    ("ip", "blocked"),
    [
        ("127.0.0.1", True),
        ("10.0.0.1", True),
        ("172.16.5.5", True),
        ("192.168.1.1", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("0.0.0.0", True),  # noqa: S104
        ("224.0.0.1", True),
        ("::1", True),
        ("fe80::1", True),
        ("fd00::1", True),
        ("::ffff:10.0.0.1", True),
        ("2002:0a00:0001::1", True),
        ("2001:db8::1", True),
        ("8.8.8.8", False),
        ("93.184.216.34", False),
        ("2606:4700::1111", False),
    ],
)
def test_blocked_addresses(ip: str, blocked: bool) -> None:
    assert is_blocked_address(ip_address(ip)) is blocked


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Book a viewing!", "book_a_viewing"),
        ("  Check--Availability  ", "check_availability"),
        ("CRM: update_lead", "crm_update_lead"),
        ("___", ""),
        ("Ünïcode name", "n_code_name"),
    ],
)
def test_function_name_mirrors_runtime_rule(name: str, expected: str) -> None:
    assert function_name(name) == expected
