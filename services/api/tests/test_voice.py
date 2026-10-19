"""AI providers and agents against a contract-faithful fake voice runtime."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.workflow_builder import OPT_OUT_FIELD, compile_prompt
from app.models.audit import AuditLog
from app.models.voice import RuntimeAccount
from app.schemas.voice import AgentConfig
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime


@pytest.fixture
def runtime() -> Iterator[FakeRuntime]:
    fake = FakeRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        yield fake


async def _setup_models(client: httpx.AsyncClient, acct: Account) -> dict[str, str]:
    ids = {}
    for provider in ("openai", "deepgram", "elevenlabs"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=acct.headers,
            json={
                "name": provider.title(),
                "provider": provider,
                "credentials": {"api_key": VALID_KEY},
            },
        )
        assert r.status_code == 201, r.text
        ids[provider] = r.json()["id"]
    r = await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={
            "llm": {
                "connection_id": ids["openai"],
                "settings": {"model": "gpt-4.1-mini", "temperature": 0.3},
            },
            "stt": {"connection_id": ids["deepgram"], "settings": {"model": "nova-3"}},
            "tts": {"connection_id": ids["elevenlabs"], "settings": {"voice": "v1"}},
        },
    )
    assert r.status_code == 200, r.text
    return ids


async def _agent(client: httpx.AsyncClient, acct: Account, **body: object) -> dict[str, object]:
    r = await client.post(
        "/api/v1/agents", headers=acct.headers, json={"name": "Qualifier", **body}
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


# ------------------------------------------------------------------ provisioning & providers


async def test_tenant_provisioning_is_isolated_and_removes_vendor_models(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    assert (await client.get("/api/v1/ai/connections", headers=owner.headers)).json() == []
    await client.get("/api/v1/ai/connections", headers=owner.headers)
    assert runtime.count("POST", "^/auth/signup$") == 1, "provisioned exactly once"

    org = next(iter(runtime.orgs.values()))
    bootstrap = next(iter(org.connections.values()))
    assert bootstrap["is_active"], "kept until the tenant chooses its own models (runtime rule)"
    models = (await client.get("/api/v1/ai/models", headers=owner.headers)).json()
    assert models == {"llm": None, "stt": None, "tts": None, "embeddings": None}
    cred = next(iter(org.credentials.values()))
    assert cred["credential_type"] == "custom_header"

    async with get_sessionmaker()() as db:
        account = (await db.execute(select(RuntimeAccount))).scalar_one()
    assert account.api_key_encrypted and "key-" not in account.api_key_encrypted

    other = await signup(client, "other@example.com", "Other Org")
    await client.get("/api/v1/ai/connections", headers=other.headers)
    assert len(runtime.orgs) == 2


async def test_catalog_offers_only_supported_providers(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    cat = (await client.get("/api/v1/ai/catalog", headers=owner.headers)).json()
    assert [p["id"] for p in cat["llm"]] == ["openai", "speaches"]
    local = next(p for p in cat["llm"] if p["id"] == "speaches")
    assert local["self_hosted"] and local["title"].startswith("Self-hosted")
    assert "vendorhosted" not in str(cat)


async def test_connection_keys_are_verified_and_never_echoed(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    bad = await client.post(
        "/api/v1/ai/connections",
        headers=owner.headers,
        json={"name": "OpenAI", "provider": "openai", "credentials": {"api_key": "sk-wrong"}},
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "voice_service_rejected"
    assert "invalid credentials" in bad.json()["error"]["message"]

    ok = await client.post(
        "/api/v1/ai/connections",
        headers=owner.headers,
        json={"name": "OpenAI", "provider": "openai", "credentials": {"api_key": VALID_KEY}},
    )
    assert ok.status_code == 201
    assert ok.json()["configured_credentials"] == ["api_key"]
    assert VALID_KEY not in ok.text

    unsupported = await client.post(
        "/api/v1/ai/connections",
        headers=owner.headers,
        json={"name": "X", "provider": "vendorhosted", "credentials": {"api_key": "k"}},
    )
    assert unsupported.status_code == 422
    smuggled = await client.post(
        "/api/v1/ai/connections",
        headers=owner.headers,
        json={"name": "X", "provider": "openai", "credentials": {"password": "p"}},
    )
    assert smuggled.status_code == 422

    async with get_sessionmaker()() as db:
        logs = (
            (await db.execute(select(AuditLog).where(AuditLog.action == "ai.connection_created")))
            .scalars()
            .all()
        )
    assert len(logs) == 1 and VALID_KEY not in str(logs[0].changes)


async def test_self_hosted_connection_needs_no_key(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    r = await client.post(
        "/api/v1/ai/connections",
        headers=owner.headers,
        json={
            "name": "Local LLM",
            "provider": "speaches",
            "connection_settings": {"base_url": "http://host.docker.internal:11434/v1"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["connection_settings"] == {"base_url": "http://host.docker.internal:11434/v1"}


async def test_model_selection_roundtrip_and_in_use_protection(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    ids = await _setup_models(client, owner)
    org = next(iter(runtime.orgs.values()))
    vendor = [c for c in org.connections.values() if c["provider"] == "vendorhosted"]
    assert vendor and not vendor[0]["is_active"], "bootstrap models retired after first save"
    sel = (await client.get("/api/v1/ai/models", headers=owner.headers)).json()
    assert sel["llm"] == {
        "connection_id": ids["openai"],
        "settings": {"model": "gpt-4.1-mini", "temperature": 0.3},
    }
    assert (await client.post("/api/v1/ai/models/check", headers=owner.headers)).json() == {
        "ok": True,
        "problems": [],
    }

    # Saving again updates the same managed configuration instead of creating another.
    ids = await _setup_models(client, owner)
    assert runtime.count("POST", "^/model-connections/model-configurations$") == 1

    r = await client.delete(f"/api/v1/ai/connections/{ids['openai']}", headers=owner.headers)
    assert r.status_code == 422 and "in use" in r.json()["error"]["message"]

    org = next(iter(runtime.orgs.values()))
    org.validate_problems = [{"model": "tts", "message": "quota exceeded"}]
    check = (await client.post("/api/v1/ai/models/check", headers=owner.headers)).json()
    assert check == {"ok": False, "problems": ["TTS: quota exceeded"]}


async def test_only_admins_change_providers(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    assert (await client.get("/api/v1/ai/connections", headers=manager.headers)).status_code == 200
    r = await client.post(
        "/api/v1/ai/connections",
        headers=manager.headers,
        json={"name": "OpenAI", "provider": "openai", "credentials": {"api_key": VALID_KEY}},
    )
    assert r.status_code == 403
    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    assert (await client.get("/api/v1/ai/connections", headers=viewer.headers)).status_code == 403


async def test_runtime_outage_is_reported_as_degraded(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    runtime.down = True
    r = await client.get("/api/v1/ai/connections", headers=owner.headers)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "integration_unavailable"
    # CRM keeps working while voice is down.
    assert (await client.get("/api/v1/leads", headers=owner.headers)).status_code == 200


# ------------------------------------------------------------------ agents


async def test_agent_from_template_is_personalised(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    agent = await _agent(client, owner, template="appointment_setter")
    cfg = agent["config"]
    assert cfg["company_name"] == "Acme Realty"  # type: ignore[index]
    assert "Acme Realty" in cfg["greeting"]  # type: ignore[index]
    assert agent["status"] == "draft" and agent["published_version"] is None
    templates = (await client.get("/api/v1/agents/templates", headers=owner.headers)).json()
    assert {t["id"] for t in templates} >= {"lead_qualifier", "appointment_setter", "reengagement"}


async def test_config_versions_only_on_change_and_restore(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"
    cfg = dict(agent["config"])  # type: ignore[call-overload]
    same = await client.put(f"{url}/config", headers=owner.headers, json={"config": cfg})
    assert same.json()["latest_version"] == 1
    cfg["greeting"] = "Hello {{first_name}}!"
    changed = await client.put(
        f"{url}/config", headers=owner.headers, json={"config": cfg, "note": "Shorter greeting"}
    )
    assert changed.json()["latest_version"] == 2
    restored = await client.post(f"{url}/versions/1/restore", headers=owner.headers)
    assert restored.json()["latest_version"] == 3
    assert restored.json()["config"]["greeting"] == agent["config"]["greeting"]  # type: ignore[index]
    versions = (await client.get(f"{url}/versions", headers=owner.headers)).json()
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert versions[0]["note"] == "Restored from version 1"


async def test_publish_requires_ai_providers(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    agent = await _agent(client, owner)
    r = await client.post(f"/api/v1/agents/{agent['id']}/publish", headers=owner.headers, json={})
    assert r.status_code == 422
    assert "AI providers" in r.json()["error"]["message"]
    assert runtime.count("POST", "^/workflow/create") == 0


async def test_publish_compiles_and_syncs_workflow(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    await _setup_models(client, owner)
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"
    r = await client.post(f"{url}/publish", headers=owner.headers, json={"note": "First release"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active" and body["published_version"] == 1
    assert body["sync_status"] == "synced" and not body["has_unpublished_changes"]
    assert "runtime_workflow_id" not in body and "workflow" not in str(body).lower()

    sent = runtime.last("PUT", r"^/workflow/\d+$")
    nodes = {n["type"]: n for n in sent["workflow_definition"]["nodes"]}
    assert nodes["startCall"]["data"]["greeting"] == agent["config"]["greeting"]  # type: ignore[index]
    assert "Acme Realty" in nodes["startCall"]["data"]["prompt"]
    extraction = {v["name"] for v in nodes["endCall"]["data"]["extraction_variables"]}
    assert OPT_OUT_FIELD in extraction and "budget" in extraction
    report = nodes["webhook"]["data"]
    assert (
        report["endpoint_url"] == "http://host.docker.internal:8080/api/v1/webhooks/voice-runtime"
    )
    assert report["payload_template"]["atlas_call_id"] == "{{initial_context.atlas_call_id}}"
    codes = [d["code"] for d in sent["workflow_configurations"]["call_dispositions"]]
    assert "qualified" in codes and "do_not_call" in codes

    # Editing creates unpublished changes; republishing updates the same workflow.
    cfg = dict(body["config"])
    cfg["closing"] = "Thank them and confirm the follow-up."
    edited = (await client.put(f"{url}/config", headers=owner.headers, json={"config": cfg})).json()
    assert edited["has_unpublished_changes"]
    await client.post(f"{url}/publish", headers=owner.headers, json={})
    assert runtime.count("POST", "^/workflow/create") == 1
    assert runtime.count("POST", r"^/workflow/\d+/publish$") == 2


async def test_publish_failure_is_explained_in_guided_terms(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ATLAS validates before the voice service; issues point at the guided form's fields."""
    await _setup_models(client, owner)
    agent = await _agent(client, owner)
    from app.integrations.voice_runtime import workflow_builder

    monkeypatch.setattr(workflow_builder, "compile_prompt", lambda cfg: " ")
    r = await client.post(f"/api/v1/agents/{agent['id']}/publish", headers=owner.headers, json={})
    assert r.status_code == 422
    [detail] = r.json()["error"]["details"]
    assert (detail["section"], detail["field"], detail["target"]) == (
        "Script",
        "instructions",
        "node",
    )
    assert runtime.count("POST", "^/workflow/create") == 0
    body = (await client.get(f"/api/v1/agents/{agent['id']}", headers=owner.headers)).json()
    assert body["sync_status"] == "not_synced" and body["published_version"] is None


async def test_lifecycle_state_machine(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    await _setup_models(client, owner)
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"
    assert (await client.post(f"{url}/pause", headers=owner.headers)).status_code == 409
    await client.post(f"{url}/publish", headers=owner.headers, json={})
    assert (await client.post(f"{url}/pause", headers=owner.headers)).json()["status"] == "paused"
    assert (await client.post(f"{url}/resume", headers=owner.headers)).json()["status"] == "active"
    archived = await client.post(f"{url}/archive", headers=owner.headers)
    assert archived.json()["status"] == "archived"
    wf = next(iter(next(iter(runtime.orgs.values())).workflows.values()))
    assert wf["status"] == "archived"
    assert (
        await client.put(f"{url}/config", headers=owner.headers, json={"config": agent["config"]})
    ).status_code == 409
    listed = (await client.get("/api/v1/agents", headers=owner.headers)).json()
    assert listed == []
    assert (await client.post(f"{url}/unarchive", headers=owner.headers)).json()[
        "status"
    ] == "paused"


async def test_browser_test_session(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    await _setup_models(client, owner)
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"
    assert (
        await client.post(f"{url}/test-session", headers=owner.headers, json={})
    ).status_code == 409
    await client.post(f"{url}/publish", headers=owner.headers, json={})

    lead = (
        await client.post(
            "/api/v1/leads",
            headers=owner.headers,
            json={"first_name": "Hina", "company": "Northwind"},
        )
    ).json()
    r = await client.post(
        f"{url}/test-session", headers=owner.headers, json={"lead_id": lead["id"]}
    )
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["signaling_url"].startswith(
        "ws://localhost:8000/api/v1/ws/public/signaling/emb_session_"
    )
    assert s["ice_servers"] == [
        {"urls": "stun:stun.l.google.com:19302", "username": None, "credential": None}
    ]
    assert s["variables"]["first_name"] == "Hina" and s["variables"]["company"] == "Northwind"
    # The session is restricted to the web app's origin; no API key reaches the browser.
    token_req = runtime.last("POST", r"/embed-token$")
    assert token_req["allowed_domains"] == ["localhost:5173"]
    assert "key-" not in r.text


async def test_browser_test_session_gets_the_turn_relay(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    """With a TURN server on the voice service, browsers get its time-limited credentials."""
    await _setup_models(client, owner)
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"
    await client.post(f"{url}/publish", headers=owner.headers, json={})
    runtime.turn_enabled = True
    r = await client.post(f"{url}/test-session", headers=owner.headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["ice_servers"] == [
        {"urls": "stun:turn.example.test:3478", "username": None, "credential": None},
        {
            "urls": [
                "turn:turn.example.test:3478",
                "turn:turn.example.test:3478?transport=tcp",
            ],
            "username": "1900000000:embed:emb_session",
            "credential": "hmac-of-the-username",
        },
    ]


async def test_agents_are_tenant_isolated_and_role_gated(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime
) -> None:
    agent = await _agent(client, owner)
    other = await signup(client, "b@example.com", "Tenant B")
    assert (
        await client.get(f"/api/v1/agents/{agent['id']}", headers=other.headers)
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/agents/{agent['id']}/publish", headers=other.headers, json={})
    ).status_code == 404
    assert (await client.get("/api/v1/agents", headers=other.headers)).json() == []

    agent_role = await invite_and_join(client, owner, "rep@example.com", "agent")
    assert (await client.get("/api/v1/agents", headers=agent_role.headers)).status_code == 200
    assert (
        await client.post("/api/v1/agents", headers=agent_role.headers, json={"name": "X"})
    ).status_code == 403


def test_prompt_compiler_includes_rules_and_questions() -> None:
    cfg = AgentConfig(
        company_name="Acme",
        greeting="Hi",
        instructions="Qualify the lead for a mortgage product.",
        qualification_questions=["What is your budget?", "  ", "When do you want to move?"],
        language="ur",
    )
    prompt = compile_prompt(cfg)
    assert "on behalf of Acme" in prompt
    assert "1. What is your budget?\n2. When do you want to move?" in prompt
    assert "Speak in Urdu" in prompt and "AI assistant" in prompt
    no_disclosure = compile_prompt(cfg.model_copy(update={"ai_disclosure": False}))
    assert "AI assistant" not in no_disclosure


def test_config_rejects_duplicate_or_reserved_fields() -> None:
    with pytest.raises(ValueError):
        AgentConfig(
            company_name="A",
            greeting="Hi",
            instructions="x" * 25,
            capture_fields=[{"name": "opted_out", "description": "dup"}],
        )  # type: ignore[list-item]
    with pytest.raises(ValueError):
        AgentConfig(
            company_name="A",
            greeting="Hi",
            instructions="x" * 25,
            outcomes=[{"code": "a", "description": "x"}, {"code": "a", "description": "y"}],
        )  # type: ignore[list-item]
