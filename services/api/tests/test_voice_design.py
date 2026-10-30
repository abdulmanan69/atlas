"""Voice design through ATLAS, against a fake voice design server: the Pocket TTS voice API
plus GET /v1/capabilities, POST /v1/voices/design and GET /v1/voices/{id}/sample."""

from __future__ import annotations

import io
import json
import re
import secrets
import uuid
import wave
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import get_secret_box
from app.db.session import get_sessionmaker
from app.models.ai_servers import AiServer
from app.models.audit import AuditLog
from app.models.voice_cloning import CustomVoice
from app.services import voice_cloning, voice_design
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime

DESIGN = "https://design.test/v1"
CAPABILITIES: dict[str, Any] = {
    "model": "qwen3-tts",
    "cloning": True,
    "design": True,
    "instruct": True,
    "sound_tags": [],
    "languages": ["en", "de", "fr"],
    "streaming": "sentence",
    "reference_seconds": {"min": 3, "max": 30},
}
DESCRIPTION = "A warm, unhurried woman in her forties with a soft Irish accent."


def wav(seconds: float = 2.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00\xf0\xff" * int(seconds * rate / 2))
    return buf.getvalue()


class FakeDesignServer:
    """The voice design server's contract (infra/docker/voice-design)."""

    def __init__(self) -> None:
        self.capabilities_api = True  # False: a server from before /v1/capabilities
        self.capabilities: dict[str, Any] = dict(CAPABILITIES)
        self.tags: list[str] | None = None  # answered at /v1/audio/sound-tags when set
        self.voices: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self.reply: httpx.Response | None = None
        self.sample = wav()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/v1/capabilities":
            if not self.capabilities_api:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json=self.capabilities)
        if path == "/v1/audio/sound-tags":
            if self.tags is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={"tags": self.tags})
        if path == "/v1/audio/voices":
            return httpx.Response(200, json={"data": [{"id": "vivian", "default": True}]})
        if path == "/v1/cloning":
            return httpx.Response(200, json={"state": "on", "token_source": "environment"})
        if path == "/v1/voices" and method == "GET":
            builtin = [
                {"id": "vivian", "name": "Vivian", "type": "builtin", "license": "Apache-2.0"}
            ]
            return httpx.Response(200, json={"data": builtin + list(self.voices.values())})
        if path == "/v1/voices/design" and method == "POST":
            body = json.loads(request.content)
            self.posts.append(body)
            if self.reply is not None:
                return self.reply
            slug = re.sub("[^a-z0-9]+", "-", body["name"].lower()).strip("-")
            vid = f"design-{slug}-{secrets.token_hex(8)}"
            self.voices[vid] = {
                "id": vid,
                "name": body["name"],
                "type": "designed",
                "description": body["description"],
                "language": body.get("language"),
                "created_at": "2026-10-30T10:00:00Z",
            }
            return httpx.Response(201, json=self.voices[vid])
        if path.startswith("/v1/voices/") and path.endswith("/sample") and method == "GET":
            vid = path.removeprefix("/v1/voices/").removesuffix("/sample")
            if vid not in self.voices:
                return httpx.Response(404, json={"detail": "Voice not found"})
            return httpx.Response(200, content=self.sample, headers={"content-type": "audio/wav"})
        if path.startswith("/v1/voices/") and method == "DELETE":
            vid = path.rsplit("/", 1)[-1]
            return httpx.Response(204 if self.voices.pop(vid, None) else 404)
        return httpx.Response(404, json={"detail": "Not Found"})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture(autouse=True)
def _fresh_answers() -> Iterator[None]:
    voice_design.reset_capabilities_cache()
    voice_cloning.reset_sound_tags_cache()
    yield
    voice_design.reset_capabilities_cache()
    voice_cloning.reset_sound_tags_cache()


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
def design(
    router: respx.MockRouter, runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> FakeDesignServer:
    """A design server at its own address (private addresses allowed: no DNS lookup)."""
    fake = FakeDesignServer()
    monkeypatch.setattr(get_settings(), "allow_private_model_urls", True)
    router.route(host="design.test", name="design").mock(side_effect=fake.handle)
    return fake


async def _connection(
    client: httpx.AsyncClient, acct: Account, name: str = "Voice design", base: str = DESIGN
) -> str:
    r = await client.post(
        "/api/v1/ai/connections",
        headers=acct.headers,
        json={"name": name, "provider": "speaches", "connection_settings": {"base_url": base}},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _capabilities(
    client: httpx.AsyncClient, acct: Account, conn: str
) -> tuple[int, dict[str, Any]]:
    r = await client.get(f"/api/v1/ai/connections/{conn}/capabilities", headers=acct.headers)
    return r.status_code, r.json()


async def _design(
    client: httpx.AsyncClient,
    acct: Account,
    conn: str,
    *,
    name: str = "Maeve",
    description: str = DESCRIPTION,
    **extra: Any,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/ai/connections/{conn}/voices/design",
        headers=acct.headers,
        json={"name": name, "description": description, **extra},
    )


async def _audit(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        return list((await db.scalars(select(AuditLog).where(AuditLog.action == action))).all())


async def _select_voice(
    client: httpx.AsyncClient, acct: Account, conn: str, voice: str
) -> httpx.Response:
    ids = {}
    for provider in ("openai", "deepgram"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=acct.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}},
        )
        ids[provider] = r.json()["id"]
    return await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={
            "llm": {"connection_id": ids["openai"], "settings": {"model": "gpt-4.1-mini"}},
            "stt": {"connection_id": ids["deepgram"], "settings": {"model": "nova-3"}},
            "tts": {"connection_id": conn, "settings": {"model": "qwen3-tts", "voice": voice}},
        },
    )


# ---------------------------------------------------------------- capabilities


async def test_capabilities_come_from_the_server_and_are_kept_a_minute(
    client: httpx.AsyncClient, owner: Account, design: FakeDesignServer
) -> None:
    conn = await _connection(client, owner)
    status, body = await _capabilities(client, owner, conn)
    assert status == 200, body
    assert body == CAPABILITIES
    assert design.paths().count("/v1/capabilities") == 1
    # Kept for a while; the voice list does not ask again either.
    assert (await _capabilities(client, owner, conn))[1] == CAPABILITIES
    r = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    assert r.status_code == 200, r.text
    assert design.paths().count("/v1/capabilities") == 1

    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    assert (await _capabilities(client, manager, conn))[0] == 200
    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    assert (await _capabilities(client, viewer, conn))[0] == 403
    assert (await _capabilities(client, owner, "nope"))[0] == 404
    other = await signup(client, "other@example.com", org="Other Co")
    assert (await _capabilities(client, other, conn))[0] == 404

    # Only the known fields, bounded; nonsense for one of them is left out.
    voice_design.reset_capabilities_cache()
    design.capabilities = CAPABILITIES | {
        "model": "  qwen3-tts\n",
        "sound_tags": ["[laugh]", "<script>", "laugh"],
        "languages": ["en", 7, "en", " de "],
        "streaming": "firehose",
        "reference_seconds": {"min": 30, "max": 3},
        "extra": "ignored",
    }
    status, body = await _capabilities(client, owner, conn)
    assert status == 200
    assert body == CAPABILITIES | {
        "sound_tags": ["laugh"],
        "languages": ["en", "de"],
        "streaming": "unknown",
        "reference_seconds": None,
    }


async def test_capabilities_are_derived_for_a_server_without_them(
    client: httpx.AsyncClient,
    owner: Account,
    design: FakeDesignServer,
    router: respx.MockRouter,
) -> None:
    design.capabilities_api = False
    design.tags = ["[laugh]", "[sigh]"]
    conn = await _connection(client, owner, name="Expressive voice")
    status, body = await _capabilities(client, owner, conn)
    assert status == 200, body
    assert body == {
        "model": None,
        "cloning": True,
        "design": False,
        "instruct": False,
        "sound_tags": ["laugh", "sigh"],
        "languages": [],
        "streaming": "unknown",
        "reference_seconds": None,
    }
    # Derived from what the server has, then kept: no second round of questions.
    asked = design.paths()
    assert asked.count("/v1/capabilities") == 1 and asked.count("/v1/cloning") == 1
    assert (await _capabilities(client, owner, conn))[1]["cloning"] is True
    assert design.paths() == asked

    # Nor a design: the server has no such endpoint, and ATLAS does not try it.
    r = await _design(client, owner, conn)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["message"] == voice_design.NO_DESIGN
    assert design.posts == []

    voice_design.reset_capabilities_cache()
    router.routes["design"].mock(side_effect=httpx.ConnectError("refused"))
    status, body = await _capabilities(client, owner, conn)
    assert status == 503 and body["error"]["code"] == "integration_unavailable"


async def test_a_registered_ai_server_is_asked_with_its_token(
    client: httpx.AsyncClient,
    owner: Account,
    design: FakeDesignServer,
    router: respx.MockRouter,
) -> None:
    async with get_sessionmaker()() as db:
        db.add(
            AiServer(
                organization_id=uuid.UUID(owner.org_id),
                name="GPU box",
                base_url="https://ai.test",
                token_encrypted=get_secret_box().encrypt("gpu-box-token"),
            )
        )
        await db.commit()

    def behind_gateway(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer gpu-box-token":
            return httpx.Response(401, json={"error": "unauthorized"})
        inner = httpx.Request(
            request.method,
            request.url.copy_with(path=request.url.path.removeprefix("/design")),
            headers=request.headers,
            content=request.content,
        )
        return design.handle(inner)

    router.route(host="ai.test").mock(side_effect=behind_gateway)
    # Not named as any voice server: its capabilities say it has the voice API.
    conn = await _connection(client, owner, name="Studio", base="https://AI.test/design/v1/")
    status, body = await _capabilities(client, owner, conn)
    assert status == 200, body
    assert body["design"] is True
    r = await _design(client, owner, conn)
    assert r.status_code == 201, r.text
    voice = r.json()["id"]
    r = await client.get(
        f"/api/v1/ai/connections/{conn}/voices/{voice}/sample", headers=owner.headers
    )
    assert r.status_code == 200 and r.content == design.sample
    sent = [r for r in router.calls if r.request.url.host == "ai.test"]
    assert {c.request.headers["authorization"] for c in sent} == {"Bearer gpu-box-token"}
    assert [c.request.url.path for c in sent][-3:] == [
        "/design/v1/capabilities",
        "/design/v1/voices/design",
        f"/design/v1/voices/{voice}/sample",
    ]


# ---------------------------------------------------------------- designing a voice


async def test_design_list_select_preview_and_delete_with_audit(
    client: httpx.AsyncClient, owner: Account, design: FakeDesignServer
) -> None:
    conn = await _connection(client, owner)
    r = await _design(client, owner, conn, name="  Maeve   O'Brien ", language="en", seed=7)
    assert r.status_code == 201, r.text
    voice = r.json()
    assert re.fullmatch(r"design-maeve-o-brien-[0-9a-f]{16}", voice["id"])
    assert voice["name"] == "Maeve O'Brien"
    assert (voice["designed"], voice["cloned"]) == (True, False)
    assert voice["description"] == DESCRIPTION
    assert str(voice["created_by"]) == owner.user_id and voice["created_at"]
    assert design.posts == [
        {"name": "Maeve O'Brien", "description": DESCRIPTION, "language": "en", "seed": 7}
    ]
    async with get_sessionmaker()() as db:
        row = await db.scalar(select(CustomVoice))
        assert row is not None and row.voice_id == voice["id"] and row.connection_id == conn
        assert str(row.organization_id) == owner.org_id
    [logged] = await _audit("ai.voice_designed")
    assert logged.changes["voice_id"] == voice["id"] and logged.changes["language"] == "en"
    assert logged.changes["description_chars"] == len(DESCRIPTION)
    assert "Irish" not in json.dumps(logged.changes), "the description's text stays on the server"
    assert str(logged.actor_user_id) == owner.user_id

    listed = (
        await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    ).json()
    assert [(v["id"], v["cloned"], v["designed"]) for v in listed["voices"]] == [
        ("vivian", False, False),
        (voice["id"], False, True),
    ]
    assert listed["voices"][1]["description"] == DESCRIPTION

    r = await client.get(
        f"/api/v1/ai/connections/{conn}/voices/{voice['id']}/sample", headers=owner.headers
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["content-disposition"] == f'inline; filename="{voice["id"]}.wav"'
    assert r.headers["content-length"] == str(len(design.sample))
    assert r.content == design.sample

    # Chosen as the organization's voice like a cloned one; deleting waits until it is not.
    assert (await _select_voice(client, owner, conn, voice["id"])).status_code == 200
    url = f"/api/v1/ai/connections/{conn}/voices/{voice['id']}"
    r = await client.delete(url, headers=owner.headers)
    assert r.status_code == 422 and "used by" in r.json()["error"]["message"]
    models = (await client.get("/api/v1/ai/models", headers=owner.headers)).json()
    models["tts"]["settings"]["voice"] = "vivian"
    assert (await client.put("/api/v1/ai/models", headers=owner.headers, json=models)).is_success
    assert (await client.delete(url, headers=owner.headers)).status_code == 204
    assert design.voices == {}
    [deleted] = await _audit("ai.voice_deleted")
    assert deleted.changes["kind"] == "designed" and deleted.changes["voice_id"] == voice["id"]
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    r = await client.get(f"{url}/sample", headers=owner.headers)
    assert r.status_code == 404


async def test_design_is_checked_before_anything_is_sent(
    client: httpx.AsyncClient, owner: Account, design: FakeDesignServer
) -> None:
    conn = await _connection(client, owner)

    async def refused(**kwargs: Any) -> dict[str, Any]:
        r = await _design(client, owner, conn, **kwargs)
        assert r.status_code == 422, r.text
        error: dict[str, Any] = r.json()["error"]
        return error

    assert (await refused(name="   "))["details"] == {"field": "name"}
    assert (await refused(name="x" * 61))["details"] == {"field": "name"}
    error = await refused(description="Too short")
    assert error["details"] == {"field": "description"} and "at least 10" in error["message"]
    error = await refused(description="x" * 301)
    assert "at most 300" in error["message"]
    error = await refused(description="A calm voice\x07 with a bell")
    assert "control characters" in error["message"]
    error = await refused(language="xx")
    assert error["details"] == {"field": "language"}
    assert error["message"] == "This voice server does not speak xx. It offers: en, de, fr."
    assert (await refused(sample_text="Hello\x00"))["details"] == {"field": "sample_text"}
    assert design.posts == [], "nothing reached the voice server"

    # Whitespace is tidied, a language the server offers is passed on, the rest left out.
    r = await _design(client, owner, conn, description=f"  {DESCRIPTION}\n\n ", language=" de ")
    assert r.status_code == 201, r.text
    assert design.posts[-1] == {"name": "Maeve", "description": DESCRIPTION, "language": "de"}
    # Without a language list the server takes what it is given.
    voice_design.reset_capabilities_cache()
    design.capabilities = CAPABILITIES | {"languages": []}
    r = await _design(client, owner, conn, language="ga")
    assert r.status_code == 201 and design.posts[-1]["language"] == "ga"

    # A server whose capabilities say no design is refused with the reason.
    voice_design.reset_capabilities_cache()
    design.capabilities = CAPABILITIES | {"design": False}
    r = await _design(client, owner, conn)
    assert r.status_code == 422 and r.json()["error"]["message"] == voice_design.NO_DESIGN
    assert len(design.posts) == 2
    assert len(await _audit("ai.voice_designed")) == 2


async def test_roles_and_organizations(
    client: httpx.AsyncClient, owner: Account, design: FakeDesignServer
) -> None:
    conn = await _connection(client, owner)
    voice = (await _design(client, owner, conn)).json()["id"]
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    assert (await _design(client, manager, conn)).status_code == 403
    sample = f"/api/v1/ai/connections/{conn}/voices/{voice}/sample"
    assert (await client.get(sample, headers=manager.headers)).status_code == 200
    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    assert (await client.get(sample, headers=viewer.headers)).status_code == 403
    admin = await invite_and_join(client, owner, "admin@example.com", "admin")
    assert (await _design(client, admin, conn, name="Niamh")).status_code == 201

    # Another organization neither sees, plays, selects nor deletes it.
    other = await signup(client, "other@example.com", org="Other Co")
    played = design.paths().count(f"/v1/voices/{voice}/sample")
    assert (await client.get(sample, headers=other.headers)).status_code == 404
    theirs = await _connection(client, other)
    listed = (
        await client.get(f"/api/v1/ai/connections/{theirs}/voices", headers=other.headers)
    ).json()
    assert voice not in [v["id"] for v in listed["voices"]]
    r = await client.get(
        f"/api/v1/ai/connections/{theirs}/voices/{voice}/sample", headers=other.headers
    )
    assert r.status_code == 404
    r = await _select_voice(client, other, theirs, voice)
    assert r.status_code == 422 and "designed voice" in r.json()["error"]["message"]
    r = await client.delete(
        f"/api/v1/ai/connections/{theirs}/voices/{voice}", headers=other.headers
    )
    assert r.status_code == 404 and voice in design.voices
    assert design.paths().count(f"/v1/voices/{voice}/sample") == played, "never asked for"

    # Nor is a voice of a Kokoro-style connection designed: it is not a voice server of this kind.
    speech = await _connection(client, owner, name="Local speech", base="http://speech:8000/v1")
    r = await _design(client, owner, speech)
    assert r.status_code == 422 and "Pocket TTS" in r.json()["error"]["message"]


async def test_server_answers_are_explained(
    client: httpx.AsyncClient,
    owner: Account,
    design: FakeDesignServer,
    router: respx.MockRouter,
) -> None:
    conn = await _connection(client, owner)
    design.reply = httpx.Response(429, json={"detail": "Another voice is being made."})
    r = await _design(client, owner, conn)
    assert r.status_code == 429 and r.json()["error"]["message"] == "Another voice is being made."
    design.reply = httpx.Response(422, json={"detail": "Describe a voice, not a person."})
    r = await _design(client, owner, conn)
    assert (
        r.status_code == 422 and r.json()["error"]["message"] == "Describe a voice, not a person."
    )
    design.reply = httpx.Response(404, json={"detail": "Not Found"})
    r = await _design(client, owner, conn)
    assert r.status_code == 422 and r.json()["error"]["message"] == voice_design.NO_DESIGN
    # An id ATLAS cannot own is not recorded.
    design.reply = httpx.Response(201, json={"id": "custom-maeve-0123456789abcdef"})
    r = await _design(client, owner, conn)
    assert r.status_code == 503, r.text
    assert r.json()["error"]["message"] == "The voice server returned an unexpected voice id."
    async with get_sessionmaker()() as db:
        assert (await db.scalars(select(CustomVoice))).all() == []
    assert await _audit("ai.voice_designed") == []

    design.reply = None
    voice = (await _design(client, owner, conn)).json()["id"]
    sample = f"/api/v1/ai/connections/{conn}/voices/{voice}/sample"
    del design.voices[voice]  # gone from the server meanwhile
    r = await client.get(sample, headers=owner.headers)
    assert r.status_code == 404 and r.json()["error"]["message"] == voice_design.NO_SAMPLE

    router.routes["design"].mock(side_effect=httpx.ConnectError("refused"))
    assert (await client.get(sample, headers=owner.headers)).status_code == 503
    r = await _design(client, owner, conn)
    assert r.status_code == 503 and "cannot reach" in r.json()["error"]["message"]
