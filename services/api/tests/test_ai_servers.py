"""AI servers (ADR 0007): registering a models host behind the AI gateway, against a fake
gateway (infra/deploy/oci/caddy/ai-gateway.caddy and infra/docker/ai-gateway/app.py) and the
usual fake voice service."""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Iterator
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core import net_policy
from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.db.base import Base
from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.workflow_builder import build_configurations
from app.models.ai_servers import AiServer
from app.models.audit import AuditLog
from app.schemas.voice import AgentConfig
from app.services import ai_servers, llm_warmup
from tests import fake_runtime
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import FakeRuntime

GATEWAY = "https://ai.test"
# Shaped like the real values (256-bit tokens), never real ones.
TOKEN = "gw" + "Tk7pQ2vXr9" * 4 + "Ab"
NEW_TOKEN = "gw" + "Nw3mZ8yLq1" * 4 + "Cd"
LIVE_SECRET = "ls" + "9f2cA81bE4" * 3
NEW_LIVE_SECRET = "ls" + "Hq5tR0wPz7" * 3
DEEPGRAM_KEY = "dg" + "Qm4nW8rTx6" * 4
SECRETS = (TOKEN, NEW_TOKEN, LIVE_SECRET, NEW_LIVE_SECRET, DEEPGRAM_KEY)
PUBLIC_IP = ip_address("93.184.216.34")

HEALTH: dict[str, dict[str, Any]] = {
    "llm": {
        "up": True,
        "state": "up",
        "latency_ms": 4,
        "version": "0.40.6",
        "models": ["qwen2.5:1.5b", "llama3.2:3b"],
        "loaded": ["qwen2.5:1.5b"],
    },
    "whisper": {
        "up": True,
        "state": "up",
        "latency_ms": 3,
        "version": "v0.8.2",
        "models": ["Systran/faster-whisper-base"],
        "voice_models": ["speaches-ai/Kokoro-82M-v1.0-ONNX"],
    },
    "tts": {
        "up": True,
        "state": "up",
        "latency_ms": 2,
        "version": "1.0.0",
        "models": ["pocket-tts"],
        "voices": 20,
        "default_voice": "alba",
        "cloning": "off",
    },
    "live_stt": {
        "up": True,
        "state": "up",
        "latency_ms": 2,
        "version": "tdt-0.6",
        "model": "parakeet-en-110m",
        "streaming": False,
        "language": "en",
        "sessions": 0,
        "max_sessions": 4,
    },
}


class FakeGateway:
    """The gateway's paths, token check and /health answer."""

    def __init__(self) -> None:
        self.tokens = {TOKEN}
        self.live_secret = LIVE_SECRET
        self.services: dict[str, dict[str, Any]] = copy.deepcopy(HEALTH)
        self.requests: list[httpx.Request] = []
        self.chats: list[dict[str, Any]] = []

    def _json(self, status: int, body: Any) -> httpx.Response:
        return httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/live-stt/"):
            parts = path.split("/", 3)  # "", "live-stt", secret, rest
            if parts[2] == self.live_secret and parts[3:] == ["v1/listen"]:
                # The speech server refuses a plain HTTP request with an unusable query.
                return self._json(400, {"err_code": "Bad Request", "err_msg": "encoding"})
            return self._json(404, {"error": "not_found"})
        if request.headers.get("authorization") not in {f"Bearer {t}" for t in self.tokens}:
            return self._json(401, {"error": "unauthorized"})
        if path == "/health":
            up = sum(1 for s in self.services.values() if s["up"])
            status = "ok" if up == len(self.services) else "degraded" if up else "down"
            body = {"status": status, "checked_at": "2026-10-22T10:15:02Z"}
            return self._json(200 if up else 503, body | {"services": self.services})
        if path == "/llm/v1/chat/completions":
            self.chats.append(
                {"auth": request.headers["authorization"], **json.loads(request.content)}
            )
            return self._json(200, {"choices": [{"message": {"content": "Hi"}}]})
        if path == "/tts/v1/voices":
            return self._json(
                200, {"data": [{"id": "alba", "name": "alba", "type": "builtin", "default": True}]}
            )
        if path == "/tts/v1/cloning":
            return self._json(200, {"state": "on", "terms_url": None, "token_source": "saved"})
        return self._json(404, {"error": "not_found"})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real DNS: hosts resolve to a public address, *.internal.test to a private one."""

    async def resolve(host: str, port: int) -> list[IPv4Address | IPv6Address]:
        return [ip_address("10.0.0.5")] if host.endswith(".internal.test") else [PUBLIC_IP]

    monkeypatch.setattr(net_policy, "resolve_host", resolve)
    # A key the fake voice service accepts for Deepgram, distinctive enough to search for.
    monkeypatch.setattr(fake_runtime, "VALID_KEY", DEEPGRAM_KEY)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def runtime(router: respx.MockRouter) -> FakeRuntime:
    fake = FakeRuntime()
    router.route(host="runtime.test", name="runtime").mock(side_effect=fake.handle)
    return fake


@pytest.fixture
def gateway(router: respx.MockRouter, runtime: FakeRuntime) -> FakeGateway:
    fake = FakeGateway()
    router.route(host="ai.test", name="gateway").mock(side_effect=fake.handle)
    return fake


async def _register(client: httpx.AsyncClient, acct: Account, **extra: Any) -> httpx.Response:
    body = {
        "name": "GPU box",
        "url": GATEWAY,
        "token": TOKEN,
        "live_stt_secret": LIVE_SECRET,
        "deepgram_key": DEEPGRAM_KEY,
    } | extra
    return await client.post(
        "/api/v1/ai/servers", headers=acct.headers, json={k: v for k, v in body.items() if v}
    )


async def _registered(client: httpx.AsyncClient, acct: Account, **extra: Any) -> dict[str, Any]:
    r = await _register(client, acct, **extra)
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def _ids(server: dict[str, Any]) -> dict[str, str | None]:
    return {s["service"]: s["connection_id"] for s in server["services"]}


async def _audit(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        return list((await db.scalars(select(AuditLog).where(AuditLog.action == action))).all())


async def _cloud_models(client: httpx.AsyncClient, acct: Account) -> dict[str, str]:
    """OpenAI, Deepgram and ElevenLabs connections, selected as the organization's models."""
    ids = {}
    for provider in ("openai", "deepgram", "elevenlabs"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=acct.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": DEEPGRAM_KEY}},
        )
        assert r.status_code == 201, r.text
        ids[provider] = r.json()["id"]
    r = await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={
            "llm": {"connection_id": ids["openai"], "settings": {"model": "gpt-4.1-mini"}},
            "stt": {"connection_id": ids["deepgram"], "settings": {"model": "nova-3"}},
            "tts": {"connection_id": ids["elevenlabs"], "settings": {"voice": "v1"}},
        },
    )
    assert r.status_code == 200, r.text
    return ids


# ---------------------------------------------------------------- registering


async def test_register_creates_a_connection_for_every_service_that_is_up(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, gateway: FakeGateway
) -> None:
    server = await _registered(client, owner, url="https://AI.test/")
    assert server["url"] == GATEWAY, "normalised: lower case, no trailing slash"
    assert server["name"] == "GPU box" and server["status"] == "ok"
    assert server["last_latency_ms"] is not None and server["last_error"] is None
    assert server["live_stt_secret_set"] is True
    assert [(s["service"], s["created"]) for s in server["setup"]] == [
        ("llm", True),
        ("whisper", True),
        ("tts", True),
        ("live_stt", True),
    ]
    services = {s["service"]: s for s in server["services"]}
    assert services["llm"]["models"] == ["qwen2.5:1.5b", "llama3.2:3b"]
    assert services["llm"]["loaded"] == ["qwen2.5:1.5b"]
    assert services["tts"]["default_voice"] == "alba" and services["tts"]["voices"] == 20
    assert services["live_stt"]["models"] == ["parakeet-en-110m"]
    assert services["live_stt"]["max_sessions"] == 4

    health = next(r for r in gateway.requests if r.url.path == "/health")
    assert health.headers["authorization"] == f"Bearer {TOKEN}"
    live_check = next(r for r in gateway.requests if r.url.path.startswith("/live-stt/"))
    assert "authorization" not in live_check.headers, "the secret travels in the path only"

    created = [b for m, p, b in runtime.calls if m == "POST" and "provider-connections" in p]
    assert [(c["name"], c["provider"], c["connection_settings"]) for c in created] == [
        ("GPU box · Language model", "speaches", {"base_url": f"{GATEWAY}/llm/v1"}),
        ("GPU box · Whisper", "speaches", {"base_url": f"{GATEWAY}/whisper/v1"}),
        ("GPU box · Pocket TTS", "speaches", {"base_url": f"{GATEWAY}/tts/v1"}),
        ("GPU box · Live speech", "deepgram", {"base_url": f"{GATEWAY}/live-stt/{LIVE_SECRET}"}),
    ]
    assert [c["credentials"] for c in created] == [{"api_key": TOKEN}] * 3 + [
        {"api_key": DEEPGRAM_KEY}
    ]

    listed = (await client.get("/api/v1/ai/connections", headers=owner.headers)).json()
    by_id = {c["id"]: c for c in listed}
    ids = _ids(server)
    assert set(by_id) == set(ids.values())
    live = by_id[ids["live_stt"]]
    assert live["connection_settings"] == {"base_url": f"{GATEWAY}/live-stt/[secret]"}
    assert live["configured_credentials"] == ["api_key"]

    logs = await _audit("ai_server.created")
    assert len(logs) == 1 and logs[0].changes["url"] == GATEWAY
    assert logs[0].changes["connections"] == {k: v for k, v in ids.items()}

    listing = (await client.get("/api/v1/ai/servers", headers=owner.headers)).json()
    assert [s["id"] for s in listing] == [server["id"]]
    one = await client.get(f"/api/v1/ai/servers/{server['id']}", headers=owner.headers)
    assert one.status_code == 200 and one.json()["services"] == server["services"]

    again = await _register(client, owner, url=f"{GATEWAY}/")
    assert again.status_code == 409 and "already registered as GPU box" in again.text


async def test_register_explains_what_is_wrong_and_saves_nothing(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: FakeRuntime,
    gateway: FakeGateway,
    router: respx.MockRouter,
) -> None:
    async def refused(**extra: Any) -> dict[str, Any]:
        r = await _register(client, owner, **extra)
        assert r.status_code == 422, r.text
        error: dict[str, Any] = r.json()["error"]
        return error

    error = await refused(token="gw-not-the-token")
    assert error["message"] == "The server refused this token."
    assert error["details"] == {"field": "token"}

    error = await refused(live_stt_secret="not-the-secret")
    assert error["details"] == {"field": "live_stt_secret"}
    assert "live speech-to-text secret" in error["message"]

    error = await refused(url="http://ai.test")
    assert error["message"] == "AI server URL must use https"
    error = await refused(url="https://models.internal.test")
    assert error["message"] == "AI server URL must point to a public address"
    assert error["details"] == {"field": "url"}

    router.route(host="nothing.test").mock(side_effect=httpx.ConnectError("refused"))
    error = await refused(url="https://nothing.test")
    assert error["message"].startswith("The server could not be reached")
    router.route(host="badcert.test").mock(
        side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    )
    error = await refused(url="https://badcert.test")
    assert "TLS certificate was not accepted" in error["message"]
    router.route(host="slow.test").mock(side_effect=httpx.ReadTimeout("slow"))
    error = await refused(url="https://slow.test")
    assert error["message"] == "The server did not answer within 8 seconds."
    router.route(host="www.test").mock(return_value=httpx.Response(404, text="Not Found"))
    error = await refused(url="https://www.test")
    assert error["message"] == "No AI gateway answers at this address (it has no /health)."

    assert runtime.count("POST", "provider-connections") == 0
    async with get_sessionmaker()() as db:
        assert (await db.scalars(select(AiServer))).all() == []
    assert await _audit("ai_server.created") == []


async def test_only_services_that_are_up_get_a_connection(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: FakeRuntime,
    gateway: FakeGateway,
    router: respx.MockRouter,
) -> None:
    gateway.services["whisper"] = {
        "up": False,
        "state": "down",
        "latency_ms": None,
        "error": "timeout",
    }
    gateway.services["tts"] = {"up": False, "state": "loading", "latency_ms": 9}
    del gateway.services["live_stt"]
    server = await _registered(client, owner, live_stt_secret=None, deepgram_key=None)
    assert server["status"] == "degraded"
    reasons = {s["service"]: s["reason"] for s in server["setup"]}
    assert reasons == {
        "llm": None,
        "whisper": "The server reports it down (timeout).",
        "tts": "It is still loading its model. Check the server again in a few minutes.",
        "live_stt": "The server does not offer it.",
    }
    states = {s["service"]: s["state"] for s in server["services"]}
    assert states == {"llm": "up", "whisper": "down", "tts": "loading", "live_stt": "not_offered"}
    assert runtime.count("POST", "provider-connections") == 1

    # Once Pocket TTS has loaded, checking the server adds its connection.
    gateway.services["tts"] = copy.deepcopy(HEALTH["tts"])
    r = await client.post(f"/api/v1/ai/servers/{server['id']}/check", headers=owner.headers)
    assert r.status_code == 200, r.text
    checked = r.json()
    assert _ids(checked)["tts"] and not _ids(checked)["whisper"]
    assert runtime.last("POST", "provider-connections")["name"] == "GPU box · Pocket TTS"

    # Live speech-to-text needs both the secret and a Deepgram key.
    gateway.services["live_stt"] = copy.deepcopy(HEALTH["live_stt"])
    router.route(host="gpu2.test").mock(side_effect=gateway.handle)
    other = await _registered(
        client, owner, url="https://gpu2.test", live_stt_secret=LIVE_SECRET, deepgram_key=None
    )
    live = next(s for s in other["setup"] if s["service"] == "live_stt")
    assert live == {
        "service": "live_stt",
        "created": False,
        "connection_id": None,
        "connection_name": None,
        "reason": "Give a Deepgram key to use it.",
    }
    # ... the key can follow later.
    r = await client.patch(
        f"/api/v1/ai/servers/{other['id']}",
        headers=owner.headers,
        json={"deepgram_key": DEEPGRAM_KEY},
    )
    assert r.status_code == 200, r.text
    assert _ids(r.json())["live_stt"]
    assert runtime.last("POST", "provider-connections")["connection_settings"] == {
        "base_url": f"https://gpu2.test/live-stt/{LIVE_SECRET}"
    }


# ---------------------------------------------------------------- changing it


async def test_replacing_the_token_updates_every_connection(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, gateway: FakeGateway
) -> None:
    server = await _registered(client, owner)
    ids = _ids(server)
    url = f"/api/v1/ai/servers/{server['id']}"

    # The server does not know the new token yet: refused before anything changes.
    r = await client.patch(url, headers=owner.headers, json={"token": NEW_TOKEN})
    assert r.status_code == 422 and r.json()["error"]["details"] == {"field": "token"}
    assert runtime.count("PATCH", "provider-connections") == 0

    gateway.tokens.add(NEW_TOKEN)  # ATLAS_AI_TOKEN_NEXT on the models host
    r = await client.patch(url, headers=owner.headers, json={"token": NEW_TOKEN})
    assert r.status_code == 200, r.text
    patches = [(p, b) for m, p, b in runtime.calls if m == "PATCH"]
    assert patches == [
        (
            f"/model-connections/provider-connections/{ids[s]}",
            {"credentials": {"api_key": NEW_TOKEN}},
        )
        for s in ("llm", "whisper", "tts")
    ], "live speech-to-text keeps its Deepgram key"
    logs = await _audit("ai_server.token_rotated")
    assert len(logs) == 1 and logs[0].changes == {"connections": ["llm", "tts", "whisper"]}

    # The old token is retired on the models host: ATLAS uses the new one.
    gateway.tokens = {NEW_TOKEN}
    r = await client.post(f"{url}/check", headers=owner.headers)
    assert r.json()["status"] == "ok"
    assert gateway.requests[-1].headers["authorization"] == f"Bearer {NEW_TOKEN}"

    gateway.live_secret = NEW_LIVE_SECRET
    r = await client.patch(url, headers=owner.headers, json={"live_stt_secret": NEW_LIVE_SECRET})
    assert r.status_code == 200, r.text
    assert runtime.last("PATCH", ids["live_stt"] or "") == {
        "connection_settings": {"base_url": f"{GATEWAY}/live-stt/{NEW_LIVE_SECRET}"}
    }

    r = await client.patch(url, headers=owner.headers, json={"name": "Big GPU"})
    assert r.status_code == 200 and r.json()["name"] == "Big GPU"
    names = sorted(
        c["name"]
        for c in (await client.get("/api/v1/ai/connections", headers=owner.headers)).json()
    )
    assert names == [
        "Big GPU · Language model",
        "Big GPU · Live speech",
        "Big GPU · Pocket TTS",
        "Big GPU · Whisper",
    ]
    updated = await _audit("ai_server.updated")
    assert [log.changes["fields"] for log in updated] == [["live_stt_secret"], ["name"]]


async def test_use_for_the_organization_with_a_fallback(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, gateway: FakeGateway
) -> None:
    cloud = await _cloud_models(client, owner)
    server = await _registered(client, owner)
    ids = _ids(server)
    use = f"/api/v1/ai/servers/{server['id']}/use"

    r = await client.post(
        use,
        headers=owner.headers,
        json={"fallback_llm_connection_id": cloud["openai"], "fallback_after_ms": 2000},
    )
    assert r.status_code == 200, r.text
    selection = r.json()
    assert selection["llm"] == {
        "connection_id": ids["llm"],
        "settings": {"model": "qwen2.5:1.5b", "temperature": 0.3},
    }
    assert selection["stt"] == {
        "connection_id": ids["live_stt"],
        "settings": {"model": "parakeet-en-110m", "language": "en"},
    }
    assert selection["tts"] == {
        "connection_id": ids["tts"],
        "settings": {"model": "pocket-tts", "voice": "alba"},
    }
    # The cloud model answers when the server is slow or fails, with its current settings.
    assert selection["llm_fallback"] == {
        "connection_id": cloud["openai"],
        "settings": {"model": "gpt-4.1-mini"},
        "after_ms": 2000,
        "on_error": True,
    }
    sent = runtime.last("PATCH", "model-configurations")["configuration"]
    target = {"provider_connection_uuid": cloud["openai"], "settings": {"model": "gpt-4.1-mini"}}
    assert sent["llm_fallback"] == {
        "version": 1,
        "rules": [
            {"condition": {"type": "no_output", "after_ms": 2000}, "target": target},
            {"condition": {"type": "error"}, "target": target},
        ],
    }
    used = await _audit("ai_server.used")
    assert used[-1].changes["fallback"] == {"connection_id": cloud["openai"], "after_ms": 2000}

    # Saving the models on the AI providers screen keeps the fallback ...
    models = {k: v for k, v in selection.items() if k != "llm_fallback"}
    r = await client.put("/api/v1/ai/models", headers=owner.headers, json=models)
    assert r.status_code == 200 and r.json()["llm_fallback"]["connection_id"] == cloud["openai"]
    # ... and the fallback connection cannot be removed while it is one.
    r = await client.delete(f"/api/v1/ai/connections/{cloud['openai']}", headers=owner.headers)
    assert r.status_code == 422 and "fallback LLM" in r.json()["error"]["message"]

    # Whisper instead of live speech, another model on the server, and no fallback.
    r = await client.post(
        use, headers=owner.headers, json={"llm_model": "llama3.2:3b", "use_live_stt": False}
    )
    assert r.status_code == 200, r.text
    selection = r.json()
    assert selection["llm"]["settings"]["model"] == "llama3.2:3b"
    assert selection["stt"] == {
        "connection_id": ids["whisper"],
        "settings": {"model": "Systran/faster-whisper-base"},
    }
    assert selection["llm_fallback"] is None
    assert "llm_fallback" not in runtime.last("PATCH", "model-configurations")["configuration"]

    r = await client.post(use, headers=owner.headers, json={"llm_model": "gpt-5"})
    assert r.status_code == 422 and "does not offer gpt-5" in r.json()["error"]["message"]
    r = await client.post(
        use, headers=owner.headers, json={"fallback_llm_connection_id": ids["llm"]}
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"] == {"field": "fallback_llm_connection_id"}
    r = await client.post(
        use, headers=owner.headers, json={"fallback_llm_connection_id": cloud["deepgram"]}
    )
    assert r.status_code == 422 and "Unknown language model" in r.json()["error"]["message"]


async def test_removing_is_refused_while_in_use(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, gateway: FakeGateway
) -> None:
    server = await _registered(client, owner)
    ids = _ids(server)
    url = f"/api/v1/ai/servers/{server['id']}"
    assert (await client.post(f"{url}/use", headers=owner.headers, json={})).status_code == 200

    r = await client.delete(url, headers=owner.headers)
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["message"].startswith(
        "This AI server is in use by the organization's models for calls "
        "(language model, speech-to-text, voice)."
    )

    await _cloud_models(client, owner)
    agent = (
        await client.post("/api/v1/agents", headers=owner.headers, json={"name": "Hina"})
    ).json()
    config = agent["config"] | {
        "model_override": {
            "llm": {"connection_id": ids["llm"], "settings": {"model": "qwen2.5:1.5b"}}
        }
    }
    r = await client.put(
        f"/api/v1/agents/{agent['id']}/config", headers=owner.headers, json={"config": config}
    )
    assert r.status_code == 200, r.text
    r = await client.delete(url, headers=owner.headers)
    assert r.status_code == 409 and r.json()["error"]["details"] == {"in_use": ["agent Hina"]}
    # The organization's fallback backs up its own model, not an agent's own one.
    sent = build_configurations(AgentConfig.model_validate(config))
    assert sent["model_configuration_override"] == {
        "llm": {"provider_connection_uuid": ids["llm"], "settings": {"model": "qwen2.5:1.5b"}},
        "llm_fallback": {"version": 1, "rules": []},
    }

    config["model_override"] = None
    r = await client.put(
        f"/api/v1/agents/{agent['id']}/config", headers=owner.headers, json={"config": config}
    )
    assert r.status_code == 200, r.text
    assert (await client.delete(url, headers=owner.headers)).status_code == 204
    org = next(iter(runtime.orgs.values()))
    assert all(not org.connections[c]["is_active"] for c in ids.values() if c)
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    logs = await _audit("ai_server.deleted")
    assert len(logs) == 1 and logs[0].changes["connections_removed"] == 4


async def test_editing_the_live_speech_connection_keeps_its_secret(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, gateway: FakeGateway
) -> None:
    """The connection dialog sends back the masked address it was shown."""
    live = _ids(await _registered(client, owner))["live_stt"]
    assert live is not None
    url = f"/api/v1/ai/connections/{live}"
    masked = {"base_url": f"{GATEWAY}/live-stt/[secret]"}
    r = await client.patch(
        url, headers=owner.headers, json={"name": "Live", "connection_settings": masked}
    )
    assert r.status_code == 200, r.text
    assert r.json()["connection_settings"] == masked
    assert runtime.last("PATCH", live)["connection_settings"] == {
        "base_url": f"{GATEWAY}/live-stt/{LIVE_SECRET}"
    }
    logs = await _audit("ai.connection_updated")
    assert logs[-1].changes["connection_settings"] == {"base_url": f"{GATEWAY}/live-stt/[REDACTED]"}

    # A masked value that is not the stored address cannot be completed.
    other = {"base_url": "https://elsewhere.test/live-stt/[secret]"}
    r = await client.patch(url, headers=owner.headers, json={"connection_settings": other})
    assert r.status_code == 422
    assert r.json()["error"]["details"] == {"field": "base_url"}


# ---------------------------------------------------------------- watching it


async def test_monitor_records_status_changes_only(
    client: httpx.AsyncClient, owner: Account, gateway: FakeGateway, router: respx.MockRouter
) -> None:
    server = await _registered(client, owner)
    assert await ai_servers.check_all() == {"checked": 1, "changed": 0}

    router.routes["gateway"].mock(side_effect=httpx.ConnectError("refused"))
    assert await ai_servers.check_all() == {"checked": 1, "changed": 1}
    assert await ai_servers.check_all() == {"checked": 1, "changed": 0}
    down = (await client.get(f"/api/v1/ai/servers/{server['id']}", headers=owner.headers)).json()
    assert down["status"] == "down" and down["last_error"].startswith(
        "The server could not be reached"
    )
    llm = next(s for s in down["services"] if s["service"] == "llm")
    assert (llm["up"], llm["state"], llm["error"]) == (False, "down", "unreachable")
    assert llm["models"] == ["qwen2.5:1.5b", "llama3.2:3b"], "what it offered is kept"

    router.routes["gateway"].mock(side_effect=gateway.handle)
    gateway.services["whisper"] = {"up": False, "state": "down", "error": "refused"}
    assert await ai_servers.check_all() == {"checked": 1, "changed": 1}
    changes = [
        (log.changes["from"], log.changes["to"]) for log in await _audit("ai_server.status_changed")
    ]
    assert changes == [("ok", "down"), ("down", "degraded")]
    assert all(log.actor_user_id is None for log in await _audit("ai_server.status_changed"))


# ---------------------------------------------------------------- what uses it


async def test_warm_up_reaches_the_server_with_its_token(
    client: httpx.AsyncClient, owner: Account, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "local_llm_warmup_enabled", True)
    # The operator's own model server: never used for a registered AI server.
    monkeypatch.setattr(get_settings(), "local_llm_warmup_url", "http://local-llm:11434/v1")
    llm_warmup.reset_state()
    server = await _registered(client, owner)
    assert (
        await client.post(f"/api/v1/ai/servers/{server['id']}/use", headers=owner.headers, json={})
    ).status_code == 200
    cfg = AgentConfig(
        company_name="Northwind",
        greeting="Hello, this is Hina from Northwind.",
        instructions="Ask whether the caller wants to book a viewing of the flat.",
    )
    org = uuid.UUID(owner.org_id)
    target = await llm_warmup.resolve_target(org, cfg)
    assert target is not None
    assert (target.base_url, target.model, target.api_key) == (
        f"{GATEWAY}/llm/v1",
        "qwen2.5:1.5b",
        TOKEN,
    )
    assert TOKEN not in repr(target)
    assert await llm_warmup.warm_agent_prompt(org, cfg, {}) == "sent"
    assert gateway.chats[0]["auth"] == f"Bearer {TOKEN}"
    assert gateway.chats[0]["model"] == "qwen2.5:1.5b" and gateway.chats[0]["max_tokens"] == 1


async def test_voice_cloning_reaches_the_servers_pocket_tts(
    client: httpx.AsyncClient,
    owner: Account,
    gateway: FakeGateway,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The operator's local Pocket TTS: never used for a registered AI server.
    monkeypatch.setattr(get_settings(), "local_tts_url", "https://tts.local.test/v1")
    local = router.route(host="tts.local.test").mock(return_value=httpx.Response(500))
    server = await _registered(client, owner)
    tts = _ids(server)["tts"]
    r = await client.get(f"/api/v1/ai/connections/{tts}/voices", headers=owner.headers)
    assert r.status_code == 200, r.text
    assert [v["id"] for v in r.json()["voices"]] == ["alba"]
    r = await client.get(f"/api/v1/ai/connections/{tts}/voice-cloning", headers=owner.headers)
    assert r.status_code == 200 and r.json()["state"] == "on"
    pocket = [r for r in gateway.requests if r.url.path.startswith("/tts/")]
    assert [r.url.path for r in pocket] == ["/tts/v1/voices", "/tts/v1/cloning"]
    assert {r.headers["authorization"] for r in pocket} == {f"Bearer {TOKEN}"}
    assert not local.called

    gateway.tokens = {NEW_TOKEN}  # replaced on the models host, not yet in ATLAS
    r = await client.get(f"/api/v1/ai/connections/{tts}/voices", headers=owner.headers)
    assert r.status_code == 503
    assert r.json()["error"]["message"] == (
        "The AI server GPU box refused its token. Replace the token under AI providers, AI servers."
    )


# ---------------------------------------------------------------- who may do what


async def test_roles_and_organizations(
    client: httpx.AsyncClient, owner: Account, runtime: FakeRuntime, gateway: FakeGateway
) -> None:
    server = await _registered(client, owner)
    url = f"/api/v1/ai/servers/{server['id']}"
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    assert (await client.get("/api/v1/ai/servers", headers=manager.headers)).status_code == 200
    assert (await client.get(url, headers=manager.headers)).status_code == 200
    assert (await _register(client, manager, url="https://ai.test/b")).status_code == 403
    for method, path, body in (
        ("POST", f"{url}/check", None),
        ("PATCH", url, {"name": "x"}),
        ("POST", f"{url}/use", {}),
        ("DELETE", url, None),
    ):
        r = await client.request(method, path, headers=manager.headers, json=body)
        assert r.status_code == 403, (method, path)
    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    assert (await client.get("/api/v1/ai/servers", headers=viewer.headers)).status_code == 403

    # Tenant API keys go through the same role checks.
    r = await client.post(
        "/api/v1/developer/api-keys", headers=owner.headers, json={"name": "CI", "role": "manager"}
    )
    key = {"X-API-Key": r.json()["key"]}
    assert (await client.get("/api/v1/ai/servers", headers=key)).status_code == 200
    assert (await client.post(f"{url}/check", headers=key)).status_code == 403

    other = await signup(client, "other@example.com", org="Other Co")
    assert (await client.get("/api/v1/ai/servers", headers=other.headers)).json() == []
    for method, path, body in (
        ("GET", url, None),
        ("POST", f"{url}/check", None),
        ("PATCH", url, {"name": "Mine now"}),
        ("POST", f"{url}/use", {}),
        ("DELETE", url, None),
    ):
        r = await client.request(method, path, headers=other.headers, json=body)
        assert r.status_code == 404, (method, path)
    # The same gateway can be registered by another organization, with its own connections.
    theirs = await _registered(client, other)
    assert set(_ids(theirs).values()).isdisjoint(_ids(server).values())


async def test_secrets_are_never_logged_audited_stored_or_returned(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: FakeRuntime,
    gateway: FakeGateway,
    router: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    replies: list[httpx.Response] = []

    # The voice service quotes what it was given in an error: scrubbed before it is shown.
    def quoting(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and body.get("provider") == "deepgram":
            quoted = (
                f"{body['credentials']['api_key']} at {body['connection_settings']['base_url']}"
            )
            return httpx.Response(422, json={"detail": f"Deepgram refused {quoted}"})
        return runtime.handle(request)

    router.routes["runtime"].mock(side_effect=quoting)
    replies.append(r := await _register(client, owner))
    assert r.status_code == 422
    assert r.json()["error"]["message"] == (
        "The voice service could not use the server's live speech: Deepgram refused "
        f"[REDACTED] at {GATEWAY}/live-stt/[REDACTED]"
    )
    assert r.json()["error"]["details"] == {"field": "deepgram_key"}
    org = next(iter(runtime.orgs.values()))
    assert not any(c["is_active"] for c in org.connections.values() if "GPU box" in c["name"]), (
        "the connections created before are removed again"
    )
    router.routes["runtime"].mock(side_effect=runtime.handle)

    replies.append(await _register(client, owner, token="gw-wrong-" + TOKEN[::-1]))
    replies.append(r := await _register(client, owner))
    server = r.json()
    url = f"/api/v1/ai/servers/{server['id']}"
    gateway.tokens.add(NEW_TOKEN)
    gateway.live_secret = NEW_LIVE_SECRET
    replies.append(
        await client.patch(
            url,
            headers=owner.headers,
            json={
                "token": NEW_TOKEN,
                "live_stt_secret": NEW_LIVE_SECRET,
                "deepgram_key": DEEPGRAM_KEY,
            },
        )
    )
    replies.append(await client.post(f"{url}/check", headers=owner.headers))
    replies.append(await client.post(f"{url}/use", headers=owner.headers, json={}))
    replies.append(await client.get("/api/v1/ai/servers", headers=owner.headers))
    replies.append(await client.get("/api/v1/ai/connections", headers=owner.headers))
    replies.append(await client.get("/api/v1/ai/models", headers=owner.headers))
    replies.append(await client.get("/api/v1/audit-logs", headers=owner.headers))
    assert [r.status_code for r in replies] == [422, 422, 201, 200, 200, 200, 200, 200, 200, 200]
    for reply in replies:
        for secret in SECRETS:
            assert secret not in reply.text, (reply.request.url, secret)

    formatter = JsonFormatter("api")
    lines = [formatter.format(record) for record in caplog.records]
    # httpx logs every request's URL: the live speech-to-text check's is masked.
    assert any("/live-stt/[REDACTED]/v1/listen" in line for line in lines), lines
    for record in caplog.records:
        for secret in SECRETS:
            assert secret not in formatter.format(record)
            assert secret not in repr(record.__dict__)
    async with get_sessionmaker()() as db:
        for table in Base.metadata.sorted_tables:
            rows = repr((await db.execute(select(table))).all())
            for secret in SECRETS:
                assert secret not in rows, table.name
        stored = await db.scalar(select(AiServer))
        assert stored is not None and stored.token_encrypted and stored.live_stt_secret_encrypted
