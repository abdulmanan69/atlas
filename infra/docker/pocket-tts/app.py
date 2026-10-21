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
Hugging Face, then send a token of that account to `PUT /v1/cloning` (the ATLAS web app does
this for organization owners; no restart) or pass it as HF_TOKEN. Then `POST /v1/voices`
turns a 5-30 s WAV sample of a consenting speaker into a reusable voice. Only the computed
voice state (safetensors) and a small JSON record are kept; the uploaded audio is decoded in
memory and discarded. Without a token, or when the gated weights do not load, the
/v1/voices endpoints answer 501 with the setup steps. The model card forbids cloning
without consent, fraud, and passing generated speech off as a real person.

Endpoints (all but /health need POCKET_TTS_API_KEY when it is set):
  GET    /health                 "ok" once the model is loaded
  GET    /v1/models, /v1/audio/voices  the model id and the allowed built-in voices
  POST   /v1/audio/speech        OpenAI speech API: 24 kHz 16-bit mono PCM or WAV, streamed
  GET    /v1/audio/phrase-cache  phrase cache counts (never the texts)
  GET    /v1/cloning             cloning state: off, enabling, on or failed, a message, and
                                 where the token comes from (never the token)
  PUT    /v1/cloning             {"hf_token": "hf_..."}: checked with Hugging Face, saved,
                                 then the gated weights load in the background (202)
  DELETE /v1/cloning             removes the saved token: cloning off at once (204)
  GET    /v1/voices, POST /v1/voices, DELETE /v1/voices/{id}  cloned voices (501 when off)

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
                             downloader; never logged). Without it, the token saved by
                             PUT /v1/cloning is used: $HF_HOME/token (mode 0600, in the
                             weights volume), read the same way at every start
  POCKET_TTS_VOICE_DIR       where cloned voices are kept (default /home/app/.cache/voices,
                             inside the weights volume)
  POCKET_TTS_MAX_CUSTOM_VOICES  most cloned voices kept on disk (default 50)
  POCKET_TTS_CUSTOM_VOICE_CACHE cloned voice states held in memory, least recently used
                             evicted first (default 4; each is 6-19 MB)
  POCKET_TTS_PHRASE_CACHE    "|"-separated phrases synthesised at startup and kept, so a
                             reply that opens with one of them starts at once (default: the
                             demo agent's openers, DEFAULT_CACHED_PHRASES; "" = none)
  POCKET_TTS_PHRASE_CACHE_VOICES  comma-separated voices those phrases are made for
                             (default: POCKET_TTS_DEFAULT_VOICE)
  POCKET_TTS_PHRASE_CACHE_MAX_CHARS  other requests up to this many characters are kept
                             too once fully spoken, least recently used evicted first
                             (default 32; 0 = only the startup phrases)
  POCKET_TTS_PHRASE_CACHE_MB memory for all kept audio (default 16; about 330 s of speech)
"""

from __future__ import annotations

import asyncio
import contextlib
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
import safetensors.torch
import scipy.io.wavfile
import torch
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from huggingface_hub import HfApi
from huggingface_hub import constants as hf_constants
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError
from pocket_tts import TTSModel, export_model_state
from pocket_tts.data.audio_utils import convert_audio
from pocket_tts.modules.conv import pad_for_conv1d
from pocket_tts.modules.stateful_module import StatefulModule
from pocket_tts.utils.utils import download_if_necessary
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

VOICE_DIR = Path(os.environ.get("POCKET_TTS_VOICE_DIR", "/home/app/.cache/voices"))
MAX_CUSTOM_VOICES = int(os.environ.get("POCKET_TTS_MAX_CUSTOM_VOICES", "50"))
CUSTOM_VOICE_CACHE = int(os.environ.get("POCKET_TTS_CUSTOM_VOICE_CACHE", "4"))
CLONING_REPO = "kyutai/pocket-tts"
CLONING_TERMS_URL = f"https://huggingface.co/{CLONING_REPO}"
HF_TOKENS_URL = "https://huggingface.co/settings/tokens"
CLONING_UNAVAILABLE = (
    f"Voice cloning needs the gated weights: accept the terms at {CLONING_TERMS_URL}, then "
    "turn voice cloning on with a Hugging Face read token of that account (or set HF_TOKEN "
    "on the server)."
)
_TOKEN_REJECTED = (
    f"Voice cloning needs the gated weights of {CLONING_TERMS_URL}. A token is set, but the "
    "gated weights did not download: accept the terms with the account that owns the token, "
    "check that the token can read gated repositories, then "
)
CLONING_TOKEN_REJECTED = _TOKEN_REJECTED + "restart the server."  # token from HF_TOKEN
CLONING_SAVED_TOKEN_REJECTED = _TOKEN_REJECTED + "turn voice cloning on again."
CLONING_ENABLING = (
    "Voice cloning is being turned on: the server is loading the voice-cloning weights "
    "(about 0.2 GB from Hugging Face the first time). Built-in voices keep working meanwhile."
)
CLONING_DOWNLOAD_FAILED = (
    "The voice-cloning weights could not be downloaded from Hugging Face: check that the "
    "server can reach huggingface.co, then turn voice cloning on again."
)
CLONING_BUSY = "Voice cloning is already being turned on: wait until it is on, then try again."
CLONING_ENV_PUT = (
    "The server already has a Hugging Face token in its environment (POCKET_TTS_HF_TOKEN, "
    "passed to it as HF_TOKEN), which takes precedence: change it there and restart the "
    "server, or remove it there to manage the token from ATLAS."
)
CLONING_ENV_DELETE = (
    "Voice cloning is turned on by a token in the server's environment: remove "
    "POCKET_TTS_HF_TOKEN / HF_TOKEN from the server's environment and restart it to turn "
    "voice cloning off."
)
TOKEN_NOT_A_TOKEN = (
    'Send {"hf_token": "hf_..."}: a Hugging Face user access token starts with hf_ '
    f"(create a read token at {HF_TOKENS_URL})."
)
TOKEN_NOT_ACCEPTED = (
    "Hugging Face did not accept this token: check that it was copied whole and has not "
    f"expired or been deleted, or create a new read token at {HF_TOKENS_URL}."
)
TOKEN_NO_ACCESS = (
    "This token cannot download the voice-cloning weights yet: accept the terms at "
    f"{CLONING_TERMS_URL} with the account that owns the token, then try again (a "
    "fine-grained token also needs read access to public gated repositories)."
)
HF_UNREACHABLE = "Hugging Face could not be reached to check the token: try again in a minute."
# Hugging Face user access tokens are "hf_" and 34 letters and digits; some leeway here.
HF_TOKEN_SHAPE = re.compile(r"hf_[A-Za-z0-9_-]{17,197}")
HF_TOKEN_REDACT = re.compile(r"hf_[A-Za-z0-9]{20,}")
# huggingface_hub's HTTP client has no timeout of its own.
HF_CHECK_SECONDS = 20.0
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

# ------------------------------------------------------------------ phrase cache settings

# Keep in sync with the opener rule in infra/scripts/demo_configure.py: the demo agent opens
# every reply with one of these, so its first words come from memory instead of the model.
# The list also holds the openers qwen2.5:1.5b picks on its own most often ("Perfect.",
# "Of course.", "You're welcome.", "Certainly.", "Absolutely."), seen in test calls.
DEFAULT_CACHED_PHRASES = (
    "Got it.", "Okay.", "Sure.", "Great.", "Alright.", "Perfect.", "Of course.", "I see.",
    "Understood.", "That makes sense.", "No problem.", "Thanks.", "You're welcome.",
    "Certainly.", "Absolutely.",
)
_phrases_env = os.environ.get("POCKET_TTS_PHRASE_CACHE")
CACHED_PHRASES: list[str] = (
    list(DEFAULT_CACHED_PHRASES)
    if _phrases_env is None
    else [p.strip() for p in _phrases_env.split("|") if p.strip()]
)
_phrase_voices_env = os.environ.get("POCKET_TTS_PHRASE_CACHE_VOICES", "").strip()
PHRASE_CACHE_VOICES: list[str] = (
    [v.strip() for v in _phrase_voices_env.split(",") if v.strip()]
    if _phrase_voices_env
    else [DEFAULT_VOICE]
)
PHRASE_CACHE_MAX_CHARS = int(os.environ.get("POCKET_TTS_PHRASE_CACHE_MAX_CHARS", "32"))
PHRASE_CACHE_BYTES = int(float(os.environ.get("POCKET_TTS_PHRASE_CACHE_MB", "16")) * 1024 * 1024)

_model: TTSModel | None = None
_voice_states: dict[str, object] = {}
# Cloned voices: metadata of every voice on disk, and an LRU of loaded states.
_custom_voices: dict[str, dict[str, Any]] = {}
_custom_states: OrderedDict[str, object] = OrderedDict()
# The cloning endpoints and cloned voices work. Stays on while a new token loads (PUT when on).
_cloning_enabled = False
_cloning_status = "off"  # off | enabling | on | failed, as GET /v1/cloning reports it
_cloning_problem: str | None = None  # why it is off or failed (never holds a token)
# Every PUT and DELETE of /v1/cloning starts a new attempt: a background load that finishes
# for an older one changes nothing (cloning was turned off, or on again, meanwhile).
_cloning_attempt = 0
_cloning_thread: threading.Thread | None = None
_switch_lock = threading.Lock()
# One background load at a time (a PUT right after a DELETE may start one while one runs).
_enable_lock = threading.Lock()
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
    found: dict[str, dict[str, Any]] = {}
    paths = sorted(VOICE_DIR.glob("custom-*.json")) if VOICE_DIR.is_dir() else []
    for meta_path in paths:
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
        found[voice_id] = meta
    # Swapped in at once: this also runs while the server answers (PUT /v1/cloning).
    with _state_lock:
        _custom_voices.clear()
        _custom_voices.update(found)
        _custom_states.clear()


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
    if _cloning_status == "enabling":
        return CLONING_ENABLING
    return _cloning_problem or CLONING_UNAVAILABLE


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


# ------------------------------------------------------------------ voice cloning switch
#
# The token is checked with Hugging Face, kept only in huggingface_hub's own token file and
# read from there by the library's downloader. It is never logged, returned or put in an
# exception message; upstream error texts are logged only through _redact.


def token_file() -> Path:
    """Where huggingface_hub keeps a token ($HF_HOME/token) and reads it from."""
    return Path(hf_constants.HF_TOKEN_PATH)


def _environment_token() -> bool:
    # huggingface_hub reads these before the token file (utils/_auth.py, v1.32.0).
    return any(os.environ.get(n, "").strip() for n in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"))


def cloning_token_source() -> str | None:
    """"environment" or "saved", in the order huggingface_hub looks, or None."""
    if _environment_token():
        return "environment"
    try:
        return "saved" if token_file().read_text(encoding="utf-8").strip() else None
    except OSError:  # no token file
        return None


def save_token(token: str) -> None:
    """Replace the token file atomically; only this user can read it (mode 0600)."""
    path = token_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def write(tmp: Path) -> None:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)

    _write_atomic(path, write)


def _redact(text: str) -> str:
    return HF_TOKEN_REDACT.sub("hf_***", text)


def _http_status(exc: BaseException) -> int:
    return int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)


class TokenCheckError(Exception):
    """The token cannot turn cloning on; `detail` is safe to show (it never holds the token)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def check_token(token: str) -> None:
    """Raise TokenCheckError unless Hugging Face accepts `token` and its account may download
    the gated weights (blocking: two small requests to huggingface.co)."""
    api = HfApi(token=token)
    step = "whoami"
    try:
        api.whoami()  # 401: mistyped, expired or deleted
        step = "auth_check"
        api.auth_check(CLONING_REPO)  # GatedRepoError (403): terms not accepted
    except HfHubHTTPError as exc:
        status = _http_status(exc)
        if status == 429 or status >= 500:
            raise TokenCheckError(502, HF_UNREACHABLE) from None
        if step == "whoami":
            raise TokenCheckError(401, TOKEN_NOT_ACCEPTED) from None
        raise TokenCheckError(403, TOKEN_NO_ACCESS) from None
    except Exception as exc:  # network errors (httpx), offline mode
        logger.warning("Hugging Face could not check the token: %s", type(exc).__name__)
        raise TokenCheckError(502, HF_UNREACHABLE) from None


async def _run_blocking(fn: Any, *args: Any, seconds: float) -> None:
    """Run `fn` in its own daemon thread and wait at most `seconds` (TimeoutError).

    Not asyncio.to_thread alone: a request without a timeout could hold one of the event
    loop's executor threads, which the speech streams use, for good.
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            fn(*args)
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    await asyncio.to_thread(worker.join, seconds)
    if not outcome:
        raise TimeoutError
    if outcome[0] is not None:
        raise outcome[0]


def load_cloning_weights(model: Any) -> None:
    """Download the gated weights with the saved token and load them into the running model.

    TTSModel.load_model loads the same file (`weights_path` of the model's config, strict
    key match; the open weights have the same keys) at startup. A second model loaded next
    to the one in use peaked at 1.43 GiB in a bare process, measured with the open weights;
    the server adds about 70 MiB, over the container's 1.5 GiB limit. Copying into the model
    in use peaked at 1.04 GiB. Synthesis waits for the copy only, not the download.
    """
    weights = safetensors.torch.load_file(download_if_necessary(model.config.weights_path))
    with _generation_lock:
        model.load_state_dict(weights, strict=True)
        model.has_voice_cloning = True


def _finish_attempt(attempt: int, status: str, problem: str | None = None) -> bool:
    global _cloning_status, _cloning_problem, _cloning_enabled
    with _switch_lock:
        if attempt != _cloning_attempt:
            return False
        _cloning_status, _cloning_problem = status, problem
        _cloning_enabled = status == "on"
        return True


def enable_cloning(attempt: int) -> None:
    """Background part of PUT /v1/cloning: load the gated weights unless the model has them
    already, then turn cloning on the way the startup does."""
    with _enable_lock:
        started = time.perf_counter()
        try:
            model = _model
            if model is None:  # PUT answers 503 until the model is loaded
                raise RuntimeError("model not loaded")
            reloaded = not getattr(model, "has_voice_cloning", False)
            if reloaded:
                load_cloning_weights(model)
            use_bounded_encoder(model)
            load_voice_index()
            preload_custom_states()
            if reloaded:
                # Kept states and audio were made before the weights changed.
                with _state_lock:
                    _voice_states.clear()
                _voice_state(DEFAULT_VOICE)
                rebuild_phrase_cache()
        except Exception as exc:
            refused = isinstance(exc, RepositoryNotFoundError) or _http_status(exc) in (401, 403)
            logger.warning(
                "Voice cloning could not be turned on: %s", _redact(f"{type(exc).__name__}: {exc}")
            )
            _finish_attempt(
                attempt, "failed",
                CLONING_SAVED_TOKEN_REJECTED if refused else CLONING_DOWNLOAD_FAILED,
            )
            return
        if _finish_attempt(attempt, "on"):
            logger.info(
                "Voice cloning enabled: %d cloned voice(s) on disk (%.1fs)",
                len(_custom_voices), time.perf_counter() - started,
            )


def cloning_state() -> dict[str, Any]:
    return {
        "state": _cloning_status,
        "message": None if _cloning_status == "on" else _cloning_message(),
        "terms_url": CLONING_TERMS_URL,
        "token_source": cloning_token_source(),
    }


async def _read_token(request: Request) -> str:
    """The `hf_token` of a JSON body, if it looks like a token (422 otherwise, never echoed)."""
    token: Any = None
    length = request.headers.get("content-length") or "0"
    if length.isdigit() and int(length) <= 4096:
        with contextlib.suppress(ValueError, TypeError, KeyError):
            token = json.loads(await request.body())["hf_token"]
    token = token.strip() if isinstance(token, str) else ""
    if not HF_TOKEN_SHAPE.fullmatch(token):
        raise HTTPException(status_code=422, detail=TOKEN_NOT_A_TOKEN)
    return token


# ------------------------------------------------------------------ phrase cache

_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def normalize_phrase(text: str) -> str:
    """The cache key of a text: Unicode-normalised, straight quotes, single spaces, any case."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return " ".join(text.split()).casefold()


class PhraseCache:
    """Synthesised audio of short phrases, per voice, held in memory only (never on disk).

    The voice service starts playing a sentence only once it has received 0.5 s of its
    audio (or all of it), which takes Pocket TTS 0.4-0.5 s on the Oracle server's CPU; a
    kept phrase is answered from memory instead, in chunks of the size the model produces.
    Phrases made at startup are pinned; other short texts are kept once fully spoken, least
    recently used evicted first. Total audio is bounded by `max_bytes`.
    """

    def __init__(self, max_bytes: int, max_chars: int) -> None:
        self.max_bytes = max(max_bytes, 0)
        self.max_chars = max(max_chars, 0)
        self._items: OrderedDict[tuple[str, str], tuple[bytes, ...]] = OrderedDict()
        self._pinned: set[tuple[str, str]] = set()
        # Pinned phrases that may open a longer sentence: (voice, key) -> pattern.
        self._openers: dict[tuple[str, str], re.Pattern[str]] = {}
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.opener_hits = 0

    def cacheable(self, text: str) -> bool:
        return 0 < len(normalize_phrase(text)) <= self.max_chars

    def get(self, voice: str, text: str) -> tuple[bytes, ...] | None:
        key = (voice, normalize_phrase(text))
        with self._lock:
            chunks = self._items.get(key)
            if chunks is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return chunks

    def put(self, voice: str, text: str, chunks: list[bytes], *, pin: bool = False) -> bool:
        key = (voice, normalize_phrase(text))
        stored = tuple(c for c in chunks if c)
        size = sum(len(c) for c in stored)
        if not key[1] or size == 0:
            return False
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= sum(len(c) for c in old)
            # Make room from the least recently used unpinned entries.
            for victim in [k for k in self._items if k not in self._pinned]:
                if self._bytes + size <= self.max_bytes:
                    break
                self._bytes -= sum(len(c) for c in self._items.pop(victim))
            if self._bytes + size > self.max_bytes:
                self._pinned.discard(key)
                return False
            self._items[key] = stored
            self._bytes += size
            if pin:
                self._pinned.add(key)
                stem = key[1].rstrip(".!?")
                if stem:
                    self._openers[key] = _opener_pattern(stem)
            return True

    def opener(self, voice: str, text: str) -> tuple[tuple[bytes, ...], str] | None:
        """A pinned phrase that opens `text`, then ",", "." or "!" (and maybe more words).

        Returns that phrase's audio and the rest of the text ("" when there is none) with its
        first letter in upper case: "Got it, perfect timing." is spoken as the kept "Got it."
        and "Perfect timing.", so the first words are heard at once while the rest is
        synthesised; "Great!" is spoken as the kept "Great.". Longest match wins.
        """
        with self._lock:
            found = None
            for key, pattern in self._openers.items():
                if key[0] != voice or (found and len(key[1]) <= len(found[0][1])):
                    continue
                m = pattern.fullmatch(text)
                if m:
                    found = (key, m.group(1) or "")
            if found is None:
                return None
            key, rest = found
            chunks = self._items.get(key)
            if chunks is None:
                return None
            self.opener_hits += 1
            return chunks, rest[:1].upper() + rest[1:]

    def drop_voice(self, voice: str) -> None:
        with self._lock:
            for key in [k for k in self._items if k[0] == voice]:
                self._bytes -= sum(len(c) for c in self._items.pop(key))
                self._pinned.discard(key)
                self._openers.pop(key, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._items),
                "pinned": len(self._pinned),
                "bytes": self._bytes,
                "max_bytes": self.max_bytes,
                "max_chars": self.max_chars,
                "hits": self.hits,
                "opener_hits": self.opener_hits,
                "misses": self.misses,
            }


def _opener_pattern(stem: str) -> re.Pattern[str]:
    """Matches `stem` (a normalised phrase without its end mark) at the start of a text,
    followed by ",", "." or "!" and either nothing or more words; group 1 is the rest."""
    words = [
        "['\u2019]".join(re.escape(part) for part in word.split("'")) for word in stem.split()
    ]
    return re.compile(
        r"\s*" + r"\s+".join(words) + r"\s*[,.!](?:\s+(\S.*))?\s*", re.IGNORECASE | re.DOTALL
    )


_phrase_cache = PhraseCache(PHRASE_CACHE_BYTES, PHRASE_CACHE_MAX_CHARS)

# Pocket TTS starts each text with 80-175 ms of near-silence (measured on the Oracle server,
# voice alba). A kept phrase usually opens a reply, so it is stored from KEEP_LEAD_MS before
# its first sound: the caller hears it that much sooner.
LEAD_THRESHOLD_DB = -40.0
KEEP_LEAD_MS = 30


def sound_start(data: bytes, sample_rate: int) -> int | None:
    """Byte offset KEEP_LEAD_MS before the first 10 ms window at or above LEAD_THRESHOLD_DB
    (RMS), or None when there is no such window."""
    samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768
    win = max(sample_rate // 100, 1)
    n = len(samples) // win
    if n == 0:
        return None
    rms = np.sqrt(np.mean(samples[: n * win].reshape(n, win) ** 2, axis=1))
    loud = np.nonzero(rms >= 10 ** (LEAD_THRESHOLD_DB / 20))[0]
    if loud.size == 0:
        return None
    return max(0, int(loud[0]) * win - sample_rate * KEEP_LEAD_MS // 1000) * 2


def trim_leading_silence(chunks: list[bytes], sample_rate: int) -> list[bytes]:
    """Drop the near-silence before the first sound (10 ms windows), keeping KEEP_LEAD_MS.

    The rest is re-cut into chunks of the first chunk's size. Audio without any sound above
    LEAD_THRESHOLD_DB is returned unchanged.
    """
    data = b"".join(chunks)
    if not data or not chunks[0]:
        return chunks
    start = sound_start(data, sample_rate)
    if not start:
        return chunks
    rest = data[start:]
    size = len(chunks[0])
    return [rest[i : i + size] for i in range(0, len(rest), size)]


def prewarm_phrases(
    phrases: list[str], voices: list[str], cache: PhraseCache | None = None
) -> None:
    """Synthesise and pin `phrases` for each voice (startup; blocking)."""
    if cache is None:
        cache = _phrase_cache
    if not phrases or not voices:
        return
    started = time.perf_counter()
    kept = 0
    for voice in voices:
        for phrase in phrases:
            assert _model is not None
            chunks = trim_leading_silence(
                list(_pcm_chunks(phrase, voice, threading.Event())), _model.sample_rate
            )
            if cache.put(voice, phrase, chunks, pin=True):
                kept += 1
            else:
                logger.warning("Phrase cache full: %d of the startup phrases were kept", kept)
                break
    stats = cache.stats()
    logger.info(
        "Phrase cache: %d phrase(s) for %s in %.1fs (%d KB)",
        kept, ",".join(voices), time.perf_counter() - started, stats["bytes"] // 1024,
    )


def _phrase_voices() -> list[str]:
    voices = []
    for v in PHRASE_CACHE_VOICES:
        if v in ALLOWED_VOICES or v in _custom_voices:
            voices.append(v)
        else:
            logger.warning("POCKET_TTS_PHRASE_CACHE_VOICES: voice %s is not available; skipped", v)
    return voices


def rebuild_phrase_cache() -> None:
    """Make the startup phrases again with the current weights, then use that cache (the old
    one answers meanwhile; one phrase at a time waits for the synthesis lock like a request)."""
    global _phrase_cache
    fresh = PhraseCache(PHRASE_CACHE_BYTES, PHRASE_CACHE_MAX_CHARS)
    prewarm_phrases(CACHED_PHRASES, _phrase_voices(), fresh)
    _phrase_cache = fresh


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
    global _model, _cloning_enabled, _cloning_status, _cloning_problem
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
    # The library tries the gated kyutai/pocket-tts weights first (voice cloning; needs a
    # token of an account that accepted the terms: HF_TOKEN, else the token file that
    # PUT /v1/cloning saves) and otherwise falls back to the ungated
    # kyutai/pocket-tts-without-voice-cloning weights (CC BY 4.0), which built-in voices need.
    # Once downloaded, the pinned gated weights load from the cache without a token, so
    # cloning also needs a token now: turning it off only removes the token.
    _model = TTSModel.load_model(language=LANGUAGE)
    source = cloning_token_source()
    if source is None:
        _cloning_status, _cloning_problem = "off", None
    elif getattr(_model, "has_voice_cloning", False):
        _cloning_status, _cloning_problem = "on", None
    else:
        _cloning_status = "failed"
        _cloning_problem = (
            CLONING_TOKEN_REJECTED if source == "environment" else CLONING_SAVED_TOKEN_REJECTED
        )
    _cloning_enabled = _cloning_status == "on"
    if _cloning_enabled:
        use_bounded_encoder(_model)
        load_voice_index()
        preload_custom_states()
        logger.info(
            "Voice cloning enabled (%s token): %d cloned voice(s) on disk",
            source, len(_custom_voices),
        )
    else:
        logger.info("Voice cloning off: %s", _cloning_message())
    _voice_state(DEFAULT_VOICE)
    if WARMUP:
        for _ in _pcm_chunks("Hello there.", DEFAULT_VOICE, threading.Event()):
            pass
    prewarm_phrases(CACHED_PHRASES, _phrase_voices())
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
    _phrase_cache.drop_voice(voice_id)
    return None


@app.get("/v1/cloning")
async def get_cloning(authorization: str | None = Header(default=None)) -> dict:
    """Cloning state (off, enabling, on, failed), what to do next, and where the token
    comes from ("environment", "saved" or null). Never the token."""
    _check_auth(authorization)
    return cloning_state()


@app.put("/v1/cloning", status_code=202)
async def put_cloning(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """Turn voice cloning on with `{"hf_token": "hf_..."}`, without a restart.

    The token is checked with Hugging Face before anything is saved: 422 not a token, 401 not
    accepted, 403 its account has not accepted the terms, 502 Hugging Face unreachable; 409
    while cloning is being turned on or when the server's environment sets a token. Then it
    is saved, the answer is 202 with state "enabling", and the weights load in the
    background: GET /v1/cloning says "on" or "failed" when done.
    """
    global _cloning_status, _cloning_problem, _cloning_attempt, _cloning_thread
    _check_auth(authorization)
    token = await _read_token(request)
    if _model is None:
        raise HTTPException(status_code=503, detail="Model is still loading")
    if _cloning_status == "enabling":
        raise HTTPException(status_code=409, detail=CLONING_BUSY)
    if _environment_token():
        raise HTTPException(status_code=409, detail=CLONING_ENV_PUT)
    try:
        await _run_blocking(check_token, token, seconds=HF_CHECK_SECONDS)
    except TokenCheckError as exc:
        logger.info("Voice cloning token not used: Hugging Face check gave %d", exc.status)
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None
    except TimeoutError:
        raise HTTPException(status_code=502, detail=HF_UNREACHABLE) from None
    with _switch_lock:
        if _cloning_status == "enabling":  # another PUT got here during the check
            raise HTTPException(status_code=409, detail=CLONING_BUSY)
        try:
            save_token(token)
        except OSError as exc:
            logger.error("The Hugging Face token could not be saved: %s", exc.strerror)
            raise HTTPException(
                status_code=500, detail="The server could not save the token in its volume."
            ) from None
        _cloning_attempt += 1
        _cloning_status, _cloning_problem = "enabling", None
        body = cloning_state()
        logger.info("Voice cloning token checked and saved: loading the voice-cloning weights")
        _cloning_thread = threading.Thread(
            target=enable_cloning, args=(_cloning_attempt,), name="enable-cloning", daemon=True
        )
        _cloning_thread.start()
    return JSONResponse(status_code=202, content=body)


@app.delete("/v1/cloning", status_code=204)
async def delete_cloning(authorization: str | None = Header(default=None)):
    """Turn voice cloning off at once by removing the saved token (409 when the token comes
    from the environment). Downloaded weights and cloned voices stay on disk for next time."""
    global _cloning_status, _cloning_problem, _cloning_attempt, _cloning_enabled
    _check_auth(authorization)
    if _environment_token():
        raise HTTPException(status_code=409, detail=CLONING_ENV_DELETE)
    with _switch_lock:
        try:
            token_file().unlink(missing_ok=True)
        except OSError as exc:
            logger.error("The saved Hugging Face token could not be removed: %s", exc.strerror)
            raise HTTPException(
                status_code=500, detail="The server could not remove the saved token."
            ) from None
        _cloning_attempt += 1
        _cloning_status, _cloning_problem, _cloning_enabled = "off", None, False
    with _state_lock:
        _custom_states.clear()  # loaded from disk again when cloning is turned on
    logger.info("Voice cloning turned off: the saved token was removed")
    return None


@app.get("/v1/audio/phrase-cache")
async def phrase_cache_stats(authorization: str | None = Header(default=None)) -> dict:
    """Counts only (never the cached texts): entries, bytes, hits and misses."""
    _check_auth(authorization)
    return _phrase_cache.stats()


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
    media_type = "audio/wav" if body.response_format == "wav" else "audio/pcm"
    received = time.perf_counter()

    # head: kept audio sent first; rest: text still to synthesise after it (None: nothing).
    head: tuple[bytes, ...] = ()
    rest: str | None = text
    status = "miss"
    cached = _phrase_cache.get(voice, text)
    if cached is not None:
        head, rest, status = cached, None, "hit"
    else:
        split = _phrase_cache.opener(voice, text)
        if split is not None:
            head, rest = split
            status = "opener"
            if not rest:  # "Great!" -> the kept "Great."
                rest = None
            else:
                tail = _phrase_cache.get(voice, rest)  # "Got it, thanks." -> both kept
                if tail is not None:
                    head, rest = head + tail, None
    headers = {"X-Phrase-Cache": status}

    if rest is None:
        async def replay() -> AsyncIterator[bytes]:
            if body.response_format == "wav":
                yield _wav_header(sample_rate)
            for chunk in head:
                yield chunk
            # Lengths only: the text itself is never logged.
            logger.info(
                "speech cache=%s chars=%d audio_s=%.2f",
                status, len(text), sum(len(c) for c in head) / (2 * sample_rate),
            )

        return StreamingResponse(replay(), media_type=media_type, headers=headers)

    to_speak = rest
    queue: Queue[bytes | BaseException | None] = Queue(maxsize=64)
    stop = threading.Event()
    keep = not head and _phrase_cache.cacheable(to_speak)

    def worker() -> None:
        chunks: list[bytes] = []
        try:
            for pcm in _pcm_chunks(to_speak, voice, stop):
                queue.put(pcm)
                if keep:
                    chunks.append(pcm)
            # Only audio that was spoken to the end is kept.
            if keep and not stop.is_set():
                _phrase_cache.put(voice, to_speak, trim_leading_silence(chunks, sample_rate))
        except BaseException as exc:  # surfaced to the response stream below
            queue.put(exc)
        finally:
            queue.put(None)

    threading.Thread(target=worker, daemon=True).start()
    loop = asyncio.get_running_loop()

    async def stream() -> AsyncIterator[bytes]:
        first_ms: float | None = None
        audio_bytes = 0
        # After a kept opener, the synthesised rest starts at its first sound (it would
        # otherwise add its own 80-175 ms of lead-in to the pause between the two).
        pending: bytearray | None = bytearray() if head else None
        try:
            if body.response_format == "wav":
                yield _wav_header(sample_rate)
            for chunk in head:
                if first_ms is None:
                    first_ms = (time.perf_counter() - received) * 1000
                audio_bytes += len(chunk)
                yield chunk
            while True:
                item = await loop.run_in_executor(None, queue.get)
                if item is None:
                    if pending:
                        yield bytes(pending)
                    break
                if isinstance(item, BaseException):
                    logger.error("Synthesis failed: %s", item)
                    break
                if pending is not None:
                    pending += item
                    start = sound_start(bytes(pending), sample_rate)
                    if start is None and len(pending) < sample_rate * 2 * 3 // 10:
                        continue
                    item, pending = bytes(pending[start or 0 :]), None
                if first_ms is None:
                    first_ms = (time.perf_counter() - received) * 1000
                audio_bytes += len(item)
                yield item
                if await request.is_disconnected():
                    break
        finally:
            # Client gone or done: stop generating, and drain so the worker can exit.
            stop.set()
            while not queue.empty():
                queue.get_nowait()
            logger.info(
                "speech cache=%s chars=%d first_audio_ms=%s audio_s=%.2f total_ms=%.0f",
                status, len(text), "-" if first_ms is None else f"{first_ms:.0f}",
                audio_bytes / (2 * sample_rate), (time.perf_counter() - received) * 1000,
            )

    return StreamingResponse(stream(), media_type=media_type, headers=headers)
