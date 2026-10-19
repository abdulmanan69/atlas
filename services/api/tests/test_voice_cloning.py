"""Pocket TTS voice cloning through ATLAS, against a fake Pocket TTS server and voice runtime."""

from __future__ import annotations

import io
import re
import secrets
import wave
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from app.models.voice_cloning import CustomVoice
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime

TTS = "https://tts.test/v1"
UNAVAILABLE = (
    "Voice cloning needs the gated weights: accept the terms at "
    "https://huggingface.co/kyutai/pocket-tts and set HF_TOKEN."
)


def wav(seconds: float = 6.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00\xf0\xff" * int(seconds * rate / 2))
    return buf.getvalue()


def _multipart(request: httpx.Request) -> dict[str, bytes]:
    boundary = re.search(r"boundary=([^;]+)", request.headers["content-type"])
    assert boundary
    fields: dict[str, bytes] = {}
    for part in request.content.split(b"--" + boundary.group(1).encode()):
        head, _, body = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name:
            fields[name.group(1).decode()] = body.removesuffix(b"\r\n")
    return fields


class FakePocket:
    """The adapter's /v1/voices contract (infra/docker/pocket-tts/app.py)."""

    def __init__(self) -> None:
        self.cloning = True
        self.voices: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, bytes]] = []
        self.reply: httpx.Response | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/audio/voices" and method == "GET":
            return httpx.Response(
                200, json={"data": [{"id": "alba", "license": "CC-BY-4.0", "default": True}]}
            )
        if not path.startswith("/v1/voices"):
            return httpx.Response(404, json={"detail": "Not Found"})
        if not self.cloning:
            return httpx.Response(501, json={"detail": UNAVAILABLE})
        if self.reply is not None:
            return self.reply
        if path == "/v1/voices" and method == "GET":
            builtin = [{"id": "alba", "name": "alba", "type": "builtin", "license": "CC-BY-4.0"}]
            return httpx.Response(200, json={"data": builtin + list(self.voices.values())})
        if path == "/v1/voices" and method == "POST":
            fields = _multipart(request)
            self.posts.append(fields)
            assert fields["consent"] == b"true"
            name = fields["name"].decode()
            slug = re.sub("[^a-z0-9]+", "-", name.lower()).strip("-")
            vid = f"custom-{slug}-{secrets.token_hex(8)}"
            self.voices[vid] = {"id": vid, "name": name, "type": "custom", "sample_seconds": 6.0}
            return httpx.Response(201, json=self.voices[vid])
        if method == "DELETE":
            vid = path.rsplit("/", 1)[-1]
            return httpx.Response(204 if self.voices.pop(vid, None) else 404)
        return httpx.Response(405)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def runtime(router: respx.MockRouter) -> FakeRuntime:
    fake = FakeRuntime()
    router.route(host="runtime.test").mock(side_effect=fake.handle)
    return fake


@pytest.fixture
def pocket(
    router: respx.MockRouter, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> FakePocket:
    fake = FakePocket()
    monkeypatch.setattr(get_settings(), "local_tts_url", TTS)
    router.route(host="tts.test", name="tts").mock(side_effect=fake.handle)
    return fake


async def _connection(client: httpx.AsyncClient, acct: Account, name: str = "Pocket TTS") -> str:
    base = "http://local-tts:8000/v1" if "Pocket" in name else "http://local-speech:8000/v1"
    r = await client.post(
        "/api/v1/ai/connections",
        headers=acct.headers,
        json={"name": name, "provider": "speaches", "connection_settings": {"base_url": base}},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _clone(
    client: httpx.AsyncClient,
    acct: Account,
    conn: str,
    *,
    audio: bytes | None = None,
    consent: str | None = "true",
    name: str = "Sam Example",
) -> httpx.Response:
    data = {"name": name}
    if consent is not None:
        data["consent_attestation"] = consent
    return await client.post(
        f"/api/v1/ai/connections/{conn}/voices",
        headers=acct.headers,
        data=data,
        files={"audio": ("sam.wav", wav() if audio is None else audio, "audio/wav")},
    )


async def _audit(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        return list((await db.scalars(select(AuditLog).where(AuditLog.action == action))).all())


async def test_cloning_off_lists_built_in_voices_and_explains_setup(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    pocket.cloning = False
    conn = await _connection(client, owner)
    r = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cloning_available"] is False
    assert body["setup_message"] == UNAVAILABLE
    assert body["setup_url"] == "https://huggingface.co/kyutai/pocket-tts"
    assert [v["id"] for v in body["voices"]] == ["alba"]

    r = await _clone(client, owner, conn)
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "voice_cloning_unavailable"
    assert r.json()["error"]["message"] == UNAVAILABLE
    async with get_sessionmaker()() as db:
        assert (await db.scalars(select(CustomVoice))).all() == []
    assert await _audit("ai.voice_cloned") == []


async def test_consent_type_and_size_are_checked_before_forwarding(
    client: httpx.AsyncClient,
    owner: Account,
    pocket: FakePocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = await _connection(client, owner)
    for consent in (None, "false"):
        r = await _clone(client, owner, conn, consent=consent)
        assert r.status_code == 422
        assert "permission" in r.json()["error"]["message"]
    r = await _clone(client, owner, conn, audio=b"ID3\x04" + b"\x00" * 600)
    assert r.status_code == 415
    assert "WAV" in r.json()["error"]["message"]
    assert (await _clone(client, owner, conn, name="   ")).status_code == 422
    assert (await _clone(client, owner, conn, name="x" * 61)).status_code == 422
    monkeypatch.setattr(get_settings(), "voice_clone_max_upload_bytes", 1000)
    assert (await _clone(client, owner, conn)).status_code == 413
    assert pocket.posts == [], "nothing reached the Pocket TTS server"


async def test_clone_list_select_and_delete_with_audit(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    conn = await _connection(client, owner)
    r = await _clone(client, owner, conn)
    assert r.status_code == 201, r.text
    voice = r.json()
    assert voice["cloned"] is True and voice["name"] == "Sam Example"
    assert re.fullmatch(r"custom-sam-example-[0-9a-f]{16}", voice["id"])
    sent = pocket.posts[0]
    assert sent["audio"].startswith(b"RIFF")
    assert owner.user_id.encode() in sent["consent_statement"]
    assert b"permission to clone their voice" in sent["consent_statement"]

    logs = await _audit("ai.voice_cloned")
    assert len(logs) == 1
    changes = logs[0].changes
    assert changes["voice_id"] == voice["id"] and changes["consent_confirmed"] is True
    assert changes["consent_attestation"] == (
        "I have the speaker's permission to clone their voice."
    )
    assert str(logs[0].actor_user_id) == owner.user_id
    assert "RIFF" not in str(changes), "the audio is never recorded"

    listed = (
        await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    ).json()
    assert listed["cloning_available"] is True
    assert [(v["id"], v["cloned"]) for v in listed["voices"]] == [
        ("alba", False),
        (voice["id"], True),
    ]

    # In use by the organization's models: deleting is refused until another voice is chosen.
    ids = {}
    for provider in ("openai", "deepgram"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=owner.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}},
        )
        ids[provider] = r.json()["id"]
    models = {
        "llm": {"connection_id": ids["openai"], "settings": {"model": "gpt-4.1-mini"}},
        "stt": {"connection_id": ids["deepgram"], "settings": {"model": "nova-3"}},
        "tts": {"connection_id": conn, "settings": {"model": "pocket-tts", "voice": voice["id"]}},
    }
    r = await client.put("/api/v1/ai/models", headers=owner.headers, json=models)
    assert r.status_code == 200, r.text
    url = f"/api/v1/ai/connections/{conn}/voices/{voice['id']}"
    r = await client.delete(url, headers=owner.headers)
    assert r.status_code == 422 and "used by" in r.json()["error"]["message"]

    models["tts"]["settings"]["voice"] = "alba"
    assert (await client.put("/api/v1/ai/models", headers=owner.headers, json=models)).is_success
    assert (await client.delete(url, headers=owner.headers)).status_code == 204
    assert pocket.voices == {}
    assert len(await _audit("ai.voice_deleted")) == 1
    assert (await client.delete(url, headers=owner.headers)).status_code == 404


async def test_organizations_only_see_and_use_their_own_voices(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    conn_a = await _connection(client, owner)
    voice_a = (await _clone(client, owner, conn_a)).json()["id"]

    other = await signup(client, "other@example.com", org="Other Co")
    conn_b = await _connection(client, other)
    listed = (
        await client.get(f"/api/v1/ai/connections/{conn_b}/voices", headers=other.headers)
    ).json()
    assert voice_a not in [v["id"] for v in listed["voices"]]
    # Another organization's connection is not found, nor is the voice under its own.
    r = await client.get(f"/api/v1/ai/connections/{conn_a}/voices", headers=other.headers)
    assert r.status_code == 404
    r = await client.delete(
        f"/api/v1/ai/connections/{conn_b}/voices/{voice_a}", headers=other.headers
    )
    assert r.status_code == 404
    assert voice_a in pocket.voices

    ids = {}
    for provider in ("openai", "deepgram"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=other.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}},
        )
        ids[provider] = r.json()["id"]
    r = await client.put(
        "/api/v1/ai/models",
        headers=other.headers,
        json={
            "llm": {"connection_id": ids["openai"], "settings": {}},
            "stt": {"connection_id": ids["deepgram"], "settings": {}},
            "tts": {"connection_id": conn_b, "settings": {"voice": voice_a}},
        },
    )
    assert r.status_code == 422 and "cloned voice" in r.json()["error"]["message"]

    # Nor as one agent's own voice.
    agent = (await client.post("/api/v1/agents", headers=other.headers, json={"name": "B"})).json()
    config = agent["config"] | {
        "model_override": {"tts": {"connection_id": conn_b, "settings": {"voice": voice_a}}}
    }
    r = await client.put(
        f"/api/v1/agents/{agent['id']}/config", headers=other.headers, json={"config": config}
    )
    assert r.status_code == 422 and "cloned voice" in r.json()["error"]["message"]
    # Its owner can.
    mine = (await client.post("/api/v1/agents", headers=owner.headers, json={"name": "A"})).json()
    config = mine["config"] | {
        "model_override": {"tts": {"connection_id": conn_a, "settings": {"voice": voice_a}}}
    }
    r = await client.put(
        f"/api/v1/agents/{mine['id']}/config", headers=owner.headers, json={"config": config}
    )
    assert r.status_code == 200, r.text


async def test_roles_and_connection_kind(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    conn = await _connection(client, owner)
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    url = f"/api/v1/ai/connections/{conn}/voices"
    assert (await client.get(url, headers=manager.headers)).status_code == 200
    assert (await _clone(client, manager, conn)).status_code == 403
    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    assert (await client.get(url, headers=viewer.headers)).status_code == 403

    kokoro = await _connection(client, owner, name="Local speech (Whisper)")
    r = await client.get(f"/api/v1/ai/connections/{kokoro}/voices", headers=owner.headers)
    assert r.status_code == 422 and "Pocket TTS" in r.json()["error"]["message"]
    assert pocket.posts == []


async def test_server_errors_are_explained(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket, router: respx.MockRouter
) -> None:
    conn = await _connection(client, owner)
    pocket.reply = httpx.Response(
        422, json={"detail": "The sample is 3.0 s long: it must be between 5 and 30 seconds."}
    )
    r = await _clone(client, owner, conn)
    assert r.status_code == 422
    assert r.json()["error"]["message"].startswith("The sample is 3.0 s long")
    pocket.reply = httpx.Response(429, json={"detail": "Another voice is being created."})
    assert (await _clone(client, owner, conn)).status_code == 429

    router.routes["tts"].mock(side_effect=httpx.ConnectError("refused"))
    r = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    assert r.status_code == 503
    assert "ATLAS_LOCAL_TTS_URL" in r.json()["error"]["message"]
