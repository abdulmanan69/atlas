"""Live speech-to-text server that speaks the Deepgram v1 live-listen protocol (CPU).

The voice service already streams call audio to Deepgram over a websocket and accepts a
custom endpoint, so this server answers that same protocol from a local model. Callers
get live partial transcripts while they speak instead of waiting for the whole utterance.

Wire protocol (what the Deepgram SDK used by the voice service sends and parses):
  - `GET /v1/listen?...` websocket upgrade. Query: `encoding` (only `linear16`),
    `sample_rate`, `channels` (only 1), `interim_results`, `endpointing` (`false`
    disables endpoint finals), `utterance_end_ms`, `vad_events`, `model`, `language`;
    anything else (keyterm, punctuate, smart_format, ...) is accepted and ignored.
  - Client -> server: binary frames of 16-bit little-endian mono PCM at `sample_rate`;
    text frames `{"type": "KeepAlive" | "Finalize" | "CloseStream"}`.
  - Server -> client: `Results` (interim `is_final=false` while words change; final
    `is_final=true, speech_final=true` at an endpoint, and `from_finalize=true` right
    after a Finalize), `SpeechStarted` / `UtteranceEnd` when asked for, `Metadata` before
    closing on CloseStream, `Error` for a malformed control message.
  - `GET /health` returns JSON. `Authorization` is accepted as is unless STT_API_KEY is set.

How audio becomes text. A voice activity detector (Silero VAD) watches the caller's audio;
when speech starts, the audio from just before it is handed to the model, and it keeps
receiving audio until the segment ends (Finalize from the voice service, a pause of
STT_ENDPOINT_SILENCE_MS, or STT_MAX_UTTERANCE_S). Between segments the model does no work.
  - Whole-utterance models (kind "offline", e.g. Parakeet TDT, the default) transcribe the
    segment when it ends; live words come from re-reading the growing segment every
    STT_INTERIM_MS. The detector notices a pause about 0.1 s before the voice service
    sends Finalize, so the segment is already being transcribed when Finalize arrives.
  - Streaming models (kind "online", e.g. Nemotron Speech Streaming) process each chunk as
    it arrives, so a Finalize only has to flush the last chunk.
One recognizer is shared by all connections; each segment gets its own model stream.
Audio at other rates (8 kHz phone audio, 48 kHz) is resampled to the models' 16 kHz once,
as it arrives (windowed-sinc filter), for the detector and the model alike.

Environment (defaults in brackets):
  STT_MODEL [parakeet-110m]        which model (fetch_model.MODELS)
  STT_MODEL_DIR [/models]          where fetch_model.py stores the models
  STT_MODEL_ID [per model]         name reported in results and /health
  STT_NUM_THREADS [2]              CPU threads per decode
  STT_MAX_SESSIONS [4]             concurrent connections; more get HTTP 503
  STT_ENDPOINT_SILENCE_MS [400]    silence after speech that ends a segment
  STT_MAX_UTTERANCE_S [20]         a segment is finalised after this long regardless
  STT_VAD_THRESHOLD [0.5]          speech probability that counts as speech
  STT_PREROLL_MS [600]             audio before the detected start that is kept
  STT_INTERIM_MS [400]             whole-utterance models: how often live words are refreshed
  STT_FINALIZE_PAD_MS [per model]  silence added on a final to flush the last words
  STT_MIN_CONFIDENCE [per model]   text the model is less sure of (mean token probability)
                                   is dropped: background talk, another language, noise
  STT_IDLE_TIMEOUT_S [30]          close a connection that sends nothing for this long
  STT_API_KEY [unset]              if set, require "Token <key>" or "Bearer <key>"
  LOG_LEVEL [INFO]                 transcripts are never logged at INFO
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import logging
import math
import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import numpy as np
import sherpa_onnx
from fetch_model import ModelSpec, ensure_model, spec_from_env
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("streaming_stt")
# The websockets library logs every failed handshake (e.g. health probes) at INFO.
logging.getLogger("websockets").setLevel(logging.WARNING)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


SPEC: ModelSpec = spec_from_env()
MODEL_DIR = Path(os.environ.get("STT_MODEL_DIR", "/models"))
MODEL_ID = os.environ.get("STT_MODEL_ID", SPEC.model_id)
MODEL_VERSION = SPEC.version
MODEL_UUID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, f"https://huggingface.co/{SPEC.repo}@{SPEC.revision}")
)
MODEL_RATE = 16000
NUM_THREADS = int(os.environ.get("STT_NUM_THREADS", "2"))
MAX_SESSIONS = int(os.environ.get("STT_MAX_SESSIONS", "4"))
ENDPOINT_SILENCE_S = _env_float("STT_ENDPOINT_SILENCE_MS", 400) / 1000
MAX_UTTERANCE_S = _env_float("STT_MAX_UTTERANCE_S", 20)
VAD_THRESHOLD = _env_float("STT_VAD_THRESHOLD", 0.5)
PREROLL_S = _env_float("STT_PREROLL_MS", 600) / 1000
INTERIM_S = _env_float("STT_INTERIM_MS", 400) / 1000
FINALIZE_PAD_S = _env_float("STT_FINALIZE_PAD_MS", SPEC.tail_pad_s * 1000) / 1000
IDLE_TIMEOUT_S = _env_float("STT_IDLE_TIMEOUT_S", 30)
API_KEY = os.environ.get("STT_API_KEY") or None
# 64 KiB per frame (2 s at 16 kHz); the voice service sends 20 ms frames. With QUEUE_LIMIT
# this bounds buffered audio per connection to 16 MiB.
MAX_MESSAGE_BYTES = 64 * 1024
FEED_SLICE_S = 0.1  # audio that queued up while decoding is processed in slices this long
QUEUE_LIMIT = 256  # frames buffered per connection before the reader waits (backpressure)
# CloseStream decodes everything still queued (at most QUEUE_LIMIT frames).
CLOSE_FLUSH_TIMEOUT_S = 60.0
VAD_WINDOW = 512  # samples at 16 kHz (32 ms), what Silero VAD expects
# Whole-utterance models: live words are re-read at most every STT_INTERIM_MS, and never
# sooner than this many times the last read took, so re-reading a long segment uses at
# most about 40 % of a session's CPU time and cannot fall behind the audio.
INTERIM_COST_FACTOR = 2.5
MIN_CONFIDENCE = _env_float("STT_MIN_CONFIDENCE", SPEC.min_confidence)
# Trailing silence kept on a whole-utterance segment before it is transcribed.
OFFLINE_TAIL_S = 0.3

_paths: dict[str, Path] = {}
_engine: Engine | None = None
_pool = ThreadPoolExecutor(max_workers=MAX_SESSIONS, thread_name_prefix="decode")
_active = 0
_started_at = time.time()


# ------------------------------------------------------------------ models


@dataclass(slots=True)
class Hypothesis:
    text: str
    tokens: list[str]
    timestamps: list[float]  # seconds from the start of the segment
    confidence: float = 1.0


class Engine(Protocol):
    """A model: `start` opens a segment, audio goes in with `accept`, `partial` returns new
    live words (or None), `final` returns the segment's text and closes it. keep_s: the
    seconds of the segment worth transcribing (trailing silence after it is ignored)."""

    streaming: bool

    def start(self, rate: int) -> Any: ...
    def accept(self, seg: Any, samples: np.ndarray) -> None: ...
    def partial(self, seg: Any) -> Hypothesis | None: ...
    def prepare(self, seg: Any, mark: float) -> None: ...
    def final(self, seg: Any, keep_s: float | None, mark: float | None = None) -> Hypothesis: ...


def _clean(raw: str) -> str:
    if SPEC.cased:
        return " ".join(raw.split())
    # Lower-case models without punctuation: make it read like a sentence.
    words = raw.strip().lower().split()
    words = ["I" if w == "i" else ("I" + w[1:] if w.startswith("i'") else w) for w in words]
    if words:
        words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)


class OnlineEngine:
    """Streaming transducer: chunks are decoded as they arrive; a final flushes the last one."""

    streaming = True

    def __init__(self) -> None:
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(_paths["tokens"]),
            encoder=str(_paths["encoder"]),
            decoder=str(_paths["decoder"]),
            joiner=str(_paths["joiner"]),
            num_threads=NUM_THREADS,
            sample_rate=MODEL_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            provider="cpu",
        )

    def start(self, rate: int) -> dict[str, Any]:
        stream = self.rec.create_stream()
        if lang := SPEC.extra.get("language"):
            stream.set_option("language", lang)
        return {"stream": stream, "rate": rate, "text": ""}

    def accept(self, seg: dict[str, Any], samples: np.ndarray) -> None:
        seg["stream"].accept_waveform(seg["rate"], samples)

    def _hyp(self, seg: dict[str, Any]) -> Hypothesis:
        while self.rec.is_ready(seg["stream"]):
            self.rec.decode_stream(seg["stream"])
        result = json.loads(self.rec.get_result_as_json_string(seg["stream"]))
        return Hypothesis(
            text=_clean(result.get("text", "")),
            tokens=list(result.get("tokens") or []),
            timestamps=[float(t) for t in result.get("timestamps") or []],
            confidence=_confidence(list(result.get("ys_probs") or [])),  # log-probabilities
        )

    def partial(self, seg: dict[str, Any]) -> Hypothesis | None:
        hyp = self._hyp(seg)
        if hyp.text == seg["text"]:
            return None
        seg["text"] = hyp.text
        return hyp

    def prepare(self, seg: dict[str, Any], mark: float) -> None:
        pass  # chunks are decoded as they arrive; only the last one is left for the final

    def final(
        self, seg: dict[str, Any], keep_s: float | None, mark: float | None = None
    ) -> Hypothesis:
        rate = seg["rate"]
        seg["stream"].accept_waveform(rate, np.zeros(int(rate * FINALIZE_PAD_S), np.float32))
        seg["stream"].input_finished()
        return self._hyp(seg)


class OfflineEngine:
    """Whole-utterance transducer: the segment is transcribed when it ends."""

    streaming = False

    def __init__(self) -> None:
        self.rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(_paths["encoder"]),
            decoder=str(_paths["decoder"]),
            joiner=str(_paths["joiner"]),
            tokens=str(_paths["tokens"]),
            num_threads=NUM_THREADS,
            sample_rate=MODEL_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            model_type=SPEC.model_type or "transducer",
        )

    def start(self, rate: int) -> dict[str, Any]:
        return {
            "chunks": [],
            "n": 0,
            "rate": rate,
            "text": "",
            "read_at": 0,
            "cost": 0.0,
            "spec": None,  # (mark, hypothesis) read as soon as the speaker paused
        }

    def accept(self, seg: dict[str, Any], samples: np.ndarray) -> None:
        seg["chunks"].append(samples)
        seg["n"] += samples.size

    def _decode(self, seg: dict[str, Any], n: int | None = None) -> Hypothesis:
        audio = np.concatenate(seg["chunks"]) if seg["chunks"] else np.zeros(0, np.float32)
        seg["chunks"] = [audio]
        if n is not None:
            audio = audio[:n]
        stream = self.rec.create_stream()
        stream.accept_waveform(seg["rate"], audio)
        started = time.perf_counter()
        self.rec.decode_stream(stream)
        seg["cost"] = time.perf_counter() - started
        result = stream.result
        return Hypothesis(
            text=_clean(result.text),
            tokens=list(result.tokens),
            timestamps=[float(t) for t in result.timestamps],
            confidence=_confidence(list(getattr(result, "ys_log_probs", None) or [])),
        )

    def partial(self, seg: dict[str, Any]) -> Hypothesis | None:
        wait_s = max(INTERIM_S, INTERIM_COST_FACTOR * seg["cost"])
        if seg["n"] - seg["read_at"] < wait_s * seg["rate"]:
            return None
        seg["read_at"] = seg["n"]
        hyp = self._decode(seg)
        if hyp.text == seg["text"]:
            return None
        seg["text"] = hyp.text
        return hyp

    def prepare(self, seg: dict[str, Any], mark: float) -> None:
        """The speaker just paused: transcribe now, so a Finalize that follows finds the
        text ready. mark identifies the pause (when the speech before it ended)."""
        if seg["spec"] is None or seg["spec"][0] != mark:
            seg["spec"] = (mark, self._decode(seg))

    def final(
        self, seg: dict[str, Any], keep_s: float | None, mark: float | None = None
    ) -> Hypothesis:
        if mark is not None and seg["spec"] is not None and seg["spec"][0] == mark:
            return seg["spec"][1]  # nothing was said since the pause was transcribed
        n = None if keep_s is None else max(int(keep_s * seg["rate"]), 0)
        return self._decode(seg, n)


def _confidence(log_probs: list[float]) -> float:
    """Mean token probability, the geometric mean (1.0 when the model reports none)."""
    return float(np.exp(np.mean(log_probs))) if log_probs else 1.0


def _trusted(hyp: Hypothesis) -> Hypothesis:
    """Drop text the model is unsure of. Measured with Parakeet 110M on real calls: clear
    English scored 0.71 or more (one exception, transcribed wrongly anyway), while 24 of 34
    stretches of background talk in another language, mumbling or noise scored under 0.6;
    such text would otherwise reach the agent as words the caller never said."""
    if hyp.text and hyp.confidence < MIN_CONFIDENCE:
        logger.debug("dropped text with confidence %.2f", hyp.confidence)
        return Hypothesis("", [], [], hyp.confidence)
    return hyp


def _load_engine() -> Engine:
    return OnlineEngine() if SPEC.kind == "online" else OfflineEngine()


# ------------------------------------------------------------------ request options


class BadRequestError(ValueError):
    pass


def _flag(params: dict[str, list[str]], name: str, default: bool) -> bool:
    values = params.get(name)
    if not values:
        return default
    return values[-1].strip().lower() in ("true", "1", "yes")


@dataclass(frozen=True, slots=True)
class Options:
    sample_rate: int
    interim_results: bool
    endpointing: bool
    utterance_end_s: float | None
    vad_events: bool
    model: str
    language: str

    @classmethod
    def from_path(cls, path: str) -> Options:
        params = parse_qs(urlsplit(path).query, keep_blank_values=True)

        def one(name: str, default: str) -> str:
            values = params.get(name)
            return values[-1] if values else default

        encoding = one("encoding", "linear16").lower()
        if encoding != "linear16":
            raise BadRequestError("Only encoding=linear16 is supported")
        if one("channels", "1") != "1" or _flag(params, "multichannel", False):
            raise BadRequestError("Only mono audio (channels=1) is supported")
        try:
            sample_rate = int(one("sample_rate", "16000"))
        except ValueError as exc:
            raise BadRequestError("sample_rate must be an integer") from exc
        if not 8000 <= sample_rate <= 48000:
            raise BadRequestError("sample_rate must be between 8000 and 48000")
        endpointing = one("endpointing", "").strip().lower() != "false"
        utterance_end: float | None = None
        if raw := one("utterance_end_ms", "").strip():
            try:
                utterance_end = max(int(raw), 1000) / 1000  # Deepgram's minimum is 1000 ms
            except ValueError as exc:
                raise BadRequestError("utterance_end_ms must be an integer") from exc
        return cls(
            sample_rate=sample_rate,
            interim_results=_flag(params, "interim_results", False),
            endpointing=endpointing,
            utterance_end_s=utterance_end,
            vad_events=_flag(params, "vad_events", False),
            model=one("model", MODEL_ID),
            language=one("language", "en"),
        )


# ------------------------------------------------------------------ audio


class Resampler:
    """Streaming resampler, so audio at any rate is converted once as it arrives; state is
    kept between calls, so frame boundaries leave no clicks. Small ratios (8, 24, 32, 48
    kHz to 16 kHz) use a windowed-sinc filter; others (11.025, 22.05, 44.1 kHz) a moving
    average and linear interpolation, which is enough for speech."""

    def __init__(self, src: int, dst: int, taps_per_phase: int = 16) -> None:
        g = math.gcd(src, dst)
        self.up, self.down = dst // g, src // g
        self.exact = max(self.up, self.down) <= 6
        if self.exact:
            n = 2 * taps_per_phase * max(self.up, self.down) + 1
            cutoff = 0.5 / max(self.up, self.down) * 0.92  # of the upsampled rate
            t = np.arange(n) - (n - 1) / 2
            h = 2 * cutoff * np.sinc(2 * cutoff * t) * np.hanning(n)
            self.h = (h * self.up / h.sum()).astype(np.float32)
        else:
            k = max(round(src / dst), 1)
            self.h = np.full(k, 1 / k, np.float32)
        self.history = np.zeros(self.h.size - 1, np.float32)
        self.phase = 0
        self.step = src / dst
        self.pos = 1.0  # next output position, in input samples after `history`'s last
        self.last = np.zeros(1, np.float32)

    def _filter(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.history, x])
        self.history = buf[buf.size - self.history.size :]
        return np.convolve(buf, self.h, mode="valid")

    def process(self, x: np.ndarray) -> np.ndarray:
        if x.size == 0:
            return x
        if self.exact:
            up = np.zeros(x.size * self.up, np.float32)
            up[:: self.up] = x
            y = self._filter(up)
            out = y[self.phase :: self.down]
            self.phase = (self.phase - y.size) % self.down
            return out.astype(np.float32)
        y = self._filter(x) if self.h.size > 1 else x
        buf = np.concatenate([self.last, y])
        points = np.arange(self.pos, buf.size - 1 + 1e-9, self.step)
        out = np.interp(points, np.arange(buf.size), buf)
        self.pos = (points[-1] + self.step - (buf.size - 1)) if points.size else self.pos - y.size
        self.last = buf[-1:]
        return out.astype(np.float32)


class Vad:
    """Silero VAD on 16 kHz windows, plus a loudness detector for narrowband (phone) audio.

    The Silero model in sherpa-onnx takes 16 kHz only, and on 8 kHz audio brought up to
    16 kHz it misses speech: measured on the caller recordings, 23 of 55 utterances were
    never detected (0 of 55 at 16 kHz). For narrowband sessions a window also counts as
    speech when it is 15 dB above the background level and louder than -50 dBFS; text from
    noise that this lets through is caught by STT_MIN_CONFIDENCE.
    """

    ENERGY_OVER_FLOOR_DB = 15.0
    ENERGY_MIN_DB = -50.0
    ENERGY_HOLD = 3  # windows (about 0.1 s) that still count as speech after a loud one

    def __init__(self, narrowband: bool = False) -> None:
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(_paths["vad"])
        cfg.silero_vad.threshold = VAD_THRESHOLD
        # Short hold times: the session measures pauses itself (ENDPOINT_SILENCE_S).
        cfg.silero_vad.min_silence_duration = 0.1
        cfg.silero_vad.min_speech_duration = 0.1
        cfg.silero_vad.max_speech_duration = 3600
        cfg.silero_vad.window_size = VAD_WINDOW
        cfg.sample_rate = MODEL_RATE
        cfg.num_threads = 1
        self.model = sherpa_onnx.VadModel.create(cfg)
        self.pending = np.zeros(0, np.float32)
        self.narrowband = narrowband
        self.floor_db: float | None = None
        self.hold = 0

    def _loud(self, window: np.ndarray) -> bool:
        level = 10 * np.log10(float(np.mean(window**2)) + 1e-10)
        if level < -90:  # digital silence (e.g. a muted line) says nothing about the noise
            return self._held()
        # Background level: follows quieter windows down at once, rises slowly (3 dB/s).
        floor = level if self.floor_db is None else min(level, self.floor_db + 0.1)
        self.floor_db = floor
        if level > max(floor + self.ENERGY_OVER_FLOOR_DB, self.ENERGY_MIN_DB):
            self.hold = self.ENERGY_HOLD
            return True
        return self._held()

    def _held(self) -> bool:
        if self.hold:
            self.hold -= 1
            return True
        return False

    def speech(self, x: np.ndarray) -> list[bool]:
        """One verdict per complete 32 ms window in the audio received so far."""
        self.pending = np.concatenate([self.pending, x])
        out = []
        while self.pending.size >= VAD_WINDOW:
            window = self.pending[:VAD_WINDOW]
            verdict = bool(self.model.is_speech(window))
            if self.narrowband:
                verdict = self._loud(window) or verdict
            out.append(verdict)
            self.pending = self.pending[VAD_WINDOW:]
        return out


# ------------------------------------------------------------------ one connection


class Session:
    """State for one websocket. All methods run in the decode pool, one at a time."""

    def __init__(self, opts: Options) -> None:
        assert _engine is not None
        self.engine = _engine
        self.opts = opts
        # Everything below works on 16 kHz audio; other rates are converted on arrival.
        self.rate = MODEL_RATE
        self.resampler = (
            None if opts.sample_rate == MODEL_RATE else Resampler(opts.sample_rate, MODEL_RATE)
        )
        self.request_id = str(uuid.uuid4())
        self.vad = Vad(narrowband=opts.sample_rate < MODEL_RATE)
        self.preroll: collections.deque[np.ndarray] = collections.deque()
        self.preroll_n = 0
        self.seg: Any = None  # the model's state for the current segment
        self.seg_start_s = 0.0
        self.seg_n = 0  # samples handed to the model for this segment
        self.samples = 0  # samples received (at 16 kHz)
        self.last_speech_s: float | None = None  # end of the last speech window
        self.speaking = False
        self.last_word_s: float | None = None
        self.had_words = False
        self.utterance_end_due = False
        self.sha = hashlib.sha256()
        self.leftover = b""

    @property
    def audio_s(self) -> float:
        return self.samples / self.rate

    # -------------------------------------------------------------- messages

    def _words(self, hyp: Hypothesis) -> list[dict[str, Any]]:
        words = hyp.text.split()
        if not words:
            return []
        start_times: list[float] = []
        for token, ts in zip(hyp.tokens, hyp.timestamps, strict=False):
            if token.startswith(("▁", " ")) or not start_times:
                start_times.append(ts)
        seg_end = max(self.seg_n / self.rate, 0.0)
        if len(start_times) != len(words):  # fall back to an even split of the segment
            step = seg_end / max(len(words), 1)
            start_times = [i * step for i in range(len(words))]
        out = []
        for i, word in enumerate(words):
            start = self.seg_start_s + start_times[i]
            end = self.seg_start_s + (start_times[i + 1] if i + 1 < len(words) else seg_end)
            out.append(
                {
                    "word": word.lower().strip(".,?!;:\"'"),
                    "start": round(start, 3),
                    "end": round(max(end, start), 3),
                    "confidence": round(hyp.confidence, 4),
                    "punctuated_word": word,
                }
            )
        return out

    def _results(
        self,
        hyp: Hypothesis,
        *,
        is_final: bool,
        speech_final: bool = False,
        from_finalize: bool = False,
    ) -> dict[str, Any]:
        return {
            "type": "Results",
            "channel_index": [0, 1],
            "duration": round(self.seg_n / self.rate, 3),
            "start": round(self.seg_start_s, 3),
            "is_final": is_final,
            "speech_final": speech_final,
            "from_finalize": from_finalize,
            "channel": {
                "alternatives": [
                    {
                        "transcript": hyp.text,
                        "confidence": round(hyp.confidence, 4),
                        "words": self._words(hyp),
                    }
                ]
            },
            "metadata": {
                "request_id": self.request_id,
                "model_info": {"name": MODEL_ID, "version": MODEL_VERSION, "arch": SPEC.arch},
                "model_uuid": MODEL_UUID,
            },
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "type": "Metadata",
            "transaction_key": "deprecated",
            "request_id": self.request_id,
            "sha256": self.sha.hexdigest(),
            "created": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "duration": round(self.audio_s, 3),
            "channels": 1,
            "models": [MODEL_UUID],
            "model_info": {
                MODEL_UUID: {"name": MODEL_ID, "version": MODEL_VERSION, "arch": SPEC.arch}
            },
        }

    # -------------------------------------------------------------- segments

    def _start_segment(self, include_preroll: bool) -> list[dict[str, Any]]:
        self.seg = self.engine.start(self.rate)
        self.seg_n = 0
        self.seg_start_s = self.audio_s
        if include_preroll and self.preroll:
            audio = np.concatenate(self.preroll)
            self.seg_start_s = self.audio_s - audio.size / self.rate
            self.engine.accept(self.seg, audio)
            self.seg_n = audio.size
        self.preroll.clear()
        self.preroll_n = 0
        if self.opts.vad_events:
            start = round(self.seg_start_s, 3)
            return [{"type": "SpeechStarted", "channel": [0, 1], "timestamp": start}]
        return []

    def _final(self, *, speech_final: bool, from_finalize: bool) -> dict[str, Any]:
        if self.seg is None:
            hyp = Hypothesis("", [], [])
            self.seg_n = 0
            self.seg_start_s = self.audio_s
        else:
            keep = None
            if self.last_speech_s is not None:
                keep = self.last_speech_s - self.seg_start_s + OFFLINE_TAIL_S
            hyp = _trusted(self.engine.final(self.seg, keep, self.last_speech_s))
        msg = self._results(
            hyp, is_final=True, speech_final=speech_final, from_finalize=from_finalize
        )
        if hyp.text:
            self.had_words = True
            self.last_word_s = self.last_speech_s or self.audio_s
            self.utterance_end_due = self.opts.utterance_end_s is not None
        self.seg = None
        self.seg_n = 0
        return msg

    def _step(self, part: np.ndarray, verdicts: list[bool]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        heard = any(verdicts)
        if heard:
            self.last_speech_s = self.audio_s
        self.speaking = verdicts[-1] if verdicts else self.speaking
        if self.seg is None:
            self.preroll.append(part)
            self.preroll_n += part.size
            while self.preroll and self.preroll_n - self.preroll[0].size >= PREROLL_S * self.rate:
                self.preroll_n -= self.preroll.popleft().size
            if heard:
                self.utterance_end_due = False
                events.extend(self._start_segment(include_preroll=True))
        else:
            self.engine.accept(self.seg, part)
            self.seg_n += part.size
        if self.seg is None:
            return events

        quiet_s = self.audio_s - (self.last_speech_s or self.seg_start_s)
        if self.opts.interim_results and (self.engine.streaming or self.speaking):
            hyp = self.engine.partial(self.seg)
            if hyp is not None and _trusted(hyp).text:
                self.utterance_end_due = False
                events.append(self._results(hyp, is_final=False))
        elif self.engine.streaming:
            self.engine.partial(self.seg)  # keep decoding as audio arrives
        if not self.speaking and self.last_speech_s is not None:
            # The voice service sends Finalize about 0.2 s after the speaker stops; the
            # detector notices the pause about 0.1 s sooner, so start on the text now.
            self.engine.prepare(self.seg, self.last_speech_s)

        if self.opts.endpointing and quiet_s >= ENDPOINT_SILENCE_S:
            events.append(self._final(speech_final=True, from_finalize=False))
        elif self.seg_n / self.rate >= MAX_UTTERANCE_S:
            events.append(self._final(speech_final=False, from_finalize=False))
            if self.speaking:
                events.extend(self._start_segment(include_preroll=False))
        elif not self.opts.endpointing and quiet_s >= max(ENDPOINT_SILENCE_S, 2.0):
            # endpointing=false: no final at pauses, but a long silence still closes the
            # segment quietly so the model does no work between turns.
            hyp = _trusted(self.engine.final(self.seg, None))
            if hyp.text:
                events.append(self._results(hyp, is_final=True))
                self.had_words = True
                self.last_word_s = self.last_speech_s
                self.utterance_end_due = self.opts.utterance_end_s is not None
            self.seg = None
        return events

    def _utterance_end(self) -> list[dict[str, Any]]:
        if (
            self.utterance_end_due
            and self.seg is None
            and self.last_word_s is not None
            and self.opts.utterance_end_s is not None
            and self.audio_s - self.last_word_s >= self.opts.utterance_end_s
        ):
            self.utterance_end_due = False
            last = round(self.last_word_s, 3)
            return [{"type": "UtteranceEnd", "channel": [0, 1], "last_word_end": last}]
        return []

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self.sha.update(data)
        data = self.leftover + data
        cut = len(data) - (len(data) % 2)
        self.leftover = data[cut:]
        if not cut:
            return []
        pcm = np.frombuffer(data[:cut], dtype="<i2").astype(np.float32) / 32768.0
        if self.resampler is not None:
            pcm = self.resampler.process(pcm)
        # Audio that queued up while decoding arrives here in one piece. Feed it in short
        # slices so a pause inside it still ends its segment instead of two utterances
        # running together.
        events: list[dict[str, Any]] = []
        step = max(int(self.rate * FEED_SLICE_S), 1)
        for start in range(0, pcm.size, step):
            part = pcm[start : start + step]
            self.samples += part.size
            events.extend(self._step(part, self.vad.speech(part)))
            events.extend(self._utterance_end())
        return events

    def finalize(self, *, from_finalize: bool) -> list[dict[str, Any]]:
        """Transcribe what is pending and emit it as final (an empty one if nothing is)."""
        msg = self._final(speech_final=True, from_finalize=from_finalize)
        if not from_finalize and not msg["channel"]["alternatives"][0]["transcript"]:
            return []  # closing with nothing pending: no empty result
        return [msg]


# ------------------------------------------------------------------ websocket handling

_FINALIZE = object()
_CLOSE = object()


def _json_response(
    connection: ServerConnection, status: HTTPStatus, body: dict[str, Any]
) -> Response:
    response = connection.respond(status, json.dumps(body) + "\n")
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = "application/json"
    return response


def _health() -> dict[str, Any]:
    return {
        "status": "ok" if _engine is not None else "loading",
        "model": MODEL_ID,
        "model_key": SPEC.key,
        "model_version": MODEL_VERSION,
        "streaming": SPEC.kind == "online",
        "language": "en",
        "sample_rate": MODEL_RATE,
        "sessions": _active,
        "max_sessions": MAX_SESSIONS,
        "min_confidence": MIN_CONFIDENCE,
        "uptime_s": round(time.time() - _started_at),
    }


def process_request(connection: ServerConnection, request: Request) -> Response | None:
    path = urlsplit(request.path).path.rstrip("/") or "/"
    if path == "/health":
        return _json_response(connection, HTTPStatus.OK, _health())
    if path != "/v1/listen":
        return _json_response(connection, HTTPStatus.NOT_FOUND, {"err_msg": "Not found"})
    if API_KEY is not None:
        auth = request.headers.get("Authorization", "")
        if auth not in (f"Token {API_KEY}", f"Bearer {API_KEY}"):
            return _json_response(
                connection,
                HTTPStatus.UNAUTHORIZED,
                {"err_code": "INVALID_AUTH", "err_msg": "Invalid credentials."},
            )
    try:
        Options.from_path(request.path)
    except BadRequestError as exc:
        return _json_response(
            connection, HTTPStatus.BAD_REQUEST, {"err_code": "Bad Request", "err_msg": str(exc)}
        )
    if _engine is None:
        return _json_response(
            connection, HTTPStatus.SERVICE_UNAVAILABLE, {"err_msg": "Model is loading"}
        )
    if _active >= MAX_SESSIONS:
        return _json_response(
            connection,
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"err_msg": f"All {MAX_SESSIONS} sessions are busy"},
        )
    return None


async def _send(ws: ServerConnection, events: list[dict[str, Any]]) -> None:
    for event in events:
        await ws.send(json.dumps(event, ensure_ascii=False))


async def _decode_worker(ws: ServerConnection, session: Session, queue: asyncio.Queue[Any]) -> None:
    loop = asyncio.get_running_loop()
    while True:
        item = await queue.get()
        if isinstance(item, bytes):
            # Coalesce audio that queued up while the previous chunk was decoding.
            chunks = [item]
            while not queue.empty() and isinstance(queue._queue[0], bytes):  # type: ignore[attr-defined]
                chunks.append(queue.get_nowait())
            events = await loop.run_in_executor(_pool, session.feed, b"".join(chunks))
            await _send(ws, events)
        elif item is _FINALIZE:
            events = await loop.run_in_executor(_pool, lambda: session.finalize(from_finalize=True))
            await _send(ws, events)
        elif item is _CLOSE:
            events = await loop.run_in_executor(
                _pool, lambda: session.finalize(from_finalize=False)
            )
            await _send(ws, [*events, session.metadata()])
            await ws.close(1000)
            return
        else:  # None: reader is gone
            return


async def handler(ws: ServerConnection) -> None:
    global _active
    assert ws.request is not None
    opts = Options.from_path(ws.request.path)
    _active += 1
    session = Session(opts)
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=QUEUE_LIMIT)
    worker = asyncio.create_task(_decode_worker(ws, session, queue))
    started = time.perf_counter()
    logger.info(
        "session %s started: sample_rate=%d interim=%s model=%s language=%s",
        session.request_id,
        opts.sample_rate,
        opts.interim_results,
        opts.model,
        opts.language,
    )
    reason = "worker stopped"
    try:
        while not worker.done():
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=IDLE_TIMEOUT_S)
            except TimeoutError:
                reason = "idle"
                logger.info(
                    "session %s idle for %.0fs, closing", session.request_id, IDLE_TIMEOUT_S
                )
                await ws.close(1011, "No audio or KeepAlive received")
                break
            if isinstance(message, bytes):
                await queue.put(message)
                continue
            try:
                kind = json.loads(message).get("type")
            except (ValueError, AttributeError):
                kind = None
            if kind == "KeepAlive":
                continue
            if kind == "Finalize":
                await queue.put(_FINALIZE)
            elif kind == "CloseStream":
                await queue.put(_CLOSE)
                reason = "close stream"
                break
            elif kind != "Configure":  # Configure (keyterm updates) is accepted and ignored
                await ws.send(
                    json.dumps(
                        {
                            "type": "Error",
                            "variant": "SchemaError",
                            "description": "Unknown or malformed control message",
                            "message": str(message)[:200],
                        }
                    )
                )
    except ConnectionClosed:
        reason = "disconnected"
    finally:
        # CloseStream: let the worker flush, answer and close. Otherwise nobody is
        # listening any more, so queued audio is dropped instead of decoded.
        if reason != "close stream":
            worker.cancel()
        try:
            await asyncio.wait_for(worker, timeout=CLOSE_FLUSH_TIMEOUT_S)
        except (ConnectionClosed, asyncio.CancelledError, TimeoutError):
            pass
        except Exception:
            logger.exception("session %s recognition failed", session.request_id)
            with contextlib.suppress(ConnectionClosed):
                await ws.close(1011, "Recognition failed")
        _active -= 1
        logger.info(
            "session %s ended (%s): %.1fs audio in %.1fs",
            session.request_id,
            reason,
            session.audio_s,
            time.perf_counter() - started,
        )


def _warm_up() -> None:
    """The first decode allocates ONNX buffers; do it before any caller waits."""
    assert _engine is not None
    noise = (np.random.default_rng(0).standard_normal(MODEL_RATE * 2) * 0.05).astype(np.float32)
    seg = _engine.start(MODEL_RATE)
    _engine.accept(seg, noise)
    _engine.partial(seg)
    _engine.final(seg, None)
    # The detector and the 8 kHz resampler of a session, as a phone call would use them.
    warm = Session(Options.from_path("/v1/listen?sample_rate=8000&interim_results=true"))
    warm.feed((noise[::2] * 32767).astype("<i2").tobytes())
    warm.finalize(from_finalize=True)


async def main() -> None:
    global _engine, _paths
    started = time.perf_counter()
    _paths = ensure_model(MODEL_DIR, SPEC)
    engine = _load_engine()
    _engine = engine
    _warm_up()
    logger.info(
        "ready in %.1fs: model=%s key=%s (%s, %s) threads=%d max_sessions=%d "
        "endpoint_silence=%.0fms",
        time.perf_counter() - started,
        MODEL_ID,
        SPEC.key,
        MODEL_VERSION,
        "streaming" if SPEC.kind == "online" else "whole utterance",
        NUM_THREADS,
        MAX_SESSIONS,
        ENDPOINT_SILENCE_S * 1000,
    )
    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(
            sig, lambda: stop.done() or stop.set_result(None)
        )
    async with serve(
        handler,
        "0.0.0.0",  # noqa: S104 - inside the container; compose publishes to 127.0.0.1 only
        int(os.environ.get("PORT", "8000")),
        process_request=process_request,
        max_size=MAX_MESSAGE_BYTES,
        compression=None,
        ping_interval=20,
        ping_timeout=20,
        server_header=None,
    ):
        await stop


if __name__ == "__main__":
    asyncio.run(main())
