"""OpenAI-compatible speech server for Kyutai Pocket TTS (CPU).

The voice runtime talks to self-hosted voices through the OpenAI speech API
(`POST /v1/audio/speech` with `response_format: "pcm"`, read as 24 kHz, 16-bit, mono).
Pocket TTS ships its own server (`pocket-tts serve`), but it exposes `POST /tts` with form
fields, which that client cannot call, so this small app adapts the library to the
OpenAI shape.

Speech requests name a built-in voice or a cloned voice id; never a URL or a file. The
default allow-list holds only built-in voices whose recordings are licensed for commercial
use (CC0 or CC BY 4.0); see docs/local-models.md for the per-voice table.

Voice cloning (optional) needs the gated weights of kyutai/pocket-tts: accept the terms on
Hugging Face and pass a token as HF_TOKEN. Then `POST /v1/voices` turns a 5-30 s WAV sample
of a consenting speaker into a reusable voice. Only the computed voice state (safetensors)
and a small JSON record are kept; the uploaded audio is decoded in memory and discarded.
Without the token, or when the gated weights do not load, the /v1/voices endpoints answer
501 with the setup steps. The model card forbids cloning without consent, fraud, and
passing generated speech off as a real person.

Environment:
  POCKET_TTS_LANGUAGE        model language config (default "english")
  POCKET_TTS_MODEL_ID        id clients send as `model` (default "pocket-tts")
  POCKET_TTS_DEFAULT_VOICE   voice used when a request names an OpenAI voice (default "alba")
  POCKET_TTS_VOICES          comma-separated allow-list (default: commercial-use voices)
  POCKET_TTS_API_KEY         if set, require `Authorization: Bearer <key>`
  POCKET_TTS_WARMUP          "1" (default) runs one short synthesis at startup
  POCKET_TTS_THREADS         torch CPU threads for synthesis (default: the library's own
                             choice, 1; the audio decoder runs on a second thread)
  HF_TOKEN                   Hugging Face token of an account that accepted the terms of
                             kyutai/pocket-tts; enables voice cloning (read by the library's
                             downloader; never logged)
  POCKET_TTS_VOICE_DIR       where cloned voices are kept (default /home/app/.cache/voices,
                             inside the weights volume)
  POCKET_TTS_MAX_CUSTOM_VOICES  most cloned voices kept on disk (default 50)
  POCKET_TTS_CUSTOM_VOICE_CACHE cloned voice states held in memory, least recently used
                             evicted first (default 4; each is 6-19 MB)
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
import unicodedata
import warnings
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any, Literal

import numpy as np
import scipy.io.wavfile
import torch
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pocket_tts import TTSModel, export_model_state
from pocket_tts.data.audio_utils import convert_audio
from pocket_tts.modules.conv import pad_for_conv1d
from pocket_tts.modules.stateful_module import StatefulModule
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

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
# Importing pocket_tts calls torch.set_num_threads(1) (models/tts_model.py, v3.3.0); a
# value here replaces it. torch applies it to the synthesis worker threads as well.
_threads_env = os.environ.get("POCKET_TTS_THREADS", "").strip()
THREADS: int | None = int(_threads_env) if _threads_env else None
_voices_env = os.environ.get("POCKET_TTS_VOICES", "").strip()
ALLOWED_VOICES: list[str] = (
    [v.strip() for v in _voices_env.split(",") if v.strip()]
    if _voices_env
    else [v for v, lic in VOICE_LICENSES.items() if lic in COMMERCIAL_LICENSES]
)


# ------------------------------------------------------------------ voice cloning settings

HF_TOKEN_SET = bool(os.environ.get("HF_TOKEN", "").strip())
VOICE_DIR = Path(os.environ.get("POCKET_TTS_VOICE_DIR", "/home/app/.cache/voices"))
MAX_CUSTOM_VOICES = int(os.environ.get("POCKET_TTS_MAX_CUSTOM_VOICES", "50"))
CUSTOM_VOICE_CACHE = int(os.environ.get("POCKET_TTS_CUSTOM_VOICE_CACHE", "4"))
CLONING_TERMS_URL = "https://huggingface.co/kyutai/pocket-tts"
CLONING_UNAVAILABLE = (
    f"Voice cloning needs the gated weights: accept the terms at {CLONING_TERMS_URL} "
    "and set HF_TOKEN."
)
CLONING_TOKEN_REJECTED = (
    CLONING_UNAVAILABLE + " A token is set, but the gated weights did not download: accept "
    "the terms with the account that owns the token, check that the token can read gated "
    "repositories, then restart the server."
)
DEFAULT_CONSENT_STATEMENT = "I have the speaker's permission to clone their voice."
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_SAMPLE_SECONDS = 5.0
MAX_SAMPLE_SECONDS = 30.0
# Common rates only: resampling between unrelated rates needs very long filters.
SAMPLE_RATES = frozenset(
    {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000}
)
CUSTOM_PREFIX = "custom-"
CUSTOM_ID = re.compile(r"^custom-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{16}$")
MAX_NAME_CHARS = 60
MAX_METADATA_BYTES = 16 * 1024

_model: TTSModel | None = None
_voice_states: dict[str, object] = {}
# Cloned voices: metadata of every voice on disk, and an LRU of loaded states.
_custom_voices: dict[str, dict[str, Any]] = {}
_custom_states: OrderedDict[str, object] = OrderedDict()
_cloning_enabled = False
# generate_audio_stream is not thread-safe: one synthesis at a time, others wait.
_generation_lock = threading.Lock()
_state_lock = threading.Lock()
# One voice is computed at a time (several seconds of CPU and up to ~200 MB while encoding).
_clone_lock = asyncio.Lock()


class SampleRejectedError(ValueError):
    """The uploaded sample is not usable; the message is safe to show to the user."""


class UnsupportedMediaError(SampleRejectedError):
    """The upload is not a WAV file."""


def _voice_state(name: str) -> object:
    if name.startswith(CUSTOM_PREFIX):
        return _custom_state(name)
    with _state_lock:
        state = _voice_states.get(name)
        if state is None:
            assert _model is not None
            state = _model.get_state_for_audio_prompt(name)
            _voice_states[name] = state
        return state


def _voice_file(voice_id: str) -> Path:
    # Ids are validated against CUSTOM_ID before they reach a path.
    return VOICE_DIR / f"{voice_id}.safetensors"


def _custom_state(voice_id: str) -> object:
    with _state_lock:
        state = _custom_states.get(voice_id)
        if state is not None:
            _custom_states.move_to_end(voice_id)
            return state
        if voice_id not in _custom_voices:
            raise KeyError(voice_id)
        assert _model is not None
        # A .safetensors path loads the saved state (no code is executed, unlike pickle).
        state = _model.get_state_for_audio_prompt(_voice_file(voice_id))
        _custom_states[voice_id] = state
        while len(_custom_states) > max(CUSTOM_VOICE_CACHE, 0):
            _custom_states.popitem(last=False)
        return state


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:24].strip("-")
    return slug or "voice"


def new_voice_id(name: str) -> str:
    """`custom-<slug>-<16 hex>`: the random part makes ids unguessable."""
    return f"{CUSTOM_PREFIX}{_slug(name)}-{secrets.token_hex(8)}"


def clean_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise SampleRejectedError("Give the voice a name.")
    if len(name) > MAX_NAME_CHARS:
        raise SampleRejectedError(f"The name is longer than {MAX_NAME_CHARS} characters.")
    if any(unicodedata.category(c).startswith("C") for c in name):
        raise SampleRejectedError("The name contains control characters.")
    return name


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV file held in memory to mono float32 in [-1, 1] and its sample rate.

    Only RIFF/WAVE is accepted (PCM 8/16/24/32-bit or float), parsed by scipy; nothing in
    the upload is written to disk or executed. Browsers convert MP3, M4A and recordings to
    WAV before uploading (the image has no MP3/AAC decoder, on purpose).
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise UnsupportedMediaError(
            "Upload a WAV file (PCM). MP3, M4A and browser recordings are converted to WAV "
            "by the ATLAS web app before upload."
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rate, samples = scipy.io.wavfile.read(io.BytesIO(data), mmap=False)
    except Exception as exc:  # malformed or unsupported encodings (e.g. compressed WAV)
        raise SampleRejectedError(
            "The WAV file could not be read: use 16-bit PCM, 24-bit PCM or float WAV."
        ) from exc
    if rate not in SAMPLE_RATES:
        raise SampleRejectedError(
            f"Unsupported sample rate {rate} Hz: use 16, 22.05, 24, 44.1 or 48 kHz."
        )
    if samples.ndim == 2:
        if not 1 <= samples.shape[1] <= 8:
            raise SampleRejectedError("The WAV file has an unsupported number of channels.")
    elif samples.ndim != 1:
        raise SampleRejectedError("The WAV file could not be read.")
    seconds = samples.shape[0] / rate
    if seconds < MIN_SAMPLE_SECONDS or seconds > MAX_SAMPLE_SECONDS:
        raise SampleRejectedError(
            f"The sample is {seconds:.1f} s long: it must be between "
            f"{MIN_SAMPLE_SECONDS:.0f} and {MAX_SAMPLE_SECONDS:.0f} seconds."
        )
    kind = samples.dtype
    if kind == np.uint8:
        audio = (samples.astype(np.float32) - 128.0) / 128.0
    elif np.issubdtype(kind, np.signedinteger):
        # scipy returns 24-bit PCM left-justified in int32, so dividing by the type's range
        # is right for 16, 24 and 32 bits.
        audio = samples.astype(np.float32) / float(np.iinfo(kind).max + 1)
    elif np.issubdtype(kind, np.floating):
        audio = samples.astype(np.float32)
        if not np.isfinite(audio).all():
            raise SampleRejectedError("The WAV file contains invalid samples.")
        audio = np.clip(audio, -1.0, 1.0)
    else:
        raise SampleRejectedError("Unsupported WAV sample format.")
    if audio.ndim == 2:
        audio = audio.mean(axis=1, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak < 1e-3:
        raise SampleRejectedError("The sample is silent: record the speaker talking.")
    return np.ascontiguousarray(audio), int(rate)


def prepare_sample(data: bytes) -> tuple[torch.Tensor, float]:
    """Decode, validate and resample an upload to the model's rate: ([1, samples], seconds)."""
    assert _model is not None
    audio, rate = decode_wav(data)
    seconds = audio.shape[0] / rate
    tensor = torch.from_numpy(audio).unsqueeze(0)
    tensor = convert_audio(tensor, rate, _model.sample_rate, 1).to(torch.float32)
    return tensor.contiguous(), seconds


# Encoding a voice sample in one pass (the library's MimiModel.encode_to_latent, v3.3.0)
# builds a T x T attention mask in the encoder transformer (200 frames per second), so memory
# and time grow with the square of the length: measured on the dev CPU, a 30 s sample took
# 27 s and peaked 840 MiB above the idle server, more than the container limit allows. The
# bounded version below gives the same result (to float rounding: BoundedEncoderTest in
# test_app.py) in 8 s for 30 s of audio, without rising above the model's load peak:
# - the SEANet convolutions run on 4 s chunks, carrying their streaming state (exact: they
#   are causal and pad with zeros, which is what the initial state holds);
# - the transformer runs on windows of ENCODE_WINDOW frames with ENCODE_OVERLAP frames of
#   left context. Each layer attends to at most `context` earlier frames, so after
#   `num_layers` layers an output depends on at most num_layers * context earlier inputs;
#   rotary position embeddings only depend on distances, so the window's start offset
#   does not change the result.
ENCODE_CHUNK_FRAMES = 50  # 50 x 1920 samples = 4 s at 24 kHz per SEANet pass
ENCODE_WINDOW = 800  # encoder-transformer frames per window (4 s at 200 frames/s)


def encode_to_latent_bounded(mimi: Any, x: torch.Tensor) -> torch.Tensor:
    """`mimi.encode_to_latent(x)` with memory and time linear in the sample length."""
    assert x.dim() == 3, f"expected audio of shape [B, C, T], got {tuple(x.shape)}"
    frame_size = mimi.frame_size
    x = pad_for_conv1d(x, frame_size, frame_size)
    state = {
        m._module_absolute_name: m.init_state(x.shape[0], 0)
        for m in mimi.encoder.modules()
        if isinstance(m, StatefulModule)
    }
    chunk = frame_size * ENCODE_CHUNK_FRAMES
    emb = torch.cat(
        [mimi.encoder(x[..., i : i + chunk], model_state=state) for i in range(0, x.shape[-1], chunk)],
        dim=-1,
    )
    projected = mimi.encoder_transformer
    transformer = projected.transformer
    overlap = int(transformer.context or emb.shape[-1]) * len(transformer.layers)
    outputs = []
    for start in range(0, emb.shape[-1], ENCODE_WINDOW):
        lo = max(0, start - overlap)
        (window,) = projected(emb[..., lo : start + ENCODE_WINDOW], model_state=None)
        outputs.append(window[..., start - lo :])
    emb = mimi._to_framerate(torch.cat(outputs, dim=-1))
    return emb.transpose(-1, -2)


def use_bounded_encoder(model: Any) -> None:
    """Route the model's voice-prompt encoding through encode_to_latent_bounded."""
    mimi = model.mimi
    mimi.encode_to_latent = lambda x: encode_to_latent_bounded(mimi, x)


def _write_atomic(path: Path, write: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def create_voice(name: str, audio: torch.Tensor, seconds: float, consent: str) -> dict[str, Any]:
    """Compute the voice state once and keep it (blocking; run in a worker thread)."""
    assert _model is not None
    voice_id = new_voice_id(name)
    started = time.perf_counter()
    with torch.no_grad():
        state = _model.get_state_for_audio_prompt(audio)
    VOICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "id": voice_id,
        "name": name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sample_seconds": round(seconds, 2),
        "language": LANGUAGE,
        "consent": {"attested": True, "statement": consent},
    }
    _write_atomic(_voice_file(voice_id), lambda p: export_model_state(state, p))
    _write_atomic(
        VOICE_DIR / f"{voice_id}.json",
        lambda p: p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"),
    )
    with _state_lock:
        _custom_voices[voice_id] = meta
        _custom_states[voice_id] = state
        while len(_custom_states) > max(CUSTOM_VOICE_CACHE, 0):
            _custom_states.popitem(last=False)
    logger.info(
        "Cloned voice %s from %.1f s of audio in %.1fs",
        voice_id, seconds, time.perf_counter() - started,
    )
    return meta


def delete_voice(voice_id: str) -> bool:
    with _state_lock:
        known = _custom_voices.pop(voice_id, None) is not None
        _custom_states.pop(voice_id, None)
    for path in (_voice_file(voice_id), VOICE_DIR / f"{voice_id}.json"):
        if path.exists():
            known = True
            path.unlink()
    return known


def load_voice_index() -> None:
    """Read the metadata of every cloned voice on disk (the states load on first use)."""
    _custom_voices.clear()
    _custom_states.clear()
    if not VOICE_DIR.is_dir():
        return
    for meta_path in sorted(VOICE_DIR.glob("custom-*.json")):
        voice_id = meta_path.stem
        if not CUSTOM_ID.match(voice_id) or not _voice_file(voice_id).is_file():
            continue
        try:
            if meta_path.stat().st_size > MAX_METADATA_BYTES:
                raise ValueError("metadata too large")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping cloned voice %s: unreadable metadata (%s)", voice_id, exc)
            continue
        if not isinstance(meta, dict) or meta.get("id") != voice_id:
            logger.warning("Skipping cloned voice %s: metadata does not match", voice_id)
            continue
        if meta.get("language") != LANGUAGE:
            logger.warning(
                "Skipping cloned voice %s: made with the %s model, this server runs %s",
                voice_id, meta.get("language"), LANGUAGE,
            )
            continue
        _custom_voices[voice_id] = meta


def preload_custom_states() -> None:
    newest = sorted(_custom_voices.values(), key=lambda m: str(m.get("created_at")), reverse=True)
    for meta in reversed(newest[: max(CUSTOM_VOICE_CACHE, 0)]):
        try:
            _custom_state(meta["id"])
        except Exception as exc:
            logger.warning("Cloned voice %s could not be loaded: %s", meta["id"], exc)
            with _state_lock:
                _custom_voices.pop(meta["id"], None)


def _cloning_message() -> str:
    return CLONING_TOKEN_REJECTED if HF_TOKEN_SET else CLONING_UNAVAILABLE


def _require_cloning() -> None:
    if _model is None:
        raise HTTPException(status_code=503, detail="Model is still loading")
    if not _cloning_enabled:
        raise HTTPException(status_code=501, detail=_cloning_message())


def _voice_out(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "type": "custom",
        "created_at": meta.get("created_at"),
        "sample_seconds": meta.get("sample_seconds"),
    }


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
    global _model, _cloning_enabled
    unknown = [v for v in ALLOWED_VOICES if v not in VOICE_LICENSES]
    if unknown:
        raise RuntimeError(f"Unknown voices in POCKET_TTS_VOICES: {unknown}")
    for v in ALLOWED_VOICES:
        if VOICE_LICENSES[v] not in COMMERCIAL_LICENSES:
            logger.warning("Voice %s is licensed %s: check before commercial use", v, VOICE_LICENSES[v])
    if DEFAULT_VOICE not in ALLOWED_VOICES:
        raise RuntimeError(f"POCKET_TTS_DEFAULT_VOICE={DEFAULT_VOICE} is not in the allow-list")
    if THREADS is not None:
        if THREADS < 1:
            raise RuntimeError(f"POCKET_TTS_THREADS must be at least 1, got {THREADS}")
        torch.set_num_threads(THREADS)
    started = time.perf_counter()
    # The library tries the gated kyutai/pocket-tts weights first (voice cloning; needs
    # HF_TOKEN of an account that accepted the terms) and otherwise falls back to the ungated
    # kyutai/pocket-tts-without-voice-cloning weights (CC BY 4.0), which built-in voices need.
    _model = TTSModel.load_model(language=LANGUAGE)
    _cloning_enabled = HF_TOKEN_SET and bool(getattr(_model, "has_voice_cloning", False))
    if _cloning_enabled:
        use_bounded_encoder(_model)
        load_voice_index()
        preload_custom_states()
        logger.info("Voice cloning enabled: %d cloned voice(s) on disk", len(_custom_voices))
    else:
        logger.info("Voice cloning off: %s", _cloning_message())
    _voice_state(DEFAULT_VOICE)
    if WARMUP:
        for _ in _pcm_chunks("Hello there.", DEFAULT_VOICE, threading.Event()):
            pass
    logger.info(
        "Pocket TTS ready in %.1fs (language=%s, torch threads=%d)",
        time.perf_counter() - started, LANGUAGE, torch.get_num_threads(),
    )
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


# ------------------------------------------------------------------ voice cloning endpoints


@app.get("/v1/voices")
async def list_all_voices(authorization: str | None = Header(default=None)) -> dict:
    """Built-in and cloned voices (501 when voice cloning is not set up)."""
    _check_auth(authorization)
    _require_cloning()
    with _state_lock:
        custom = sorted(_custom_voices.values(), key=lambda m: str(m.get("created_at")))
    return {
        "object": "list",
        "data": [
            *[
                {"id": v, "name": v, "type": "builtin", "license": VOICE_LICENSES[v],
                 "default": v == DEFAULT_VOICE}
                for v in ALLOWED_VOICES
            ],
            *[_voice_out(m) for m in custom],
        ],
    }


@app.post("/v1/voices", status_code=201)
async def create_custom_voice(
    request: Request, authorization: str | None = Header(default=None)
) -> dict:
    """Clone a voice from a 5-30 s WAV sample of a speaker who gave permission.

    Multipart fields: `name`, `audio` (WAV, at most 10 MB), `consent` ("true"), and an
    optional `consent_statement` kept with the voice.
    """
    _check_auth(authorization)
    _require_cloning()
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length is required")
    if int(length) > MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(status_code=413, detail="The sample is larger than 10 MB.")
    if len(_custom_voices) >= MAX_CUSTOM_VOICES:
        raise HTTPException(
            status_code=409,
            detail=f"This server already keeps {MAX_CUSTOM_VOICES} cloned voices: delete one first.",
        )
    try:
        async with request.form(max_files=1, max_fields=4, max_part_size=4096) as form:
            consent = str(form.get("consent") or "").strip().lower()
            if consent != "true":
                raise HTTPException(
                    status_code=422,
                    detail="Consent is required: confirm you have the speaker's permission "
                    "to clone their voice (consent=true).",
                )
            statement = " ".join(str(form.get("consent_statement") or "").split())[:500]
            name = clean_name(str(form.get("name") or ""))
            upload = form.get("audio")
            if not isinstance(upload, UploadFile):
                raise SampleRejectedError("Attach the voice sample as the `audio` file field.")
            data = await upload.read(MAX_UPLOAD_BYTES + 1)
    except SampleRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception as exc:  # malformed multipart bodies
        raise HTTPException(status_code=400, detail="The upload could not be read.") from exc
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The sample is larger than 10 MB.")
    if _clone_lock.locked():
        raise HTTPException(
            status_code=429, detail="Another voice is being created. Try again in a moment."
        )
    async with _clone_lock:
        if len(_custom_voices) >= MAX_CUSTOM_VOICES:
            raise HTTPException(
                status_code=409,
                detail=f"This server already keeps {MAX_CUSTOM_VOICES} cloned voices: delete one first.",
            )
        try:
            audio, seconds = await asyncio.to_thread(prepare_sample, data)
        except UnsupportedMediaError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from None
        except SampleRejectedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        meta = await asyncio.to_thread(
            create_voice, name, audio, seconds, statement or DEFAULT_CONSENT_STATEMENT
        )
    return _voice_out(meta)


@app.delete("/v1/voices/{voice_id}", status_code=204)
async def delete_custom_voice(voice_id: str, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    _require_cloning()
    if not CUSTOM_ID.match(voice_id):
        raise HTTPException(status_code=404, detail="Cloned voice not found")
    if not await asyncio.to_thread(delete_voice, voice_id):
        raise HTTPException(status_code=404, detail="Cloned voice not found")
    return None


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
    if voice.startswith(CUSTOM_PREFIX):
        if not _cloning_enabled:
            raise HTTPException(
                status_code=422,
                detail=f"Voice '{voice}' is a cloned voice, which this server cannot speak "
                f"now. {_cloning_message()}",
            )
        if not CUSTOM_ID.match(voice) or voice not in _custom_voices:
            raise HTTPException(status_code=422, detail=f"Voice '{voice}' is not available.")
    elif voice not in ALLOWED_VOICES:
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
