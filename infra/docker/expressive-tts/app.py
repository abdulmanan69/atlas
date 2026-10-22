"""OpenAI-compatible speech server for Resemble AI Chatterbox-Turbo (GPU; CPU works, slowly).

The voice runtime talks to self-hosted voices through the OpenAI speech API
(`POST /v1/audio/speech` with `response_format: "pcm"`, read as 24 kHz, 16-bit, mono). This
server answers it the way the Pocket TTS adapter (infra/docker/pocket-tts) does: same request,
same audio, same phrase cache, same voice and cloning endpoints, so either can be the voice of
an agent.

What it adds: human sounds. Chatterbox-Turbo performs a few sounds written in the text as
tags, such as "[laugh]", "[sigh]" or "[gasp]" (GET /v1/audio/sound-tags lists them); any other
bracketed text is removed before synthesis, so it is never read aloud. Turbo makes a whole
sentence at a time (it does not stream within one), so a reply is split into sentences and
each is sent as soon as it is made: the first is heard after about 0.9 s on a T4, a kept
opener at once.

Voices: the model's built-in voice ("default": conds.pt of ResembleAI/chatterbox-turbo, MIT)
and cloned voices. Cloning needs no gated weights and no token: `POST /v1/voices` turns a
5-30 s WAV sample of a consenting speaker into a reusable voice. Only the computed
conditioning (safetensors, about 0.2 MB) and a small JSON record are kept; the uploaded audio
is decoded in memory and discarded. Every output carries Resemble AI's Perth watermark
(imperceptible, added by the library; the server does not start without it). The model card
asks not to use the model "to do bad things": clone a voice only with the speaker's consent,
never to deceive anyone or to pass generated speech off as a real person.

Endpoints (all but /health need EXPRESSIVE_TTS_API_KEY when it is set):
  GET    /health                 "ok" once the model is loaded and warm
  GET    /v1/models, /v1/audio/voices  the model id and the built-in voice
  GET    /v1/audio/sound-tags    {"tags": ["[laugh]", ...]}: the sounds this voice performs
  POST   /v1/audio/speech        OpenAI speech API: 24 kHz 16-bit mono PCM or WAV, streamed
                                 sentence by sentence
  GET    /v1/audio/phrase-cache  phrase cache counts (never the texts)
  GET    /v1/cloning             state "on" (or "off" when the operator turned cloning off),
                                 in the Pocket adapter's shape
  PUT    /v1/cloning             200, state "on": there is nothing to turn on (the body, which
                                 may hold a Hugging Face token, is never read)
  DELETE /v1/cloning             409: cloning needs no token, so there is nothing to remove
  GET    /v1/voices, POST /v1/voices, DELETE /v1/voices/{id}  built-in and cloned voices

Environment:
  EXPRESSIVE_TTS_MODEL_ID        id clients send as `model` (default "chatterbox-turbo")
  EXPRESSIVE_TTS_MODEL_REVISION  revision of ResembleAI/chatterbox-turbo to download (default:
                                 the one measured, see MODEL_REVISION)
  EXPRESSIVE_TTS_DEVICE          "auto" (default: the GPU when there is one), "cuda" or "cpu"
  EXPRESSIVE_TTS_DEFAULT_VOICE   voice used when a request names an OpenAI voice (default
                                 "default", the only built-in voice)
  EXPRESSIVE_TTS_SOUND_TAGS      comma-separated sounds to perform (default: the nine of
                                 DEFAULT_SOUND_TAGS); other tags are removed like any
                                 bracketed text
  EXPRESSIVE_TTS_API_KEY         if set, require `Authorization: Bearer <key>`
  EXPRESSIVE_TTS_WARMUP          "1" (default) runs one short synthesis at startup (the first
                                 one takes about 17 s on a T4: CUDA kernels and the watermarker)
  EXPRESSIVE_TTS_FIRST_SENTENCE_CHARS  a first sentence longer than this is cut after its
                                 first clause mark (",", ";", ":", "...", " - ") within the
                                 limit, so the reply starts sooner (default 60; 0 = never)
  EXPRESSIVE_TTS_MAX_SENTENCE_CHARS  longer sentences are cut at clause marks, else between
                                 words (default 300; Turbo makes at most 40 s per call)
  EXPRESSIVE_TTS_VOICE_DIR       where cloned voices are kept (default /home/app/.cache/voices,
                                 inside the weights volume)
  EXPRESSIVE_TTS_MAX_CUSTOM_VOICES  most cloned voices kept on disk (default 50; 0 turns voice
                                 cloning off: the /v1/voices endpoints answer 501)
  EXPRESSIVE_TTS_CUSTOM_VOICE_CACHE  cloned voices held in memory (on the GPU), least recently
                                 used evicted first (default 16; about 0.2 MB each)
  EXPRESSIVE_TTS_PHRASE_CACHE    "|"-separated phrases synthesised at startup and kept, so a
                                 reply that opens with one of them starts at once (default: the
                                 demo agent's openers, DEFAULT_CACHED_PHRASES; "" = none)
  EXPRESSIVE_TTS_PHRASE_CACHE_VOICES  comma-separated voices those phrases are made for
                                 (default: EXPRESSIVE_TTS_DEFAULT_VOICE)
  EXPRESSIVE_TTS_PHRASE_CACHE_ANY_VOICE  "true" (default): any other voice, a cloned one too,
                                 gets them made in the background the first time it speaks
  EXPRESSIVE_TTS_PHRASE_CACHE_MAX_CHARS  other sentences up to this many characters are kept
                                 too once spoken, least recently used evicted first (default
                                 32; 0 = only the startup phrases)
  EXPRESSIVE_TTS_PHRASE_CACHE_MB memory for all kept audio (default 16; about 330 s of speech)
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
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
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from safetensors import safe_open
from starlette.datastructures import UploadFile

logger = logging.getLogger("expressive_tts_server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
# huggingface_hub's client logs every request of the weights download at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)

REPO_ID = "ResembleAI/chatterbox-turbo"
# The revision measured on a T4 (docs/perf/gpu-model-eval.md, 2026-10-22). Pinned so that a new
# upload cannot change the voice, or the format of saved voices, under a running deployment.
MODEL_REVISION = os.environ.get(
    "EXPRESSIVE_TTS_MODEL_REVISION", "749d1c1a46eb10492095d68fbcf55691ccf137cd"
)
# What ChatterboxTurboTTS.from_local reads (chatterbox-tts 0.1.7). from_pretrained would also
# download s3gen.safetensors (1 GB), the decoder of the non-Turbo model, which Turbo never loads.
MODEL_FILES = [
    "t3_turbo_v1.safetensors", "s3gen_meanflow.safetensors", "ve.safetensors", "conds.pt",
    "*.json", "merges.txt",
]
MODEL_CARD_URL = f"https://huggingface.co/{REPO_ID}"
BUILTIN_VOICE = "default"
# conds.pt is part of the model repository (MIT). Resemble AI does not say whose recording it
# was made from; there are no other built-in voices.
VOICE_LICENSES: dict[str, str] = {BUILTIN_VOICE: "MIT"}
ALLOWED_VOICES = list(VOICE_LICENSES)
# Turbo is English only; a cloned voice keeps the accent of its sample.
LANGUAGE = "english"
OPENAI_VOICE_NAMES = {
    "alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse",
}

MODEL_ID = os.environ.get("EXPRESSIVE_TTS_MODEL_ID", "chatterbox-turbo")
DEVICE = os.environ.get("EXPRESSIVE_TTS_DEVICE", "auto").strip().lower() or "auto"
DEFAULT_VOICE = os.environ.get("EXPRESSIVE_TTS_DEFAULT_VOICE", BUILTIN_VOICE)
API_KEY = os.environ.get("EXPRESSIVE_TTS_API_KEY") or None
WARMUP = os.environ.get("EXPRESSIVE_TTS_WARMUP", "1") == "1"
FIRST_SENTENCE_CHARS = int(os.environ.get("EXPRESSIVE_TTS_FIRST_SENTENCE_CHARS", "60"))
MAX_SENTENCE_CHARS = max(int(os.environ.get("EXPRESSIVE_TTS_MAX_SENTENCE_CHARS", "300")), 40)
# The first piece of a cut first sentence is at least this long, so it is not a lone "Hmm,".
MIN_FIRST_CHARS = 20


# ------------------------------------------------------------------ sound tags

# The sounds of Resemble AI's own Turbo demo (EVENT_TAGS in gradio_tts_turbo_app.py,
# github.com/resemble-ai/chatterbox); each is one token of the model's tokenizer
# (added_tokens.json). The tokenizer also holds undocumented style tokens ([angry], [happy],
# [whispering], ...): of the two tried in the evaluation, [surprised] turned "Wow!" into
# "Well, how?" (docs/perf/gpu-tts-eval.json), so none of them is used by default.
DEFAULT_SOUND_TAGS = (
    "laugh", "chuckle", "sigh", "gasp", "cough", "clear throat", "groan", "sniff", "shush",
)
# Other spellings of a sound, besides its own forms (_tag_forms).
_EXTRA_FORMS = {"laugh": "laughter", "sniff": "sniffl(?:e|es|ing)", "shush": "shh+"}


def _tag_name(raw: str) -> str:
    """'[Clear  Throat]' -> 'clear throat'."""
    return " ".join(raw.strip().strip("[]<>/").replace("_", " ").split()).casefold()


def _tag_forms(tag: str) -> str:
    """'laugh' -> laugh, laughs, laughed, laughing; 'clear throat' -> clears his throat, ...:
    the forms a language model writes, the same ones the Pocket TTS adapter removes."""
    first, _, rest = tag.partition(" ")
    if first.endswith("e"):
        word = re.escape(first[:-1]) + "(?:e|es|ed|ing)"
    else:
        word = re.escape(first) + "(?:s|es|ed|ing)?"
    if rest:
        word += r"(?:\s+(?:my|his|her|their|the))?\s+" + re.escape(rest)
    if tag in _EXTRA_FORMS:
        word = f"(?:{word}|{_EXTRA_FORMS[tag]})"
    return word


@functools.lru_cache(maxsize=64)
def _tag_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(_tag_forms(tag))


_tags_env = os.environ.get("EXPRESSIVE_TTS_SOUND_TAGS")
SOUND_TAGS: list[str] = (
    list(DEFAULT_SOUND_TAGS)
    if _tags_env is None
    else [t for t in (_tag_name(x) for x in _tags_env.split(",")) if t]
)

# Bracketed text: [anything] (not nested), or an XML-like tag such as <laugh>, </laugh> or
# <break time="1s"/> (a letter or "/" right after "<", so "a < b" is left alone).
_BRACKETED = re.compile(r"\[([^\[\]]*)\]|<(/?[A-Za-z][^<>]{0,60})>")
_TAG = re.compile(r"\[[^\[\]]*\]")
_SPACE_BEFORE_MARK = re.compile(r"\s+([,.!?;:])")


# Emoji and pictographs a language model may add ("Have a great day! 🚗"): nothing to say
# (the Pocket TTS adapter's set).
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF⬀-⯿‍️]+")


def sound_tag(inner: str) -> str | None:
    """The supported sound a bracketed text names ("Laughs" -> "laugh"), or None."""
    name = _tag_name(inner)
    return next((t for t in SOUND_TAGS if _tag_pattern(t).fullmatch(name)), None)


def clean_text(text: str) -> tuple[str, int, int]:
    """The text as the model gets it, with the number of sound tags kept and of bracketed
    parts removed.

    Supported sounds are written the model's way ("[Laughs]", "[clears his throat]" and
    "<laugh>" become "[laugh]" and "[clear throat]"); any other bracketed text ("[pause]",
    "<break/>", "[smiles warmly]"), stray square brackets and emoji are removed, so they are
    never read aloud.
    """
    text = _EMOJI.sub(" ", text)
    kept = removed = 0
    parts: list[str] = []
    last = 0
    for m in _BRACKETED.finditer(text):
        parts.append(text[last : m.start()].replace("[", " ").replace("]", " "))
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        tag = None if inner.startswith("/") else sound_tag(inner)
        if tag is None:
            removed += 1
            parts.append(" ")
        else:
            kept += 1
            parts.append(f" [{tag}] ")
        last = m.end()
    parts.append(text[last:].replace("[", " ").replace("]", " "))
    out = " ".join("".join(parts).split())
    # "Sure [pause], thanks" -> "Sure , thanks" -> "Sure, thanks"
    return _SPACE_BEFORE_MARK.sub(r"\1", out), kept, removed


def has_words(text: str) -> bool:
    """Whether `text` says anything besides its sound tags."""
    return bool(re.search(r"\w", _TAG.sub(" ", text)))


# A sentence end: ".", "!", "?" or an ellipsis, then closing quotes, then a space.
_SENTENCE_END = re.compile(r"(?:[.!?]+|…)[\"'”’)]*(?=\s)")
# Words a period does not end a sentence after ("Dr. Lee", "Main St. and"). Not "no": "No."
# is a sentence in a phone call far more often than "No. 5".
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
    """The last space at or before `hi` that is not inside a tag ("[clear throat]")."""
    tags = [m.span() for m in _TAG.finditer(text)]
    for m in reversed(list(re.finditer(r"\s", text[: hi + 1]))):
        if m.start() > 0 and not any(a < m.start() < b for a, b in tags):
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

    A sentence ends at ".", "!", "?" or "..." followed by a space and a capital letter, digit,
    quote or tag, unless the period closes an abbreviation ("Dr.", "e.g.", "J."). Sound tags
    stay with their sentence: a tag right after a sentence end opens the next sentence
    ("Realty. [laugh] Sorry, ..." -> "[laugh] Sorry, ..."), a tag at the very end closes the
    last one, and a piece of tags only joins its neighbour. A first sentence longer than
    FIRST_SENTENCE_CHARS is cut after its first clause mark within that length, and any piece
    longer than MAX_SENTENCE_CHARS at clause marks, else between words.
    """
    pieces: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        after = text[m.end() :].lstrip()
        if not after or not (after[0].isupper() or after[0].isdigit() or after[0] in "[\"'“‘("):
            continue
        if m.group().rstrip("\"'”’)") == "." and _ends_with_abbreviation(text[start : m.start()]):
            continue
        pieces.append(text[start : m.end()].strip())
        start = m.end()
    pieces.append(text[start:].strip())
    merged: list[str] = []
    for piece in filter(None, pieces):
        if merged and not has_words(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    if len(merged) > 1 and not has_words(merged[-1]):
        tail = merged.pop()
        merged[-1] = f"{merged[-1]} {tail}"
    if merged and 0 < FIRST_SENTENCE_CHARS < len(merged[0]):
        first = merged[0]
        cut = _clause_end(first, MIN_FIRST_CHARS, FIRST_SENTENCE_CHARS, last=False)
        if cut and has_words(first[cut:]):
            merged[0:1] = [first[:cut].strip(), first[cut:].strip()]
    return [part for piece in merged for part in _split_long(piece, MAX_SENTENCE_CHARS)]


def check_sound_tags(added_tokens: set[str]) -> None:
    """Keep only the configured sounds that are single tokens of the model's tokenizer: any
    other tag would be read out as words, so it is removed from texts like unknown ones."""
    missing = [t for t in SOUND_TAGS if f"[{t}]" not in added_tokens]
    for tag in missing:
        logger.warning("Sound tag [%s] is not a token of this model: it is removed from texts", tag)
    SOUND_TAGS[:] = [t for t in SOUND_TAGS if t not in missing]


# ------------------------------------------------------------------ voice cloning settings

VOICE_DIR = Path(os.environ.get("EXPRESSIVE_TTS_VOICE_DIR", "/home/app/.cache/voices"))
MAX_CUSTOM_VOICES = int(os.environ.get("EXPRESSIVE_TTS_MAX_CUSTOM_VOICES", "50"))
CUSTOM_VOICE_CACHE = int(os.environ.get("EXPRESSIVE_TTS_CUSTOM_VOICE_CACHE", "16"))
# Saved voices hold the model's conditioning tensors: only this model can use them.
CONDS_FORMAT = "chatterbox-turbo"
CLONING_ALWAYS_ON = (
    "Voice cloning is always on with this voice server: Chatterbox-Turbo clones a voice from a "
    "sample without gated weights, so it needs no Hugging Face token (none was read or saved)."
)
CLONING_CANNOT_TURN_OFF = (
    "Voice cloning cannot be turned off from ATLAS on this voice server: it needs no token, so "
    "there is nothing to remove. Delete cloned voices you no longer use; the server's operator "
    "can turn cloning off with EXPRESSIVE_TTS_MAX_CUSTOM_VOICES=0."
)
CLONING_TURNED_OFF = (
    "Voice cloning is turned off on this voice server by its operator "
    "(EXPRESSIVE_TTS_MAX_CUSTOM_VOICES=0). Built-in voices keep working."
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
# Cloning waits for the model like a sentence whose caller has this much audio left: live
# calls about to run out go first, but a clone is not held back for long.
CLONE_DEADLINE_S = 1.0

# ------------------------------------------------------------------ phrase cache settings

# Keep in sync with the opener rule in infra/scripts/demo_configure.py and with the Pocket TTS
# adapter: the demo agent opens every reply with one of these, so its first words come from
# memory instead of the model. The list also holds the openers qwen2.5:1.5b picks on its own
# most often ("Perfect.", "Of course.", "You're welcome.", "Certainly.", "Absolutely."), and
# the reactions friendly agents vary between ("Oh nice!", "Ha, fair enough.").
DEFAULT_CACHED_PHRASES = (
    "Got it.", "Okay.", "Sure.", "Great.", "Alright.", "Perfect.", "Of course.", "I see.",
    "Understood.", "That makes sense.", "No problem.", "Thanks.", "You're welcome.",
    "Certainly.", "Absolutely.", "Oh nice!", "Oh great!", "Love that.", "Ha, fair enough.",
    "Gotcha.", "Oh, great choice!", "Awesome.", "Nice!", "Cool.", "Sounds good.", "Oh, wow!",
)
_phrases_env = os.environ.get("EXPRESSIVE_TTS_PHRASE_CACHE")
CACHED_PHRASES: list[str] = (
    list(DEFAULT_CACHED_PHRASES)
    if _phrases_env is None
    else [p.strip() for p in _phrases_env.split("|") if p.strip()]
)
_phrase_voices_env = os.environ.get("EXPRESSIVE_TTS_PHRASE_CACHE_VOICES", "").strip()
PHRASE_CACHE_VOICES: list[str] = (
    [v.strip() for v in _phrase_voices_env.split(",") if v.strip()]
    if _phrase_voices_env
    else [DEFAULT_VOICE]
)
PHRASE_CACHE_MAX_CHARS = int(os.environ.get("EXPRESSIVE_TTS_PHRASE_CACHE_MAX_CHARS", "32"))
# Any other voice (a cloned one too) gets the same phrases made in the background the first
# time it speaks, so its replies also open from memory; "false" = only the voices above.
PHRASE_CACHE_ANY_VOICE = (
    os.environ.get("EXPRESSIVE_TTS_PHRASE_CACHE_ANY_VOICE", "true").lower() != "false"
)
# Those background phrases wait for the model like sentences whose callers still have this
# much audio to play: live speech that would run out sooner goes first.
BACKGROUND_DEADLINE_S = 10.0
PHRASE_CACHE_BYTES = int(
    float(os.environ.get("EXPRESSIVE_TTS_PHRASE_CACHE_MB", "16")) * 1024 * 1024
)
# Audio is sent in pieces of this length (a sentence is made whole, then sent at once).
CHUNK_SECONDS = 0.1

_engine: Any = None  # TurboEngine once loaded (a fake in the tests)
# Cloned voices: metadata of every voice on disk, and an LRU of loaded conditionings.
_custom_voices: dict[str, dict[str, Any]] = {}
_custom_conds: OrderedDict[str, Any] = OrderedDict()
_state_lock = threading.Lock()
# One voice is computed at a time.
_clone_lock = asyncio.Lock()


class SampleRejectedError(ValueError):
    """The uploaded sample is not usable; the message is safe to show to the user."""


class UnsupportedMediaError(SampleRejectedError):
    """The upload is not a WAV file."""


# ------------------------------------------------------------------ the model

class GpuTurns:
    """One synthesis at a time on the model; waiting ones go earliest deadline first.

    A request's deadline is when its caller would run out of audio: the time it arrived while
    nothing has been sent, then the time its first audio was sent plus the seconds sent so far.
    So a new reply's first sentence goes before the later sentences of replies already
    playing, and those go in the order they would run dry. (With a plain lock the thread that
    just finished a sentence usually takes the next turn too, so one long reply would hold back
    every other call.)
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


class TurboEngine:
    """The parts of chatterbox-tts 0.1.7's ChatterboxTurboTTS that this server uses.

    torch and chatterbox are imported in the methods, not at the top: the tests replace this
    class with a fake and run without them, and without a GPU.
    """

    def __init__(self, model: Any) -> None:
        self.model = model
        self.sample_rate = int(model.sr)
        self.device = str(model.device)
        self.builtin = model.conds  # conds.pt of the model repository: the built-in voice

    def synthesize(self, text: str, conds: Any) -> np.ndarray:
        """Watermarked float32 audio of `text` in the voice of `conds` (blocking)."""
        import torch

        self.model.conds = conds
        # The library prints "S3 Token -> Mel Inference..." for every sentence. Only stray
        # prints are lost: log handlers keep the streams they were given at startup, and one
        # synthesis runs at a time.
        with torch.inference_mode(), contextlib.redirect_stdout(io.StringIO()):
            wav = self.model.generate(text)
        return wav.squeeze(0).detach().float().cpu().numpy()

    def conditionals(self, audio: np.ndarray) -> Any:
        """ChatterboxTurboTTS.prepare_conditionals (0.1.7) for a sample already in memory at
        the model's rate, without replacing the voice the model holds.

        As in the library: loudness set to -27 LUFS; the decoder is conditioned on the first
        10 s, the speech-token prompt on the first 15 s, the speaker embedding on all of it.
        """
        import librosa
        import torch
        from chatterbox.models.s3tokenizer import S3_SR
        from chatterbox.models.t3.modules.cond_enc import T3Cond
        from chatterbox.tts_turbo import Conditionals

        model = self.model
        with torch.inference_mode():
            # float32 as librosa.load gives it to the library (the gain is a float64 scalar).
            wav = np.asarray(model.norm_loudness(audio, self.sample_rate), dtype=np.float32)
            ref_16k = librosa.resample(wav, orig_sr=self.sample_rate, target_sr=S3_SR)
            ref_16k = np.asarray(ref_16k, dtype=np.float32)
            gen = model.s3gen.embed_ref(
                wav[: model.DEC_COND_LEN], self.sample_rate, device=model.device
            )
            tokens, _ = model.s3gen.tokenizer.forward(
                [ref_16k[: model.ENC_COND_LEN]], max_len=model.t3.hp.speech_cond_prompt_len
            )
            speaker = torch.from_numpy(model.ve.embeds_from_wavs([ref_16k], sample_rate=S3_SR))
            t3 = T3Cond(
                speaker_emb=speaker.mean(axis=0, keepdim=True).to(model.device),
                cond_prompt_speech_tokens=torch.atleast_2d(tokens).to(model.device),
                emotion_adv=0.5 * torch.ones(1, 1, 1),
            ).to(device=model.device)
        return Conditionals(t3, gen)

    def make_conds(self, t3: dict[str, Any], gen: dict[str, Any]) -> Any:
        """A conditioning from saved parts (arrays_to_parts), on the model's device."""
        import torch
        from chatterbox.models.t3.modules.cond_enc import T3Cond
        from chatterbox.tts_turbo import Conditionals

        def tensor(value: Any) -> Any:
            # A copy: arrays loaded from safetensors may be read-only.
            return None if value is None else torch.from_numpy(np.array(value, copy=True))

        conds = Conditionals(
            T3Cond(**{k: tensor(v) for k, v in t3.items()}), {k: tensor(v) for k, v in gen.items()}
        )
        return conds.to(self.device)

    def added_tokens(self) -> set[str]:
        return set(self.model.tokenizer.get_added_vocab())


def load_engine() -> TurboEngine:
    """Download (first start only) and load Chatterbox-Turbo (blocking)."""
    # The library shows a progress bar for every sentence.
    os.environ.setdefault("TQDM_DISABLE", "1")
    import perth
    import torch
    from huggingface_hub import snapshot_download

    # perth sets this to None when it cannot import itself (setuptools 81 removed the
    # pkg_resources it needs); the library would then fail on its first sentence.
    if getattr(perth, "PerthImplicitWatermarker", None) is None:
        raise RuntimeError(
            "The Perth watermarker did not load (it needs setuptools<81): every output must "
            "carry it, so the server does not start without it."
        )
    from chatterbox.tts_turbo import ChatterboxTurboTTS

    device = DEVICE
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "EXPRESSIVE_TTS_DEVICE=cuda, but no CUDA GPU is visible: give the container the GPU "
            "(NVIDIA Container Toolkit) or set EXPRESSIVE_TTS_DEVICE=cpu."
        )
    if device == "cpu":
        logger.warning(
            "No GPU: Chatterbox-Turbo runs on the CPU, several times slower than real time. "
            "Fine for trying it out, too slow for live calls."
        )
    else:
        logger.info("GPU: %s", torch.cuda.get_device_name(0))
    # Ungated weights: never send a token, even if the environment has one.
    path = snapshot_download(
        REPO_ID, revision=MODEL_REVISION, allow_patterns=MODEL_FILES, token=False
    )
    return TurboEngine(ChatterboxTurboTTS.from_local(path, device))


# ------------------------------------------------------------------ voices


def _voice_file(voice_id: str) -> Path:
    # Ids are validated against CUSTOM_ID before they reach a path.
    return VOICE_DIR / f"{voice_id}.safetensors"


def _voice_conds(voice: str) -> Any:
    if voice.startswith(CUSTOM_PREFIX):
        return _custom_conds_for(voice)
    return _engine.builtin


def _custom_conds_for(voice_id: str) -> Any:
    with _state_lock:
        conds = _custom_conds.get(voice_id)
        if conds is not None:
            _custom_conds.move_to_end(voice_id)
            return conds
        if voice_id not in _custom_voices:
            raise KeyError(voice_id)
        conds = load_conds(_voice_file(voice_id))
        _custom_conds[voice_id] = conds
        while len(_custom_conds) > max(CUSTOM_VOICE_CACHE, 0):
            _custom_conds.popitem(last=False)
        return conds


def conds_to_arrays(conds: Any) -> tuple[dict[str, np.ndarray], list[str]]:
    """A voice's conditioning as named arrays ("t3.speaker_emb", "gen.prompt_feat", ...) and
    the names of its empty (None) parts, which safetensors cannot hold."""
    arrays: dict[str, np.ndarray] = {}
    empty: list[str] = []
    for prefix, parts in (("t3", vars(conds.t3)), ("gen", conds.gen)):
        for key, value in parts.items():
            name = f"{prefix}.{key}"
            if value is None:
                empty.append(name)
                continue
            if hasattr(value, "detach"):  # a torch tensor
                value = value.detach().cpu().numpy()
            value = np.asarray(value)
            # (not np.ascontiguousarray: it makes a 0-d array 1-d)
            arrays[name] = value if value.flags.c_contiguous else value.copy(order="C")
    return arrays, empty


def arrays_to_parts(
    arrays: dict[str, np.ndarray], empty: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    parts: dict[str, dict[str, Any]] = {"t3": {}, "gen": {}}
    for name, value in [*arrays.items(), *((n, None) for n in empty)]:
        prefix, _, key = name.partition(".")
        if prefix not in parts or not key:
            raise ValueError(f"unexpected entry {name!r}")
        parts[prefix][key] = value
    return parts["t3"], parts["gen"]


def save_conds(path: Path, conds: Any) -> None:
    arrays, empty = conds_to_arrays(conds)
    meta = {"format": CONDS_FORMAT, "empty": ",".join(empty)}
    _write_atomic(path, lambda p: safetensors.numpy.save_file(arrays, str(p), metadata=meta))


def load_conds(path: Path) -> Any:
    # safetensors holds plain arrays: loading one executes no code (unlike torch.load).
    arrays = safetensors.numpy.load_file(str(path))
    with safe_open(str(path), framework="numpy") as f:
        meta = f.metadata() or {}
    if meta.get("format") != CONDS_FORMAT:
        raise ValueError("not a voice of this model")
    empty = [n for n in meta.get("empty", "").split(",") if n]
    return _engine.make_conds(*arrays_to_parts(arrays, empty))


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


def prepare_sample(data: bytes) -> tuple[np.ndarray, float]:
    """Decode, validate and resample an upload to the model's rate: (samples, seconds)."""
    audio, rate = decode_wav(data)
    seconds = audio.shape[0] / rate
    target = _engine.sample_rate
    if rate != target:
        # The resampler librosa.load uses for the library's own reference clips (soxr_hq).
        audio = soxr.resample(audio, rate, target, quality="HQ")
    return np.ascontiguousarray(audio, dtype=np.float32), seconds


def _write_atomic(path: Path, write: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def create_voice(name: str, audio: np.ndarray, seconds: float, consent: str) -> dict[str, Any]:
    """Compute the voice's conditioning once and keep it (blocking; run in a worker thread)."""
    voice_id = new_voice_id(name)
    started = time.perf_counter()
    with _gpu.turn(time.monotonic() + CLONE_DEADLINE_S):
        conds = _engine.conditionals(audio)
    VOICE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "id": voice_id,
        "name": name,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sample_seconds": round(seconds, 2),
        "model": CONDS_FORMAT,
        "consent": {"attested": True, "statement": consent},
    }
    save_conds(_voice_file(voice_id), conds)
    _write_atomic(
        VOICE_DIR / f"{voice_id}.json",
        lambda p: p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"),
    )
    with _state_lock:
        _custom_voices[voice_id] = meta
        _custom_conds[voice_id] = conds
        while len(_custom_conds) > max(CUSTOM_VOICE_CACHE, 0):
            _custom_conds.popitem(last=False)
    logger.info(
        "Cloned voice %s from %.1f s of audio in %.1fs",
        voice_id, seconds, time.perf_counter() - started,
    )
    return meta


def delete_voice(voice_id: str) -> bool:
    with _state_lock:
        known = _custom_voices.pop(voice_id, None) is not None
        _custom_conds.pop(voice_id, None)
    for path in (_voice_file(voice_id), VOICE_DIR / f"{voice_id}.json"):
        if path.exists():
            known = True
            path.unlink()
    return known


def load_voice_index() -> None:
    """Read the metadata of every cloned voice on disk (the conditionings load on first use)."""
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
        if meta.get("model") != CONDS_FORMAT:
            logger.warning(
                "Skipping cloned voice %s: made for %s, this server runs %s",
                voice_id, meta.get("model"), CONDS_FORMAT,
            )
            continue
        found[voice_id] = meta
    with _state_lock:
        _custom_voices.clear()
        _custom_voices.update(found)
        _custom_conds.clear()


def preload_custom_conds() -> None:
    newest = sorted(_custom_voices.values(), key=lambda m: str(m.get("created_at")), reverse=True)
    for meta in reversed(newest[: max(CUSTOM_VOICE_CACHE, 0)]):
        try:
            _custom_conds_for(meta["id"])
        except Exception as exc:
            logger.warning("Cloned voice %s could not be loaded: %s", meta["id"], exc)
            with _state_lock:
                _custom_voices.pop(meta["id"], None)


def cloning_on() -> bool:
    return MAX_CUSTOM_VOICES > 0


def _require_cloning() -> None:
    if _engine is None:
        raise HTTPException(status_code=503, detail="Model is still loading")
    if not cloning_on():
        raise HTTPException(status_code=501, detail=CLONING_TURNED_OFF)


def _voice_out(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": meta["id"],
        "name": meta.get("name") or meta["id"],
        "type": "custom",
        "created_at": meta.get("created_at"),
        "sample_seconds": meta.get("sample_seconds"),
    }


def cloning_state() -> dict[str, Any]:
    """The Pocket adapter's answer shape. Never "enabling" or "failed": nothing is loaded."""
    on = cloning_on()
    return {
        "state": "on" if on else "off",
        "message": None if on else CLONING_TURNED_OFF,
        "terms_url": MODEL_CARD_URL,
        "token_source": None,
    }


# ------------------------------------------------------------------ phrase cache

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def normalize_phrase(text: str) -> str:
    """The cache key of a text: Unicode-normalised, straight quotes, single spaces, any case."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return " ".join(text.split()).casefold()


def _capitalize(text: str) -> str:
    """Upper-case the first letter after any leading sound tags."""
    m = re.match(r"(?:\[[^\[\]]*\]\s*)*", text)
    i = m.end() if m else 0
    return text[:i] + text[i : i + 1].upper() + text[i + 1 :]


class PhraseCache:
    """Synthesised audio of short phrases, per voice, held in memory only (never on disk).

    The voice service starts playing a sentence only once it has received 0.5 s of its audio
    (or all of it); Chatterbox-Turbo makes a short sentence in 0.5-1.4 s on a T4, so a kept
    phrase is answered from memory instead. Phrases made at startup are pinned; other short
    sentences are kept once spoken, least recently used evicted first. Total audio is bounded
    by `max_bytes`. (The same class as in the Pocket TTS adapter.)
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
    """Matches `stem` (a normalised phrase without its end mark) at the start of a text,
    followed by ",", "." or "!" and either nothing or more words; group 1 is the rest."""
    words = [
        "['’]".join(re.escape(part) for part in word.split("'")) for word in stem.split()
    ]
    return re.compile(
        r"\s*" + r"\s+".join(words) + r"\s*[,.!](?:\s+(\S.*))?\s*", re.IGNORECASE | re.DOTALL
    )


_phrase_cache = PhraseCache(PHRASE_CACHE_BYTES, PHRASE_CACHE_MAX_CHARS)

# A kept phrase usually opens a reply, so it is stored from KEEP_LEAD_MS before its first sound
# (whatever near-silence the model puts first): the caller hears it that much sooner.
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


def to_pcm(audio: np.ndarray) -> bytes:
    """float audio in [-1, 1] as 16-bit little-endian PCM."""
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def cut_chunks(pcm: bytes, sample_rate: int) -> list[bytes]:
    size = int(sample_rate * CHUNK_SECONDS) * 2
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


def synth_pcm(text: str, voice: str, deadline: float) -> bytes:
    """One cleaned sentence in `voice` as 16-bit PCM, made when its turn comes (blocking)."""
    conds = _voice_conds(voice)
    with _gpu.turn(deadline):
        audio = _engine.synthesize(text, conds)
    return to_pcm(audio)


def prewarm_phrases(
    phrases: list[str], voices: list[str], cache: PhraseCache | None = None,
    wait_s: float = 0.0,
) -> None:
    """Synthesise and pin `phrases` for each voice (blocking). Each phrase waits for the model
    like a sentence whose caller has `wait_s` of audio left."""
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


# Voices whose phrases are made (or being made); see ensure_phrases.
_phrase_voices_made: set[str] = set()
_phrase_threads: list[threading.Thread] = []


def ensure_phrases(voice: str, after: threading.Event | None = None) -> None:
    """Make the kept phrases for a voice the first time it speaks, in the background: once
    `after` is set (the first sentence of the reply that asked), then each phrase after live
    speech that would run out within BACKGROUND_DEADLINE_S. A phrase being made is not
    interrupted: measured on a T4, live replies started up to 1 s later during the 33 s it
    took to make 26 phrases."""
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
        if v in ALLOWED_VOICES or v in _custom_voices:
            voices.append(v)
        else:
            logger.warning(
                "EXPRESSIVE_TTS_PHRASE_CACHE_VOICES: voice %s is not available; skipped", v
            )
    return voices


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
    global _engine
    if DEFAULT_VOICE not in ALLOWED_VOICES:
        raise RuntimeError(
            f"EXPRESSIVE_TTS_DEFAULT_VOICE={DEFAULT_VOICE} is not a built-in voice: {ALLOWED_VOICES}"
        )
    started = time.perf_counter()
    engine = load_engine()
    check_sound_tags(engine.added_tokens())
    _engine = engine
    if cloning_on():
        load_voice_index()
        preload_custom_conds()
        logger.info("Voice cloning on: %d cloned voice(s) on disk", len(_custom_voices))
    else:
        logger.info("Voice cloning off: EXPRESSIVE_TTS_MAX_CUSTOM_VOICES=0")
    if WARMUP:
        warm = time.perf_counter()
        synth_pcm("Hello there.", DEFAULT_VOICE, time.monotonic())
        logger.info("Warm-up synthesis in %.1fs", time.perf_counter() - warm)
    prewarm_phrases(CACHED_PHRASES, _phrase_voices())
    _phrase_voices_made.update(_phrase_voices())
    logger.info(
        "Chatterbox-Turbo ready in %.1fs (device=%s, revision=%s, sound tags: %s)",
        time.perf_counter() - started, engine.device, MODEL_REVISION[:12],
        ", ".join(SOUND_TAGS) or "none",
    )
    yield


app = FastAPI(title="Chatterbox-Turbo (OpenAI-compatible)", version="1.0.0", lifespan=lifespan)


def _check_auth(authorization: str | None) -> None:
    if API_KEY is None:
        return
    if not secrets.compare_digest((authorization or "").encode(), f"Bearer {API_KEY}".encode()):
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
    if _engine is None:
        return {"status": "loading"}
    return {"status": "ok", "device": _engine.device}


@app.get("/v1/models")
async def list_models(authorization: str | None = Header(default=None)) -> dict:
    _check_auth(authorization)
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ID, "object": "model", "created": 0, "owned_by": "resemble-ai",
            "language": LANGUAGE, "sample_rate": _engine.sample_rate if _engine else 24000,
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
    """The sounds this voice performs, written as a reply should carry them ("[laugh]")."""
    _check_auth(authorization)
    return {"tags": [f"[{t}]" for t in SOUND_TAGS]}


# ------------------------------------------------------------------ voice cloning endpoints


@app.get("/v1/voices")
async def list_all_voices(authorization: str | None = Header(default=None)) -> dict:
    """Built-in and cloned voices (501 when the operator turned cloning off)."""
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
    optional `consent_statement` kept with the voice. About 10 s of clear speech is best: the
    model uses the first 10-15 s.
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
    _phrase_voices_made.discard(voice_id)
    return None


@app.get("/v1/cloning")
async def get_cloning(authorization: str | None = Header(default=None)) -> dict:
    """Cloning state in the Pocket adapter's shape: "on" (no token involved), or "off" when
    the operator turned it off."""
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
    """409 while cloning is on: there is no token to remove. 204 when it is off already."""
    _check_auth(authorization)
    if cloning_on():
        raise HTTPException(status_code=409, detail=CLONING_CANNOT_TURN_OFF)
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
    if _engine is None:
        raise HTTPException(status_code=503, detail="Model is still loading")
    if body.model != MODEL_ID:
        raise HTTPException(status_code=404, detail=f"Model '{body.model}' not found; use '{MODEL_ID}'")
    sample_rate = _engine.sample_rate
    if body.sample_rate not in (None, sample_rate):
        raise HTTPException(status_code=422, detail=f"Only sample_rate {sample_rate} is supported")
    voice = body.voice
    if voice in OPENAI_VOICE_NAMES:
        voice = DEFAULT_VOICE
    if voice.startswith(CUSTOM_PREFIX):
        if not cloning_on():
            raise HTTPException(
                status_code=422,
                detail=f"Voice '{voice}' is a cloned voice, which this server cannot speak "
                f"now. {CLONING_TURNED_OFF}",
            )
        if not CUSTOM_ID.match(voice) or voice not in _custom_voices:
            raise HTTPException(status_code=422, detail=f"Voice '{voice}' is not available.")
    elif voice not in ALLOWED_VOICES:
        raise HTTPException(
            status_code=422, detail=f"Voice '{body.voice}' is not available. Voices: {ALLOWED_VOICES}"
        )
    if body.speed != 1.0:
        logger.info("Chatterbox-Turbo has no speed control; ignoring speed=%s", body.speed)
    text = body.input.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Input text is empty")
    spoken, tags, removed = clean_text(text)
    media_type = "audio/wav" if body.response_format == "wav" else "audio/pcm"
    # Set once this reply's first sentence is made: a new voice's phrases wait for it.
    first_made = threading.Event()
    ensure_phrases(voice, first_made)
    received = time.perf_counter()
    arrived = time.monotonic()

    # head: kept audio sent first; rest: text still to synthesise after it (None: nothing).
    head: tuple[bytes, ...] = ()
    rest: str | None = spoken
    status = "miss"
    if not re.search(r"\w", spoken):  # only removed tags or punctuation: nothing to say
        rest = None
    elif (cached := _phrase_cache.get(voice, spoken)) is not None:
        head, rest, status = cached, None, "hit"
    else:
        split = _phrase_cache.opener(voice, spoken)
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
        first_made.set()

        async def replay() -> AsyncIterator[bytes]:
            if body.response_format == "wav":
                yield _wav_header(sample_rate)
            for chunk in head:
                yield chunk
            # Lengths and counts only: the text itself is never logged.
            logger.info(
                "speech cache=%s chars=%d tags=%d removed=%d audio_s=%.2f",
                status, len(text), tags, removed, sum(len(c) for c in head) / (2 * sample_rate),
            )

        return StreamingResponse(replay(), media_type=media_type, headers=headers)

    sentences = split_sentences(rest)
    # One item per sentence: its audio in chunks, an exception, or None at the end.
    queue: Queue[tuple[bytes, ...] | BaseException | None] = Queue()
    stop = threading.Event()

    def worker() -> None:
        # When the caller started hearing audio, and how much of it has been queued since.
        heard_from = arrived if head else None
        queued = sum(len(c) for c in head) / (2 * sample_rate)
        try:
            for i, sentence in enumerate(sentences):
                if stop.is_set():
                    break
                chunks = None
                # A single sentence was looked up above already.
                if len(sentences) > 1 and _phrase_cache.cacheable(sentence):
                    chunks = _phrase_cache.get(voice, sentence)
                if chunks is None:
                    deadline = arrived if heard_from is None else heard_from + queued
                    pcm = synth_pcm(sentence, voice, deadline)
                    if _phrase_cache.cacheable(sentence):
                        _phrase_cache.put(
                            voice, sentence,
                            trim_leading_silence(cut_chunks(pcm, sample_rate), sample_rate),
                        )
                    if i == 0 and head:
                        # After a kept opener, start at the first sound: the model's lead-in
                        # would otherwise add to the pause between the two.
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
            # Client gone or done: no further sentences (the one being made is finished).
            stop.set()
            logger.info(
                "speech cache=%s chars=%d sentences=%d tags=%d removed=%d first_audio_ms=%s "
                "audio_s=%.2f total_ms=%.0f",
                status, len(text), len(sentences), tags, removed,
                "-" if first_ms is None else f"{first_ms:.0f}",
                audio_bytes / (2 * sample_rate), (time.perf_counter() - received) * 1000,
            )

    return StreamingResponse(stream(), media_type=media_type, headers=headers)
