"""Streaming speech-to-text server that speaks the Deepgram v1 live-listen protocol (CPU).

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

The model is a streaming (online) Zipformer transducer run by sherpa-onnx, one
recognizer stream per connection. Audio at other rates (8 kHz phone audio) is resampled
to the model's 16 kHz by sherpa-onnx itself.

Environment (defaults in brackets):
  STT_MODEL_DIR [/models]          where fetch_model.py stores the model
  STT_MODEL_ID [zipformer-en-streaming]  name reported in results and /health
  STT_NUM_THREADS [2]              CPU threads per decode
  STT_MAX_SESSIONS [4]             concurrent connections; more get HTTP 503
  STT_ENDPOINT_SILENCE_MS [400]    silence after speech that ends a segment
  STT_MAX_UTTERANCE_S [20]         a segment is finalised after this long regardless
  STT_FINALIZE_PAD_MS [800]        silence appended on Finalize to flush the last words
  STT_IDLE_TIMEOUT_S [30]          close a connection that sends nothing for this long
  STT_STREAM_RECYCLE_S [300]       start a fresh recognizer stream (between utterances)
                                   after this much audio, so memory stays bounded
  STT_API_KEY [unset]              if set, require "Token <key>" or "Bearer <key>"
  LOG_LEVEL [INFO]                 transcripts are never logged at INFO
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np
import sherpa_onnx
from fetch_model import FILES, REPO, REVISION, ensure_model
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


MODEL_DIR = Path(os.environ.get("STT_MODEL_DIR", "/models"))
MODEL_ID = os.environ.get("STT_MODEL_ID", "zipformer-en-streaming")
MODEL_VERSION = f"{REPO.split('/')[-1]}@{REVISION[:8]}"
MODEL_UUID = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://huggingface.co/{REPO}@{REVISION}"))
MODEL_RATE = 16000
NUM_THREADS = int(os.environ.get("STT_NUM_THREADS", "2"))
MAX_SESSIONS = int(os.environ.get("STT_MAX_SESSIONS", "4"))
ENDPOINT_SILENCE_S = _env_float("STT_ENDPOINT_SILENCE_MS", 400) / 1000
MAX_UTTERANCE_S = _env_float("STT_MAX_UTTERANCE_S", 20)
FINALIZE_PAD_S = _env_float("STT_FINALIZE_PAD_MS", 800) / 1000
IDLE_TIMEOUT_S = _env_float("STT_IDLE_TIMEOUT_S", 30)
STREAM_RECYCLE_S = _env_float("STT_STREAM_RECYCLE_S", 300)
API_KEY = os.environ.get("STT_API_KEY") or None
# 64 KiB per frame (2 s at 16 kHz); the voice service sends 20 ms frames. With QUEUE_LIMIT
# this bounds buffered audio per connection to 16 MiB.
MAX_MESSAGE_BYTES = 64 * 1024
FEED_SLICE_S = 0.16  # half a model chunk: end-of-speech is checked at least this often
QUEUE_LIMIT = 256  # frames buffered per connection before the reader waits (backpressure)
# CloseStream decodes everything still queued; a full queue is about 8.5 min of 16 kHz audio,
# about 45 s of work on the reference CPU.
CLOSE_FLUSH_TIMEOUT_S = 60.0

_recognizer: sherpa_onnx.OnlineRecognizer | None = None
_pool = ThreadPoolExecutor(max_workers=MAX_SESSIONS, thread_name_prefix="decode")
_active = 0
_started_at = time.time()


def _load_recognizer() -> sherpa_onnx.OnlineRecognizer:
    names = list(FILES)
    encoder, decoder, joiner, tokens = (str(MODEL_DIR / n) for n in names)
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=tokens,
        encoder=encoder,
        decoder=decoder,
        joiner=joiner,
        num_threads=NUM_THREADS,
        sample_rate=MODEL_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        enable_endpoint_detection=True,
        # rule1: nothing recognised and a long silence -> reset quietly (bounds memory).
        rule1_min_trailing_silence=2.4,
        # rule2: silence after words -> end of segment (final, speech_final).
        rule2_min_trailing_silence=ENDPOINT_SILENCE_S,
        # rule3: very long segment -> finalise anyway.
        rule3_min_utterance_length=MAX_UTTERANCE_S,
        provider="cpu",
    )


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


# ------------------------------------------------------------------ one connection


def _format(raw: str) -> str:
    """Model output is upper case without punctuation; make it read like a sentence."""
    words = raw.strip().lower().split()
    words = ["I" if w == "i" else ("I" + w[1:] if w.startswith("i'") else w) for w in words]
    if words:
        words[0] = words[0][:1].upper() + words[0][1:]
    return " ".join(words)


class Session:
    """Recognizer state for one websocket. All methods run in the decode pool, in order."""

    def __init__(self, opts: Options) -> None:
        assert _recognizer is not None
        self.rec = _recognizer
        self.opts = opts
        self.request_id = str(uuid.uuid4())
        self.stream = self.rec.create_stream()
        self.samples = 0  # input samples received
        self.stream_started_s = 0.0
        self.seg_start_s = 0.0
        self.last_text = ""
        self.last_word_s: float | None = None  # audio time when the words last changed
        self.speech_started = False
        self.utterance_end_due = False
        self.sha = hashlib.sha256()
        self.leftover = b""

    @property
    def audio_s(self) -> float:
        return self.samples / self.opts.sample_rate

    # -------------------------------------------------------------- messages

    def _words(self, text: str, result: dict[str, Any]) -> list[dict[str, Any]]:
        words = text.split()
        if not words:
            return []
        start_times: list[float] = []
        tokens, stamps = result.get("tokens") or [], result.get("timestamps") or []
        for token, ts in zip(tokens, stamps, strict=False):
            if token.startswith(("▁", " ")) or not start_times:
                start_times.append(float(ts))
        seg_end = max(self.audio_s - self.seg_start_s, 0.0)
        if len(start_times) != len(words):  # fall back to an even split of the segment
            step = seg_end / max(len(words), 1)
            start_times = [i * step for i in range(len(words))]
        probs = result.get("ys_probs") or []
        conf = float(np.exp(np.mean(probs))) if probs else 1.0
        out = []
        for i, word in enumerate(words):
            start = self.seg_start_s + start_times[i]
            end = self.seg_start_s + (start_times[i + 1] if i + 1 < len(words) else seg_end)
            out.append(
                {
                    "word": word.lower(),
                    "start": round(start, 3),
                    "end": round(max(end, start), 3),
                    "confidence": round(conf, 4),
                    "punctuated_word": word,
                }
            )
        return out

    def _results(
        self,
        text: str,
        result: dict[str, Any],
        *,
        is_final: bool,
        speech_final: bool = False,
        from_finalize: bool = False,
    ) -> dict[str, Any]:
        probs = result.get("ys_probs") or []
        return {
            "type": "Results",
            "channel_index": [0, 1],
            "duration": round(max(self.audio_s - self.seg_start_s, 0.0), 3),
            "start": round(self.seg_start_s, 3),
            "is_final": is_final,
            "speech_final": speech_final,
            "from_finalize": from_finalize,
            "channel": {
                "alternatives": [
                    {
                        "transcript": text,
                        "confidence": round(float(np.exp(np.mean(probs))), 4) if probs else 1.0,
                        "words": self._words(text, result),
                    }
                ]
            },
            "metadata": {
                "request_id": self.request_id,
                "model_info": {"name": MODEL_ID, "version": MODEL_VERSION, "arch": "zipformer"},
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
                MODEL_UUID: {"name": MODEL_ID, "version": MODEL_VERSION, "arch": "zipformer"}
            },
        }

    # -------------------------------------------------------------- decoding

    def _result(self) -> dict[str, Any]:
        return json.loads(self.rec.get_result_as_json_string(self.stream))

    def _reset(self) -> None:
        self.rec.reset(self.stream)
        self.seg_start_s = self.audio_s
        self.last_text = ""
        self.speech_started = False
        if self.audio_s - self.stream_started_s >= STREAM_RECYCLE_S:
            # A stream keeps its feature frames; a new one between utterances bounds memory.
            self.stream = self.rec.create_stream()
            self.stream_started_s = self.audio_s

    def _final(self, *, speech_final: bool, from_finalize: bool) -> dict[str, Any]:
        result = self._result()
        text = _format(result.get("text", ""))
        msg = self._results(
            text, result, is_final=True, speech_final=speech_final, from_finalize=from_finalize
        )
        if text:
            self.utterance_end_due = self.opts.utterance_end_s is not None
        self._reset()
        return msg

    def _decode(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)
        result = self._result()
        text = _format(result.get("text", ""))
        if text != self.last_text:
            self.last_text = text
            if text:
                self.last_word_s = self.audio_s
                self.utterance_end_due = False
                if self.opts.vad_events and not self.speech_started:
                    self.speech_started = True
                    events.append(
                        {
                            "type": "SpeechStarted",
                            "channel": [0, 1],
                            "timestamp": round(self.seg_start_s, 3),
                        }
                    )
                if self.opts.interim_results:
                    events.append(self._results(text, result, is_final=False))
        if self.rec.is_endpoint(self.stream):
            if text and self.opts.endpointing:
                events.append(self._final(speech_final=True, from_finalize=False))
            elif not text:
                self._reset()  # long silence with nothing said: drop decoder state
            elif self.audio_s - self.seg_start_s >= MAX_UTTERANCE_S:
                # endpointing=false: only the length cap ends a segment besides Finalize.
                events.append(self._final(speech_final=False, from_finalize=False))
        if (
            self.utterance_end_due
            and self.last_word_s is not None
            and self.opts.utterance_end_s is not None
            and self.audio_s - self.last_word_s >= self.opts.utterance_end_s
            and not self.last_text
        ):
            self.utterance_end_due = False
            events.append(
                {
                    "type": "UtteranceEnd",
                    "channel": [0, 1],
                    "last_word_end": round(self.last_word_s, 3),
                }
            )
        return events

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self.sha.update(data)
        data = self.leftover + data
        cut = len(data) - (len(data) % 2)
        self.leftover = data[cut:]
        if not cut:
            return []
        pcm = np.frombuffer(data[:cut], dtype="<i2").astype(np.float32) / 32768.0
        # Audio that queued up while decoding arrives here in one piece. Feed it in short
        # slices so a pause inside it still ends its segment (end-of-speech is checked
        # after each slice) instead of two utterances running together.
        events: list[dict[str, Any]] = []
        step = max(int(self.opts.sample_rate * FEED_SLICE_S), 1)
        for start in range(0, pcm.size, step):
            part = pcm[start : start + step]
            self.samples += part.size
            self.stream.accept_waveform(self.opts.sample_rate, part)
            events.extend(self._decode())
        return events

    def finalize(self, *, from_finalize: bool) -> list[dict[str, Any]]:
        """Flush buffered audio through the model and emit what was said as final."""
        # Silence pushes the last real frames through the encoder; same rate as the
        # caller's audio, since sherpa-onnx refuses a rate change mid-stream.
        pad = np.zeros(int(self.opts.sample_rate * FINALIZE_PAD_S), dtype=np.float32)
        self.stream.accept_waveform(self.opts.sample_rate, pad)
        while self.rec.is_ready(self.stream):
            self.rec.decode_stream(self.stream)
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
        "status": "ok" if _recognizer is not None else "loading",
        "model": MODEL_ID,
        "model_version": MODEL_VERSION,
        "language": "en",
        "sample_rate": MODEL_RATE,
        "sessions": _active,
        "max_sessions": MAX_SESSIONS,
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
    if _recognizer is None:
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


async def main() -> None:
    global _recognizer
    started = time.perf_counter()
    ensure_model(MODEL_DIR)
    _recognizer = _load_recognizer()
    # Warm-up: the first decode allocates ONNX buffers; do it before any caller waits.
    warm = Session(Options.from_path("/v1/listen"))
    warm.feed(np.zeros(MODEL_RATE, dtype="<i2").tobytes())
    warm.finalize(from_finalize=True)
    logger.info(
        "ready in %.1fs: model=%s (%s) threads=%d max_sessions=%d endpoint_silence=%.0fms",
        time.perf_counter() - started,
        MODEL_ID,
        MODEL_VERSION,
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
