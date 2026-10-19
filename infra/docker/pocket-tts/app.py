"""OpenAI-compatible speech server for Kyutai Pocket TTS (CPU).

The voice runtime talks to self-hosted voices through the OpenAI speech API
(`POST /v1/audio/speech` with `response_format: "pcm"`, read as 24 kHz, 16-bit, mono).
Pocket TTS ships its own server (`pocket-tts serve`), but it exposes `POST /tts` with form
fields, which that client cannot call, so this small app adapts the library to the
OpenAI shape.

Only built-in voice names are accepted (no uploads, no URLs), so the server never clones a
voice. The default allow-list holds only voices whose recordings are licensed for
commercial use (CC0 or CC BY 4.0); see docs/local-models.md for the per-voice table.

Environment:
  POCKET_TTS_LANGUAGE        model language config (default "english")
  POCKET_TTS_MODEL_ID        id clients send as `model` (default "pocket-tts")
  POCKET_TTS_DEFAULT_VOICE   voice used when a request names an OpenAI voice (default "alba")
  POCKET_TTS_VOICES          comma-separated allow-list (default: commercial-use voices)
  POCKET_TTS_API_KEY         if set, require `Authorization: Bearer <key>`
  POCKET_TTS_WARMUP          "1" (default) runs one short synthesis at startup
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from queue import Queue
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pocket_tts import TTSModel
from pydantic import BaseModel, Field

logger = logging.getLogger("pocket_tts_server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

# Recording licenses of the built-in voices, from https://huggingface.co/kyutai/tts-voices
# (README) and the voice sources in pocket_tts/utils/utils.py (v3.3.0).
VOICE_LICENSES: dict[str, str] = {
    "alba": "CC-BY-4.0",  # alba-mackenna/casual.wav
    "marius": "CC0-1.0",  # voice-donations
    "javert": "CC0-1.0",  # voice-donations
    "anna": "CC-BY-4.0",  # VCTK
    "vera": "CC-BY-4.0",
    "fantine": "CC-BY-4.0",
    "charles": "CC-BY-4.0",
    "paul": "CC-BY-4.0",
    "eponine": "CC-BY-4.0",
    "azelma": "CC-BY-4.0",
    "george": "CC-BY-4.0",
    "mary": "CC-BY-4.0",
    "jane": "CC-BY-4.0",
    "michael": "CC-BY-4.0",
    "eve": "CC-BY-4.0",
    "bill_boerst": "CC0-1.0",  # voice-zero (LibriVox)
    "peter_yearsley": "CC0-1.0",
    "stuart_bell": "CC0-1.0",
    "caro_davy": "CC0-1.0",
    "estelle": "CC0-1.0",  # unmute-prod-website, Kyutai's own recording
    # Non-commercial only: excluded from the default allow-list.
    "cosette": "CC-BY-NC-4.0",  # Expresso
    "jean": "CC-BY-NC-4.0",  # EARS
    # Source license not documented by Kyutai: excluded until verified.
    "giovanni": "unverified",
    "lola": "unverified",
    "juergen": "unverified",
    "rafael": "unverified",
    "daan": "unverified",
}
COMMERCIAL_LICENSES = {"CC0-1.0", "CC-BY-4.0"}
OPENAI_VOICE_NAMES = {
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
}

LANGUAGE = os.environ.get("POCKET_TTS_LANGUAGE", "english")
MODEL_ID = os.environ.get("POCKET_TTS_MODEL_ID", "pocket-tts")
DEFAULT_VOICE = os.environ.get("POCKET_TTS_DEFAULT_VOICE", "alba")
API_KEY = os.environ.get("POCKET_TTS_API_KEY") or None
WARMUP = os.environ.get("POCKET_TTS_WARMUP", "1") == "1"
_voices_env = os.environ.get("POCKET_TTS_VOICES", "").strip()
ALLOWED_VOICES: list[str] = (
    [v.strip() for v in _voices_env.split(",") if v.strip()]
    if _voices_env
    else [v for v, lic in VOICE_LICENSES.items() if lic in COMMERCIAL_LICENSES]
)

_model: TTSModel | None = None
_voice_states: dict[str, object] = {}
# generate_audio_stream is not thread-safe: one synthesis at a time, others wait.
_generation_lock = threading.Lock()
_state_lock = threading.Lock()


def _voice_state(name: str) -> object:
    with _state_lock:
        state = _voice_states.get(name)
        if state is None:
            assert _model is not None
            state = _model.get_state_for_audio_prompt(name)
            _voice_states[name] = state
        return state


def _pcm_chunks(text: str, voice: str, stop: threading.Event) -> Iterator[bytes]:
    assert _model is not None
    state = _voice_state(voice)
    with _generation_lock:
        for chunk in _model.generate_audio_stream(
            model_state=state, text_to_generate=text, stop=stop
        ):
            yield (chunk.clamp(-1, 1) * 32767).short().cpu().numpy().tobytes()
            if stop.is_set():
                break


def _wav_header(sample_rate: int) -> bytes:
    # Streaming WAV: sizes unknown up front, so use the maximum (common for live streams).
    data_size = 0xFFFFFFFF - 36
    return (
        b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", data_size)
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _model
    unknown = [v for v in ALLOWED_VOICES if v not in VOICE_LICENSES]
    if unknown:
        raise RuntimeError(f"Unknown voices in POCKET_TTS_VOICES: {unknown}")
    for v in ALLOWED_VOICES:
        if VOICE_LICENSES[v] not in COMMERCIAL_LICENSES:
            logger.warning("Voice %s is licensed %s: check before commercial use", v, VOICE_LICENSES[v])
    if DEFAULT_VOICE not in ALLOWED_VOICES:
        raise RuntimeError(f"POCKET_TTS_DEFAULT_VOICE={DEFAULT_VOICE} is not in the allow-list")
    started = time.perf_counter()
    # Without a Hugging Face token the library falls back to the ungated
    # kyutai/pocket-tts-without-voice-cloning weights (CC BY 4.0), which is all this server needs.
    _model = TTSModel.load_model(language=LANGUAGE)
    _voice_state(DEFAULT_VOICE)
    if WARMUP:
        for _ in _pcm_chunks("Hello there.", DEFAULT_VOICE, threading.Event()):
            pass
    logger.info("Pocket TTS ready in %.1fs (language=%s)", time.perf_counter() - started, LANGUAGE)
    yield


app = FastAPI(title="Pocket TTS (OpenAI-compatible)", version="1.0.0", lifespan=lifespan)


def _check_auth(authorization: str | None) -> None:
    if API_KEY is None:
        return
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="Invalid API key")


class SpeechRequest(BaseModel):
    model: str
    input: str = Field(min_length=1, max_length=4096)
    voice: str
    response_format: Literal["pcm", "wav"] = "pcm"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    instructions: str | None = None
    sample_rate: int | None = None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok" if _model is not None else "loading"}


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ID, "object": "model", "created": 0, "owned_by": "kyutai",
            "language": LANGUAGE, "sample_rate": _model.sample_rate if _model else 24000,
            "voices": ALLOWED_VOICES,
        }],
    }


@app.get("/v1/audio/voices")
async def list_voices(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [{"id": v, "license": VOICE_LICENSES[v], "default": v == DEFAULT_VOICE}
                 for v in ALLOWED_VOICES],
    }


@app.post("/v1/audio/speech")
async def speech(
    body: SpeechRequest, request: Request, authorization: str | None = Header(default=None)
) -> StreamingResponse:
    _check_auth(authorization)
    if _model is None:
        raise HTTPException(status_code=503, detail="Model is still loading")
    if body.model != MODEL_ID:
        raise HTTPException(status_code=404, detail=f"Model '{body.model}' not found; use '{MODEL_ID}'")
    sample_rate = _model.sample_rate
    if body.sample_rate not in (None, sample_rate):
        raise HTTPException(status_code=422, detail=f"Only sample_rate {sample_rate} is supported")
    voice = body.voice
    if voice in OPENAI_VOICE_NAMES:
        voice = DEFAULT_VOICE
    if voice not in ALLOWED_VOICES:
        raise HTTPException(
            status_code=422, detail=f"Voice '{body.voice}' is not available. Voices: {ALLOWED_VOICES}"
        )
    if body.speed != 1.0:
        logger.info("Pocket TTS has no speed control; ignoring speed=%s", body.speed)
    text = body.input.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Input text is empty")

    queue: Queue[bytes | BaseException | None] = Queue(maxsize=64)
    stop = threading.Event()

    def worker() -> None:
        try:
            for pcm in _pcm_chunks(text, voice, stop):
                queue.put(pcm)
        except BaseException as exc:  # surfaced to the response stream below
            queue.put(exc)
        finally:
            queue.put(None)

    threading.Thread(target=worker, daemon=True).start()
    loop = asyncio.get_running_loop()

    async def stream() -> AsyncIterator[bytes]:
        try:
            if body.response_format == "wav":
                yield _wav_header(sample_rate)
            while True:
                item = await loop.run_in_executor(None, queue.get)
                if item is None:
                    break
                if isinstance(item, BaseException):
                    logger.error("Synthesis failed: %s", item)
                    break
                yield item
                if await request.is_disconnected():
                    break
        finally:
            # Client gone or done: stop generating, and drain so the worker can exit.
            stop.set()
            while not queue.empty():
                queue.get_nowait()

    media_type = "audio/wav" if body.response_format == "wav" else "audio/pcm"
    return StreamingResponse(stream(), media_type=media_type)
