"""Agent settings beyond the versioned config: sound files, voicemail screening, agent
variables, the voicemail-detection key and the website widget.

Compiled output is checked field by field against the voice runtime's own schemas (read
from its source when it is checked out next to ATLAS), and the hand-off of files and keys
against a contract-faithful fake of its upload routes and object storage.
"""

from __future__ import annotations

import ast
import io
import json
import uuid
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.workflow_builder import (
    ClassifierModel,
    PublishAssets,
    RuntimeAudio,
    apply_variables,
    build_configurations,
    compile_workflow,
    guided_to_flow,
)
from app.models.calls import Call, CallDirection, CallEvent, CallMode, CallState
from app.schemas.agent_settings import WebEmbedUpdate, normalize_domain
from app.schemas.voice import AgentConfig, AgentVariable, CallSettings, VoicemailSettings
from app.services.web_embed import origin_allowed
from tests.calls_support import CallsRuntime, published_agent
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import Org

REPO = Path(__file__).resolve().parents[3]
RUNTIME_API = REPO / "vendor" / "voice-runtime" / "api"
AUDIO_STORAGE = "http://audio-storage.test"
SITE = "https://shop.example.com"


# ------------------------------------------------------------------ fake runtime


class SettingsRuntime(CallsRuntime):
    """Adds the runtime's ambient-noise and recordings uploads, their signed storage, and
    embed sessions that create runs (routes/workflow.py, routes/workflow_recording.py,
    routes/public_embed.py)."""

    def __init__(self) -> None:
        super().__init__()
        self.blobs: dict[str, tuple[bytes, str | None]] = {}
        self.recordings: dict[str, dict[str, Any]] = {}
        self.embed_inits: list[dict[str, Any]] = []

    def audio_storage(self, request: httpx.Request) -> httpx.Response:
        if request.method != "PUT" or "X-Amz-Signature" not in str(request.url.query):
            return httpx.Response(403)
        self.blobs[request.url.path.lstrip("/")] = (
            request.content,
            request.headers.get("content-type"),
        )
        return httpx.Response(200)

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if m == "POST" and path == "/workflow/ambient-noise/upload-url":
            if body["workflow_id"] not in org.workflows:
                return httpx.Response(404, json={"detail": "Workflow not found"})
            if not 0 < body["file_size"] <= 10_485_760:
                return httpx.Response(422, json={"detail": "file_size"})
            key = f"ambient-noise/{org.id}/{body['workflow_id']}/{uuid.uuid4()}_{body['filename']}"
            return httpx.Response(
                200,
                json={
                    "upload_url": f"{AUDIO_STORAGE}/{key}?X-Amz-Signature=sig",
                    "storage_key": key,
                    "storage_backend": "minio",
                },
            )
        if m == "POST" and path == "/workflow-recordings/upload-url":
            items = []
            for f in body["files"]:
                if not 0 < f["file_size"] <= 5_242_880:
                    return httpx.Response(422, json={"detail": "file_size"})
                rid = uuid.uuid4().hex[:8]
                key = f"recordings/{org.id}/{rid}/{f['filename']}"
                items.append(
                    {
                        "upload_url": f"{AUDIO_STORAGE}/{key}?X-Amz-Signature=sig",
                        "recording_id": rid,
                        "storage_key": key,
                    }
                )
            return httpx.Response(200, json={"items": items})
        if m == "POST" and path == "/workflow-recordings/":
            out = []
            for rec in body["recordings"]:
                assert set(rec) <= {
                    "recording_id",
                    "tts_provider",
                    "tts_model",
                    "tts_voice_id",
                    "transcript",
                    "storage_key",
                    "metadata",
                }
                assert rec["storage_key"] in self.blobs, "created before the upload finished"
                self.recordings[rec["recording_id"]] = {**rec, "org": org.id}
                out.append({**rec, "id": len(self.recordings), "storage_backend": "minio"})
            return httpx.Response(200, json={"recordings": out})
        return super()._authed(org, m, path, body, request)

    def _public(self, m: str, path: str, body: Any, request: httpx.Request) -> httpx.Response:
        response = super()._public(m, path, body, request)
        if m != "POST" or path != "/public/embed/init" or response.status_code != 200:
            return response
        org_id, wf_id, _ = self.embed_tokens[body["token"]]
        context = dict(body.get("context_variables") or {})
        self.embed_inits.append(context)
        rid = self.add_run(org_id, wf_id, initial_context=context, call_type="inbound")
        return httpx.Response(200, json={**json.loads(response.content), "workflow_run_id": rid})


@pytest.fixture
def runtime() -> Iterator[SettingsRuntime]:
    fake = SettingsRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        router.route(host="storage.test").mock(side_effect=fake.storage)
        router.route(host="audio-storage.test").mock(side_effect=fake.audio_storage)
        yield fake


# ------------------------------------------------------------------ helpers


def wav_bytes(seconds: float = 0.5, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 400


async def upload(
    client: httpx.AsyncClient,
    acct: Account,
    agent_id: str,
    *,
    purpose: str,
    name: str = "office.wav",
    data: bytes | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/agents/{agent_id}/audio",
        headers=acct.headers,
        files={"file": (name, data if data is not None else wav_bytes(), "audio/wav")},
        data={"purpose": purpose},
    )


async def save(
    client: httpx.AsyncClient, acct: Account, agent: dict[str, Any], **call_settings: Any
) -> httpx.Response:
    cfg = dict(agent["config"])
    cfg["call_settings"] = {**cfg["call_settings"], **call_settings}
    return await client.put(
        f"/api/v1/agents/{agent['id']}/config", headers=acct.headers, json={"config": cfg}
    )


def org_of(fake: SettingsRuntime) -> Org:
    return next(iter(fake.orgs.values()))


def guided(**overrides: Any) -> AgentConfig:
    base: dict[str, Any] = {
        "company_name": "Acme",
        "greeting": "Hi {{first_name}}, it's {{office_name}} calling.",
        "instructions": "Qualify the lead politely and book a visit at {{ office_name }}.",
    }
    return AgentConfig(**{**base, **overrides})


def runtime_fields(module: str, cls: str) -> set[str]:
    """Field names of a pydantic model in the runtime's source, read with `ast`."""
    path = RUNTIME_API / "schemas" / module
    if not path.exists():
        pytest.skip("voice runtime source not checked out")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return {
        s.target.id
        for s in node.body
        if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
    }


# ------------------------------------------------------------------ schema rules


def test_variable_names_are_validated_and_cannot_shadow_lead_fields() -> None:
    assert AgentVariable(name="office_name", value="  Gulberg office ").value == "Gulberg office"
    for bad in ("Office", "1office", "office-name", "x" * 41, "first_name", "company", "atlas_x"):
        with pytest.raises(ValidationError):
            AgentVariable(name=bad, value="v")
    with pytest.raises(ValidationError, match="cannot contain"):
        AgentVariable(name="promo", value="{{first_name}}")
    with pytest.raises(ValidationError, match="unique"):
        guided(variables=[{"name": "a", "value": "1"}, {"name": "a", "value": "2"}])


def test_voicemail_rules() -> None:
    with pytest.raises(ValidationError, match="Upload the recording"):
        VoicemailSettings(action="leave_message", message_format="recording")
    with pytest.raises(ValidationError, match="Write the message"):
        VoicemailSettings(action="leave_message", message_format="text", message="  ")
    with pytest.raises(ValidationError, match="call screeners"):
        VoicemailSettings(screening_format="recording")
    with pytest.raises(ValidationError, match="connection and model"):
        VoicemailSettings(classifier={"model": "connection", "connection_id": "c1"})  # type: ignore[arg-type]
    off = VoicemailSettings(
        enabled=False,
        action="leave_message",
        message_format="recording",
        classifier={"model": "connection"},  # type: ignore[arg-type]
    )
    assert off.message_audio_id is None, "nothing is required while detection is off"
    compiled = build_configurations(
        guided(call_settings=CallSettings(voicemail=off)), assets=PublishAssets()
    )["voicemail_detection"]
    assert compiled["enabled"] is False and compiled["use_workflow_llm"] is True
    assert "api_key" not in compiled
    vm = VoicemailSettings(classifier={"instructions": "   "})  # type: ignore[arg-type]
    assert vm.classifier.instructions is None


def test_domains_normalise_and_origins_match_like_the_runtime() -> None:
    assert normalize_domain("https://WWW.Example.com/path?q=1") == "www.example.com"
    assert normalize_domain("*.example.com") == "*.example.com"
    assert normalize_domain("localhost:3000") == "localhost:3000"
    for bad in ("*", "*.com", "exa mple.com", "example.com:99999", ""):
        with pytest.raises(ValueError):
            normalize_domain(bad)
    update = WebEmbedUpdate(enabled=True, allowed_domains=["example.com", "Example.com", " "])
    assert update.allowed_domains == ["example.com"]

    allowed = ["example.com", "*.shop.io", "localhost:3000"]
    assert origin_allowed("https://www.example.com", allowed)
    assert origin_allowed("https://example.com", allowed)
    assert origin_allowed("https://a.b.shop.io", allowed) and origin_allowed(
        "https://shop.io", allowed
    )
    assert origin_allowed("http://localhost:3000", allowed)
    assert not origin_allowed("http://localhost:4000", allowed)
    assert not origin_allowed("https://evil-example.com", allowed)
    assert not origin_allowed("https://example.com.evil.io", allowed)
    assert not origin_allowed(None, allowed) and not origin_allowed("https://example.com", [])


# ------------------------------------------------------------------ compile


def test_agent_variables_compile_to_fallbacks_and_leave_lead_fields_alone() -> None:
    assert (
        apply_variables(
            "At {{ office_name | fallback:HQ }} for {{first_name}}", {"office_name": "Gulberg"}
        )
        == "At {{office_name | fallback:Gulberg}} for {{first_name}}"
    )
    cfg = guided(
        variables=[{"name": "office_name", "value": "Gulberg office"}],
        global_rules="Mention {{office_name}} once.",
    )
    flow = guided_to_flow(cfg)
    flow.paths[0].say_while_moving = "One moment, {{office_name}} here."
    cfg = cfg.model_copy(update={"mode": "flow", "flow": flow})
    nodes = {n["type"]: n["data"] for n in compile_workflow(cfg, webhook=None).definition["nodes"]}
    start = nodes["startCall"]
    assert (
        start["greeting"]
        == "Hi {{first_name}}, it's {{office_name | fallback:Gulberg office}} calling."
    )
    assert "{{office_name | fallback:Gulberg office}}" in start["prompt"]
    assert (
        nodes["globalNode"]["prompt"] == "Mention {{office_name | fallback:Gulberg office}} once."
    )
    edge = compile_workflow(cfg, webhook=None).definition["edges"][0]["data"]
    assert (
        edge["transition_speech"] == "One moment, {{office_name | fallback:Gulberg office}} here."
    )


def test_compiled_settings_use_the_runtime_field_names_exactly() -> None:
    ambient_id, vm_id = uuid.uuid4(), uuid.uuid4()
    cfg = guided(
        variables=[{"name": "office_name", "value": "Gulberg"}],
        call_settings=CallSettings(
            ambient={"enabled": True, "volume": 0.45, "audio_id": ambient_id},  # type: ignore[arg-type]
            voicemail={  # type: ignore[arg-type]
                "action": "leave_message",
                "message_format": "recording",
                "message_audio_id": vm_id,
                "screening_message": "This is Sara from {{office_name}}.",
                "screening_wait_seconds": 12,
                "classifier": {
                    "model": "connection",
                    "connection_id": "conn-1",
                    "model_name": "gpt-4.1-mini",
                    "instructions": "Callers greet in Urdu.",
                },
            },
        ),
    )
    assets = PublishAssets(
        ambient=RuntimeAudio("ambient-noise/1/2/x_office.wav", "minio", "office.wav"),
        recordings={str(vm_id): "rec12345"},
        classifier=ClassifierModel("openai", "gpt-4.1-mini", "sk-test"),
    )
    c = build_configurations(cfg, assets=assets)
    assert c["ambient_noise_configuration"] == {
        "enabled": True,
        "volume": 0.45,
        "storage_key": "ambient-noise/1/2/x_office.wav",
        "storage_backend": "minio",
        "original_filename": "office.wav",
    }
    vm = c["voicemail_detection"]
    assert vm == {
        "enabled": True,
        "use_workflow_llm": False,
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "api_key": "sk-test",
        "system_prompt": "Callers greet in Urdu.",
        "voicemail_action": "leave_message",
        "voicemail_message": {"recording_id": "rec12345"},
        "screening_message": {"text": "This is Sara from {{office_name | fallback:Gulberg}}."},
        "screening_wait_ms": 12000,
    }
    # Every key is one the runtime reads: AnswerSupervisorConfig plus the classifier keys
    # run_pipeline.py `_create_answer_supervisor` takes from the same object.
    supervisor = runtime_fields("answer_supervisor.py", "AnswerSupervisorConfig")
    classifier_keys = {
        "enabled",
        "use_workflow_llm",
        "provider",
        "model",
        "api_key",
        "system_prompt",
    }
    assert set(vm) <= supervisor | classifier_keys
    message_keys = runtime_fields("answer_supervisor.py", "AnswerMessage")
    assert (
        set(vm["voicemail_message"]) <= message_keys
        and set(vm["screening_message"]) <= message_keys
    )
    ambient_keys = runtime_fields("workflow_configurations.py", "AmbientNoiseConfigurationDefaults")
    # storage_key/storage_backend are read by services/pipecat/audio_mixer.py (extra keys).
    assert set(c["ambient_noise_configuration"]) <= ambient_keys | {
        "storage_key",
        "storage_backend",
        "original_filename",
    }

    # Without assets (validation before anything is handed over) text and defaults apply.
    plain = build_configurations(guided())
    assert plain["ambient_noise_configuration"] == {"enabled": False, "volume": 0.3}
    assert plain["voicemail_detection"]["use_workflow_llm"] is True
    assert "system_prompt" not in plain["voicemail_detection"]


# ------------------------------------------------------------------ sound files


async def test_audio_upload_validation_preview_and_tenancy(
    client: httpx.AsyncClient, owner: Account, runtime: SettingsRuntime
) -> None:
    agent = (await client.post("/api/v1/agents", headers=owner.headers, json={"name": "A"})).json()
    aid = agent["id"]

    r = await upload(client, owner, aid, purpose="ambient")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["purpose"] == "ambient" and body["content_type"] == "audio/wav"
    assert body["duration_seconds"] == 0.5 and "runtime" not in json.dumps(body)

    mp3 = await upload(client, owner, aid, purpose="message", name="vm.mp3", data=MP3)
    assert mp3.status_code == 201 and mp3.json()["content_type"] == "audio/mpeg"

    fake = await upload(client, owner, aid, purpose="ambient", name="x.wav", data=b"not audio" * 50)
    assert fake.status_code == 422
    wrong_ext = await upload(client, owner, aid, purpose="ambient", name="x.ogg")
    assert wrong_ext.status_code == 422
    disguised = await upload(client, owner, aid, purpose="ambient", name="x.mp3")
    assert disguised.status_code == 422, "a WAV named .mp3 is refused"

    big_message = await upload(
        client, owner, aid, purpose="message", data=wav_bytes() + b"\x00" * (5 * 1024 * 1024)
    )
    assert big_message.status_code == 413
    ten_mb = wav_bytes() + b"\x00" * (10 * 1024 * 1024 - len(wav_bytes()))
    assert (await upload(client, owner, aid, purpose="ambient", data=ten_mb)).status_code == 201
    too_big = await upload(client, owner, aid, purpose="ambient", data=ten_mb + b"\x00")
    assert too_big.status_code == 413

    preview = await client.get(
        f"/api/v1/agents/{aid}/audio/{body['id']}/file", headers=owner.headers
    )
    assert preview.status_code == 200 and preview.headers["content-type"] == "audio/wav"
    assert preview.content == wav_bytes()
    listed = (await client.get(f"/api/v1/agents/{aid}/audio", headers=owner.headers)).json()
    assert {a["id"] for a in listed} >= {body["id"], mp3.json()["id"]}

    other = await signup(client, "other@example.com", org="Other Co")
    assert (
        await client.get(f"/api/v1/agents/{aid}/audio/{body['id']}/file", headers=other.headers)
    ).status_code == 404
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    assert (await upload(client, viewer, aid, purpose="ambient")).status_code == 403

    # A config may only point at this agent's own files, with the right purpose.
    other_agent = (
        await client.post("/api/v1/agents", headers=other.headers, json={"name": "B"})
    ).json()
    foreign = (await upload(client, other, other_agent["id"], purpose="ambient")).json()
    r = await save(
        client, owner, agent, ambient={"enabled": True, "volume": 0.3, "audio_id": foreign["id"]}
    )
    assert r.status_code == 422 and "not found" in r.json()["error"]["message"]
    r = await save(
        client, owner, agent, ambient={"enabled": True, "volume": 0.3, "audio_id": mp3.json()["id"]}
    )
    assert r.status_code == 422, "a message recording is not a background sound"
    r = await save(
        client, owner, agent, ambient={"enabled": True, "volume": 0.3, "audio_id": body["id"]}
    )
    assert (
        r.status_code == 200
        and r.json()["config"]["call_settings"]["ambient"]["audio_id"] == body["id"]
    )

    # Files a saved version uses are kept; unused ones can be deleted.
    used = await client.delete(f"/api/v1/agents/{aid}/audio/{body['id']}", headers=owner.headers)
    assert used.status_code == 409
    unused = await client.delete(
        f"/api/v1/agents/{aid}/audio/{mp3.json()['id']}", headers=owner.headers
    )
    assert unused.status_code == 204


# ------------------------------------------------------------------ publishing


async def test_publish_hands_files_key_and_variables_to_the_runtime_once(
    client: httpx.AsyncClient, owner: Account, runtime: SettingsRuntime
) -> None:
    agent = await published_agent(client, owner)
    aid = agent["id"]
    agent = (await client.get(f"/api/v1/agents/{aid}", headers=owner.headers)).json()
    connections = (await client.get("/api/v1/ai/connections", headers=owner.headers)).json()
    openai = next(c["id"] for c in connections if c["provider"] == "openai")
    deepgram = next(c["id"] for c in connections if c["provider"] == "deepgram")

    ambient = (await upload(client, owner, aid, purpose="ambient")).json()
    message = (await upload(client, owner, aid, purpose="message", name="vm.mp3", data=MP3)).json()
    cfg = dict(agent["config"])
    cfg["variables"] = [{"name": "office_name", "value": "Gulberg office"}]
    cfg["greeting"] = "Hi {{first_name}}, this is {{office_name}}."
    cfg["call_settings"] = {
        **cfg["call_settings"],
        "ambient": {"enabled": True, "volume": 0.4, "audio_id": ambient["id"]},
        "voicemail": {
            "enabled": True,
            "action": "leave_message",
            "message_format": "recording",
            "message": "Please call {{office_name}} back.",
            "message_audio_id": message["id"],
            "classifier": {
                "model": "connection",
                "connection_id": openai,
                "model_name": "gpt-4.1-mini",
            },
        },
    }
    r = await client.put(
        f"/api/v1/agents/{aid}/config", headers=owner.headers, json={"config": cfg}
    )
    assert r.status_code == 200, r.text

    # The classifier needs its key first; the issue points at the setting.
    r = await client.post(f"/api/v1/agents/{aid}/publish", headers=owner.headers, json={})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"] == "voicemail.classifier"
    assert runtime.count("POST", "ambient-noise/upload-url") == 0, "nothing handed over yet"

    key_url = f"/api/v1/agents/{aid}/voicemail-classifier-key"
    bad = await client.put(
        key_url, headers=owner.headers, json={"connection_id": deepgram, "api_key": "k"}
    )
    assert bad.status_code == 422
    ok = await client.put(
        key_url, headers=owner.headers, json={"connection_id": openai, "api_key": "sk-classifier"}
    )
    assert ok.status_code == 200 and "api_key" not in ok.json()
    assert (await client.get(key_url, headers=owner.headers)).json()["connection_id"] == openai

    r = await client.post(f"/api/v1/agents/{aid}/publish", headers=owner.headers, json={})
    assert r.status_code == 200, r.text
    sent = runtime.last("PUT", r"^/workflow/\d+$")
    conf = sent["workflow_configurations"]
    amb = conf["ambient_noise_configuration"]
    assert amb["enabled"] is True and amb["volume"] == 0.4 and amb["storage_backend"] == "minio"
    assert (
        amb["storage_key"].startswith("ambient-noise/") and amb["original_filename"] == "office.wav"
    )
    assert runtime.blobs[amb["storage_key"]] == (wav_bytes(), "audio/wav")
    vm = conf["voicemail_detection"]
    rid = vm["voicemail_message"]["recording_id"]
    assert runtime.recordings[rid]["transcript"] == "Please call {{office_name}} back."
    assert runtime.blobs[runtime.recordings[rid]["storage_key"]][0] == MP3
    assert (vm["use_workflow_llm"], vm["provider"], vm["model"], vm["api_key"]) == (
        False,
        "openai",
        "gpt-4.1-mini",
        "sk-classifier",
    )
    assert sent["template_context_variables"] == {"office_name": "Gulberg office"}
    start = next(n for n in sent["workflow_definition"]["nodes"] if n["type"] == "startCall")
    assert (
        start["data"]["greeting"]
        == "Hi {{first_name}}, this is {{office_name | fallback:Gulberg office}}."
    )

    # Publishing again reuses what the runtime already has.
    cfg["closing"] = "Thanks, goodbye."
    await client.put(f"/api/v1/agents/{aid}/config", headers=owner.headers, json={"config": cfg})
    r = await client.post(f"/api/v1/agents/{aid}/publish", headers=owner.headers, json={})
    assert r.status_code == 200, r.text
    assert runtime.count("POST", "ambient-noise/upload-url") == 1
    assert runtime.count("POST", "^/workflow-recordings/$") == 1
    again = runtime.last("PUT", r"^/workflow/\d+$")["workflow_configurations"]
    assert again["ambient_noise_configuration"]["storage_key"] == amb["storage_key"]

    # Neither the key nor runtime storage details are ever returned.
    detail = (await client.get(f"/api/v1/agents/{aid}", headers=owner.headers)).text
    assert "sk-classifier" not in detail and "ambient-noise/" not in detail


# ------------------------------------------------------------------ website widget


async def _widget(
    client: httpx.AsyncClient, acct: Account, agent_id: str, domains: list[str]
) -> dict[str, Any]:
    r = await client.put(
        f"/api/v1/agents/{agent_id}/web-embed",
        headers=acct.headers,
        json={"enabled": True, "allowed_domains": domains},
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_widget_management_rotation_and_tenancy(
    client: httpx.AsyncClient, owner: Account, runtime: SettingsRuntime
) -> None:
    agent = await published_agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}/web-embed"
    first = (await client.get(url, headers=owner.headers)).json()
    assert first["enabled"] is False and first["key"] is None and first["agent_published"]

    r = await client.put(url, headers=owner.headers, json={"enabled": True, "allowed_domains": []})
    assert r.status_code == 422
    r = await client.put(
        url,
        headers=owner.headers,
        json={"enabled": True, "allowed_domains": ["https://Shop.Example.com/"]},
    )
    assert r.status_code == 200, r.text
    embed = r.json()
    assert embed["allowed_domains"] == ["shop.example.com"] and embed["key"].startswith("wk_")
    assert embed["snippet"] == (
        '<script src="http://localhost:5173/api/v1/public/embed/widget.js" '
        f'data-agent-key="{embed["key"]}" async></script>'
    )
    # The voice session token now allows the web app (test calls) and the widget's site.
    org = org_of(runtime)
    wf = next(iter(org.workflows))
    token_domains = next(d for (o, w, d) in runtime.embed_tokens.values() if w == wf)
    assert token_domains == ["localhost:5173", "shop.example.com"]

    rotated = (await client.post(f"{url}/rotate", headers=owner.headers)).json()
    assert rotated["key"] != embed["key"] and rotated["rotated_at"]
    old = await client.post(
        f"/api/v1/public/embed/{embed['key']}/sessions", headers={"Origin": SITE}, json={}
    )
    assert old.status_code == 404

    other = await signup(client, "other@example.com", org="Other Co")
    assert (await client.get(url, headers=other.headers)).status_code == 404
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    assert (await client.get(url, headers=viewer.headers)).status_code == 200
    assert (
        await client.put(
            url, headers=viewer.headers, json={"enabled": False, "allowed_domains": []}
        )
    ).status_code == 403


async def test_widget_session_checks_origin_and_records_an_inbound_web_call(
    client: httpx.AsyncClient, owner: Account, runtime: SettingsRuntime
) -> None:
    agent = await published_agent(client, owner)
    embed = await _widget(client, owner, agent["id"], ["shop.example.com"])
    sessions = f"/api/v1/public/embed/{embed['key']}/sessions"

    assert (await client.post(sessions, json={})).status_code == 403, "no origin"
    evil = await client.post(sessions, headers={"Origin": "https://evil.example.org"}, json={})
    assert evil.status_code == 403
    assert evil.headers["access-control-allow-origin"] == "https://evil.example.org"
    assert (
        await client.post(
            "/api/v1/public/embed/wk_unknownkey123/sessions", headers={"Origin": SITE}, json={}
        )
    ).status_code == 404

    preflight = await client.options(
        sessions,
        headers={
            "Origin": SITE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == SITE
    assert "access-control-allow-credentials" not in preflight.headers

    r = await client.post(sessions, headers={"Origin": SITE}, json={})
    assert r.status_code == 201, r.text
    s = r.json()
    assert r.headers["access-control-allow-origin"] == SITE
    assert s["signaling_url"].startswith(
        "ws://localhost:8000/api/v1/ws/public/signaling/emb_session_"
    )
    assert s["agent_name"] and s["ice_servers"] and "token" not in json.dumps(s["ice_servers"])
    assert runtime.embed_inits[-1] == {"atlas_call_id": s["call_id"], "source": "website"}

    async with get_sessionmaker()() as db:
        call = await db.get(Call, uuid.UUID(s["call_id"]))
        assert call is not None
        assert (call.direction, call.mode, call.state) == (
            CallDirection.INBOUND,
            CallMode.LIVE,
            CallState.IN_PROGRESS,
        )
        assert call.runtime_run_id and str(call.organization_id) == owner.org_id
        created = (
            await db.execute(
                select(CallEvent).where(CallEvent.call_id == call.id, CallEvent.type == "created")
            )
        ).scalar_one()
        assert created.data["source"] == "website" and created.data["site"] == "shop.example.com"

    # Ending: the token is checked; a call that never connected is closed as canceled.
    end = f"{sessions}/{s['call_id']}/end"
    assert (
        await client.post(end, json={"call_token": "x" * 20, "connected": False})
    ).status_code == 404
    beacon = await client.post(
        end,
        content=json.dumps({"call_token": s["call_token"], "connected": False}),
        headers={"Content-Type": "text/plain;charset=UTF-8", "Origin": SITE},
    )
    assert beacon.status_code == 204
    async with get_sessionmaker()() as db:
        call = await db.get(Call, uuid.UUID(s["call_id"]))
        assert call is not None and call.state == CallState.CANCELED
        assert call.error_code == "not_connected"
    again = await client.post(end, json={"call_token": s["call_token"], "connected": True})
    assert again.status_code == 204, "idempotent"

    # Calls appear under Calls for the tenant, as inbound.
    listed = (await client.get("/api/v1/calls", headers=owner.headers)).json()
    assert any(c["id"] == s["call_id"] and c["direction"] == "inbound" for c in listed["items"])


async def test_widget_refuses_switched_off_unpublished_and_floods(
    client: httpx.AsyncClient, owner: Account, runtime: SettingsRuntime
) -> None:
    agent = await published_agent(client, owner)
    embed = await _widget(client, owner, agent["id"], ["shop.example.com"])
    sessions = f"/api/v1/public/embed/{embed['key']}/sessions"
    origin = {"Origin": SITE}

    statuses = [
        (await client.post(sessions, headers=origin, json={})).status_code for _ in range(7)
    ]
    assert statuses[:6] == [201] * 6 and statuses[6] == 429

    from app.core.redis import get_redis

    await get_redis().flushdb()
    paused = await client.post(f"/api/v1/agents/{agent['id']}/pause", headers=owner.headers)
    assert paused.status_code == 200
    assert (await client.post(sessions, headers=origin, json={})).status_code == 409
    await client.post(f"/api/v1/agents/{agent['id']}/resume", headers=owner.headers)

    await client.put(
        f"/api/v1/agents/{agent['id']}/web-embed",
        headers=owner.headers,
        json={"enabled": False, "allowed_domains": ["shop.example.com"]},
    )
    assert (await client.post(sessions, headers=origin, json={})).status_code == 404


async def test_widget_script_is_served_without_runtime_details(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/public/embed/widget.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"
    text = r.text.lower()
    assert "data-agent-key" in text and "/api/v1/public/embed/" in text
    runtime_name = "".join(["d", "o", "g", "r", "a", "h"])
    assert runtime_name not in text and "x-api-key" not in text
