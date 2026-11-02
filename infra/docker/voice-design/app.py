"""OpenAI-compatible speech server for Qwen3-TTS, with voices designed from a description.

The voice runtime talks to self-hosted voices through the OpenAI speech API
(`POST /v1/audio/speech` with `response_format: "pcm"`, read as 24 kHz, 16-bit, mono). This
server answers it the way the Pocket TTS adapter (infra/docker/pocket-tts) and the expressive
server (infra/docker/expressive-tts) do: same request, same audio, same phrase cache, same
voice and cloning endpoints, so any of them can be the voice of an agent.

What it adds: a voice from words. `POST /v1/voices/design` takes a plain description ("warm,
upbeat American man in his forties, clear and friendly") and makes a new voice from it; no
recording, no speaker, no consent. Two Qwen3-TTS models (Apache-2.0) work together:

  design  Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign renders a short identity sample of the
          described voice once, when the voice is designed (and for the built-in "default"
          voice, at the first start).
  speech  Qwen/Qwen3-TTS-12Hz-0.6B-Base speaks every reply. It clones from a sample: the
          designed voice's identity sample (with its known transcript) is its reference, so
          the voice stays the same from one reply to the next. The same model clones a
          recording of a consenting speaker (`POST /v1/voices`, 3-30 s).

The speech model makes a whole sentence at a time, so a reply is split into sentences and each
is sent as soon as it is made (`streaming: "sentence"`); kept openers start at once. It has
no sound tags: bracketed cues ("[laugh]") and emoji are removed before synthesis. The speech
model takes no delivery instructions; `instructions` is accepted and ignored unless
VOICE_DESIGN_INSTRUCT=design (then a designed voice is re-rendered by the design model with
the description and the instructions, which costs speaker consistency).

Endpoints (all but /health need VOICE_DESIGN_API_KEY when it is set):
  GET    /health                 "ok" once the models are loaded and warm
  GET    /v1/models, /v1/audio/voices  the model id, its variants and the built-in voice
  GET    /v1/capabilities        what this server can do (cloning, design, instruct, tags,
                                 languages, streaming, reference length)
  GET    /v1/audio/sound-tags    {"tags": []}: this voice performs no sounds
  POST   /v1/audio/speech        OpenAI speech API: 24 kHz 16-bit mono PCM or WAV, streamed
                                 sentence by sentence
  GET    /v1/audio/phrase-cache  phrase cache counts (never the texts)
  GET    /v1/voices              built-in, designed and cloned voices
  POST   /v1/voices/design       design a voice from a description (JSON); 201 with the voice
  POST   /v1/voices              clone a voice from a 3-30 s WAV of a consenting speaker
  GET    /v1/voices/{id}/sample  a short WAV of the voice, for a preview
  DELETE /v1/voices/{id}         delete a designed or cloned voice
  GET/PUT/DELETE /v1/cloning     cloning state in the Pocket adapter's shape (no token here)

Environment:
  VOICE_DESIGN_MODEL_ID          id clients send as `model` (default "qwen3-tts")
  VOICE_DESIGN_SPEECH_MODEL      the Base model that speaks and clones (default
                                 Qwen/Qwen3-TTS-12Hz-0.6B-Base; Qwen/Qwen3-TTS-12Hz-1.7B-Base
                                 also works, slower)
  VOICE_DESIGN_DESIGN_MODEL      the VoiceDesign model (default
                                 Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign; "" = not loaded: no
                                 design, the built-in voice must already be on disk)
  VOICE_DESIGN_SPEECH_REVISION, VOICE_DESIGN_DESIGN_REVISION  Hugging Face revisions to
                                 download (default: the ones measured, KNOWN_REVISIONS)
  VOICE_DESIGN_DEVICE            "auto" (default: the GPU when there is one), "cuda" or "cpu"
  VOICE_DESIGN_DTYPE             "auto" (default: bfloat16 on a GPU of compute capability 8 or
                                 newer, float32 on older GPUs such as the T4 and on a CPU),
                                 "bfloat16", "float32" or "float16" (overflows: not usable)
  VOICE_DESIGN_ATTENTION         attention implementation (default "sdpa"; a T4 cannot run
                                 flash_attention_2)
  VOICE_DESIGN_LANGUAGE          language of the built-in voice and of voices made without
                                 one (default "English"; "Auto" lets the model decide)
  VOICE_DESIGN_DEFAULT_VOICE_DESCRIPTION, VOICE_DESIGN_DEFAULT_VOICE_SEED  the built-in
                                 "default" voice, designed at the first start and kept on disk
                                 (re-made when either changes)
  VOICE_DESIGN_SAMPLE_TEXT       what identity samples say (default DEFAULT_SAMPLE_TEXT)
  VOICE_DESIGN_INSTRUCT          "ignore" (default) or "design" (see above)
  VOICE_DESIGN_API_KEY           if set, require `Authorization: Bearer <key>`
  VOICE_DESIGN_WARMUP            "1" (default) runs one short synthesis at startup
  VOICE_DESIGN_FIRST_SENTENCE_CHARS  a first sentence longer than this is cut after its
                                 first clause mark within the limit (default 60; 0 = never)
  VOICE_DESIGN_MAX_SENTENCE_CHARS  longer sentences are cut at clause marks, else between
                                 words (default 300)
  VOICE_DESIGN_VOICE_DIR         where voices are kept (default /home/app/.cache/voices,
                                 inside the weights volume)
  VOICE_DESIGN_MAX_VOICES        most designed and cloned voices kept on disk (default 50)
  VOICE_DESIGN_CLONING           "true" (default) or "false": cloning from recordings off
                                 (designed voices keep working)
  VOICE_DESIGN_VOICE_CACHE       voices held in memory, least recently used evicted first
                                 (default 16)
  VOICE_DESIGN_PHRASE_CACHE, _PHRASE_CACHE_VOICES, _PHRASE_CACHE_ANY_VOICE,
  _PHRASE_CACHE_MAX_CHARS, _PHRASE_CACHE_MB  the phrase cache, as in the other servers
"""

from __future__ import annotations

import asyncio
import heapq
import io
import itertools
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
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any, Literal

import numpy as np
import safetensors.numpy
import scipy.io.wavfile
import soxr
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from safetensors import safe_open
from starlette.datastructures import UploadFile

logger = logging.getLogger("voice_design_server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
# huggingface_hub's client logs every request of the weights download at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

DEFAULT_SPEECH_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
DEFAULT_DESIGN_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
# The revisions measured on a T4 (README). Pinned so that a new upload cannot change the
# voices, or the format of saved voices, under a running deployment.
KNOWN_REVISIONS = {
    "Qwen/Qwen3-TTS-12Hz-0.6B-Base": "5d83992436eae1d760afd27aff78a71d676296fc",
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base": "fd4b254389122332181a7c3db7f27e918eec64e3",
    "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign": "5ecdb67327fd37bb2e042aab12ff7391903235d3",
}
# What Qwen3TTSModel.from_pretrained reads: the talker, its text tokenizer and the speech
# tokenizer (codec) of the repository; never the README.
MODEL_FILES = ["*.json", "*.safetensors", "merges.txt", "speech_tokenizer/*"]
SPEECH_REPO = os.environ.get("VOICE_DESIGN_SPEECH_MODEL", DEFAULT_SPEECH_REPO).strip()
DESIGN_REPO = os.environ.get("VOICE_DESIGN_DESIGN_MODEL", DEFAULT_DESIGN_REPO).strip()
SPEECH_REVISION = os.environ.get("VOICE_DESIGN_SPEECH_REVISION") or KNOWN_REVISIONS.get(SPEECH_REPO, "main")
DESIGN_REVISION = os.environ.get("VOICE_DESIGN_DESIGN_REVISION") or KNOWN_REVISIONS.get(DESIGN_REPO, "main")
MODEL_CARD_URL = f"https://huggingface.co/{SPEECH_REPO}"
BUILTIN_VOICE = "default"
# The built-in voice is the design model's output (made at the first start): the model is
# Apache-2.0, and no recording of a person is involved.
VOICE_LICENSES: dict[str, str] = {BUILTIN_VOICE: "Apache-2.0"}
ALLOWED_VOICES = list(VOICE_LICENSES)
OPENAI_VOICE_NAMES = {
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
}
# The ten languages of Qwen3-TTS, as the design request names them (any case is accepted);
# the loaded model's own list replaces this at startup.
DEFAULT_LANGUAGES = [
    "English", "Chinese", "Japanese", "Korean", "German", "French", "Russian", "Portuguese",
    "Spanish", "Italian",
]

MODEL_ID = os.environ.get("VOICE_DESIGN_MODEL_ID", "qwen3-tts")
DEVICE = os.environ.get("VOICE_DESIGN_DEVICE", "auto").strip().lower() or "auto"
DTYPE = os.environ.get("VOICE_DESIGN_DTYPE", "auto").strip().lower() or "auto"
ATTENTION = os.environ.get("VOICE_DESIGN_ATTENTION", "sdpa").strip() or "sdpa"
DEFAULT_VOICE = BUILTIN_VOICE
LANGUAGE = os.environ.get("VOICE_DESIGN_LANGUAGE", "English").strip() or "English"
DEFAULT_VOICE_DESCRIPTION = " ".join(os.environ.get(
    "VOICE_DESIGN_DEFAULT_VOICE_DESCRIPTION",
    "A warm, friendly American woman in her thirties, clear and natural, at an easy "
    "conversational pace.",
).split())
DEFAULT_VOICE_SEED = int(os.environ.get("VOICE_DESIGN_DEFAULT_VOICE_SEED", "1234"))
DEFAULT_SAMPLE_TEXT = (
    "Hi, thanks for calling. I'm glad you reached out today; tell me what you need, and "
    "I'll see what I can do."
)
SAMPLE_TEXT = " ".join(os.environ.get("VOICE_DESIGN_SAMPLE_TEXT", DEFAULT_SAMPLE_TEXT).split())
INSTRUCT_MODE = os.environ.get("VOICE_DESIGN_INSTRUCT", "ignore").strip().lower() or "ignore"
API_KEY = os.environ.get("VOICE_DESIGN_API_KEY") or None
WARMUP = os.environ.get("VOICE_DESIGN_WARMUP", "1") == "1"
FIRST_SENTENCE_CHARS = int(os.environ.get("VOICE_DESIGN_FIRST_SENTENCE_CHARS", "60"))
MAX_SENTENCE_CHARS = max(int(os.environ.get("VOICE_DESIGN_MAX_SENTENCE_CHARS", "300")), 40)
# The first piece of a cut first sentence is at least this long, so it is not a lone "Hmm,".
MIN_FIRST_CHARS = 20
# Sounds this voice performs: none. Bracketed cues are removed like any other bracketed text.
SOUND_TAGS: list[str] = []


# ------------------------------------------------------------------ text

# Bracketed text: [anything] (not nested), or an XML-like tag such as <laugh>, </laugh> or
# <break time="1s"/> (a letter or "/" right after "<", so "a < b" is left alone).
_BRACKETED = re.compile(r"\[([^\[\]]*)\]|<(/?[A-Za-z][^<>]{0,60})>")
_SPACE_BEFORE_MARK = re.compile(r"\s+([,.!?;:])")
# Emoji and pictographs a language model may add ("Have a great day! 🚗"): nothing to say.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF⬀-⯿‍️]+")


def clean_text(text: str) -> tuple[str, int]:
    """The text as the model gets it, and the number of bracketed parts removed.

    Qwen3-TTS reads what it is given, so sound cues ("[laugh]", "<sigh>"), stage directions
    ("[pause]", "[smiles warmly]"), markup ("<break/>"), stray square brackets and emoji are
    removed: none of them is ever read aloud.
    """
    text = _EMOJI.sub(" ", text)
    removed = 0
    parts: list[str] = []
    last = 0
    for m in _BRACKETED.finditer(text):
        parts.append(text[last : m.start()].replace("[", " ").replace("]", " "))
        removed += 1
        parts.append(" ")
        last = m.end()
    parts.append(text[last:].replace("[", " ").replace("]", " "))
    out = " ".join("".join(parts).split())
    # "Sure [pause], thanks" -> "Sure , thanks" -> "Sure, thanks"
    return _SPACE_BEFORE_MARK.sub(r"\1", out), removed


def has_words(text: str) -> bool:
    return bool(re.search(r"\w", text))


# A sentence end: ".", "!", "?" or an ellipsis, then closing quotes, then a space.
_SENTENCE_END = re.compile(r"(?:[.!?]+|…)[\"'”’)]*(?=\s)")
# Words a period does not end a sentence after ("Dr. Lee", "Main St. and").
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc", "approx", "dept",
    "inc", "ltd", "co", "corp", "ave", "blvd", "rd", "apt", "ft",
})
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)$")
# Where a sentence may be cut: after a comma, semicolon, colon, ellipsis or a spaced dash.
_CLAUSE_MARK = re.compile(r"(?:[,;:]|\.{2,}|…|\s[-–—])(?=\s)")


def _ends_with_abbreviation(before: str) -> bool:
    m = _LAST_WORD.search(before)
    if not m:
        return False
    word = m.group(1).lower()
    # "J." of an initial, "e.g", "a.m", "U.S" (a period inside the word)
    return word in _ABBREVIATIONS or len(word) == 1 or "." in word


def _clause_end(text: str, lo: int, hi: int, *, last: bool) -> int | None:
    ends = [m.end() for m in _CLAUSE_MARK.finditer(text) if lo <= m.end() <= hi]
    if not ends:
        return None
    return ends[-1] if last else ends[0]


def _word_end(text: str, hi: int) -> int:
    for m in reversed(list(re.finditer(r"\s", text[: hi + 1]))):
        if m.start() > 0:
            return m.start()
    return hi


def _split_long(text: str, limit: int) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = _clause_end(text, limit // 3, limit, last=True) or _word_end(text, limit)
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    parts.append(text)
    return [p for p in parts if p]


def split_sentences(text: str) -> list[str]:
    """Split a cleaned text into the pieces synthesised one at a time.

    A sentence ends at ".", "!", "?" or "..." followed by a space and a capital letter, digit
    or quote, unless the period closes an abbreviation ("Dr.", "e.g.", "J."). A first sentence
    longer than FIRST_SENTENCE_CHARS is cut after its first clause mark within that length,
    and any piece longer than MAX_SENTENCE_CHARS at clause marks, else between words.
    """
    pieces: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        after = text[m.end() :].lstrip()
        if not after or not (after[0].isupper() or after[0].isdigit() or after[0] in "\"'“‘("):
            continue
        if m.group().rstrip("\"'”’)") == "." and _ends_with_abbreviation(text[start : m.start()]):
            continue
        pieces.append(text[start : m.end()].strip())
        start = m.end()
    pieces.append(text[start:].strip())
    merged = [p for p in pieces if p]
    if merged and 0 < FIRST_SENTENCE_CHARS < len(merged[0]):
        first = merged[0]
        cut = _clause_end(first, MIN_FIRST_CHARS, FIRST_SENTENCE_CHARS, last=False)
        if cut and has_words(first[cut:]):
            merged[0:1] = [first[:cut].strip(), first[cut:].strip()]
    return [part for piece in merged for part in _split_long(piece, MAX_SENTENCE_CHARS)]


# ------------------------------------------------------------------ voice settings

VOICE_DIR = Path(os.environ.get("VOICE_DESIGN_VOICE_DIR", "/home/app/.cache/voices"))
MAX_VOICES = int(os.environ.get("VOICE_DESIGN_MAX_VOICES", "50"))
CLONING = os.environ.get("VOICE_DESIGN_CLONING", "true").strip().lower() != "false"
VOICE_CACHE = int(os.environ.get("VOICE_DESIGN_VOICE_CACHE", "16"))
# Saved voices hold the speech model's clone prompt: only a Qwen3-TTS 12 Hz model can use them.
PROMPT_FORMAT = "qwen3-tts-12hz"
CLONING_ALWAYS_ON = (
    "Voice cloning is always on with this voice server: Qwen3-TTS clones a voice from a "
    "sample without gated weights, so it needs no Hugging Face token (none was read or saved)."
)
CLONING_CANNOT_TURN_OFF = (
    "Voice cloning cannot be turned off from ATLAS on this voice server: it needs no token, so "
    "there is nothing to remove. Delete cloned voices you no longer use; the server's operator "
    "can turn cloning off with VOICE_DESIGN_CLONING=false."
)
CLONING_TURNED_OFF = (
    "Voice cloning from recordings is turned off on this voice server by its operator "
    "(VOICE_DESIGN_CLONING=false). Designed and built-in voices keep working."
)
DESIGN_TURNED_OFF = (
    "Voice design is not available on this voice server: its operator runs it without the "
    "design model (VOICE_DESIGN_DESIGN_MODEL is empty)."
)
DEFAULT_CONSENT_STATEMENT = "I have the speaker's permission to clone their voice."
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Qwen3-TTS clones from 3 s ("3-second rapid voice clone", model card); about 10 s is best.
MIN_SAMPLE_SECONDS = 3.0
MAX_SAMPLE_SECONDS = 30.0
# Common rates only: resampling between unrelated rates needs very long filters.
SAMPLE_RATES = frozenset(
    {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000}
)
CUSTOM_PREFIX = "custom-"
DESIGN_PREFIX = "design-"
CUSTOM_ID = re.compile(r"^custom-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{16}$")
DESIGN_ID = re.compile(r"^design-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{16}$")
MAX_NAME_CHARS = 60
MIN_DESCRIPTION_CHARS = 3
MAX_DESCRIPTION_CHARS = 1000
MAX_SAMPLE_TEXT_CHARS = 300
MAX_TRANSCRIPT_CHARS = 1000
MAX_SEED = 2**31 - 1
MAX_METADATA_BYTES = 16 * 1024
# Cloning and designing wait for the model like a sentence whose caller has this much audio
# left: live calls about to run out go first, but a new voice is not held back for long.
CLONE_DEADLINE_S = 1.0

# ------------------------------------------------------------------ phrase cache settings

# Keep in sync with the opener rule in infra/scripts/demo_configure.py and with the other
# voice servers: the demo agent opens every reply with one of these, so its first words come
# from memory instead of the model.
DEFAULT_CACHED_PHRASES = (
    "Got it.", "Okay.", "Sure.", "Great.", "Alright.", "Perfect.", "Of course.", "I see.",
    "Understood.", "That makes sense.", "No problem.", "Thanks.", "You're welcome.",
    "Certainly.", "Absolutely.", "Oh nice!", "Oh great!", "Love that.", "Ha, fair enough.",
    "Gotcha.", "Oh, great choice!", "Awesome.", "Nice!", "Cool.", "Sounds good.", "Oh, wow!",
)
_phrases_env = os.environ.get("VOICE_DESIGN_PHRASE_CACHE")
CACHED_PHRASES: list[str] = (
    list(DEFAULT_CACHED_PHRASES)
    if _phrases_env is None
    else [p.strip() for p in _phrases_env.split("|") if p.strip()]
)
_phrase_voices_env = os.environ.get("VOICE_DESIGN_PHRASE_CACHE_VOICES", "").strip()
PHRASE_CACHE_VOICES: list[str] = (
    [v.strip() for v in _phrase_voices_env.split(",") if v.strip()]
    if _phrase_voices_env
    else [DEFAULT_VOICE]
)
PHRASE_CACHE_MAX_CHARS = int(os.environ.get("VOICE_DESIGN_PHRASE_CACHE_MAX_CHARS", "32"))
PHRASE_CACHE_ANY_VOICE = (
    os.environ.get("VOICE_DESIGN_PHRASE_CACHE_ANY_VOICE", "true").lower() != "false"
)
BACKGROUND_DEADLINE_S = 10.0
PHRASE_CACHE_BYTES = int(
    float(os.environ.get("VOICE_DESIGN_PHRASE_CACHE_MB", "16")) * 1024 * 1024
)
# Audio is sent in pieces of this length (a sentence is made whole, then sent at once).
CHUNK_SECONDS = 0.1

_engine: Any = None  # QwenEngine once loaded (a fake in the tests)
# Voices: metadata of every voice on disk (the built-in one included), and an LRU of loaded
# clone prompts.
_voices: dict[str, dict[str, Any]] = {}
_prompts: OrderedDict[str, Any] = OrderedDict()
_state_lock = threading.Lock()
# One voice is made at a time.
_voice_lock = asyncio.Lock()


class SampleRejectedError(ValueError):
    """The request is not usable; the message is safe to show to the user."""


class UnsupportedMediaError(SampleRejectedError):
    """The upload is not a WAV file."""


# ------------------------------------------------------------------ the models

class GpuTurns:
    """One synthesis at a time on the model; waiting ones go earliest deadline first.

    A request's deadline is when its caller would run out of audio: the time it arrived while
    nothing has been sent, then the time its first audio was sent plus the seconds sent so far.
    So a new reply's first sentence goes before the later sentences of replies already
    playing, and those go in the order they would run dry.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._waiting: list[tuple[float, int]] = []
        self._order = itertools.count()
        self._busy = False

    @contextmanager
    def turn(self, deadline: float) -> Iterator[None]:
        key = (deadline, next(self._order))
        with self._cond:
            heapq.heappush(self._waiting, key)
            try:
                while self._busy or self._waiting[0] != key:
                    self._cond.wait()
            except BaseException:
                self._waiting.remove(key)
                heapq.heapify(self._waiting)
                self._cond.notify_all()
                raise
            heapq.heappop(self._waiting)
            self._busy = True
        try:
            yield
        finally:
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def waiting(self) -> int:
        with self._cond:
            return len(self._waiting)


_gpu = GpuTurns()


def token_cap(text: str) -> int:
    """Most codec tokens a synthesis may produce: the model makes 12.5 a second and speaks
    about 15 characters a second, so this is about three times the expected length. It
    bounds a generation that does not stop (the model's own default is 8192: 11 minutes)."""
    return min(8192, 60 + 3 * len(text))


class QwenEngine:
    """The parts of qwen-tts 0.1.1's Qwen3TTSModel that this server uses.

    torch and qwen_tts are imported in the methods, not at the top: the tests replace this
    class with a fake and run without them, and without a GPU.
    """

    def __init__(self, speech: Any, design: Any | None, device: str, dtype: str) -> None:
        self.speech = speech
        self.design_model = design
        self.device = device
        self.dtype = dtype
        self.sample_rate = int(speech.model.speech_tokenizer.get_output_sample_rate())
        # As the model names them ("english", ...), without "auto".
        self.languages = [
            str(lang) for lang in (speech.get_supported_languages() or []) if str(lang) != "auto"
        ]

    @property
    def can_design(self) -> bool:
        return self.design_model is not None

    def synthesize(self, text: str, prompt: Any, language: str) -> np.ndarray:
        """float32 audio of `text` in the voice of `prompt` (a clone prompt item), blocking."""
        wavs, _ = self.speech.generate_voice_clone(
            text=text, language=language, voice_clone_prompt=[prompt],
            max_new_tokens=token_cap(text),
        )
        return _check_audio(np.asarray(wavs[0], dtype=np.float32))

    def design(self, text: str, instruct: str, language: str, seed: int | None) -> np.ndarray:
        """float32 audio of `text` in the voice `instruct` describes (blocking)."""
        import torch

        if self.design_model is None:
            raise RuntimeError("the design model is not loaded")
        if seed is not None:
            torch.manual_seed(seed)
        wavs, _ = self.design_model.generate_voice_design(
            text=text, instruct=instruct, language=language, max_new_tokens=token_cap(text),
        )
        return _check_audio(np.asarray(wavs[0], dtype=np.float32))

    def clone_prompt(self, audio: np.ndarray, transcript: str | None) -> Any:
        """The speech model's clone prompt for a sample at the model's rate: with the
        transcript, the model continues the sample (ICL: best quality); without, only the
        speaker embedding is used."""
        items = self.speech.create_voice_clone_prompt(
            ref_audio=(audio, self.sample_rate), ref_text=transcript,
            x_vector_only_mode=transcript is None,
        )
        return items[0]

    def prompt_to_arrays(self, prompt: Any) -> tuple[dict[str, np.ndarray], dict[str, str]]:
        """A clone prompt as named arrays and string metadata (what safetensors can hold)."""
        arrays = {
            "ref_spk_embedding": np.ascontiguousarray(
                prompt.ref_spk_embedding.detach().float().cpu().numpy()
            )
        }
        if prompt.ref_code is not None:
            arrays["ref_code"] = np.ascontiguousarray(prompt.ref_code.detach().cpu().numpy())
        meta = {
            "mode": "xvector" if prompt.x_vector_only_mode else "icl",
            "ref_text": prompt.ref_text or "",
        }
        return arrays, meta

    def make_prompt(self, arrays: dict[str, np.ndarray], meta: dict[str, str]) -> Any:
        """A clone prompt from saved parts, on the model's device."""
        import torch
        from qwen_tts import VoiceClonePromptItem

        xvector = meta.get("mode") == "xvector"
        code = arrays.get("ref_code")
        # Copies: arrays loaded from safetensors may be read-only.
        return VoiceClonePromptItem(
            ref_code=None if xvector or code is None else torch.from_numpy(np.array(code, copy=True)).to(self.device),
            ref_spk_embedding=torch.from_numpy(
                np.array(arrays["ref_spk_embedding"], copy=True)
            ).to(self.device),
            x_vector_only_mode=xvector,
            icl_mode=not xvector,
            ref_text=meta.get("ref_text") or None,
        )


def _check_audio(audio: np.ndarray) -> np.ndarray:
    if audio.ndim != 1 or not np.isfinite(audio).all():
        # float16 overflow, or an empty generation: better refused than played as noise.
        raise RuntimeError("the model produced invalid audio")
    return audio


def _torch_dtype(name: str, device: str) -> Any:
    """The models' dtype. "auto": bfloat16 on a GPU that computes it natively (compute
    capability 8, Ampere, or newer), float32 otherwise. Not float16: the speech model's code
    predictor overflows it (measured on a T4: NaN logits, "probability tensor contains either
    inf, nan or element < 0" at the first synthesis)."""
    import torch

    if name == "auto":
        if device == "cuda" and torch.cuda.get_device_capability(0)[0] >= 8:
            name = "bfloat16"
        else:
            name = "float32"
    elif name == "float16":
        logger.warning(
            "VOICE_DESIGN_DTYPE=float16: Qwen3-TTS overflows float16 (NaN at the first synthesis "
            "on a T4); use bfloat16 or float32."
        )
    dtypes = {"float16": torch.float16, "bfloat16": torch.bfloat16, "float32": torch.float32}
    if name not in dtypes:
        raise RuntimeError(f"VOICE_DESIGN_DTYPE={name}: use auto, float16, bfloat16 or float32")
    return dtypes[name]


def _file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_engine() -> QwenEngine:
    """Download (first start only) and load the speech model and the design model (blocking)."""
    os.environ.setdefault("TQDM_DISABLE", "1")
    import torch
    from huggingface_hub import snapshot_download
    from qwen_tts import Qwen3TTSModel

    device = DEVICE
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "VOICE_DESIGN_DEVICE=cuda, but no CUDA GPU is visible: give the container the GPU "
            "(NVIDIA Container Toolkit) or set VOICE_DESIGN_DEVICE=cpu."
        )
    if device == "cpu":
        logger.warning(
            "No GPU: Qwen3-TTS runs on the CPU, many times slower than real time. Fine for "
            "trying it out, too slow for live calls."
        )
    else:
        logger.info("GPU: %s", torch.cuda.get_device_name(0))
    dtype = _torch_dtype(DTYPE, device)

    def load(repo: str, revision: str) -> tuple[Any, Path]:
        # Ungated weights: never send a token, even if the environment has one.
        path = snapshot_download(repo, revision=revision, allow_patterns=MODEL_FILES, token=False)
        model = Qwen3TTSModel.from_pretrained(
            path, device_map=device, dtype=dtype, attn_implementation=ATTENTION
        )
        return model, Path(path)

    started = time.perf_counter()
    speech, speech_path = load(SPEECH_REPO, SPEECH_REVISION)
    logger.info("Speech model %s loaded in %.1fs", SPEECH_REPO, time.perf_counter() - started)
    design = None
    if DESIGN_REPO:
        started = time.perf_counter()
        design, design_path = load(DESIGN_REPO, DESIGN_REVISION)
        logger.info("Design model %s loaded in %.1fs", DESIGN_REPO, time.perf_counter() - started)
        # Each repository ships its own copy of the speech tokenizer (the codec). When the two
        # files are the same, one copy serves both models and the other is freed.
        codec = "speech_tokenizer/model.safetensors"
        if _file_sha256(speech_path / codec) == _file_sha256(design_path / codec):
            design.model.load_speech_tokenizer(speech.model.speech_tokenizer)
            if device == "cuda":
                torch.cuda.empty_cache()
            logger.info("Speech tokenizer shared by both models (identical weights)")
        else:
            logger.info("Speech tokenizers differ between the two models: both kept")
    return QwenEngine(speech, design, device, str(dtype).replace("torch.", ""))


# ------------------------------------------------------------------ voices on disk


def _voice_path(voice_id: str, suffix: str) -> Path:
    # Ids are validated against CUSTOM_ID / DESIGN_ID (or are the built-in one) before they
    # reach a path.
    return VOICE_DIR / f"{voice_id}{suffix}"


def is_voice_id(voice_id: str) -> bool:
    return voice_id == BUILTIN_VOICE or bool(CUSTOM_ID.match(voice_id) or DESIGN_ID.match(voice_id))


def _prompt_for(voice_id: str) -> Any:
    with _state_lock:
        prompt = _prompts.get(voice_id)
        if prompt is not None:
            _prompts.move_to_end(voice_id)
            return prompt
        if voice_id not in _voices:
            raise KeyError(voice_id)
        prompt = load_prompt(_voice_path(voice_id, ".safetensors"))
        _prompts[voice_id] = prompt
        while len(_prompts) > max(VOICE_CACHE, 0):
            _prompts.popitem(last=False)
        return prompt


def save_prompt(path: Path, prompt: Any) -> None:
    arrays, meta = _engine.prompt_to_arrays(prompt)
    meta = {"format": PROMPT_FORMAT, **meta}
    _write_atomic(path, lambda p: safetensors.numpy.save_file(arrays, str(p), metadata=meta))


def load_prompt(path: Path) -> Any:
    # safetensors holds plain arrays: loading one executes no code (unlike torch.load).
    arrays = safetensors.numpy.load_file(str(path))
    with safe_open(str(path), framework="numpy") as f:
        meta = f.metadata() or {}
    if meta.get("format") != PROMPT_FORMAT:
        raise ValueError("not a voice of this model")
    return _engine.make_prompt(arrays, meta)


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:24].strip("-")
    return slug or "voice"


def new_voice_id(name: str, prefix: str) -> str:
    """`custom-<slug>-<16 hex>` or `design-<slug>-<16 hex>`: the random part makes ids
    unguessable."""
    return f"{prefix}{_slug(name)}-{secrets.token_hex(8)}"


def _clean_line(raw: str | None, what: str, low: int, high: int) -> str:
    text = " ".join((raw or "").split())
    if len(text) < low:
        raise SampleRejectedError(
            f"Give the voice a {what}." if low <= 1 else f"The {what} needs at least {low} characters."
        )
    if len(text) > high:
        raise SampleRejectedError(f"The {what} is longer than {high} characters.")
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise SampleRejectedError(f"The {what} contains control characters.")
    return text


def clean_name(raw: str | None) -> str:
    return _clean_line(raw, "name", 1, MAX_NAME_CHARS)


def clean_description(raw: str | None) -> str:
    return _clean_line(raw, "description", MIN_DESCRIPTION_CHARS, MAX_DESCRIPTION_CHARS)


def languages() -> list[str]:
    """The languages voices can be designed in, as shown to users ("English")."""
    names = _engine.languages if _engine is not None and _engine.languages else DEFAULT_LANGUAGES
    return [str(n).capitalize() for n in names]


def clean_language(raw: str | None) -> str:
    """One of the model's languages (any case) or "Auto"; the server's default when empty."""
    text = " ".join((raw or "").split())
    if not text:
        text = LANGUAGE
    if text.lower() == "auto":
        return "Auto"
    offered = languages()
    match = next((n for n in offered if n.lower() == text.lower()), None)
    if match is None:
        raise SampleRejectedError(
            f"Language {text[:32]!r} is not available. Languages: {', '.join(offered)}, Auto."
        )
    return match


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV file held in memory to mono float32 in [-1, 1] and its sample rate.

    Only RIFF/WAVE is accepted (PCM 8/16/24/32-bit or float), parsed by scipy; nothing in
    the upload is written to disk or executed.
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


def prepare_sample(data: bytes) -> tuple[np.ndarray, float]:
    """Decode, validate and resample an upload to the model's rate: (samples, seconds)."""
    audio, rate = decode_wav(data)
    seconds = audio.shape[0] / rate
    target = _engine.sample_rate
    if rate != target:
        audio = soxr.resample(audio, rate, target, quality="HQ")
    return np.ascontiguousarray(audio, dtype=np.float32), seconds


def _write_atomic(path: Path, write: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _wav_file(audio: np.ndarray, sample_rate: int) -> bytes:
    pcm = to_pcm(audio)
    return _wav_header(sample_rate, len(pcm)) + pcm


def _keep_voice(voice_id: str, meta: dict[str, Any], prompt: Any, sample: np.ndarray) -> None:
    """Write the voice's prompt, preview and record, then make it available."""
    VOICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    save_prompt(_voice_path(voice_id, ".safetensors"), prompt)
    wav = _wav_file(sample, _engine.sample_rate)
    _write_atomic(_voice_path(voice_id, ".wav"), lambda p: p.write_bytes(wav))
    _write_atomic(
        _voice_path(voice_id, ".json"),
        lambda p: p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"),
    )
    with _state_lock:
        _voices[voice_id] = meta
        _prompts[voice_id] = prompt
        while len(_prompts) > max(VOICE_CACHE, 0):
            _prompts.popitem(last=False)


def design_voice(
    name: str, description: str, language: str, sample_text: str, seed: int | None,
    voice_id: str | None = None,
) -> dict[str, Any]:
    """Render the identity sample with the design model, make the speech model's clone
    prompt from it and keep both (blocking; run in a worker thread)."""
    voice_id = voice_id or new_voice_id(name, DESIGN_PREFIX)
    started = time.perf_counter()
    with _gpu.turn(time.monotonic() + CLONE_DEADLINE_S):
        sample = _engine.design(sample_text, description, language, seed)
        prompt = _engine.clone_prompt(sample, sample_text)
    seconds = sample.shape[0] / _engine.sample_rate
    meta: dict[str, Any] = {
        "id": voice_id,
        "name": name,
        "type": "designed",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": PROMPT_FORMAT,
        "design_model": DESIGN_REPO,
        "description": description,
        "language": language,
        "sample_text": sample_text,
        "seed": seed,
        "sample_seconds": round(seconds, 2),
    }
    _keep_voice(voice_id, meta, prompt, sample)
    logger.info(
        "Designed voice %s: %d-character description, %.1f s sample, in %.1fs",
        voice_id, len(description), seconds, time.perf_counter() - started,
    )
    return meta


def create_voice(
    name: str, audio: np.ndarray, seconds: float, consent: str, transcript: str | None,
) -> dict[str, Any]:
    """Compute a cloned voice's prompt once and keep it, with a rendered preview (blocking;
    run in a worker thread). The uploaded audio is never written."""
    voice_id = new_voice_id(name, CUSTOM_PREFIX)
    started = time.perf_counter()
    with _gpu.turn(time.monotonic() + CLONE_DEADLINE_S):
        prompt = _engine.clone_prompt(audio, transcript)
        preview = _engine.synthesize(SAMPLE_TEXT, prompt, LANGUAGE)
    meta: dict[str, Any] = {
        "id": voice_id,
        "name": name,
        "type": "custom",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": PROMPT_FORMAT,
        "language": LANGUAGE,
        "sample_seconds": round(seconds, 2),
        "clone_mode": "icl" if transcript else "xvector",
        "consent": {"attested": True, "statement": consent},
    }
    _keep_voice(voice_id, meta, prompt, preview)
    logger.info(
        "Cloned voice %s from %.1f s of audio (%s) in %.1fs",
        voice_id, seconds, meta["clone_mode"], time.perf_counter() - started,
    )
    return meta


def delete_voice(voice_id: str) -> bool:
    with _state_lock:
        known = _voices.pop(voice_id, None) is not None
        _prompts.pop(voice_id, None)
    for suffix in (".safetensors", ".json", ".wav"):
        path = _voice_path(voice_id, suffix)
        if path.exists():
            known = True
            path.unlink()
    return known


def load_voice_index() -> None:
    """Read the record of every voice on disk (the prompts load on first use)."""
    found: dict[str, dict[str, Any]] = {}
    paths = sorted(VOICE_DIR.glob("*.json")) if VOICE_DIR.is_dir() else []
    for meta_path in paths:
        voice_id = meta_path.stem
        if not is_voice_id(voice_id) or not _voice_path(voice_id, ".safetensors").is_file():
            continue
        try:
            if meta_path.stat().st_size > MAX_METADATA_BYTES:
                raise ValueError("metadata too large")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping voice %s: unreadable metadata (%s)", voice_id, exc)
            continue
        if not isinstance(meta, dict) or meta.get("id") != voice_id:
            logger.warning("Skipping voice %s: metadata does not match", voice_id)
            continue
        if meta.get("model") != PROMPT_FORMAT:
            logger.warning(
                "Skipping voice %s: made for %s, this server runs %s",
                voice_id, meta.get("model"), PROMPT_FORMAT,
            )
            continue
        found[voice_id] = meta
    with _state_lock:
        _voices.clear()
        _voices.update(found)
        _prompts.clear()


def preload_prompts() -> None:
    newest = sorted(_voices.values(), key=lambda m: str(m.get("created_at")), reverse=True)
    for meta in reversed(newest[: max(VOICE_CACHE, 0)]):
        try:
            _prompt_for(meta["id"])
        except Exception as exc:
            logger.warning("Voice %s could not be loaded: %s", meta["id"], exc)
            with _state_lock:
                _voices.pop(meta["id"], None)


def ensure_default_voice() -> None:
    """The built-in voice: designed from DEFAULT_VOICE_DESCRIPTION at the first start and kept
    on disk; made again when the description, seed, language or sample text changed."""
    meta = _voices.get(BUILTIN_VOICE)
    wanted = {
        "description": DEFAULT_VOICE_DESCRIPTION, "seed": DEFAULT_VOICE_SEED,
        "language": clean_language(LANGUAGE), "sample_text": SAMPLE_TEXT,
    }
    if meta is not None and all(meta.get(k) == v for k, v in wanted.items()):
        return
    if not _engine.can_design:
        raise RuntimeError(
            "The built-in voice is not on disk (or its description changed) and the design "
            "model is not loaded: set VOICE_DESIGN_DESIGN_MODEL, or restore the voices volume."
        )
    logger.info("Designing the built-in voice (%s)", "first start" if meta is None else "settings changed")
    delete_voice(BUILTIN_VOICE)
    design_voice(
        BUILTIN_VOICE, wanted["description"], wanted["language"], wanted["sample_text"],
        wanted["seed"], voice_id=BUILTIN_VOICE,
    )


def cloning_on() -> bool:
    return CLONING and MAX_VOICES > 0


def design_on() -> bool:
    return _engine is not None and _engine.can_design and MAX_VOICES > 0


def _voice_out(meta: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "type": meta.get("type", "custom"),
        "created_at": meta.get("created_at"),
    }
    if out["type"] == "designed":
        out["description"] = meta.get("description")
        out["language"] = meta.get("language")
        out["seed"] = meta.get("seed")
    out["sample_seconds"] = meta.get("sample_seconds")
    return out


def cloning_state() -> dict[str, Any]:
    """The Pocket adapter's answer shape. Never "enabling" or "failed": nothing is loaded."""
    on = cloning_on()
    return {
        "state": "on" if on else "off",
        "message": None if on else CLONING_TURNED_OFF,
        "terms_url": MODEL_CARD_URL,
        "token_source": None,
    }


def capabilities() -> dict[str, Any]:
    return {
        "model": MODEL_ID,
        "cloning": cloning_on(),
        "design": design_on(),
        "instruct": INSTRUCT_MODE == "design" and design_on(),
        "sound_tags": [f"[{t}]" for t in SOUND_TAGS],
        "languages": languages(),
        "streaming": "sentence",
        "reference_seconds": {"min": int(MIN_SAMPLE_SECONDS), "max": int(MAX_SAMPLE_SECONDS)},
    }


# ------------------------------------------------------------------ phrase cache

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def normalize_phrase(text: str) -> str:
    """The cache key of a text: Unicode-normalised, straight quotes, single spaces, any case."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return " ".join(text.split()).casefold()


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:]


class PhraseCache:
    """Synthesised audio of short phrases, per voice, held in memory only (never on disk).

    Phrases made at startup are pinned; other short sentences are kept once spoken, least
    recently used evicted first. Total audio is bounded by `max_bytes`. (The same class as in
    the other voice servers.)
    """

    def __init__(self, max_bytes: int, max_chars: int) -> None:
        self.max_bytes = max(max_bytes, 0)
        self.max_chars = max(max_chars, 0)
        self._items: OrderedDict[tuple[str, str], tuple[bytes, ...]] = OrderedDict()
        self._pinned: set[tuple[str, str]] = set()
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
        """A pinned phrase that opens `text`, then ",", "." or "!" (and maybe more words):
        its audio and the rest of the text ("" when there is none), capitalised. Longest
        match wins."""
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
            return chunks, _capitalize(rest)

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
    words = [
        "['’]".join(re.escape(part) for part in word.split("'")) for word in stem.split()
    ]
    return re.compile(
        r"\s*" + r"\s+".join(words) + r"\s*[,.!](?:\s+(\S.*))?\s*", re.IGNORECASE | re.DOTALL
    )


_phrase_cache = PhraseCache(PHRASE_CACHE_BYTES, PHRASE_CACHE_MAX_CHARS)

# A kept phrase usually opens a reply, so it is stored from KEEP_LEAD_MS before its first sound.
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
    data = b"".join(chunks)
    if not data or not chunks[0]:
        return chunks
    start = sound_start(data, sample_rate)
    if not start:
        return chunks
    rest = data[start:]
    size = len(chunks[0])
    return [rest[i : i + size] for i in range(0, len(rest), size)]


def to_pcm(audio: np.ndarray) -> bytes:
    """float audio in [-1, 1] as 16-bit little-endian PCM."""
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def cut_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    size = int(sample_rate * CHUNK_SECONDS) * 2
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


def _voice_language(voice: str) -> str:
    return str(_voices.get(voice, {}).get("language") or clean_language(LANGUAGE))


def synth_pcm(text: str, voice: str, deadline: float, instructions: str | None = None) -> bytes:
    """One cleaned sentence in `voice` as 16-bit PCM, made when its turn comes (blocking).

    With `instructions` in design mode, a designed voice is rendered by the design model
    from its description and the instructions (the speech model takes none).
    """
    meta = _voices.get(voice) or {}
    language = _voice_language(voice)
    if instructions and INSTRUCT_MODE == "design" and meta.get("type") == "designed" and design_on():
        with _gpu.turn(deadline):
            audio = _engine.design(
                text, f"{meta['description']} {instructions}", language, meta.get("seed")
            )
        return to_pcm(audio)
    prompt = _prompt_for(voice)
    with _gpu.turn(deadline):
        audio = _engine.synthesize(text, prompt, language)
    return to_pcm(audio)


def prewarm_phrases(
    phrases: list[str], voices: list[str], cache: PhraseCache | None = None,
    wait_s: float = 0.0,
) -> None:
    """Synthesise and pin `phrases` for each voice (blocking)."""
    if cache is None:
        cache = _phrase_cache
    if not phrases or not voices:
        return
    started = time.perf_counter()
    rate = _engine.sample_rate
    kept = 0
    for voice in voices:
        for phrase in phrases:
            spoken = clean_text(phrase)[0]
            if not has_words(spoken):
                continue
            pcm = synth_pcm(spoken, voice, time.monotonic() + wait_s)
            if cache.put(voice, spoken, trim_leading_silence(cut_chunks(pcm, rate), rate), pin=True):
                kept += 1
            else:
                logger.warning("Phrase cache full: %d of the startup phrases were kept", kept)
                break
    stats = cache.stats()
    logger.info(
        "Phrase cache: %d phrase(s) for %s in %.1fs (%d KB)",
        kept, ",".join(voices), time.perf_counter() - started, stats["bytes"] // 1024,
    )


_phrase_voices_made: set[str] = set()
_phrase_threads: list[threading.Thread] = []


def ensure_phrases(voice: str, after: threading.Event | None = None) -> None:
    """Make the kept phrases for a voice the first time it speaks, in the background: once
    `after` is set (the first sentence of the reply that asked), then each phrase after live
    speech that would run out within BACKGROUND_DEADLINE_S."""
    if not PHRASE_CACHE_ANY_VOICE or not CACHED_PHRASES or voice in _phrase_voices_made:
        return
    _phrase_voices_made.add(voice)
    cache = _phrase_cache

    def make() -> None:
        if after is not None:
            after.wait(60)
        try:
            prewarm_phrases(CACHED_PHRASES, [voice], cache, wait_s=BACKGROUND_DEADLINE_S)
        except Exception as exc:  # a voice deleted meanwhile
            logger.warning("Kept phrases for voice %s could not be made: %s", voice, type(exc).__name__)
            _phrase_voices_made.discard(voice)

    thread = threading.Thread(target=make, daemon=True)
    _phrase_threads.append(thread)
    thread.start()


def _phrase_voices() -> list[str]:
    voices = []
    for v in PHRASE_CACHE_VOICES:
        if v in _voices:
            voices.append(v)
        else:
            logger.warning("VOICE_DESIGN_PHRASE_CACHE_VOICES: voice %s is not available; skipped", v)
    return voices


def _wav_header(sample_rate: int, data_size: int | None = None) -> bytes:
    """A WAV header; without `data_size` a streaming one (sizes unknown up front, so the
    maximum, common for live streams)."""
    if data_size is None:
        riff, data = 0xFFFFFFFF, 0xFFFFFFFF - 36
    else:
        riff, data = 36 + data_size, data_size
    return (
        b"RIFF" + struct.pack("<I", riff) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", data)
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _engine
    if INSTRUCT_MODE not in ("ignore", "design"):
        raise RuntimeError(f"VOICE_DESIGN_INSTRUCT={INSTRUCT_MODE}: use ignore or design")
    started = time.perf_counter()
    engine = load_engine()
    _engine = engine
    clean_language(LANGUAGE)  # a wrong VOICE_DESIGN_LANGUAGE stops the start, not a request
    load_voice_index()
    ensure_default_voice()
    preload_prompts()
    logger.info(
        "Voices: %d on disk; cloning %s, design %s",
        len(_voices), "on" if cloning_on() else "off", "on" if design_on() else "off",
    )
    if WARMUP:
        warm = time.perf_counter()
        synth_pcm("Hello there.", DEFAULT_VOICE, time.monotonic())
        logger.info("Warm-up synthesis in %.1fs", time.perf_counter() - warm)
    prewarm_phrases(CACHED_PHRASES, _phrase_voices())
    _phrase_voices_made.update(_phrase_voices())
    logger.info(
        "Qwen3-TTS ready in %.1fs (device=%s, dtype=%s, speech=%s@%s, design=%s@%s)",
        time.perf_counter() - started, engine.device, engine.dtype, SPEECH_REPO,
        SPEECH_REVISION[:12], DESIGN_REPO or "none", DESIGN_REVISION[:12] if DESIGN_REPO else "-",
    )
    yield


app = FastAPI(title="Qwen3-TTS voice design (OpenAI-compatible)", version="1.0.0", lifespan=lifespan)


def _check_auth(authorization: str | None) -> None:
    if API_KEY is None:
        return
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {API_KEY}".encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")


def _require_loaded() -> None:
    if _engine is None:
        raise HTTPException(status_code=503, detail="Model is still loading")


class SpeechRequest(BaseModel):
    model: str
    input: str = Field(min_length=1, max_length=4096)
    voice: str
    response_format: Literal["pcm", "wav"] = "pcm"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    instructions: str | None = Field(default=None, max_length=500)
    sample_rate: int | None = None


class DesignRequest(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(max_length=4000)
    language: str | None = Field(default=None, max_length=32)
    sample_text: str | None = Field(default=None, max_length=1000)
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED)


@app.get("/health")
async def health() -> dict[str, str]:
    if _engine is None:
        return {"status": "loading"}
    return {"status": "ok", "device": _engine.device}


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ID, "object": "model", "created": 0, "owned_by": "qwen",
            "variant": " + ".join(
                r.rsplit("Qwen3-TTS-12Hz-", 1)[-1] for r in (SPEECH_REPO, DESIGN_REPO) if r
            ),
            "repos": {"speech": SPEECH_REPO, "design": DESIGN_REPO or None},
            "languages": languages(),
            "sample_rate": _engine.sample_rate if _engine else 24000,
            "voices": ALLOWED_VOICES, "sound_tags": [f"[{t}]" for t in SOUND_TAGS],
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


@app.get("/v1/audio/sound-tags")
async def list_sound_tags(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {"tags": [f"[{t}]" for t in SOUND_TAGS]}


@app.get("/v1/capabilities")
async def get_capabilities(authorization: str | None = Header(default=None)) -> dict:
    """What this server can do, for ATLAS: cloning, design, instructions, sounds, languages,
    how speech is delivered and the sample length cloning accepts."""
    _check_auth(authorization)
    return capabilities()


# ------------------------------------------------------------------ voices


@app.get("/v1/voices")
async def list_all_voices(authorization: str | None = Header(default=None)) -> dict:
    """Built-in, designed and cloned voices."""
    _check_auth(authorization)
    _require_loaded()
    with _state_lock:
        made = sorted(
            (m for v, m in _voices.items() if v != BUILTIN_VOICE),
            key=lambda m: str(m.get("created_at")),
        )
    return {
        "object": "list",
        "data": [
            *[
                {"id": v, "name": v, "type": "builtin", "license": VOICE_LICENSES[v],
                 "default": v == DEFAULT_VOICE}
                for v in ALLOWED_VOICES
            ],
            *[_voice_out(m) for m in made],
        ],
    }


def _check_room() -> None:
    if sum(1 for v in _voices if v != BUILTIN_VOICE) >= MAX_VOICES:
        raise HTTPException(
            status_code=409,
            detail=f"This server already keeps {MAX_VOICES} voices: delete one first.",
        )


@app.post("/v1/voices/design", status_code=201)
async def design_custom_voice(
    body: DesignRequest, authorization: str | None = Header(default=None)
) -> dict:
    """Design a voice from a description: the design model renders a short identity sample
    of it (`sample_text`, or the server's own line), which the speech model then clones for
    every reply. `seed` makes the design repeatable. 201 with the voice."""
    _check_auth(authorization)
    _require_loaded()
    if not design_on():
        raise HTTPException(status_code=501, detail=DESIGN_TURNED_OFF)
    try:
        name = clean_name(body.name)
        description = clean_description(body.description)
        language = clean_language(body.language)
        sample_text = (
            _clean_line(body.sample_text, "sample text", 1, MAX_SAMPLE_TEXT_CHARS)
            if body.sample_text and body.sample_text.strip() else SAMPLE_TEXT
        )
    except SampleRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    spoken = clean_text(sample_text)[0]
    if not has_words(spoken):
        raise HTTPException(status_code=422, detail="The sample text says nothing.")
    _check_room()
    if _voice_lock.locked():
        raise HTTPException(
            status_code=429, detail="Another voice is being made. Try again in a moment."
        )
    async with _voice_lock:
        _check_room()
        try:
            meta = await asyncio.to_thread(
                design_voice, name, description, language, spoken, body.seed
            )
        except RuntimeError as exc:
            logger.error("Voice design failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=500, detail="The voice could not be designed. Try again."
            ) from None
    return _voice_out(meta)


@app.post("/v1/voices", status_code=201)
async def create_custom_voice(
    request: Request, authorization: str | None = Header(default=None)
) -> dict:
    """Clone a voice from a 3-30 s WAV sample of a speaker who gave permission.

    Multipart fields: `name`, `audio` (WAV, at most 10 MB), `consent` ("true"), an optional
    `consent_statement` kept with the voice, and an optional `transcript` of the sample
    (with it the model continues the sample, which clones better; without it only the
    speaker embedding is used). About 10 s of clear speech is best.
    """
    _check_auth(authorization)
    _require_loaded()
    if not cloning_on():
        raise HTTPException(status_code=501, detail=CLONING_TURNED_OFF)
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length is required")
    if int(length) > MAX_UPLOAD_BYTES + 64 * 1024:
        raise HTTPException(status_code=413, detail="The sample is larger than 10 MB.")
    _check_room()
    try:
        async with request.form(max_files=1, max_fields=5, max_part_size=4096) as form:
            consent = str(form.get("consent") or "").strip().lower()
            if consent != "true":
                raise HTTPException(
                    status_code=422,
                    detail="Consent is required: confirm you have the speaker's permission "
                    "to clone their voice (consent=true).",
                )
            statement = " ".join(str(form.get("consent_statement") or "").split())[:500]
            name = clean_name(str(form.get("name") or ""))
            transcript = " ".join(str(form.get("transcript") or "").split())[:MAX_TRANSCRIPT_CHARS] or None
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
    if _voice_lock.locked():
        raise HTTPException(
            status_code=429, detail="Another voice is being made. Try again in a moment."
        )
    async with _voice_lock:
        _check_room()
        try:
            audio, seconds = await asyncio.to_thread(prepare_sample, data)
        except UnsupportedMediaError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from None
        except SampleRejectedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        try:
            meta = await asyncio.to_thread(
                create_voice, name, audio, seconds, statement or DEFAULT_CONSENT_STATEMENT,
                transcript,
            )
        except RuntimeError as exc:
            logger.error("Voice cloning failed: %s", type(exc).__name__)
            raise HTTPException(
                status_code=500, detail="The voice could not be cloned. Try again."
            ) from None
    return _voice_out(meta)


@app.get("/v1/voices/{voice_id}/sample")
async def voice_sample(voice_id: str, authorization: str | None = Header(default=None)) -> Response:
    """A short WAV of the voice: a designed voice's identity sample, a cloned voice's
    rendered preview (never the uploaded recording)."""
    _check_auth(authorization)
    _require_loaded()
    if not is_voice_id(voice_id) or voice_id not in _voices:
        raise HTTPException(status_code=404, detail="Voice not found")
    path = _voice_path(voice_id, ".wav")
    try:
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise HTTPException(status_code=404, detail="This voice has no preview.") from None
    return Response(content=data, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


@app.delete("/v1/voices/{voice_id}", status_code=204)
async def delete_custom_voice(voice_id: str, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    _require_loaded()
    if not (CUSTOM_ID.match(voice_id) or DESIGN_ID.match(voice_id)):
        raise HTTPException(status_code=404, detail="Voice not found")
    if not await asyncio.to_thread(delete_voice, voice_id):
        raise HTTPException(status_code=404, detail="Voice not found")
    _phrase_cache.drop_voice(voice_id)
    _phrase_voices_made.discard(voice_id)
    return None


@app.get("/v1/cloning")
async def get_cloning(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return cloning_state()


@app.put("/v1/cloning")
async def put_cloning(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Nothing to turn on: 200 with the state and why. The request body (ATLAS sends a Hugging
    Face token here for the Pocket adapter) is never read, so no token is used or kept."""
    _check_auth(authorization)
    if not cloning_on():
        raise HTTPException(status_code=409, detail=CLONING_TURNED_OFF)
    return JSONResponse(status_code=200, content={**cloning_state(), "message": CLONING_ALWAYS_ON})


@app.delete("/v1/cloning", status_code=204)
async def delete_cloning(authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    if cloning_on():
        raise HTTPException(status_code=409, detail=CLONING_CANNOT_TURN_OFF)
    return None


@app.get("/v1/audio/phrase-cache")
async def phrase_cache_stats(authorization: str | None = Header(default=None)) -> dict:
    """Counts only (never the cached texts): entries, bytes, hits and misses."""
    _check_auth(authorization)
    return _phrase_cache.stats()


# ------------------------------------------------------------------ speech


@app.post("/v1/audio/speech")
async def speech(
    body: SpeechRequest, request: Request, authorization: str | None = Header(default=None)
) -> StreamingResponse:
    _check_auth(authorization)
    _require_loaded()
    if body.model != MODEL_ID:
        raise HTTPException(status_code=404, detail=f"Model '{body.model}' not found; use '{MODEL_ID}'")
    sample_rate = _engine.sample_rate
    if body.sample_rate not in (None, sample_rate):
        raise HTTPException(status_code=422, detail=f"Only sample_rate {sample_rate} is supported")
    voice = body.voice
    if voice in OPENAI_VOICE_NAMES:
        voice = DEFAULT_VOICE
    if voice not in ALLOWED_VOICES and not (
        (CUSTOM_ID.match(voice) or DESIGN_ID.match(voice)) and voice in _voices
    ):
        raise HTTPException(status_code=422, detail=f"Voice '{body.voice}' is not available.")
    if body.speed != 1.0:
        logger.info("Qwen3-TTS has no speed control; ignoring speed=%s", body.speed)
    instructions = " ".join((body.instructions or "").split()) or None
    if instructions and INSTRUCT_MODE != "design":
        logger.info("instructions ignored (VOICE_DESIGN_INSTRUCT=%s): %d chars", INSTRUCT_MODE, len(instructions))
        instructions = None
    elif instructions and _voices.get(voice, {}).get("type") != "designed":
        logger.info("instructions ignored for a cloned voice: %d chars", len(instructions))
        instructions = None
    text = body.input.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Input text is empty")
    spoken, removed = clean_text(text)
    media_type = "audio/wav" if body.response_format == "wav" else "audio/pcm"
    first_made = threading.Event()
    if instructions is None:
        ensure_phrases(voice, first_made)
    received = time.perf_counter()
    arrived = time.monotonic()

    # head: kept audio sent first; rest: text still to synthesise after it (None: nothing).
    head: tuple[bytes, ...] = ()
    rest: str | None = spoken
    status = "miss"
    if not has_words(spoken):  # only removed cues or punctuation: nothing to say
        rest = None
    elif instructions is not None:
        status = "bypass"  # delivery differs: kept audio is neither used nor stored
    elif (cached := _phrase_cache.get(voice, spoken)) is not None:
        head, rest, status = cached, None, "hit"
    else:
        split = _phrase_cache.opener(voice, spoken)
        if split is not None:
            head, rest = split
            status = "opener"
            if not rest:
                rest = None
            else:
                tail = _phrase_cache.get(voice, rest)
                if tail is not None:
                    head, rest = head + tail, None
    headers = {"X-Phrase-Cache": status}

    if rest is None:
        first_made.set()

        async def replay() -> AsyncIterator[bytes]:
            if body.response_format == "wav":
                yield _wav_header(sample_rate)
            for chunk in head:
                yield chunk
            # Lengths and counts only: the text itself is never logged.
            logger.info(
                "speech cache=%s chars=%d removed=%d audio_s=%.2f",
                status, len(text), removed, sum(len(c) for c in head) / (2 * sample_rate),
            )

        return StreamingResponse(replay(), media_type=media_type, headers=headers)

    sentences = split_sentences(rest)
    queue: Queue[tuple[bytes, ...] | BaseException | None] = Queue()
    stop = threading.Event()

    def worker() -> None:
        heard_from = arrived if head else None
        queued = sum(len(c) for c in head) / (2 * sample_rate)
        try:
            for i, sentence in enumerate(sentences):
                if stop.is_set():
                    break
                chunks = None
                if instructions is None and len(sentences) > 1 and _phrase_cache.cacheable(sentence):
                    chunks = _phrase_cache.get(voice, sentence)
                if chunks is None:
                    deadline = arrived if heard_from is None else heard_from + queued
                    pcm = synth_pcm(sentence, voice, deadline, instructions)
                    if instructions is None and _phrase_cache.cacheable(sentence):
                        _phrase_cache.put(
                            voice, sentence,
                            trim_leading_silence(cut_chunks(pcm, sample_rate), sample_rate),
                        )
                    if i == 0 and head:
                        pcm = pcm[sound_start(pcm, sample_rate) or 0 :]
                    chunks = tuple(cut_chunks(pcm, sample_rate))
                queue.put(chunks)
                first_made.set()
                if heard_from is None:
                    heard_from = time.monotonic()
                queued += sum(len(c) for c in chunks) / (2 * sample_rate)
        except BaseException as exc:  # surfaced to the response stream below
            queue.put(exc)
        finally:
            first_made.set()
            queue.put(None)

    threading.Thread(target=worker, daemon=True).start()
    loop = asyncio.get_running_loop()

    async def stream() -> AsyncIterator[bytes]:
        first_ms: float | None = None
        audio_bytes = 0
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
                    break
                if isinstance(item, BaseException):
                    # The type only: a library's message could quote the text.
                    logger.error("Synthesis failed: %s", type(item).__name__)
                    break
                for chunk in item:
                    if first_ms is None:
                        first_ms = (time.perf_counter() - received) * 1000
                    audio_bytes += len(chunk)
                    yield chunk
                if await request.is_disconnected():
                    break
        finally:
            stop.set()
            logger.info(
                "speech cache=%s chars=%d sentences=%d removed=%d instructions=%s "
                "first_audio_ms=%s audio_s=%.2f total_ms=%.0f",
                status, len(text), len(sentences), removed, "yes" if instructions else "no",
                "-" if first_ms is None else f"{first_ms:.0f}",
                audio_bytes / (2 * sample_rate), (time.perf_counter() - received) * 1000,
            )

    return StreamingResponse(stream(), media_type=media_type, headers=headers)
