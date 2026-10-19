#!/usr/bin/env python3
"""End-to-end test call for ATLAS: no human, no microphone.

A scripted caller talks to a published agent through the same path as a browser test
call: ATLAS API (login, test session) -> voice service signalling (WebSocket) -> WebRTC
audio -> the voice pipeline with the organization's models (here: self-hosted Whisper,
qwen2.5 and Pocket TTS). The caller's lines are synthesised with the local Pocket TTS
server in a different voice from the agent's, or read from WAV files. Each line is sent
in real time as 20 ms frames once the agent has finished speaking; the agent's audio is
recorded and analysed with a simple energy detector. Afterwards the voice service's own
per-turn measurements are fetched and printed next to the client's. Full guide:
docs/testing-voice.md.

  make e2e-call ARGS="--runs 2"                    (Linux/macOS)
  .\\make.ps1 e2e-call --runs 2                     (Windows)
  python infra/scripts/e2e_test_call.py --runs 2 --json docs/perf/e2e-test-call.json

Where it runs (--runner): the WebRTC client needs `aiortc`. `auto` (default) uses it when
it can be imported here; otherwise the script copies itself into the voice-runtime
container (which ships aiortc), runs there - ATLAS at host.docker.internal, signalling at
localhost:8000, Pocket TTS at local-tts:8000 - and copies the results back. Running inside
the container also gives WebRTC a direct UDP path to the voice service, which a Windows or
macOS host does not have without a TURN relay.

Password: --password, else env ATLAS_DEMO_PASSWORD, else ATLAS_DEMO_PASSWORD in the
repository's .env. It is never printed and is handed to the container through the
environment, not the command line.

Exit code: 0 every run passed; 1 a run failed (no connection, no greeting, a reply that
never came or took longer than --max-reply-ms, or the voice service reported an error);
2 setup error (login, agent, voice for the caller, container).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import datetime as dt
import fractions
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

try:  # the launcher (host side) needs only the standard library
    import numpy as np
except ImportError:  # pragma: no cover - only the runner needs numpy
    np = None  # type: ignore[assignment]

try:
    import av
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.mediastreams import MediaStreamError

    HAVE_AIORTC = np is not None
except ImportError:  # pragma: no cover - the launcher hands over to the container
    HAVE_AIORTC = False
    MediaStreamTrack = object  # type: ignore[assignment,misc]

    class MediaStreamError(Exception):  # type: ignore[no-redef]
        pass


HERE = Path(__file__).resolve()
DEFAULT_AGENT = "Mortgage lead qualifier"
DEFAULT_EMAIL = "demo@example.com"
# The demo agent greets with "Is now a good time for two quick questions?" and then asks
# about goals, timeline, budget and who else decides.
DEFAULT_UTTERANCES = [
    "Yes, now is fine. I'm looking to buy my first home.",
    "Sometime in the next three months, ideally.",
    "Our budget is around four hundred thousand dollars.",
    "Just me and my wife. That's all for now, thank you.",
]
RATE = 48_000  # WebRTC Opus clock: frames are sent and analysed at this rate
FRAME = RATE * 20 // 1000  # 20 ms
SERVICE = "voice-runtime"
CONTAINER_PY = "/opt/venv/bin/python"
CONTAINER_SCRIPT = "/tmp/atlas_e2e_test_call.py"
CONTAINER_WORK = "/tmp/atlas-e2e"
PASSWORD_ENV = "ATLAS_E2E_PASSWORD"
HOST_TTS = "http://127.0.0.1:8002"
CONTAINER_TTS = "http://local-tts:8000"


class SetupError(Exception):
    """Something outside the call itself is missing (exit code 2)."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def repo_root() -> Path | None:
    for parent in HERE.parents:
        if (parent / "docker-compose.yml").exists() and (parent / "services").is_dir():
            return parent
    return None


def read_dotenv(path: Path | None) -> dict[str, str]:
    """KEY=VALUE pairs from a .env file (values are never printed)."""
    out: dict[str, str] = {}
    if path is None:
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def in_container() -> bool:
    return Path("/.dockerenv").exists()


# ----------------------------------------------------------------------------- ATLAS API


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class Atlas:
    """Minimal ATLAS client on urllib (no third-party dependency on either side)."""

    def __init__(self, base: str, secrets: list[str]) -> None:
        self.base = base.rstrip("/") + "/api/v1"
        self.headers: dict[str, str] = {"Accept": "application/json"}
        self.secrets = [s for s in secrets if s]

    def _redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def request(self, method: str, path: str, body: Any = None, timeout: float = 60.0) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ApiError(0, f"{self.base}{path} unreachable: {reason}") from None
        if status >= 300:
            text = raw.decode("utf-8", "replace")
            with contextlib.suppress(ValueError, AttributeError):
                err = json.loads(text)
                text = (err.get("error") or {}).get("message") or err.get("detail") or text
            raise ApiError(status, self._redact(str(text))[:300])
        return json.loads(raw) if raw else None

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        return await asyncio.to_thread(self.request, method, path, body)


def sign_in(api: Atlas, email: str, password: str, org_name: str | None, agent_name: str) -> tuple[dict, dict]:
    """Log in, pick the organization (by name, else the first) and find the agent."""
    try:
        data = api.request("POST", "/auth/login", {"email": email, "password": password})
    except ApiError as exc:
        if exc.status in (401, 403):
            raise SetupError(f"Sign-in as {email} failed ({exc.status}): check the password") from None
        raise SetupError(f"Sign-in failed: {exc}") from None
    api.headers["Authorization"] = f"Bearer {data['access_token']}"
    orgs = [m["organization"] for m in data.get("memberships") or []]
    if not orgs:
        raise SetupError(f"{email} belongs to no organization")
    if org_name:
        want = org_name.strip().lower()
        candidates = [o for o in orgs if want in (o["name"].lower(), o.get("slug", "").lower(), o["id"])]
        if not candidates:
            raise SetupError(f"{email} is not a member of an organization named {org_name!r}")
    else:
        candidates = orgs  # the first; the others only if the agent is not there
    for org in candidates:
        api.headers["X-Organization-Id"] = org["id"]
        agents = api.request("GET", "/agents")
        match = [a for a in agents if a["name"].strip().lower() == agent_name.strip().lower()]
        if match:
            agent = match[0]
            if agent.get("published_version") is None:
                raise SetupError(f"Agent {agent_name!r} is not published: test calls use the published version")
            return org, agent
    names = ", ".join(o["name"] for o in candidates)
    raise SetupError(f"No agent named {agent_name!r} in {names}")


# ----------------------------------------------------------------------------- audio helpers


def level_db(samples: Any) -> float:
    """RMS level in dBFS of int16 samples; -120 for digital silence."""
    if len(samples) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
    return 20.0 * math.log10(rms / 32768.0) if rms > 0 else -120.0


def parse_wav(raw: bytes) -> tuple[Any, int]:
    """Mono int16 samples and rate from WAV bytes; tolerates streamed headers (size 0/-1)."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos : pos + 4], int.from_bytes(raw[pos + 4 : pos + 8], "little")
        body = pos + 8
        if cid == b"fmt ":
            fmt = (
                int.from_bytes(raw[body : body + 2], "little"),
                int.from_bytes(raw[body + 2 : body + 4], "little"),
                int.from_bytes(raw[body + 4 : body + 8], "little"),
                int.from_bytes(raw[body + 14 : body + 16], "little"),
            )
        elif cid == b"data":
            end = len(raw) if size in (0, 0xFFFFFFFF) or body + size > len(raw) else body + size
            data = raw[body:end]
            break
        pos = body + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError("WAV without fmt/data chunk")
    audio_format, channels, rate, bits = fmt
    if audio_format not in (1, 0xFFFE) or bits != 16:
        raise ValueError("only 16-bit PCM WAV is supported")
    usable = len(data) // (2 * channels) * 2 * channels
    samples = np.frombuffer(data[:usable], dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels).astype(np.float32).mean(axis=1)
    return np.asarray(samples).round().astype(np.int16), rate


def resample(samples: Any, rate: int, target: int = RATE) -> Any:
    """Linear-interpolation resample (speech only, so no filter is needed for upsampling)."""
    if rate == target or len(samples) == 0:
        return samples.astype(np.int16)
    n = int(round(len(samples) * target / rate))
    x_old = np.arange(len(samples), dtype=np.float64) / rate
    x_new = np.arange(n, dtype=np.float64) / target
    out = np.interp(x_new, x_old, samples.astype(np.float32))
    return out.round().clip(-32768, 32767).astype(np.int16)


def trim(samples: Any, rate: int, threshold_db: float) -> Any:
    """Cut leading and trailing silence (10 ms windows below the threshold)."""
    win = max(1, rate // 100)
    n = len(samples) // win
    if n == 0:
        return samples
    frames = samples[: n * win].astype(np.float32).reshape(n, win)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    lvl = 20 * np.log10(np.maximum(rms, 1e-9) / 32768.0)
    voiced = np.nonzero(lvl >= threshold_db)[0]
    if len(voiced) == 0:
        return samples
    return samples[voiced[0] * win : (voiced[-1] + 1) * win]


def write_wav(path: Path, samples: Any, rate: int, channels: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def frame_to_mono(frame: Any) -> Any:
    """A decoded aiortc frame (usually s16 stereo 48 kHz) as mono int16 at RATE."""
    arr = frame.to_ndarray()
    channels = len(frame.layout.channels)
    if frame.format.is_planar:
        mono = arr.reshape(channels, -1).astype(np.float32).mean(axis=0)
    else:
        mono = arr.reshape(-1, channels).astype(np.float32).mean(axis=1)
    if frame.format.name in ("flt", "fltp", "dbl", "dblp"):
        mono = mono * 32767.0
    mono = mono.round().clip(-32768, 32767).astype(np.int16)
    return resample(mono, frame.sample_rate) if frame.sample_rate != RATE else mono


def synthesize(tts_url: str, model: str, voice: str, text: str) -> tuple[Any, int]:
    body = json.dumps({"model": model, "input": text, "voice": voice, "response_format": "wav"}).encode()
    req = urllib.request.Request(
        tts_url.rstrip("/") + "/v1/audio/speech",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise SetupError(f"Pocket TTS refused the caller's line ({exc.code}): {exc.read()[:200]!r}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise SetupError(
            f"Pocket TTS is not reachable at {tts_url} ({getattr(exc, 'reason', exc)}). "
            "Start it (make local-tts-up) or pass --utterance-wav files."
        ) from None
    return parse_wav(raw)


def prepare_utterances(args: argparse.Namespace, out_dir: Path) -> list[dict]:
    """Caller audio at 48 kHz: from --utterance-wav files, else synthesised (cached)."""
    items: list[dict] = []
    if args.utterance_wav:
        texts = args.utterance or []
        for i, path in enumerate(args.utterance_wav):
            samples, rate = parse_wav(Path(path).read_bytes())
            text = texts[i] if i < len(texts) else f"(audio file {Path(path).name})"
            items.append({"text": text, "source": f"file:{Path(path).name}", "samples": samples, "rate": rate})
    else:
        tts_dir = out_dir / "caller-audio"
        for i, text in enumerate(args.utterance or DEFAULT_UTTERANCES):
            key = hashlib.sha1(f"{args.tts_model}|{args.tts_voice}|{text}".encode()).hexdigest()[:12]
            cached = tts_dir / f"{i + 1:02d}-{key}.wav"
            if cached.exists():
                samples, rate = parse_wav(cached.read_bytes())
            else:
                started = time.monotonic()
                samples, rate = synthesize(args.tts_url, args.tts_model, args.tts_voice, text)
                write_wav(cached, samples, rate)
                log(f"  caller line {i + 1} synthesised in {time.monotonic() - started:.1f} s ({args.tts_voice})")
            items.append({"text": text, "source": f"tts:{args.tts_model}/{args.tts_voice}", "samples": samples, "rate": rate})
    for item in items:
        audio = trim(resample(item.pop("samples"), item.pop("rate")), RATE, args.vad_db)
        item["audio"] = audio
        item["speech_ms"] = round(len(audio) * 1000 / RATE)
    return items


# ----------------------------------------------------------------------------- WebRTC media


class CallerTrack(MediaStreamTrack):  # type: ignore[misc,valid-type]
    """Outgoing microphone stand-in: silence, or queued speech, paced in real time."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: collections.deque = collections.deque()
        self._silence = np.zeros(FRAME, dtype=np.int16)
        self._pts = 0
        self._start: float | None = None
        self._utt_start = 0.0
        self.first_send: float | None = None
        self.sent: list[tuple[int, Any]] = []  # (pts, samples) of speech, for the recording

    def play(self, samples: Any) -> asyncio.Future:
        """Queue speech; resolves to (speech start, speech end), monotonic seconds."""
        done = asyncio.get_running_loop().create_future()
        count = max(1, math.ceil(len(samples) / FRAME))
        padded = np.zeros(count * FRAME, dtype=np.int16)
        padded[: len(samples)] = samples
        for i in range(count):
            last = i == count - 1
            valid = len(samples) - i * FRAME if last else FRAME
            self._queue.append((padded[i * FRAME : (i + 1) * FRAME], i == 0, last, valid, done))
        return done

    async def recv(self) -> Any:
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = self.first_send = time.monotonic()
        else:
            self._pts += FRAME
            wait = self._start + self._pts / RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        now = time.monotonic()
        data = self._silence
        if self._queue:
            data, first, last, valid, done = self._queue.popleft()
            if first:
                self._utt_start = now
            if last and not done.done():
                done.set_result((self._utt_start, now + valid / RATE))
            self.sent.append((self._pts, data))
        frame = av.AudioFrame.from_ndarray(data.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, RATE)
        return frame


class AgentListener:
    """Records the agent's audio and finds its speech with an energy detector.

    Per received frame (10-20 ms): voiced when the RMS level >= --vad-db. Speech starts
    at the first voiced frame once --min-speech-ms of voiced audio falls within 300 ms
    (a lone click is ignored). Speech ends at the last voiced frame before
    --end-silence-ms of unvoiced audio. Inside speech, an unvoiced run >= --gap-ms is a
    gap; it is "silent" when it holds >= --gap-ms of filler (below --silence-db): the
    voice service fills with exact zeros when it has no audio queued (a starved TTS),
    whereas a natural pause carries low-level sound. A pause in packet arrival >= --gap-ms
    during speech is a stall (network or an overloaded server).
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.vad_db, self.silence_db = args.vad_db, args.silence_db
        self.gap_ms, self.end_ms, self.min_ms = args.gap_ms, args.end_silence_ms, args.min_speech_ms
        self.chunks: list[Any] = []
        self.first_arrival: float | None = None
        self.last_arrival: float | None = None
        self.frames = 0
        self.max_interval_ms = 0.0
        self.lost_ms = 0.0
        self._next_pts: float | None = None
        self.segments: list[dict] = []
        self.cur: dict | None = None
        self._cand: list[float] | None = None  # [start, voiced ms, span ms]
        self._run_start = 0.0
        self._reset_run()
        self.done = asyncio.Event()

    async def consume(self, track: Any) -> None:
        try:
            while True:
                frame = await track.recv()
                self.feed(frame, time.monotonic())
        except MediaStreamError:
            pass
        finally:
            self.done.set()

    def feed(self, frame: Any, now: float) -> None:
        pcm = frame_to_mono(frame)
        if frame.pts is not None and frame.time_base is not None and frame.sample_rate:
            pts_s = float(frame.pts * frame.time_base)
            if self._next_pts is not None:
                jump = pts_s - self._next_pts
                if 0.005 < jump < 30:  # missing packets (a negative/huge jump is a wrap)
                    self.lost_ms += jump * 1000
            self._next_pts = pts_s + frame.samples / frame.sample_rate
        interval_ms = 0.0
        if self.last_arrival is None:
            self.first_arrival = now
        else:
            interval_ms = (now - self.last_arrival) * 1000
            self.max_interval_ms = max(self.max_interval_ms, interval_ms)
        self.last_arrival = now
        self.frames += 1
        self.chunks.append(pcm)
        self._detect(now, pcm, interval_ms)

    def _reset_run(self) -> None:
        self._run_ms, self._run_max_db = 0.0, -120.0
        self._filler_cur = self._filler_max = 0.0

    def _detect(self, now: float, pcm: Any, interval_ms: float) -> None:
        dur = len(pcm) * 1000.0 / RATE
        db = level_db(pcm)
        voiced = db >= self.vad_db
        seg = self.cur
        if seg is None:
            if voiced:
                if self._cand is None:
                    self._cand = [now, 0.0, 0.0]
                self._cand[1] += dur
            if self._cand is not None:
                self._cand[2] += dur
                if self._cand[1] >= self.min_ms:
                    self.cur = {
                        "start": self._cand[0],
                        "last_voiced": now + dur / 1000,
                        "voiced_ms": self._cand[1],
                        "peak_db": db,
                        "gaps": [],
                        "stalls": [],
                    }
                    self._cand = None
                    self._reset_run()
                elif self._cand[2] >= 300:
                    self._cand = None
            return
        if interval_ms >= self.gap_ms:
            seg["stalls"].append({"at": now - interval_ms / 1000, "ms": round(interval_ms)})
        if voiced:
            if self._run_ms >= self.gap_ms:
                seg["gaps"].append({
                    "at": self._run_start,
                    "ms": round(self._run_ms),
                    "filler_ms": round(self._filler_max),
                    "max_level_db": round(self._run_max_db, 1),
                    "silent": self._filler_max >= self.gap_ms,
                })
            self._reset_run()
            seg["voiced_ms"] += dur
            seg["last_voiced"] = now + dur / 1000
            seg["peak_db"] = max(seg["peak_db"], db)
        else:
            if self._run_ms == 0:
                self._run_start = now
            self._run_ms += dur
            self._run_max_db = max(self._run_max_db, db)
            # Longest stretch of filler (below --silence-db) inside this quiet run.
            self._filler_cur = self._filler_cur + dur if db < self.silence_db else 0.0
            self._filler_max = max(self._filler_max, self._filler_cur)
            if self._run_ms >= self.end_ms:
                self.segments.append(seg)
                self.cur = None
                self._reset_run()

    def since(self, t: float) -> list[dict]:
        segs = [s for s in self.segments if s["start"] >= t]
        if self.cur is not None and self.cur["start"] >= t:
            segs.append(self.cur)
        return segs

    def overlapping(self, t0: float, t1: float) -> bool:
        segs = self.segments + ([self.cur] if self.cur else [])
        return any(s["start"] < t1 and s["last_voiced"] > t0 for s in segs)


class ServerFeed:
    """The voice service's live per-turn events, pushed over the signalling socket."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.bot_speaking = False
        self.muted = False

    def on_event(self, msg: dict, now: float) -> None:
        kind = msg.get("type")
        if kind == "rtf-bot-started-speaking":
            self.bot_speaking = True
        elif kind == "rtf-bot-stopped-speaking":
            self.bot_speaking = False
        elif kind == "rtf-user-mute-started":
            self.muted = True
        elif kind == "rtf-user-mute-stopped":
            self.muted = False
        self.events.append({"t": now, "type": kind, "node_name": msg.get("node_name"), "payload": msg.get("payload")})

    def window(self, t0: float, t1: float) -> dict:
        """What the voice service said live about [t0, t1): heard text, response, first bytes."""
        heard, said, response, ttfb = [], [], None, {}
        for e in self.events:
            if not (t0 <= e["t"] < t1):
                continue
            p = e["payload"] if isinstance(e["payload"], dict) else {}
            if e["type"] == "rtf-user-transcription" and p.get("final") is True and str(p.get("text") or "").strip():
                heard.append(str(p["text"]).strip())
            elif e["type"] == "rtf-bot-text" and str(p.get("text") or "").strip():
                said.append(str(p["text"]).strip())
            elif e["type"] == "rtf-latency-measured" and response is None:
                response = seconds_to_ms(p.get("latency_seconds"))
            elif e["type"] == "rtf-ttfb-metric":
                value = seconds_to_ms(p.get("ttfb_seconds"))
                if value is not None:
                    ttfb.setdefault(ttfb_kind(p), value)
        return {
            "heard": " ".join(heard) or None,
            "agent_text": re.sub(r"\s+", " ", " ".join(said)).strip() or None,
            "response_ms": response,
            "stt_ttfb_ms": ttfb.get("stt"),
            "llm_ttfb_ms": ttfb.get("llm"),
            "tts_ttfb_ms": ttfb.get("tts"),
        }


# ----------------------------------------------------------------------------- runtime events


def seconds_to_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return round(value * 1000)


def ttfb_kind(payload: dict) -> str:
    """Same rule as app/services/latency.py: `kind`, else the processor name's suffix."""
    kind = payload.get("kind")
    if isinstance(kind, str) and kind.lower() in ("stt", "llm", "tts"):
        return kind.lower()
    name = re.sub(r"#\d+$", "", str(payload.get("processor") or "").strip()).upper()
    if name.endswith("STTSERVICE"):
        return "stt"
    if name.endswith("TTSSERVICE"):
        return "tts"
    return "llm"


def _ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    with contextlib.suppress(ValueError):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def percentile(values: list[int], q: float) -> int | None:
    ordered = sorted(values)
    if not ordered:
        return None
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo))


def parse_runtime_events(events: list) -> dict | None:
    """Per-turn view from the run's `logs.realtime_feedback_events` (ADR 0006).

    Mirrors app/services/latency.py so both sources have one shape: grouped by `turn`;
    final user transcriptions give what was heard (start/end), bot text what was said;
    response = `rtf-latency-measured` of the turn, else agent start - user end; the
    first time-to-first-byte per stage.
    """
    turns: dict[int, dict] = {}
    last = 0

    def turn_for(raw: Any) -> dict:
        nonlocal last
        with contextlib.suppress(TypeError, ValueError):
            if raw is not None and not isinstance(raw, bool):
                last = int(raw)
        return turns.setdefault(last, {
            "turn": last, "user": [], "agent": [], "user_start": None, "user_end": None,
            "agent_start": None, "agent_end": None, "measured": None, "ttfb": {}, "node_name": None,
        })

    def lo(a: Any, b: Any) -> Any:
        return b if a is None else a if b is None else min(a, b)

    def hi(a: Any, b: Any) -> Any:
        return b if a is None else a if b is None else max(a, b)

    for e in events:
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if kind == "rtf-user-transcription":
            text = str(p.get("text") or "").strip()
            if p.get("final") is not True or not text:
                continue
            t = turn_for(e.get("turn"))
            t["user"].append(text)
            t["user_start"] = lo(t["user_start"], _ts(p.get("timestamp")))
            t["user_end"] = hi(t["user_end"], _ts(p.get("end_timestamp")))
            t["node_name"] = t["node_name"] or e.get("node_name")
        elif kind == "rtf-bot-text":
            t = turn_for(e.get("turn"))
            text = str(p.get("text") or "").strip()
            if text:
                t["agent"].append(text)
            t["agent_start"] = lo(t["agent_start"], _ts(p.get("timestamp")))
            t["agent_end"] = hi(t["agent_end"], _ts(p.get("end_timestamp")))
            t["node_name"] = e.get("node_name") or t["node_name"]
        elif kind == "rtf-latency-measured":
            t = turn_for(e.get("turn"))
            value = seconds_to_ms(p.get("latency_seconds"))
            if value is not None and t["measured"] is None:
                t["measured"] = value
        elif kind == "rtf-ttfb-metric":
            value = seconds_to_ms(p.get("ttfb_seconds"))
            if value is not None:
                turn_for(e.get("turn"))["ttfb"].setdefault(ttfb_kind(p), value)

    out = []
    for n in sorted(turns):
        t = turns[n]
        if not t["user"] and not t["agent"]:
            continue
        response = t["measured"]
        if response is None and t["user"] and t["user_end"] and t["agent_start"]:
            response = round((t["agent_start"] - t["user_end"]).total_seconds() * 1000)
        out.append({
            "turn": n,
            "node_name": t["node_name"],
            "user_text": " ".join(t["user"]) or None,
            "agent_text": re.sub(r"\s+", " ", " ".join(t["agent"])).strip() or None,
            "user_ended_at": t["user_end"].isoformat(timespec="milliseconds") if t["user_end"] else None,
            "agent_started_at": t["agent_start"].isoformat(timespec="milliseconds") if t["agent_start"] else None,
            "response_ms": response,
            "stt_ttfb_ms": t["ttfb"].get("stt"),
            "llm_ttfb_ms": t["ttfb"].get("llm"),
            "tts_ttfb_ms": t["ttfb"].get("tts"),
        })
    if not out:
        return None
    responses = [t["response_ms"] for t in out if t["response_ms"] is not None]
    return {
        "summary": {
            "source": "runtime",
            "turns": len(out),
            "measured_turns": len(responses),
            "response_p50_ms": percentile(responses, 0.5),
            "response_p95_ms": percentile(responses, 0.95),
            "response_max_ms": max(responses) if responses else None,
        },
        "turns": out,
    }


async def voice_db_row(args: argparse.Namespace, sql_asyncpg: str, sql_psql: str, param: Any) -> list[str] | None:
    """One row from the voice service's database, read-only.

    Inside the voice-runtime container: asyncpg with its DATABASE_URL. On a host: psql in
    the voice-db container (its port is not published).
    """
    url = args.voice_db_url or (os.environ.get("DATABASE_URL") if in_container() else None)
    if url:
        try:
            import asyncpg  # type: ignore[import-not-found]
        except ImportError:
            asyncpg = None
        if asyncpg is not None:
            try:
                conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"), timeout=10)
                try:
                    row = await conn.fetchrow(sql_asyncpg, param)
                finally:
                    await conn.close()
            except Exception as exc:  # unreachable database: the stored view is optional
                log(f"  voice service database: {type(exc).__name__}: {exc}")
                return None
            return None if row is None else [None if v is None else str(v) for v in row]
    root = repo_root()
    if root is None or shutil.which("docker") is None:
        return None
    cmd = ["docker", "compose", "--profile", "voice", "exec", "-T", "voice-db", "psql", "-U", "voice", "-d", "voice",
           "-At", "-F", "\x1f", "-c", sql_psql.replace("{}", str(param))]
    proc = await asyncio.to_thread(
        subprocess.run, cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    line = proc.stdout.strip()
    return line.split("\x1f") if proc.returncode == 0 and line else None


async def run_id_for_token(args: argparse.Namespace, token: str) -> int | None:
    if not re.fullmatch(r"[A-Za-z0-9_\-.~]+", token):
        return None
    row = await voice_db_row(
        args,
        "SELECT workflow_run_id FROM embed_sessions WHERE session_token = $1",
        "SELECT workflow_run_id FROM embed_sessions WHERE session_token = '{}'",
        token,
    )
    return int(row[0]) if row and row[0] and row[0].isdigit() else None


async def run_events_from_db(args: argparse.Namespace, run_id: int, deadline: float) -> tuple[list | None, bool]:
    """The saved event stream of a run (written when the call ends); polls until then."""
    completed = False
    while True:
        row = await voice_db_row(
            args,
            "SELECT is_completed::text, logs::text FROM workflow_runs WHERE id = $1",
            "SELECT is_completed::text, logs::text FROM workflow_runs WHERE id = {}",
            int(run_id),
        )
        if row:
            completed = row[0] in ("true", "t")
            with contextlib.suppress(ValueError, TypeError):
                logs = json.loads(row[1] or "{}")
                events = logs.get("realtime_feedback_events") if isinstance(logs, dict) else None
                if isinstance(events, list) and events:
                    return events, completed
        if time.monotonic() >= deadline:
            return None, completed
        await asyncio.sleep(3)


async def fetch_runtime_view(args: argparse.Namespace, api: Atlas, run: dict, token: str) -> dict:
    """The voice service's own per-turn record of this call: ATLAS first, then its DB."""
    deadline = time.monotonic() + args.events_timeout_s
    notes = []
    call_id = run.get("call_id")
    if call_id:
        while True:
            try:
                latency = await api.call("GET", f"/calls/{call_id}/latency")
                with contextlib.suppress(ApiError):
                    run["call_state"] = (await api.call("GET", f"/calls/{call_id}")).get("state")
                if not run.get("workflow_run_id"):  # not part of ATLAS's call detail
                    run["workflow_run_id"] = await run_id_for_token(args, token)
                return {"source": "atlas", "summary": latency.get("summary"), "turns": latency.get("turns") or []}
            except ApiError as exc:
                if exc.status != 404:
                    notes.append(f"ATLAS latency: {exc}")
                    break
            if time.monotonic() >= deadline:
                notes.append(f"ATLAS had no response times for call {call_id} after {args.events_timeout_s} s")
                break
            await asyncio.sleep(3)
        deadline = max(deadline, time.monotonic() + 20)
    else:
        notes.append("ATLAS returned no call id for the test session (test calls not recorded by this API build)")
    run_id = run.get("workflow_run_id") or await run_id_for_token(args, token)
    if run_id is None:
        notes.append("voice service database not reachable from here")
        return {"source": None, "summary": None, "turns": [], "notes": notes}
    run["workflow_run_id"] = run_id
    events, completed = await run_events_from_db(args, run_id, deadline)
    if not events:
        notes.append(f"run {run_id} has no saved events yet (completed={completed})")
        return {"source": None, "summary": None, "turns": [], "notes": notes}
    parsed = parse_runtime_events(events) or {"summary": None, "turns": []}
    return {"source": "voice-db", **parsed, "events": len(events), "run_completed": completed, "notes": notes}


def _int(value: Any) -> int | None:
    with contextlib.suppress(TypeError, ValueError):
        return int(value)
    return None


# ----------------------------------------------------------------------------- the call


class Clock:
    def __init__(self) -> None:
        self.mono = time.monotonic()
        self.wall = time.time()

    def iso(self, t: float | None) -> str | None:
        if t is None:
            return None
        stamp = dt.datetime.fromtimestamp(self.wall + (t - self.mono), dt.timezone.utc)
        return stamp.isoformat(timespec="milliseconds")

    def rel(self, t: float | None) -> float | None:
        return None if t is None else round(t - self.mono, 3)


def ice_pair(pc: Any) -> str | None:
    """The candidate pair carrying the audio, e.g. "relay 1.2.3.4:50000 -> host 172.30.0.10:41000".

    "relay" on the left means this caller's audio goes through the TURN server, as for a
    browser on a network without a direct path. Reads aiortc internals: best effort.
    """
    with contextlib.suppress(Exception):
        for transceiver in pc.getTransceivers():
            connection = transceiver.sender.transport.transport._connection
            for pair in connection._nominated.values():
                local, remote = pair.local_candidate, pair.remote_candidate
                return f"{local.type} {local.host}:{local.port} -> {remote.type} {remote.host}:{remote.port}"
    return None


def override_signalling(url: str, base: str) -> str:
    """The session's signalling URL on another base (scheme and host[:port]), path kept.

    For a caller next to the voice service (e.g. inside its container on a server) while the
    session advertises the public address: ws://localhost:8000 reaches it directly.
    """
    parts, new = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
    scheme = {"http": "ws", "https": "wss"}.get(new.scheme, new.scheme)
    return urllib.parse.urlunsplit((scheme, new.netloc, parts.path, parts.query, ""))


async def ws_connect(url: str, origin: str) -> Any:
    try:
        from websockets.asyncio.client import connect
    except ImportError:  # websockets < 13
        from websockets import connect  # type: ignore[no-redef]
    return await connect(url, origin=origin, open_timeout=15, max_size=2**23)


def describe_reply(clock: Clock, segs: list[dict], after: float, gap_ms: int) -> dict:
    """Timing and continuity of the agent speech that followed `after`."""
    if not segs:
        return {"first_audio_ms": None, "speaking_ms": None, "voiced_ms": None, "gaps": [], "stalls": [],
                "max_gap_ms": None, "choppy": False, "segments": 0}
    gaps, stalls = [], []
    for i, s in enumerate(segs):
        for g in s["gaps"]:
            gaps.append({"at_s": clock.rel(g["at"]), "ms": g["ms"], "filler_ms": g["filler_ms"],
                         "max_level_db": g["max_level_db"], "kind": "silent" if g["silent"] else "pause"})
        for st in s["stalls"]:
            stalls.append({"at_s": clock.rel(st["at"]), "ms": st["ms"]})
        if i:
            prev = segs[i - 1]
            gaps.append({"at_s": clock.rel(prev["last_voiced"]), "ms": round((s["start"] - prev["last_voiced"]) * 1000),
                         "max_level_db": None, "kind": "break"})
    choppy = any(g["kind"] in ("silent", "break") and g["ms"] > gap_ms for g in gaps) or any(st["ms"] > gap_ms for st in stalls)
    return {
        "first_audio_ms": round((segs[0]["start"] - after) * 1000),
        "agent_started_at": clock.iso(segs[0]["start"]),
        "agent_ended_at": clock.iso(segs[-1]["last_voiced"]),
        "speaking_ms": round((segs[-1]["last_voiced"] - segs[0]["start"]) * 1000),
        "voiced_ms": round(sum(s["voiced_ms"] for s in segs)),
        "peak_level_db": round(max(s["peak_db"] for s in segs), 1),
        "segments": len(segs),
        "gaps": gaps,
        "stalls": stalls,
        "max_gap_ms": max((g["ms"] for g in gaps), default=0),
        "choppy": choppy,
    }


class Call:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.clock = Clock()
        self.agent = AgentListener(args)
        self.server = ServerFeed()
        self.caller: CallerTrack | None = None
        self.stop_reason: str | None = None
        self.error: str | None = None
        self.connected_at: float | None = None
        self.answer_at: float | None = None
        self.consumer: asyncio.Task | None = None

    def stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason

    async def wait_agent(self, after: float, first_timeout_s: float, total_timeout_s: float) -> tuple[str, list[dict]]:
        """Wait for the agent to start (or time out) and then to finish speaking.

        Finished = the energy detector closed its speech (--end-silence-ms of silence) and
        the voice service does not report the bot as still speaking (ignored once silence
        has lasted 3 s longer than that, in case the event never comes).
        """
        start_deadline, end_deadline = after + first_timeout_s, after + total_timeout_s
        while True:
            now = time.monotonic()
            segs = self.agent.since(after)
            if self.stop_reason:
                return self.stop_reason, segs
            if not segs:
                if now >= start_deadline:
                    return "no_reply", segs
            elif self.agent.cur is None:
                quiet_ms = (now - segs[-1]["last_voiced"]) * 1000
                if not self.server.bot_speaking or quiet_ms >= self.args.end_silence_ms + 3000:
                    return "ok", segs
            if now >= end_deadline:
                return "turn_timeout", segs
            await asyncio.sleep(0.02)

    async def wait_unmuted(self, limit_s: float = 10.0) -> None:
        deadline = time.monotonic() + limit_s
        while self.server.muted and not self.stop_reason and time.monotonic() < deadline:
            await asyncio.sleep(0.05)


async def run_call(args: argparse.Namespace, api: Atlas, agent: dict, utterances: list[dict], run_no: int, run_dir: Path) -> dict:
    call = Call(args)
    clock = call.clock
    run: dict[str, Any] = {"run": run_no, "ok": False, "failures": [], "started_at": clock.iso(clock.mono)}
    log(f"\n== Run {run_no}: test call to {agent['name']!r} (published v{agent.get('published_version')})")

    session = await api.call("POST", f"/agents/{agent['id']}/test-session", {"lead_id": args.lead_id})
    token, sig = session["session_token"], session["signaling_url"]
    if args.signalling:
        sig = override_signalling(sig, args.signalling)
    run["_token"] = token  # for the voice-db lookup only; removed before anything is written
    run["call_id"] = session.get("call_id")
    run["workflow_run_id"] = _int(session.get("workflow_run_id"))
    sig_host = urllib.parse.urlsplit(sig)
    local_sig = sig_host.hostname in ("localhost", "127.0.0.1", "::1")
    servers = []
    if args.use_ice_servers or not local_sig:
        for s in session.get("ice_servers") or []:
            servers.append(RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential")))
    run["connection"] = {"signaling": f"{sig_host.scheme}://{sig_host.netloc}", "ice_servers": len(servers)}
    log(f"  session created: call {run['call_id'] or '(not recorded by this API build)'}; signalling {sig_host.netloc}")

    pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
    caller = call.caller = CallerTrack()
    pc.addTrack(caller)
    pc_id = f"pc-{uuid.uuid4()}"

    @pc.on("track")
    def _on_track(track: Any) -> None:
        if track.kind == "audio" and call.consumer is None:
            call.consumer = asyncio.ensure_future(call.agent.consume(track))

    @pc.on("connectionstatechange")
    async def _on_state() -> None:
        if pc.connectionState == "connected" and call.connected_at is None:
            call.connected_at = time.monotonic()
        elif pc.connectionState == "failed":
            call.stop("connection_failed")

    ws = None
    reader: asyncio.Task | None = None
    t_offer = time.monotonic()
    hangup: dict[str, Any] = {"by": None, "server_confirmed": False}
    try:
        try:
            ws = await ws_connect(sig, args.origin)
        except Exception as exc:  # handshake refused (bad token / origin) or unreachable
            run["failures"].append(f"signalling connection failed: {exc}")
            return run

        async def read() -> None:
            try:
                async for raw in ws:
                    now = time.monotonic()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    kind, payload = msg.get("type"), msg.get("payload") or {}
                    if kind == "answer" and payload.get("sdp") and call.answer_at is None:
                        call.answer_at = now
                        try:
                            await pc.setRemoteDescription(RTCSessionDescription(sdp=payload["sdp"], type="answer"))
                        except Exception as exc:
                            call.error = f"answer rejected by the client: {exc}"
                            call.stop("error")
                    elif kind == "error":
                        call.error = str(payload.get("message") or payload)
                        call.stop("error")
                    elif kind == "call-ended":
                        hangup["server_confirmed"] = True
                        hangup["reason"] = payload.get("reason")
                        call.stop("agent_ended")
                    elif isinstance(kind, str) and kind.startswith("rtf-"):
                        call.server.on_event(msg, now)
            except Exception:
                pass
            finally:
                call.stop("signalling_closed")

        reader = asyncio.ensure_future(read())
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)  # aiortc gathers all candidates here
        t_offer = time.monotonic()
        await ws.send(json.dumps({"type": "offer", "payload": {"sdp": pc.localDescription.sdp, "type": "offer", "pc_id": pc_id}}))
        await ws.send(json.dumps({"type": "ice-candidate", "payload": {"pc_id": pc_id, "candidate": None}}))

        deadline = t_offer + args.connect_timeout_s
        while call.connected_at is None and not call.stop_reason and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        run["connection"].update({
            "offer_to_answer_ms": round((call.answer_at - t_offer) * 1000) if call.answer_at else None,
            "offer_to_connected_ms": round((call.connected_at - t_offer) * 1000) if call.connected_at else None,
            "state": pc.connectionState,
        })
        if call.connected_at is None:
            run["failures"].append(f"WebRTC did not connect ({call.error or call.stop_reason or pc.connectionState})")
            return run
        run["connection"]["ice_pair"] = ice_pair(pc)
        log(f"  connected in {run['connection']['offer_to_connected_ms']} ms (ICE pair: "
            f"{run['connection']['ice_pair'] or 'unknown'}); waiting for the greeting")

        status, segs = await call.wait_agent(t_offer, args.greeting_timeout_s, args.turn_timeout_s)
        greeting = {"status": status, **describe_reply(clock, segs, call.connected_at, args.gap_ms)}
        run["greeting"] = greeting
        if not segs:
            run["failures"].append(f"the greeting never arrived ({status}{': ' + call.error if call.error else ''})")
            return run
        log(f"  greeting: first audio {greeting['first_audio_ms']} ms after connect, {greeting['speaking_ms'] / 1000:.1f} s long")

        turns: list[dict] = []
        run["turns"] = turns
        for i, utt in enumerate(utterances):
            if call.stop_reason:
                turns.append({"index": i + 1, "text": utt["text"], "status": f"not_sent ({call.stop_reason})"})
                continue
            await call.wait_unmuted()
            played = caller.play(utt["audio"])
            limit = time.monotonic() + utt["speech_ms"] / 1000 + 10
            while not played.done() and not call.stop_reason and time.monotonic() < limit:
                await asyncio.sleep(0.02)
            if not played.done():
                turns.append({"index": i + 1, "text": utt["text"], "status": f"not_sent ({call.stop_reason or 'send stalled'})"})
                continue
            utt_start, speech_end = played.result()
            status, segs = await call.wait_agent(speech_end, args.reply_timeout_s, args.turn_timeout_s)
            reply = describe_reply(clock, segs, speech_end, args.gap_ms)
            turn = {
                "index": i + 1,
                "text": utt["text"],
                "audio_source": utt["source"],
                "caller_speech_ms": utt["speech_ms"],
                "caller_started_at": clock.iso(utt_start),
                "caller_ended_at": clock.iso(speech_end),
                "status": status,
                "reply_ms": reply.pop("first_audio_ms"),
                **reply,
                "overlap": call.agent.overlapping(utt_start, speech_end),
                "_window": (utt_start, speech_end),
            }
            turns.append(turn)
            log(f"  turn {i + 1}: reply after {turn['reply_ms']} ms, spoke {(turn['speaking_ms'] or 0) / 1000:.1f} s ({status})")
        await asyncio.sleep(0.5)
    finally:
        # Hang up like the browser does: close the peer, then the signalling socket (the
        # voice service disconnects the call's peer when its socket closes).
        reason = call.stop_reason
        hangup["by"] = "caller" if not reason else "agent" if reason == "agent_ended" else f"voice service ({reason})"
        caller.stop()
        with contextlib.suppress(Exception):
            await pc.close()
        t_close = time.monotonic()
        while ws is not None and not hangup["server_confirmed"] and time.monotonic() - t_close < 1.5:
            await asyncio.sleep(0.05)
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader, timeout=3)
        if call.consumer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(call.agent.done.wait(), timeout=3)
        run["hangup"] = hangup
        run["ended_at"] = clock.iso(time.monotonic())
        if call.error:
            run["voice_service_error"] = call.error
        finish_run(run, call, run_dir, t_offer)
        if run.get("call_id"):  # what the browser reports when a test call ends (newer API builds)
            try:
                await api.call("POST", f"/agents/{agent['id']}/test-calls/{run['call_id']}/end",
                               {"connected": call.connected_at is not None})
                run["atlas_end_reported"] = True
            except ApiError as exc:
                run["atlas_end_reported"] = f"not available ({exc.status})"
    return run


def finish_run(run: dict, call: Call, run_dir: Path, t_offer: float) -> None:
    """Attach the live server view per turn, write the recordings and the event log."""
    clock, agent, caller = call.clock, call.agent, call.caller
    sent = [t for t in run.get("turns") or [] if "_window" in t]
    bounds = [t.pop("_window")[0] for t in sent] + [time.monotonic()]
    if "greeting" in run:
        run["greeting"]["server_live"] = call.server.window(t_offer, bounds[0])
    for i, t in enumerate(sent):  # each line's window runs until the next line starts
        t["server_live"] = call.server.window(bounds[i], bounds[i + 1])
    run["transport"] = {
        "frames_received": agent.frames,
        "max_frame_interval_ms": round(agent.max_interval_ms),
        "lost_ms": round(agent.lost_ms),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, str] = {}
    if agent.chunks:
        audio = np.concatenate(agent.chunks)
        write_wav(run_dir / "agent.wav", audio, RATE)
        rec["agent"] = f"{run_dir.name}/agent.wav"
        # Both sides on one timeline (left = caller, right = agent), 16 kHz for listening.
        a_off = int(round((agent.first_arrival - clock.mono) * RATE))
        c_off = int(round(((caller.first_send if caller and caller.first_send else clock.mono) - clock.mono) * RATE))
        last_sent = (caller.sent[-1][0] + FRAME) if caller and caller.sent else 0
        total = max(a_off + len(audio), c_off + last_sent) + 3
        total -= total % 3
        left = np.zeros(total, dtype=np.int16)
        right = np.zeros(total, dtype=np.int16)
        right[a_off : a_off + len(audio)] = audio[: max(0, total - a_off)]
        for pts, data in caller.sent if caller else []:
            pos = c_off + pts
            left[pos : pos + len(data)] = data[: max(0, total - pos)]
        stereo = np.stack([
            left.astype(np.float32).reshape(-1, 3).mean(axis=1),
            right.astype(np.float32).reshape(-1, 3).mean(axis=1),
        ], axis=1).round().astype(np.int16)
        write_wav(run_dir / "conversation.wav", stereo.reshape(-1), RATE // 3, channels=2)
        rec["conversation"] = f"{run_dir.name}/conversation.wav"
    with (run_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in call.server.events:
            fh.write(json.dumps({"at_s": clock.rel(e["t"]), "utc": clock.iso(e["t"]), "type": e["type"],
                                 "node_name": e["node_name"], "payload": e["payload"]}) + "\n")
    rec["events"] = f"{run_dir.name}/events.jsonl"
    run["recordings"] = rec


def evaluate(run: dict, args: argparse.Namespace) -> None:
    """Pass/fail: connected, greeted, every line answered within --max-reply-ms."""
    failures = run["failures"]
    if run.get("voice_service_error") and not any("error" in f for f in failures):
        failures.append(f"voice service error: {run['voice_service_error']}")
    for t in run.get("turns") or []:
        if t.get("status", "").startswith("not_sent"):
            if not (run.get("hangup") or {}).get("by") == "agent":
                failures.append(f"turn {t['index']}: not sent ({t['status']})")
            continue
        if t.get("reply_ms") is None:
            failures.append(f"turn {t['index']}: no reply ({t.get('status')})")
        elif t["reply_ms"] > args.max_reply_ms:
            failures.append(f"turn {t['index']}: reply took {t['reply_ms']} ms (limit {args.max_reply_ms})")
    view = run.get("runtime_view") or {}
    with_user = [t for t in view.get("turns") or [] if t.get("user_text")]
    for i, t in enumerate(run.get("turns") or []):
        t["server"] = with_user[i] if i < len(with_user) else None
    run["ok"] = not failures


# ----------------------------------------------------------------------------- report


def ms(v: Any) -> str:
    return "-" if v is None else f"{v} ms"


def short(text: Any, n: int = 48) -> str:
    if not text:
        return "-"
    text = str(text).replace("|", "/").strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def report_run(run: dict) -> list[str]:
    out = []
    verdict = "PASS" if run["ok"] else "FAIL: " + "; ".join(run["failures"])
    out.append(f"### Run {run['run']} - {verdict}")
    conn = run.get("connection") or {}
    g = run.get("greeting") or {}
    view = run.get("runtime_view") or {}
    out.append(
        f"call {run.get('call_id') or '-'} | voice run {run.get('workflow_run_id') or '-'} | "
        f"connected in {ms(conn.get('offer_to_connected_ms'))} | greeting {ms(g.get('first_audio_ms'))} after connect, "
        f"{(g.get('speaking_ms') or 0) / 1000:.1f} s | hang-up by {(run.get('hangup') or {}).get('by')}, "
        f"confirmed by voice service: {(run.get('hangup') or {}).get('server_confirmed')}"
    )
    out.append("")
    out.append("| Turn | Caller said | Reply heard after (client) | Voice service response (stored / live) "
               "| STT / LLM / TTS first byte | Agent spoke | Gaps > 200 ms | Voice service heard |")
    out.append("|---|---|---|---|---|---|---|---|")
    for t in run.get("turns") or []:
        srv, live = t.get("server") or {}, t.get("server_live") or {}
        ttfb = [srv.get(k) if srv.get(k) is not None else live.get(k) for k in ("stt_ttfb_ms", "llm_ttfb_ms", "tts_ttfb_ms")]
        gaps = t.get("gaps") or []
        big = [x for x in gaps if x["ms"] > 200]
        gap_txt = "none" if not big else ", ".join(f"{x['ms']} ms {x['kind']}" for x in big[:3]) + (" ..." if len(big) > 3 else "")
        if t.get("stalls"):
            gap_txt += f"; {len(t['stalls'])} stall(s) max {max(s['ms'] for s in t['stalls'])} ms"
        if t.get("choppy"):
            gap_txt = "CHOPPY: " + gap_txt
        spoke = "-" if t.get("speaking_ms") is None else "%.1f s" % (t["speaking_ms"] / 1000)
        reply = ms(t.get("reply_ms")) + (" (overlap)" if t.get("overlap") else "")
        if t.get("status") not in (None, "ok"):
            reply += f" [{t['status']}]"
        first_bytes = " / ".join("-" if v is None else str(v) for v in ttfb)
        out.append(
            f"| {t['index']} | {short(t['text'])} | {reply} "
            f"| {ms(srv.get('response_ms'))} / {ms(live.get('response_ms'))} | {first_bytes} ms "
            f"| {spoke} | {gap_txt} | {short(srv.get('user_text') or live.get('heard'))} |"
        )
    out.append("")
    # The live text is used: it is taken from the same time window as the reply. The voice
    # service's saved log pairs each reply's text with the next reply's timestamps, so the
    # stored per-turn `agent_text` shows the previous reply (timings there are right).
    out.append("Agent said (live):")
    out.append(f"- greeting: {short((g.get('server_live') or {}).get('agent_text'), 160)}")
    for t in run.get("turns") or []:
        srv, live = t.get("server") or {}, t.get("server_live") or {}
        out.append(f"- turn {t['index']}: {short(live.get('agent_text') or srv.get('agent_text'), 160)}")
    tr = run.get("transport") or {}
    out.append("")
    out.append(
        f"Transport: {tr.get('frames_received')} frames, longest pause between packets {ms(tr.get('max_frame_interval_ms'))}, "
        f"lost {ms(tr.get('lost_ms'))}. Stored view source: {view.get('source') or 'none'}"
        + (f" ({'; '.join(view.get('notes') or [])})" if view.get("notes") else "")
    )
    return out


def summarize(runs: list[dict]) -> dict:
    replies = [t["reply_ms"] for r in runs for t in r.get("turns") or [] if t.get("reply_ms") is not None]
    server = [t["server"]["response_ms"] for r in runs for t in r.get("turns") or []
              if t.get("server") and t["server"].get("response_ms") is not None]
    return {
        "runs": len(runs),
        "passed": sum(1 for r in runs if r["ok"]),
        "turns": sum(len(r.get("turns") or []) for r in runs),
        "client_reply_ms": {"p50": percentile(replies, 0.5), "max": max(replies) if replies else None, "values": replies},
        "server_response_ms": {"p50": percentile(server, 0.5), "max": max(server) if server else None, "values": server},
        "greeting_after_connect_ms": [r.get("greeting", {}).get("first_audio_ms") for r in runs],
        "choppy_turns": sum(1 for r in runs for t in r.get("turns") or [] if t.get("choppy")),
    }


# ----------------------------------------------------------------------------- runner


async def run_local(args: argparse.Namespace) -> int:
    if not HAVE_AIORTC:
        raise SetupError("aiortc, av and numpy are needed to run the call here (pip install aiortc numpy)")
    out_dir = Path(args.results_dir)
    if not args.flat_results:
        out_dir = out_dir / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    password = args.password or os.environ.get(PASSWORD_ENV) or os.environ.get("ATLAS_DEMO_PASSWORD") \
        or read_dotenv((repo_root() or HERE.parent) / ".env").get("ATLAS_DEMO_PASSWORD")
    if not password:
        raise SetupError("No password: pass --password or set ATLAS_DEMO_PASSWORD (env or .env)")
    api = Atlas(args.api, [password])
    log(f"ATLAS {args.api} as {args.email}")
    org, agent = await asyncio.to_thread(sign_in, api, args.email, password, args.org, args.agent)
    log(f"  organization {org['name']!r}, agent {agent['name']!r} ({agent['id']})")
    log(f"Caller lines ({'files' if args.utterance_wav else f'Pocket TTS voice {args.tts_voice} at {args.tts_url}'}):")
    utterances = await asyncio.to_thread(prepare_utterances, args, out_dir)
    for i, u in enumerate(utterances):
        log(f"  {i + 1}. {u['text']}  [{u['speech_ms'] / 1000:.1f} s]")

    runs = []
    for n in range(1, args.runs + 1):
        if n > 1:
            await asyncio.sleep(args.pause_s)
        run_dir = out_dir / f"run-{n}"
        try:
            run = await run_call(args, api, agent, utterances, n, run_dir)
        except ApiError as exc:
            run = {"run": n, "ok": False, "failures": [f"ATLAS: {exc}"]}
        except Exception as exc:  # a client-side fault must still produce a report
            run = {"run": n, "ok": False, "failures": [f"client error: {type(exc).__name__}: {exc}"]}
        token = run.pop("_token", "")
        run["organization"] = {"id": org["id"], "name": org["name"]}
        run["agent"] = {"id": agent["id"], "name": agent["name"], "published_version": agent.get("published_version")}
        if run.get("greeting") is not None:
            log(f"  fetching the voice service's own per-turn record (up to {args.events_timeout_s:.0f} s)...")
            run["runtime_view"] = await fetch_runtime_view(args, api, run, token)
        evaluate(run, args)
        log("")
        for line in report_run(run):
            log(line)
        runs.append(run)

    summary = summarize(runs)
    result = {
        "tool": "infra/scripts/e2e_test_call.py",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "results_dir": str(out_dir),
        "config": {
            "api": args.api, "agent": args.agent, "organization": org["name"], "runs": args.runs,
            "caller_voice": None if args.utterance_wav else f"{args.tts_model}/{args.tts_voice}",
            "utterances": [{"text": u["text"], "speech_ms": u["speech_ms"], "source": u["source"]} for u in utterances],
            "vad": {"vad_db": args.vad_db, "silence_db": args.silence_db, "min_speech_ms": args.min_speech_ms,
                    "end_silence_ms": args.end_silence_ms, "gap_ms": args.gap_ms},
            "limits": {"max_reply_ms": args.max_reply_ms, "greeting_timeout_s": args.greeting_timeout_s,
                       "reply_timeout_s": args.reply_timeout_s, "turn_timeout_s": args.turn_timeout_s},
            "runner": "voice-runtime container" if in_container() else "local",
        },
        "summary": summary,
        "ok": all(r["ok"] for r in runs),
        "runs": runs,
    }
    log("")
    log(f"Summary: {summary['passed']}/{summary['runs']} runs passed; reply heard after (client) p50 "
        f"{ms(summary['client_reply_ms']['p50'])}, max {ms(summary['client_reply_ms']['max'])}; voice service response p50 "
        f"{ms(summary['server_response_ms']['p50'])}; choppy turns {summary['choppy_turns']}")
    text = json.dumps(result, indent=2)
    (out_dir / "result.json").write_text(text, encoding="utf-8")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(text, encoding="utf-8")
    log(f"Results: {out_dir}")
    return 0 if result["ok"] else 1


# ----------------------------------------------------------------------------- launcher


def for_container(url: str) -> str:
    """A host URL as seen from inside a container (loopback -> host.docker.internal)."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "::1"):
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    return url


FORWARD = [  # options passed through unchanged to the copy inside the container
    "email", "org", "agent", "lead_id", "tts_model", "tts_voice", "runs", "max_reply_ms",
    "greeting_timeout_s", "reply_timeout_s", "turn_timeout_s", "connect_timeout_s", "end_silence_ms",
    "gap_ms", "vad_db", "silence_db", "min_speech_ms", "events_timeout_s", "origin", "pause_s",
    "signalling",
]


def run_in_container(args: argparse.Namespace) -> int:
    root = repo_root()
    if root is None or shutil.which("docker") is None:
        raise SetupError("aiortc is not installed here and docker is not available: install aiortc and numpy")
    password = args.password or os.environ.get("ATLAS_DEMO_PASSWORD") or read_dotenv(root / ".env").get("ATLAS_DEMO_PASSWORD")
    if not password:
        raise SetupError("No password: pass --password or set ATLAS_DEMO_PASSWORD (env or .env)")
    compose = ["docker", "compose", "--profile", "voice"]

    def dc(*a: str, check: bool = True, quiet: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run([*compose, *a], cwd=root, capture_output=quiet, text=True, encoding="utf-8", errors="replace")
        if check and proc.returncode:
            raise SetupError(f"docker compose {' '.join(a[:3])} failed: {(proc.stderr or '').strip()[:300]}")
        return proc

    if not dc("ps", "--status", "running", "-q", SERVICE, check=False).stdout.strip():
        raise SetupError("The voice runtime is not running: start it with `make voice-up`")
    if dc("exec", "-T", SERVICE, CONTAINER_PY, "-c", "import aiortc, av, numpy", check=False).returncode:
        raise SetupError("aiortc is not available in the voice-runtime container: install aiortc and numpy here instead")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    work = f"{CONTAINER_WORK}/{stamp}"
    dc("exec", "-T", SERVICE, "mkdir", "-p", f"{work}/in")
    dc("cp", str(HERE), f"{SERVICE}:{CONTAINER_SCRIPT}")
    inner = [CONTAINER_PY, "-u", CONTAINER_SCRIPT, "--runner", "local", "--flat-results",
             "--api", for_container(args.api), "--results-dir", work]
    inner += ["--tts-url", for_container(args.tts_url) if args.tts_url else CONTAINER_TTS]
    for name in FORWARD:
        value = getattr(args, name)
        if value is not None:
            inner += [f"--{name.replace('_', '-')}", str(value)]
    for text in args.utterance or []:
        inner += ["--utterance", text]
    for i, path in enumerate(args.utterance_wav or []):
        dest = f"{work}/in/{i + 1:02d}.wav"
        dc("cp", str(Path(path).resolve()), f"{SERVICE}:{dest}")
        inner += ["--utterance-wav", dest]
    if args.use_ice_servers:
        inner.append("--use-ice-servers")
    log(f"aiortc is not installed here: running the caller inside the {SERVICE} container")
    env = {**os.environ, PASSWORD_ENV: password}
    try:
        code = subprocess.run([*compose, "exec", "-T", "-e", PASSWORD_ENV, SERVICE, *inner], cwd=root, env=env).returncode
        host_dir = Path(args.results_dir).resolve() / stamp
        host_dir.parent.mkdir(parents=True, exist_ok=True)
        if dc("cp", f"{SERVICE}:{work}/.", str(host_dir), check=False).returncode == 0:
            result_file = host_dir / "result.json"
            if result_file.exists():
                data = json.loads(result_file.read_text(encoding="utf-8"))
                data["results_dir"] = str(host_dir)
                text = json.dumps(data, indent=2)
                result_file.write_text(text, encoding="utf-8")
                if args.json:
                    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.json).write_text(text, encoding="utf-8")
                    log(f"JSON: {Path(args.json).resolve()}")
            log(f"Results copied to {host_dir}")
        return code
    finally:  # leave the container as it was (copied-in files are root-owned, hence -u root)
        dc("exec", "-T", "-u", "root", SERVICE, "sh", "-c",
           f"rm -rf {work} {CONTAINER_SCRIPT} /tmp/__pycache__; rmdir {CONTAINER_WORK} 2>/dev/null; true", check=False)


def parse_args(argv: list[str]) -> argparse.Namespace:
    root = repo_root()
    env = read_dotenv(root / ".env") if root else {}
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    a = p.add_argument
    a("--api", default=os.environ.get("ATLAS_E2E_API", "http://127.0.0.1:8080"), help="ATLAS API base URL")
    a("--email", default=os.environ.get("ATLAS_DEMO_EMAIL") or env.get("ATLAS_DEMO_EMAIL") or DEFAULT_EMAIL)
    a("--password", default=None, help="discouraged: prefer env/.env ATLAS_DEMO_PASSWORD")
    a("--org", default=None, help="organization name, slug or id (default: the first membership)")
    a("--agent", default=DEFAULT_AGENT, help="agent name (default: %(default)s)")
    a("--lead-id", default=None, help="fill the prompt variables from this lead")
    a("--utterance", action="append", help="a caller line (repeat; default: 4 mortgage-lead lines)")
    a("--utterance-wav", action="append", help="pre-made 16-bit WAV for a caller line (repeat, in order)")
    a("--tts-url", default=None, help=f"Pocket TTS server (default {HOST_TTS}; {CONTAINER_TTS} in the container)")
    a("--tts-model", default="pocket-tts")
    a("--tts-voice", default="marius", help="caller voice, different from the agent's (default %(default)s)")
    a("--runs", type=int, default=1, help="calls to place one after another")
    a("--pause-s", type=float, default=5.0, help="pause between runs")
    a("--max-reply-ms", type=int, default=15000, help="fail when a reply starts later than this after the caller stops")
    a("--connect-timeout-s", type=float, default=30.0)
    a("--greeting-timeout-s", type=float, default=60.0, help="wait this long for the agent's greeting")
    a("--reply-timeout-s", type=float, default=45.0, help="wait this long for a reply to start")
    a("--turn-timeout-s", type=float, default=120.0, help="cap on one agent turn, start to finish")
    a("--vad-db", type=float, default=-45.0, help="agent audio at or above this RMS level (dBFS) is speech")
    a("--silence-db", type=float, default=-90.0,
      help="a gap below this level throughout is the voice service's filler (exact zeros), not a pause")
    a("--min-speech-ms", type=int, default=60, help="voiced audio needed within 300 ms to count as speech")
    a("--end-silence-ms", type=int, default=1500, help="silence that ends an agent utterance")
    a("--gap-ms", type=int, default=200, help="a gap or packet stall longer than this inside speech is reported")
    a("--events-timeout-s", type=float, default=90.0, help="wait this long for the voice service's stored record")
    a("--origin", default=os.environ.get("ATLAS_WEB_APP_ORIGIN") or env.get("ATLAS_WEB_APP_ORIGIN") or "http://localhost:5173",
      help="Origin header for signalling (must match the web app origin the session was made for)")
    a("--use-ice-servers", action="store_true", help="use the session's STUN/TURN servers even for a local voice service")
    a("--signalling", default=None,
      help="connect signalling to this base (e.g. ws://localhost:8000) instead of the session's public address; "
           "the path and token are kept. A localhost base also means no STUN/TURN (direct path)")
    a("--voice-db-url", default=None, help="voice service database URL for the stored record (default: DATABASE_URL)")
    a("--results-dir", default=str((root / ".cache" / "e2e-test-call") if root else Path.cwd() / "e2e-test-call"))
    a("--json", default=None, help="also write the result JSON here")
    a("--runner", choices=("auto", "local", "docker"), default="auto")
    a("--flat-results", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.runs < 1:
        p.error("--runs must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(errors="replace")  # agent text may not fit a legacy console code page
    args = parse_args(sys.argv[1:] if argv is None else argv)
    runner = args.runner
    if runner == "auto":
        runner = "local" if HAVE_AIORTC else "docker"
    try:
        if runner == "docker":
            return run_in_container(args)
        if args.tts_url is None:
            args.tts_url = CONTAINER_TTS if in_container() else HOST_TTS
        return asyncio.run(run_local(args))
    except SetupError as exc:
        log(f"Setup error: {exc}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
