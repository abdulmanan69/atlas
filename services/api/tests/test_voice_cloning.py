"""Pocket TTS voice cloning through ATLAS, against a fake Pocket TTS server and voice runtime."""

from __future__ import annotations

import io
import json
import logging
import math
import re
import secrets
import struct
import sys
import wave
from collections.abc import Iterator
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

import av
import httpx
import pytest
import respx
from sqlalchemy import select

from app.core import redis as redis_core
from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.db.base import Base
from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from app.models.voice_cloning import CustomVoice
from app.services import voice_cloning, voice_samples
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime

TTS = "https://tts.test/v1"
TERMS = "https://huggingface.co/kyutai/pocket-tts"
UNAVAILABLE = (
    "Voice cloning needs the gated weights: accept the terms at "
    "https://huggingface.co/kyutai/pocket-tts and set HF_TOKEN."
)
HF_TOKEN = "hf_" + "Zq8wR3tY" * 4 + "kP"  # the shape of a real token, never a real one


def wav(seconds: float = 6.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00\xf0\xff" * int(seconds * rate / 2))
    return buf.getvalue()


def encode(
    fmt: str, codec: str, seconds: float, *, rate: int = 48000, layout: str = "mono"
) -> bytes:
    """A 200 Hz tone encoded in memory with PyAV, in 20 ms frames."""
    channels = len(av.AudioLayout(layout).channels)
    step = rate // 50
    tone = [int(8000 * math.sin(2 * math.pi * 200 * i / rate)) for i in range(step)]
    block = struct.pack(f"<{step * channels}h", *(s for s in tone for _ in range(channels)))
    buf = io.BytesIO()
    with av.open(buf, "w", format=fmt) as container:
        stream = container.add_stream(codec, rate=rate, layout=layout)
        for pts in range(0, int(seconds * rate), step):
            frame = av.AudioFrame(format="s16", layout=layout, samples=step)
            frame.planes[0].update(block)
            frame.sample_rate, frame.pts, frame.time_base = rate, pts, Fraction(1, rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buf.getvalue()


def can_encode(codec: str) -> bool:
    try:
        av.codec.Codec(codec, "w")
    except Exception:
        return False
    return True


def wav_info(data: bytes) -> tuple[int, int, int, float]:
    """(channels, rate, sample width, seconds) of a WAV file."""
    with wave.open(io.BytesIO(data)) as w:
        rate = w.getframerate()
        return w.getnchannels(), rate, w.getsampwidth(), w.getnframes() / rate


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
    """The adapter's /v1/voices and /v1/cloning contract (infra/docker/pocket-tts/app.py)."""

    def __init__(self) -> None:
        self.state = "on"  # off, enabling, on or failed
        self.token_source: str | None = "environment"
        self.cloning_api = True  # False: a server from before /v1/cloning
        self.voices: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, bytes]] = []
        self.tokens: list[str] = []
        self.reply: httpx.Response | None = None
        self.cloning_reply: httpx.Response | None = None

    @property
    def cloning(self) -> bool:
        return self.state == "on"

    @cloning.setter
    def cloning(self, on: bool) -> None:
        self.state, self.token_source = ("on", "environment") if on else ("off", None)

    def _status(self) -> dict[str, Any]:
        message = {"failed": "Hugging Face rejected the token.", "off": None}.get(self.state)
        return {
            "state": self.state,
            "message": message,
            "terms_url": TERMS,
            "token_source": self.token_source,
        }

    def _cloning(self, request: httpx.Request) -> httpx.Response:
        if not self.cloning_api:
            return httpx.Response(404, json={"detail": "Not Found"})
        if request.method == "PUT":
            self.tokens.append(json.loads(request.content)["hf_token"])
        if self.cloning_reply is not None:
            return self.cloning_reply
        if request.method == "GET":
            return httpx.Response(200, json=self._status())
        if request.method == "PUT":
            if self.state == "enabling":
                return httpx.Response(409, json={"detail": "Voice cloning is being turned on."})
            self.state, self.token_source = "enabling", "saved"
            return httpx.Response(202, json=self._status())
        if request.method == "DELETE":
            if self.token_source == "environment":
                return httpx.Response(
                    409, json={"detail": "HF_TOKEN is set where the server runs: remove it there."}
                )
            self.state, self.token_source = "off", None
            return httpx.Response(204)
        return httpx.Response(405)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/v1/audio/voices" and method == "GET":
            return httpx.Response(
                200, json={"data": [{"id": "alba", "license": "CC-BY-4.0", "default": True}]}
            )
        if path == "/v1/cloning":
            return self._cloning(request)
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
    filename: str = "sam.wav",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    data = {"name": name}
    if consent is not None:
        data["consent_attestation"] = consent
    return await client.post(
        f"/api/v1/ai/connections/{conn}/voices",
        headers=headers or acct.headers,
        data=data,
        # The type the browser claims is not trusted: the content decides.
        files={"audio": (filename, wav() if audio is None else audio, "application/octet-stream")},
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
    assert body["cloning_state"] == "off"
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
    assert r.json()["error"]["code"] == "unsupported_media_type"
    assert r.json()["error"]["message"].startswith("Could not read this audio.")
    assert (await _clone(client, owner, conn, name="   ")).status_code == 422
    assert (await _clone(client, owner, conn, name="x" * 61)).status_code == 422
    # Just over 10 MB: the sample limit answers, not the request body limit.
    r = await _clone(client, owner, conn, audio=b"RIFF" + bytes(10 * 1024 * 1024))
    assert r.status_code == 413
    assert r.json()["error"]["message"] == "Voice samples must be 10 MB or smaller."
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
    # A 16 kHz WAV is normalized like any other upload: mono 16-bit at the model's 24 kHz.
    assert wav_info(sent["audio"]) == (1, 24000, 2, 6.0)
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
    assert changes["sample_format"] == "wav"
    assert "RIFF" not in str(changes), "the audio is never recorded"

    listed = (
        await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    ).json()
    assert listed["cloning_available"] is True
    assert listed["cloning_state"] == "on"
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


# ---------------------------------------------------------------- sample formats


async def test_common_audio_formats_reach_the_server_as_24khz_mono_wav(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    conn = await _connection(client, owner)
    samples = [  # (file name, PyAV container, encoder, rate, layout, demuxer used)
        ("sam.flac", "flac", "flac", 44100, "mono", "flac"),
        ("sam.ogg", "ogg", "libopus", 48000, "stereo", "ogg"),
        ("sam.webm", "webm", "libopus", 48000, "mono", "matroska"),
        ("sam.m4a", "ipod", "aac", 44100, "stereo", "mov"),
        ("sam.mp3", "mp3", "libmp3lame", 44100, "stereo", "mp3"),
        ("sam.wav", "wav", "pcm_s24le", 48000, "stereo", "wav"),
    ]
    expected = []
    for filename, fmt, codec, rate, layout, demuxer in samples:
        if not can_encode(codec):
            continue  # this PyAV build cannot write it; decoding is what is tested
        audio = encode(fmt, codec, 7.0, rate=rate, layout=layout)
        r = await _clone(client, owner, conn, audio=audio, filename=filename)
        assert r.status_code == 201, (filename, r.text)
        sent = pocket.posts[-1]["audio"]
        channels, out_rate, width, seconds = wav_info(sent)
        assert (channels, out_rate, width) == (1, 24000, 2), filename
        assert seconds == pytest.approx(7.0, abs=0.1), filename
        peak = max(abs(s) for (s,) in struct.iter_unpack("<h", sent[44:]))
        assert peak > 2000, f"{filename}: the tone survives downmixing"
        expected.append(demuxer)
    assert {"flac", "ogg", "matroska", "mov", "wav"} <= set(expected)
    formats = [entry.changes["sample_format"] for entry in await _audit("ai.voice_cloned")]
    assert sorted(formats) == sorted(expected)


NOISE = bytes((i * 151 + 7) % 256 for i in range(4096))


async def test_samples_must_be_5_to_30_seconds_of_readable_audio(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    conn = await _connection(client, owner)
    short = encode("flac", "flac", 3.0, rate=16000)
    r = await _clone(client, owner, conn, audio=short, filename="short.flac")
    assert r.status_code == 422
    error = r.json()["error"]
    assert error["message"] == "The sample is 3.0 s long: it must be between 5 and 30 seconds."
    assert error["details"] == {"field": "audio"}
    long = encode("flac", "flac", 31.0, rate=8000)
    r = await _clone(client, owner, conn, audio=long, filename="long.flac")
    assert r.status_code == 422
    assert "longer than 30 seconds" in r.json()["error"]["message"]

    r = await _clone(client, owner, conn, audio=b"This is not audio. " * 200, filename="x.mp3")
    assert r.status_code == 415
    assert r.json()["error"]["message"].startswith("Upload the sample as WAV, MP3, M4A")
    for junk in (b"OggS" + NOISE, b"fLaC" + NOISE, b"RIFF\x00\x10\x00\x00WAVE" + NOISE):
        r = await _clone(client, owner, conn, audio=junk)
        assert r.status_code == 415, r.text
        assert r.json()["error"]["message"].startswith("Could not read this audio.")
    assert pocket.posts == [], "nothing reached the Pocket TTS server"


async def test_a_crashing_or_hanging_decoder_does_not_take_the_api_down(
    client: httpx.AsyncClient,
    owner: Account,
    pocket: FakePocket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = await _connection(client, owner)
    # PyAV 19.0.1 aborts the whole process on audio with a custom channel order (PyAV issue
    # 2444), such as a film-order 5.1 MOV. Decoding runs in a child process, so the API
    # answers (415 while PyAV cannot read it, 201 once it can).
    film = bytearray(encode("mov", "pcm_s16le", 6.0, layout="5.1"))
    chan = film.find(b"chan")
    assert chan > 0
    film[chan + 8 : chan + 12] = struct.pack(">I", (123 << 16) | 6)  # L C R Ls Rs LFE
    r = await _clone(client, owner, conn, audio=bytes(film), filename="film.mov")
    assert r.status_code in (201, 415), r.text

    child = voice_samples._child_argv
    crash = [sys.executable, "-c", "import os; os._exit(134)"]
    monkeypatch.setattr(voice_samples, "_child_argv", lambda demuxer: crash)
    r = await _clone(client, owner, conn)
    assert r.status_code == 415
    assert r.json()["error"]["message"].startswith("Could not read this audio.")

    # Like the real child, it reads the upload first, then never finishes.
    hang = [sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(60)"]
    monkeypatch.setattr(voice_samples, "_child_argv", lambda demuxer: hang)
    monkeypatch.setattr(voice_samples, "DECODE_TIMEOUT_SECONDS", 1.0)
    assert (await _clone(client, owner, conn)).status_code == 415

    monkeypatch.setattr(voice_samples, "_child_argv", child)
    assert (await _clone(client, owner, conn)).status_code == 201


def test_sniffing_picks_the_demuxer_from_the_content() -> None:
    sniff = voice_samples.sniff
    assert sniff(wav()) == "wav"
    assert sniff(b"\x00\x00\x00\x20ftypM4A " + NOISE) == "mov"
    assert sniff(b"\x1a\x45\xdf\xa3" + NOISE) == "matroska"
    assert sniff(b"\xff\xfb\x90\x00" + NOISE) == "mp3"
    assert sniff(b"\xff\xf1\x50\x80" + NOISE) == "aac"  # ADTS
    tag = b"ID3\x04\x00\x00\x00\x00\x00\x04" + b"\x00" * 4  # a 4-byte ID3v2 tag
    assert sniff(tag + b"\xff\xf9\x50\x80") == "aac"
    assert sniff(tag + b"\xff\xfb\x90\x00") == "mp3"
    assert sniff(b"<html>" + NOISE) is None
    assert sniff(b"") is None


# ---------------------------------------------------------------- turning cloning on and off

ENABLE = {"hf_token": HF_TOKEN, "terms_accepted": True}


async def _cloning(
    client: httpx.AsyncClient,
    acct: Account,
    conn: str,
    method: str = "GET",
    *,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    return await client.request(
        method,
        f"/api/v1/ai/connections/{conn}/voice-cloning",
        headers=headers or acct.headers,
        **kwargs,
    )


async def _voices(client: httpx.AsyncClient, acct: Account, conn: str) -> dict[str, Any]:
    r = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=acct.headers)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_owner_turns_voice_cloning_on_and_off(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    pocket.cloning = False
    conn = await _connection(client, owner)
    r = await _cloning(client, owner, conn)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "state": "off",
        "message": None,
        "terms_url": TERMS,
        "token_source": None,
        "can_manage": True,
    }

    r = await _cloning(client, owner, conn, "PUT", json={"hf_token": HF_TOKEN})
    assert r.status_code == 422
    assert r.json()["error"]["details"] == {"field": "terms_accepted", "terms_url": TERMS}
    assert pocket.tokens == [], "nothing is sent before the terms are confirmed"

    r = await _cloning(client, owner, conn, "PUT", json=ENABLE)
    assert r.status_code == 202, r.text
    assert r.json()["state"] == "enabling" and r.json()["token_source"] == "saved"
    assert pocket.tokens == [HF_TOKEN]
    voices = await _voices(client, owner, conn)
    assert (voices["cloning_available"], voices["cloning_state"]) == (False, "enabling")
    r = await _cloning(client, owner, conn, "PUT", json=ENABLE)
    assert r.status_code == 409
    assert r.json()["error"]["message"] == "Voice cloning is being turned on."

    pocket.state = "on"  # the weights finished downloading
    assert (await _cloning(client, owner, conn)).json()["state"] == "on"
    voices = await _voices(client, owner, conn)
    assert (voices["cloning_available"], voices["cloning_state"]) == (True, "on")

    assert (await _cloning(client, owner, conn, "DELETE")).status_code == 204
    assert (await _cloning(client, owner, conn)).json()["state"] == "off"
    assert (await _voices(client, owner, conn))["cloning_state"] == "off"

    [enabled] = await _audit("voice_cloning.enable_requested")
    assert enabled.changes == {"connection_id": conn, "terms_accepted": True, "state": "enabling"}
    assert str(enabled.actor_user_id) == owner.user_id
    assert (enabled.resource_type, enabled.resource_id) == ("ai_connection", conn)
    [disabled] = await _audit("voice_cloning.disabled")
    assert disabled.changes == {"connection_id": conn}
    assert str(disabled.actor_user_id) == owner.user_id


async def test_only_owners_turn_voice_cloning_on_or_off(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket
) -> None:
    conn = await _connection(client, owner)
    admin = await invite_and_join(client, owner, "admin@example.com", "admin")
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    for acct in (admin, manager):
        r = await _cloning(client, acct, conn)
        assert r.status_code == 200 and r.json()["can_manage"] is False
        assert (await _cloning(client, acct, conn, "PUT", json=ENABLE)).status_code == 403
        assert (await _cloning(client, acct, conn, "DELETE")).status_code == 403
    assert (await _cloning(client, viewer, conn)).status_code == 403

    # API keys act with their own role.
    keys = {}
    for role in ("owner", "admin"):
        r = await client.post(
            "/api/v1/developer/api-keys", headers=owner.headers, json={"name": role, "role": role}
        )
        assert r.status_code == 201, r.text
        keys[role] = {"X-API-Key": r.json()["key"]}
    r = await _cloning(client, owner, conn, "PUT", json=ENABLE, headers=keys["admin"])
    assert r.status_code == 403
    r = await _cloning(client, owner, conn, "PUT", json=ENABLE, headers=keys["owner"])
    assert r.status_code == 202, r.text
    assert r.json()["can_manage"] is True
    [entry] = await _audit("voice_cloning.enable_requested")
    assert entry.changes["actor"]["type"] == "api_key"
    assert pocket.tokens == [HF_TOKEN]

    # Only Pocket TTS connections, and only the organization's own.
    other = await signup(client, "other@example.com", org="Other Co")
    assert (await _cloning(client, other, conn, "PUT", json=ENABLE)).status_code == 404
    speech = await _connection(client, owner, name="Local speech (Whisper)")
    r = await _cloning(client, owner, speech, "PUT", json=ENABLE)
    assert r.status_code == 422 and "Pocket TTS" in r.json()["error"]["message"]
    assert pocket.tokens == [HF_TOKEN]


async def test_operator_managed_switch_keeps_owners_out(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "voice_cloning_switch", "operator")
    conn = await _connection(client, owner)
    r = await _cloning(client, owner, conn)
    assert r.status_code == 200 and r.json()["can_manage"] is False
    for method in ("PUT", "DELETE"):
        r = await _cloning(client, owner, conn, method, json=ENABLE if method == "PUT" else None)
        assert r.status_code == 403, r.text
        assert "operator" in r.json()["error"]["message"]
    assert pocket.tokens == [], "nothing reaches the voice server"
    assert await _audit("voice_cloning.enable_requested") == []


async def test_voice_server_answers_are_explained(
    client: httpx.AsyncClient,
    owner: Account,
    pocket: FakePocket,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_cloning, "CLONING_CHANGES_PER_MINUTE", 100)
    conn = await _connection(client, owner)

    async def enable(reply: httpx.Response) -> tuple[int, dict[str, Any]]:
        pocket.cloning_reply = reply
        r = await _cloning(client, owner, conn, "PUT", json=ENABLE)
        assert HF_TOKEN not in r.text
        return r.status_code, r.json()["error"]

    # Hugging Face refused the token (and the server quoted it back), or the terms.
    status, error = await enable(
        httpx.Response(401, json={"detail": f"Hugging Face rejected the token {HF_TOKEN}."})
    )
    assert status == 422
    assert error["message"] == "Hugging Face rejected the token [REDACTED]."
    assert error["details"] == {"field": "hf_token", "terms_url": TERMS}
    terms = "Accept the terms of kyutai/pocket-tts with the account that owns the token."
    status, error = await enable(httpx.Response(403, json={"detail": terms}))
    assert (status, error["message"], error["details"]["terms_url"]) == (422, terms, TERMS)
    # A structured validation error is never passed on: it could quote the token.
    status, error = await enable(
        httpx.Response(422, json={"detail": [{"loc": ["body", "hf_token"], "input": HF_TOKEN}]})
    )
    assert (status, error["message"]) == (422, voice_cloning.BAD_CREDENTIAL)
    status, error = await enable(
        httpx.Response(502, json={"detail": "Hugging Face could not be reached."})
    )
    assert (status, error["code"]) == (503, "integration_unavailable")
    assert error["message"] == "Hugging Face could not be reached."
    status, error = await enable(httpx.Response(401, json={"detail": "Invalid API key"}))
    assert status == 503 and "ATLAS_LOCAL_TTS_API_KEY" in error["message"]
    assert await _audit("voice_cloning.enable_requested") == []
    # Accepted with a body this version cannot read: still accepted, and audited.
    pocket.cloning_reply = httpx.Response(202, text="accepted")
    r = await _cloning(client, owner, conn, "PUT", json=ENABLE)
    assert (r.status_code, r.json()["state"]) == (202, "enabling")
    assert len(await _audit("voice_cloning.enable_requested")) == 1

    # Status replies are checked; only Hugging Face pages are passed on as links.
    pocket.cloning_reply = httpx.Response(200, json={"state": "maybe"})
    assert (await _cloning(client, owner, conn)).status_code == 503
    pocket.cloning_reply = httpx.Response(
        200,
        json={
            "state": "failed",
            "message": "The download failed.",
            "terms_url": "https://example.com/phish",
            "token_source": "somewhere",
        },
    )
    body = (await _cloning(client, owner, conn)).json()
    assert (body["state"], body["message"]) == ("failed", "The download failed.")
    assert (body["terms_url"], body["token_source"]) == (TERMS, None)

    # A token set where the server runs cannot be removed from here.
    pocket.cloning_reply = None
    pocket.cloning = True
    r = await _cloning(client, owner, conn, "DELETE")
    assert r.status_code == 409 and "HF_TOKEN" in r.json()["error"]["message"]
    assert await _audit("voice_cloning.disabled") == []

    # A server from before voice cloning could be managed from ATLAS.
    pocket.cloning_api, pocket.cloning = False, False
    body = (await _cloning(client, owner, conn)).json()
    assert body["state"] == "unsupported" and "update it" in body["message"]
    for method, extra in (("PUT", {"json": ENABLE}), ("DELETE", {})):
        r = await _cloning(client, owner, conn, method, **extra)
        assert r.status_code == 501 and r.json()["error"]["code"] == "voice_cloning_unavailable"
    voices = await _voices(client, owner, conn)
    assert (voices["cloning_state"], voices["setup_message"]) == ("unsupported", UNAVAILABLE)

    router.routes["tts"].mock(side_effect=httpx.ConnectError("refused"))
    assert (await _cloning(client, owner, conn, "PUT", json=ENABLE)).status_code == 503
    assert (await _cloning(client, owner, conn)).status_code == 503


async def test_cloning_changes_are_rate_limited_per_organization(
    client: httpx.AsyncClient, owner: Account, pocket: FakePocket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One fixed-window minute for the whole test, wherever the clock is.
    monkeypatch.setattr(redis_core, "time", SimpleNamespace(time=lambda: 6_000_000.0))
    conn = await _connection(client, owner)
    for _ in range(voice_cloning.CLONING_CHANGES_PER_MINUTE):
        pocket.state = "off"
        assert (await _cloning(client, owner, conn, "PUT", json=ENABLE)).status_code == 202
    r = await _cloning(client, owner, conn, "DELETE")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert (await _cloning(client, owner, conn)).status_code == 200, "reading is not limited"

    other = await signup(client, "other@example.com", org="Other Co")
    conn_b = await _connection(client, other)
    assert (await _cloning(client, other, conn_b, "DELETE")).status_code == 204


async def test_the_token_is_never_logged_stored_or_returned(
    client: httpx.AsyncClient,
    owner: Account,
    pocket: FakePocket,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_cloning, "CLONING_CHANGES_PER_MINUTE", 100)
    caplog.set_level(logging.DEBUG)
    pocket.cloning = False
    conn = await _connection(client, owner)
    quoted = {"state": "enabling", "message": f"Using {HF_TOKEN}.", "terms_url": TERMS}
    replies = [await _cloning(client, owner, conn, "PUT", json=ENABLE)]
    for reply in (
        httpx.Response(401, json={"detail": f"Bad token {HF_TOKEN}"}),
        httpx.Response(202, json=quoted),
    ):
        pocket.cloning_reply = reply
        replies.append(await _cloning(client, owner, conn, "PUT", json=ENABLE))
    pocket.cloning_reply = httpx.Response(200, json=quoted | {"state": "failed"})
    replies.append(await _cloning(client, owner, conn))
    spaced = {"hf_token": f"{HF_TOKEN[:12]} {HF_TOKEN[12:]}", "terms_accepted": True}
    replies.append(await _cloning(client, owner, conn, "PUT", json=spaced))
    replies.append(await _cloning(client, owner, conn, "PUT", json=ENABLE | {"terms_accepted": 0}))
    assert [r.status_code for r in replies] == [202, 422, 202, 200, 422, 422]
    assert pocket.tokens == [HF_TOKEN] * 3, "sent to the voice server, and only there"
    for r in replies:
        assert HF_TOKEN not in r.text and HF_TOKEN[12:] not in r.text, r.text

    assert caplog.records, "logs were captured"
    formatter = JsonFormatter("api")
    for record in caplog.records:
        assert HF_TOKEN not in formatter.format(record)
        assert HF_TOKEN not in repr(record.__dict__)
    assert len(await _audit("voice_cloning.enable_requested")) == 2
    async with get_sessionmaker()() as db:
        for table in Base.metadata.sorted_tables:
            rows = (await db.execute(select(table))).all()
            assert HF_TOKEN not in repr(rows), table.name


def test_expressive_and_pocket_connections_are_told_apart() -> None:
    from app.schemas.voice import ConnectionOut

    def conn(name: str, url: str, provider: str = "speaches") -> ConnectionOut:
        return ConnectionOut(
            id="c",
            name=name,
            provider=provider,
            provider_title="Self-hosted",
            connection_settings={"base_url": url},
            configured_credentials=[],
        )

    pocket = conn("Pocket TTS", "http://local-tts:8000/v1")
    gateway_pocket = conn("Voice", "https://ai.example.com/tts/v1")
    expressive = conn("Expressive voice", "http://10.0.0.5:8000/v1")
    chatterbox = conn("GPU voice", "http://chatterbox:8000/v1")
    gateway_expressive = conn("Remote", "https://ai.example.com/voice/v1")
    kokoro = conn("Local speech", "http://local-speech:8000/v1")
    cloud = conn("Expressive", "https://api.elevenlabs.io", provider="elevenlabs")
    for c in (pocket, gateway_pocket, expressive, chatterbox, gateway_expressive):
        assert voice_cloning.is_pocket_connection(c), c.name
    for c in (expressive, chatterbox, gateway_expressive):
        assert voice_cloning.is_expressive_connection(c), c.name
    for c in (pocket, gateway_pocket, kokoro, cloud):
        assert not voice_cloning.is_expressive_connection(c), c.name
    assert not voice_cloning.is_pocket_connection(kokoro)
    assert not voice_cloning.is_pocket_connection(cloud)


async def test_the_expressive_server_has_the_same_voices_and_cloning(
    client: httpx.AsyncClient,
    owner: Account,
    pocket: FakePocket,
    router: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same voice and cloning API; reached at its own operator address, never at Pocket TTS's.
    expressive = FakePocket()
    monkeypatch.setattr(get_settings(), "expressive_tts_url", "https://expressive.test/v1")
    router.route(host="expressive.test").mock(side_effect=expressive.handle)
    r = await client.post(
        "/api/v1/ai/connections",
        headers=owner.headers,
        json={
            "name": "Chatterbox",
            "provider": "speaches",
            "connection_settings": {"base_url": "http://expressive-tts:8000/v1"},
        },
    )
    assert r.status_code == 201, r.text
    conn = r.json()["id"]
    listed = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["cloning_state"] == "on"
    r = await _clone(client, owner, conn)
    assert r.status_code == 201, r.text
    assert len(expressive.posts) == 1 and pocket.posts == []
    voice = r.json()["id"]
    listed = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    assert voice in [v["id"] for v in listed.json()["voices"]]
    r = await client.delete(f"/api/v1/ai/connections/{conn}/voices/{voice}", headers=owner.headers)
    assert r.status_code == 204, r.text
    assert expressive.voices == {}

    # Without its own address it is not sent to the Pocket TTS server either.
    monkeypatch.setattr(get_settings(), "expressive_tts_url", None)
    r = await client.get(f"/api/v1/ai/connections/{conn}/voices", headers=owner.headers)
    assert r.status_code == 503, r.text
    assert "ATLAS_EXPRESSIVE_TTS_URL" in r.json()["error"]["message"]
