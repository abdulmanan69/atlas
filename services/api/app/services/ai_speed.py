"""Fast-stack guidance, one-click provider presets and pipeline health (ADR 0006).

Figures in the guide are what each provider publishes, except rows flagged `measured`
(self-hosted servers ATLAS ships, timed on its reference CPU machine, see
docs/local-models.md, or on the GPU named in `measured_on`, see docs/perf/gpu-model-eval.md);
call measurements live on each call (`calls.latency`) and in Reports. Pipeline health
reads the voice service's Prometheus endpoint, which is process-wide across every
tenant, so it is offered to owners and admins only and labelled as platform-wide.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.schemas.latency import (
    LatencyGuide,
    LatencyGuideProvider,
    LatencyPreset,
    PipelineHealth,
    PresetChoice,
    PresetChoices,
    ResponseLatency,
    ServiceLatency,
)
from app.schemas.voice import ConnectionOut, ModelSelection, ModelSelectionUpdate, ServiceChoice
from app.services import ai_settings, audit

log = logging.getLogger(__name__)

DISCLAIMER = (
    "Typical figures are published by each provider and were not measured by ATLAS, "
    "except rows marked as measured, which ATLAS timed itself: on a 6-core CPU without a "
    "GPU, or on the GPU the row names. "
    "They usually exclude network time and vary with region, load, model and settings. "
    "Self-hosted speed depends on your hardware. The response times your calls actually "
    "had are shown on each call and in Reports."
)

# The voice service accepts the speech-cache flag for every provider, but in the pinned
# version no text-to-speech service reads it (service_factory.py:757-758), so none is
# offered. Add provider ids here once the voice service implements the cache for them.
TTS_CACHE_PROVIDERS: tuple[str, ...] = ()

# The self-hosted live speech server (infra/docker/streaming-stt) answers the Deepgram live
# protocol, so it is used through a Deepgram connection whose base URL points at it. This
# is the name of the model it runs by default (NVIDIA Parakeet TDT 110M); any name outside
# the Flux models keeps the voice service on its /v1/listen client, and the server answers
# with its configured model whatever the name. Median figures from
# docs/perf/local-stt-real-voice.json (8 kHz phone audio, real-time feed through the voice
# service's own client, 8-vCPU cloud server without a GPU). It replaced a streaming
# Zipformer that was fast but misheard real callers (same file).
STREAMING_STT_MODEL = "parakeet-en-110m"
STREAMING_STT_FIRST_WORDS_MS = 520
STREAMING_STT_FINAL_MS = 440

# One GPU (16 GB, measured on an NVIDIA Tesla T4; docs/perf/gpu-model-eval.md and its JSON
# files): the most human calls. Together the three models need about 11 GB.
T4 = "NVIDIA T4 GPU"
# The same live speech server with NVIDIA Parakeet TDT 0.6B v3: 88 ms to transcribe 5 s of
# speech (warm median); 14.5 % of words wrong on Pakistani-accented English, also on 8 kHz
# phone audio (the 110M model on CPU: 23.4 % and 29.4 %).
GPU_STT_MODEL = "parakeet-en-0.6b-v3"
GPU_STT_DECODE_MS = 88
# qwen3:8b with thinking off: first sentence of a reply after 118 ms (median of 30), 25 of
# 30 replies kept to two sentences, no made-up numbers (qwen2.5:1.5b: 37 ms, phone numbers).
GPU_LLM_MODEL = "qwen3:8b"
GPU_LLM_FIRST_SENTENCE_MS = 118
GPU_LLM_SETTINGS: dict[str, str | float] = {"model": GPU_LLM_MODEL, "temperature": 0.3}
# Chatterbox-Turbo on the expressive voice server (infra/docker/expressive-tts): real
# laughs, sighs and gasps where a reply says [laugh], [sigh] or [gasp]. It does not
# stream: a reply starts once its first sentence is made (0.48-1.38 s, about 0.9 s).
EXPRESSIVE_TTS_MODEL = "chatterbox-turbo"
EXPRESSIVE_TTS_FIRST_AUDIO_MS = 900

GUIDE: tuple[LatencyGuideProvider, ...] = (
    LatencyGuideProvider(
        kind="stt",
        provider="deepgram",
        model_hint="flux-general-en",
        typical_ms=260,
        streaming=True,
        turn_aware=True,
        note=(
            "Detects the end of the person's turn itself, so the agent can reply without "
            "waiting for a silence timer. Stated end-of-turn latency. English."
        ),
    ),
    LatencyGuideProvider(
        kind="stt",
        provider="deepgram",
        model_hint="nova-3-general",
        typical_ms=300,
        streaming=True,
        turn_aware=False,
        note="Stated as under 300 ms for streaming results. Many languages.",
    ),
    LatencyGuideProvider(
        kind="stt",
        provider="assemblyai",
        model_hint="universal-streaming",
        typical_ms=300,
        streaming=True,
        turn_aware=False,
        note="Stated as about 300 ms for streaming transcripts.",
    ),
    LatencyGuideProvider(
        kind="stt",
        provider="cartesia",
        model_hint="ink-2",
        typical_ms=None,
        streaming=True,
        turn_aware=True,
        note="Detects the end of the turn itself. English only. No published figure.",
    ),
    # Whisper turn timing in the pinned voice service: a "min_words" turn start (Snappy,
    # Balanced) begins the turn only at the transcript, which resets the pause strategy and
    # restarts its 0.6 s timer; "default" starts at the speech itself, so the turn ends as
    # soon as the transcript arrives. Driven with the service's own turn classes: +0.6 s vs
    # 0 ms; real calls with "default": 2-20 ms (docs/perf/local-turn-contention.json). Not
    # changed in the presets: with streaming speech-to-text "min_words" costs nothing and
    # keeps noise from interrupting the agent.
    LatencyGuideProvider(
        kind="stt",
        provider="speaches",
        model_hint="Systran/faster-whisper-base",
        typical_ms=None,
        streaming=False,
        turn_aware=False,
        note=(
            "Self-hosted Whisper. Not streaming: audio is transcribed after the person "
            "stops speaking, so every turn waits for the whole utterance (measured: about "
            "2 s for a 5 s sentence with base on a 6-core CPU; small.en, more accurate, "
            "about 4 s per utterance on an 8-vCPU cloud server). No live words either: with Snappy "
            "or Balanced response speed, which wait for the caller's first words before "
            "treating speech as an interruption, a caller can only interrupt the agent "
            "once they stop talking. Snappy and Balanced also add 0.6 s to every reply "
            "with Whisper: they start the person's turn only when the transcript arrives, "
            "which restarts the voice service's 0.6 s pause timer. A Custom response speed "
            "with 'On any speech' and 'When they pause' skips that wait, but then any sound "
            "the microphone picks up, including the agent's own voice from a speaker, "
            "interrupts the agent at once. Streaming speech-to-text has neither problem. "
            "For English calls use the self-hosted streaming server (Private streaming "
            "preset), which also misheard real callers less often than Whisper small.en; "
            "keep Whisper for Urdu and other languages."
        ),
    ),
    LatencyGuideProvider(
        kind="stt",
        provider="deepgram",
        model_hint=STREAMING_STT_MODEL,
        typical_ms=STREAMING_STT_FINAL_MS,
        measured=True,
        streaming=True,
        turn_aware=False,
        note=(
            "Self-hosted live speech-to-text (local-stt server, NVIDIA Parakeet TDT 110M, "
            "CC BY 4.0), added as a Deepgram connection with base URL "
            "http://local-stt:8000. Chosen on recordings of real callers (accented English, "
            "browser microphone): it misheard fewer words than Whisper small.en and the "
            "first streaming model. Measured on phone audio: first live words about "
            f"{STREAMING_STT_FIRST_WORDS_MS} ms after the caller starts, final transcript "
            f"{STREAMING_STT_FINAL_MS} ms after the caller stops (including the 0.2 s "
            "silence that ends a turn). Speech it is unsure of, such as background talk, "
            "is dropped instead of becoming a turn. English only."
        ),
    ),
    LatencyGuideProvider(
        kind="stt",
        provider="deepgram",
        model_hint=GPU_STT_MODEL,
        typical_ms=GPU_STT_DECODE_MS,
        measured=True,
        measured_on=T4,
        streaming=True,
        turn_aware=False,
        note=(
            "Self-hosted live speech-to-text on a GPU: the same Deepgram connection to the "
            "local-stt server, with NVIDIA Parakeet TDT 0.6B v3 (CC BY 4.0). Measured on an "
            f"NVIDIA T4: {GPU_STT_DECODE_MS} ms to transcribe 5 s of speech (warm median), "
            "on top of the 0.2 s of silence that ends a turn. On accented English it misheard "
            "far fewer words than the CPU model and Whisper large-v3: 14.5% of words wrong "
            "for Pakistani speakers, also on phone audio (CPU model: 23.4% and 29.4%). "
            "Tested on English."
        ),
    ),
    LatencyGuideProvider(
        kind="llm",
        provider="groq",
        model_hint="llama-3.3-70b-versatile",
        typical_ms=None,
        streaming=True,
        turn_aware=False,
        note=(
            "Known for very high token throughput. Time to first token is not published "
            "and depends on model and load."
        ),
    ),
    LatencyGuideProvider(
        kind="llm",
        provider="openai",
        model_hint="gpt-4.1-mini",
        typical_ms=None,
        streaming=True,
        turn_aware=False,
        note="Smaller models answer sooner than larger ones. No published figure.",
    ),
    LatencyGuideProvider(
        kind="llm",
        provider="google",
        model_hint="gemini-2.5-flash",
        typical_ms=None,
        streaming=True,
        turn_aware=False,
        note="Flash models are positioned for low latency. No published figure.",
    ),
    LatencyGuideProvider(
        kind="llm",
        provider="speaches",
        model_hint="qwen2.5:1.5b",
        typical_ms=None,
        streaming=True,
        turn_aware=False,
        note=(
            "Self-hosted (e.g. Ollama). Small models are needed on CPU; a GPU helps most. "
            "On an NVIDIA T4 it wrote its first sentence after 37 ms (measured), but made "
            "up phone numbers; use qwen3:8b on a GPU."
        ),
    ),
    LatencyGuideProvider(
        kind="llm",
        provider="speaches",
        model_hint=GPU_LLM_MODEL,
        typical_ms=GPU_LLM_FIRST_SENTENCE_MS,
        measured=True,
        measured_on=T4,
        streaming=True,
        turn_aware=False,
        note=(
            "Self-hosted on a GPU (Ollama), with thinking off. Measured on an NVIDIA T4 with "
            f"a real agent's conversation: first sentence after {GPU_LLM_FIRST_SENTENCE_MS} "
            "ms (median of 30 replies, temperature 0.3), 25 of 30 replies kept to two "
            "sentences, no made-up numbers, and it stopped calling when asked. Larger models "
            "were slower and did not follow the rules better."
        ),
    ),
    LatencyGuideProvider(
        kind="tts",
        provider="elevenlabs",
        model_hint="eleven_flash_v2_5",
        typical_ms=75,
        streaming=True,
        turn_aware=False,
        note="Stated model latency, excluding network and application time.",
    ),
    LatencyGuideProvider(
        kind="tts",
        provider="cartesia",
        model_hint="sonic",
        typical_ms=90,
        streaming=True,
        turn_aware=False,
        note="Stated time to first audio for the Sonic models.",
    ),
    LatencyGuideProvider(
        kind="tts",
        provider="deepgram",
        model_hint="aura-2",
        typical_ms=200,
        streaming=True,
        turn_aware=False,
        note="Stated as under about 200 ms to first audio.",
    ),
    LatencyGuideProvider(
        kind="tts",
        provider="speaches",
        model_hint="pocket-tts",
        typical_ms=200,
        streaming=True,
        turn_aware=False,
        note=(
            "Self-hosted Pocket TTS. About 200 ms to first audio is the model maker's "
            "claim (Kyutai), not measured by ATLAS. English only on one server; no Urdu."
        ),
    ),
    LatencyGuideProvider(
        kind="tts",
        provider="speaches",
        model_hint="speaches-ai/Kokoro-82M-v1.0-ONNX",
        typical_ms=None,
        streaming=False,
        turn_aware=False,
        note="Self-hosted. Speed depends on your hardware.",
    ),
    LatencyGuideProvider(
        kind="tts",
        provider="speaches",
        model_hint=EXPRESSIVE_TTS_MODEL,
        typical_ms=EXPRESSIVE_TTS_FIRST_AUDIO_MS,
        measured=True,
        measured_on=T4,
        streaming=False,
        turn_aware=False,
        note=(
            "Self-hosted expressive voice on a GPU (Chatterbox-Turbo, MIT): it laughs, "
            "chuckles, sighs and gasps where a reply says [laugh], [chuckle], [sigh] or "
            "[gasp] (the agent's Natural sounds setting), and never reads them. Measured on "
            "an NVIDIA T4: a sentence's audio starts 0.5 to 1.4 s after it is sent (about "
            "0.9 s), 2.3 times faster than real time. Not streaming: each sentence starts "
            "once it is made. English only."
        ),
    ),
)

# Self-hosted model ids verified against the pinned images (WS-C, docs/local-models.md):
# speaches 0.8.3 only accepts the full Kokoro id; the short "kokoro" returns 404.
SELF_HOSTED_STT_MODEL = "Systran/faster-whisper-base"
SELF_HOSTED_LLM_MODEL = "qwen2.5:1.5b"
# At a low temperature the small model keeps its first sentence short, as the prompt asks:
# with 0.3 instead of the server default, the slowest tenth of replies on the Oracle server
# was heard 1.7 s sooner (p90 3.8 -> 2.1 s; median 2.3 -> 1.9 s, partly run-to-run
# variation at 12 turns) (docs/perf/oracle-latency.md).
SELF_HOSTED_LLM_SETTINGS: dict[str, str | float] = {
    "model": SELF_HOSTED_LLM_MODEL,
    "temperature": 0.3,
}
KOKORO_MODEL = "speaches-ai/Kokoro-82M-v1.0-ONNX"
PRESETS: tuple[LatencyPreset, ...] = (
    LatencyPreset(
        id="fast_cloud",
        title="Fastest cloud",
        description=(
            "Turn-aware speech-to-text, a high-throughput language model and a low-latency "
            "voice. Pair with the Snappy response speed."
        ),
        choices=PresetChoices(
            stt=PresetChoice(provider="deepgram", settings={"model": "flux-general-en"}),
            llm=PresetChoice(provider="groq", settings={"model": "llama-3.3-70b-versatile"}),
            tts=PresetChoice(provider="cartesia", settings={}),
        ),
        requires=["deepgram", "groq", "cartesia"],
    ),
    LatencyPreset(
        id="balanced_cloud",
        title="Balanced cloud",
        description=(
            "Widely used providers with fast streaming speech and voice, and a small, "
            "capable language model."
        ),
        choices=PresetChoices(
            stt=PresetChoice(provider="deepgram", settings={"model": "nova-3-general"}),
            llm=PresetChoice(provider="openai", settings={"model": "gpt-4.1-mini"}),
            tts=PresetChoice(provider="elevenlabs", settings={"model": "eleven_flash_v2_5"}),
        ),
        requires=["deepgram", "openai", "elevenlabs"],
    ),
    LatencyPreset(
        id="private_self_hosted",
        title="Private self-hosted",
        description=(
            "Everything runs on your own servers; no call audio or text leaves them. "
            "Slower on CPU: expect several seconds per reply without a GPU."
        ),
        choices=PresetChoices(
            stt=PresetChoice(provider="speaches", settings={"model": SELF_HOSTED_STT_MODEL}),
            llm=PresetChoice(provider="speaches", settings=dict(SELF_HOSTED_LLM_SETTINGS)),
            tts=PresetChoice(
                provider="speaches", settings={"model": KOKORO_MODEL, "voice": "af_heart"}
            ),
        ),
        requires=["speaches"],
    ),
    LatencyPreset(
        id="private_pocket_tts",
        title="Private self-hosted (Pocket TTS)",
        description=(
            "Like Private self-hosted, with the faster streaming Pocket TTS voice. Needs a "
            "self-hosted connection to the Pocket TTS server (http://local-tts:8000/v1). "
            "English only."
        ),
        choices=PresetChoices(
            stt=PresetChoice(provider="speaches", settings={"model": SELF_HOSTED_STT_MODEL}),
            llm=PresetChoice(provider="speaches", settings=dict(SELF_HOSTED_LLM_SETTINGS)),
            tts=PresetChoice(
                provider="speaches", settings={"model": "pocket-tts", "voice": "alba"}
            ),
        ),
        requires=["speaches"],
    ),
    LatencyPreset(
        id="private_streaming",
        title="Private streaming (self-hosted)",
        description=(
            "Self-hosted, with live transcription: the caller's words are recognised while "
            "they speak, so replies start sooner than with Whisper. Needs a Deepgram "
            "connection with base URL http://local-stt:8000 (the streaming server), plus "
            "the self-hosted language model and Pocket TTS connections. English only."
        ),
        choices=PresetChoices(
            stt=PresetChoice(
                provider="deepgram", settings={"model": STREAMING_STT_MODEL, "language": "en"}
            ),
            llm=PresetChoice(provider="speaches", settings=dict(SELF_HOSTED_LLM_SETTINGS)),
            tts=PresetChoice(
                provider="speaches", settings={"model": "pocket-tts", "voice": "alba"}
            ),
        ),
        requires=["deepgram", "speaches"],
    ),
    LatencyPreset(
        id="private_gpu",
        title="Private GPU (self-hosted)",
        description=(
            "The most human calls on one GPU with 16 GB (measured on an NVIDIA T4): live "
            "transcription that understands accents better, a stronger language model, and "
            "an expressive voice that can laugh and sigh. Needs a Deepgram connection to "
            "the live speech server, a self-hosted language model connection serving "
            "qwen3:8b with thinking off, and a self-hosted connection to the expressive "
            "voice server (http://expressive-tts:8000/v1, or /voice/v1 on an AI server). "
            "English only."
        ),
        choices=PresetChoices(
            stt=PresetChoice(
                provider="deepgram", settings={"model": GPU_STT_MODEL, "language": "en"}
            ),
            llm=PresetChoice(provider="speaches", settings=dict(GPU_LLM_SETTINGS)),
            tts=PresetChoice(provider="speaches", settings={"model": EXPRESSIVE_TTS_MODEL}),
        ),
        requires=["deepgram", "speaches"],
    ),
)
_PRESETS = {p.id: p for p in PRESETS}
# Choices that must use one specific self-hosted server: only connections whose name or
# address matches one of these hints qualify (otherwise the provider counts as missing).
# "live-stt": the same server behind an AI gateway (ADR 0007), at <gateway>/live-stt/<secret>;
# Pocket TTS is at <gateway>/tts/v1 and the expressive voice server at <gateway>/voice/v1
# (the same hints as voice_cloning.py).
_STREAMING_STT_HINTS = ("local-stt", "streaming-stt", "live-stt")
_POCKET_HINTS = ("local-tts", "pocket", "/tts/v1")
_EXPRESSIVE_HINTS = ("expressive", "chatterbox", "/voice/v1")
_REQUIRED_HINTS: dict[tuple[str, str], tuple[str, ...]] = {
    ("private_pocket_tts", "tts"): _POCKET_HINTS,
    ("private_streaming", "stt"): _STREAMING_STT_HINTS,
    ("private_streaming", "tts"): _POCKET_HINTS,
    ("private_gpu", "stt"): _STREAMING_STT_HINTS,
    ("private_gpu", "tts"): _EXPRESSIVE_HINTS,
}
# Presets whose voice is one the server offers (its voices are not known in advance): the
# organization's voice when it is already on that connection, else the server's default.
_VOICE_FROM_SERVER = frozenset({"private_gpu"})
# Dedicated servers that are never a fallback for other presets: a Deepgram connection
# pointed at the local streaming server cannot serve Flux or cloud-only models.
_EXCLUSIVE_HINTS = _STREAMING_STT_HINTS

# For a self-hosted provider one tenant often has two connections (language model and
# speech servers): prefer the one whose name or address suggests the right role.
_ROLE_HINTS = {
    "llm": ("llm", "ollama", "11434", "language", "vllm", "lm studio", "/llm/v1"),
    "stt": ("speech", "whisper", "stt", ":8000", "/whisper/v1"),
    "tts": ("speech", "voice", "kokoro", "tts", ":8000", "/tts/v1", "/voice/v1"),
}


class PresetRequirementsMissingError(ConflictError):
    code = "preset_requirements_missing"


def latency_guide() -> LatencyGuide:
    return LatencyGuide(
        providers=list(GUIDE),
        presets=list(PRESETS),
        tts_cache_providers=list(TTS_CACHE_PROVIDERS),
        disclaimer=DISCLAIMER,
    )


def _haystack(c: ConnectionOut) -> str:
    return " ".join([c.name, *map(str, c.connection_settings.values())]).lower()


def _pick(
    kind: str,
    provider: str,
    connections: list[ConnectionOut],
    required: tuple[str, ...] = (),
) -> ConnectionOut | None:
    candidates = [c for c in connections if c.provider == provider and not c.hosted_trial]
    if required:
        candidates = [c for c in candidates if any(h in _haystack(c) for h in required)]
    else:
        # A dedicated server (e.g. Pocket TTS) is only used when a preset asks for it;
        # an exclusive one (the streaming speech server) not even as a last resort.
        candidates = [c for c in candidates if not any(h in _haystack(c) for h in _EXCLUSIVE_HINTS)]
        dedicated = {h for hints in _REQUIRED_HINTS.values() for h in hints}
        general = [c for c in candidates if not any(h in _haystack(c) for h in dedicated)]
        candidates = general or candidates
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    hints = _ROLE_HINTS.get(kind, ())

    def score(c: ConnectionOut) -> int:
        haystack = _haystack(c)
        return sum(1 for h in hints if h in haystack)

    return max(candidates, key=score)


async def apply_preset(db: AsyncSession, ctx: TenantContext, preset_id: str) -> ModelSelection:
    """Select the preset's providers using the tenant's existing connections.

    409 with ``details.missing`` (provider ids) when a required connection is missing.
    The organization's embeddings choice is kept.
    """
    preset = _PRESETS.get(preset_id)
    if preset is None:
        raise NotFoundError("Preset not found")
    connections = await ai_settings.list_connections(db, ctx)
    chosen: dict[str, ServiceChoice] = {}
    missing: list[str] = []
    picked: dict[str, ConnectionOut] = {}
    for kind in ("stt", "llm", "tts"):
        choice: PresetChoice = getattr(preset.choices, kind)
        conn = _pick(kind, choice.provider, connections, _REQUIRED_HINTS.get((preset.id, kind), ()))
        if conn is None:
            if choice.provider not in missing:
                missing.append(choice.provider)
            continue
        picked[kind] = conn
        chosen[kind] = ServiceChoice(connection_id=conn.id, settings=dict(choice.settings))
    if missing:
        raise PresetRequirementsMissingError(
            "Add a connection for these providers first", details={"missing": missing}
        )
    current = await ai_settings.get_selection(db, ctx)
    if preset.id in _VOICE_FROM_SERVER and "voice" not in chosen["tts"].settings:
        voice = await _server_voice(db, ctx, picked["tts"], current)
        if voice:
            chosen["tts"].settings["voice"] = voice
    selection = await ai_settings.save_selection(
        db,
        ctx,
        ModelSelectionUpdate(
            llm=chosen["llm"], stt=chosen["stt"], tts=chosen["tts"], embeddings=current.embeddings
        ),
    )
    await audit.record(
        db,
        action="ai.preset_applied",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="ai_models",
        changes={"preset": preset.id},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return selection


async def _server_voice(
    db: AsyncSession, ctx: TenantContext, conn: ConnectionOut, current: ModelSelection
) -> str | None:
    """The organization's voice when it is already on this connection, else the server's
    default voice; None when the server cannot be asked (the voice is then chosen by hand)."""
    from app.services import voice_cloning  # imports ai_servers, which imports this module

    if current.tts is not None and current.tts.connection_id == conn.id:
        voice = current.tts.settings.get("voice")
        if isinstance(voice, str) and voice:
            return voice
    return await voice_cloning.default_voice(db, ctx.org_id, conn)


# ------------------------------------------------------------------ pipeline health

METRICS_PATH = "/api/v1/metrics"  # routes/main.py:242, mounted under API_PREFIX (app.py:184)
METRICS_SECRET_HEADER = "X-Dograh-Devops-Secret"  # noqa: S105 - a header name, routes/main.py:155
# Histogram names (observability/metrics.py:30-80); the exporter may append a unit suffix.
_AI_LATENCY = "dograh_ai_latency"
_RESPONSE_LATENCY = "dograh_response_latency"
_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')
HEALTH_NOTE = (
    "Platform-wide: every organization's calls on this voice service, since the voice "
    "service last started. Use it to spot a slow provider; your own calls' response "
    "times are on each call and in Reports."
)


@dataclass(slots=True)
class Histogram:
    buckets: dict[float, float]  # upper bound → cumulative count
    count: float = 0.0
    kind: str | None = None


def _labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    return {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL.findall(raw)}


def _bound(raw: str) -> float | None:
    if raw in ("+Inf", "Inf", "+inf"):
        return math.inf
    try:
        return float(raw)
    except ValueError:
        return None


def _family(name: str, base: str) -> str | None:
    """'bucket' | 'count' for samples of the histogram `base` (with any unit suffix)."""
    if not name.startswith(base):
        return None
    for suffix in ("bucket", "count"):
        if name.endswith(f"_{suffix}"):
            return suffix
    return None


def parse_prometheus(text: str) -> tuple[dict[str, Histogram], Histogram | None]:
    """Per-service AI latency histograms and the response-latency histogram.

    Series of one service (different models / conversation types) are summed. Never
    raises on malformed lines; they are skipped.
    """
    services: dict[str, Histogram] = {}
    response: Histogram | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if not match:
            continue
        name, raw_labels, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        labels = _labels(raw_labels)
        if (part := _family(name, _AI_LATENCY)) is not None:
            service = labels.get("service") or "custom"
            hist = services.setdefault(service, Histogram(buckets={}, kind=labels.get("kind")))
            if part == "count":
                hist.count += value
            elif (le := _bound(labels.get("le", ""))) is not None:
                hist.buckets[le] = hist.buckets.get(le, 0.0) + value
        elif (part := _family(name, _RESPONSE_LATENCY)) is not None:
            response = response or Histogram(buckets={})
            if part == "count":
                response.count += value
            elif (le := _bound(labels.get("le", ""))) is not None:
                response.buckets[le] = response.buckets.get(le, 0.0) + value
    return services, response


def histogram_quantile(q: float, buckets: dict[float, float]) -> float | None:
    """Prometheus-style quantile (seconds) by linear interpolation inside the bucket.

    Like PromQL's `histogram_quantile`: the first bucket's lower edge is 0, and a rank
    landing in the +Inf bucket returns the highest finite bound.
    """
    if not buckets:
        return None
    ordered = sorted(buckets.items())
    total = ordered[-1][1]
    if total <= 0:
        return None
    rank = q * total
    lower_bound, lower_count = 0.0, 0.0
    for bound, cumulative in ordered:
        if cumulative >= rank:
            if math.isinf(bound):
                return lower_bound
            if cumulative == lower_count:
                return bound
            return lower_bound + (bound - lower_bound) * (rank - lower_count) / (
                cumulative - lower_count
            )
        lower_bound, lower_count = bound, cumulative
    return lower_bound


def _ms(seconds: float | None) -> int | None:
    return round(seconds * 1000) if seconds is not None else None


def _samples(h: Histogram) -> int:
    if h.count:
        return int(h.count)
    return int(max(h.buckets.values(), default=0))


def health_from_text(text: str) -> PipelineHealth:
    services, response = parse_prometheus(text)
    rows = [
        ServiceLatency(
            service=name,
            kind=h.kind,
            p50_ms=_ms(histogram_quantile(0.5, h.buckets)),
            p95_ms=_ms(histogram_quantile(0.95, h.buckets)),
            samples=_samples(h),
        )
        for name, h in sorted(services.items())
    ]
    return PipelineHealth(
        available=True,
        services=[r for r in rows if r.samples > 0],
        response=(
            ResponseLatency(
                p50_ms=_ms(histogram_quantile(0.5, response.buckets)),
                p95_ms=_ms(histogram_quantile(0.95, response.buckets)),
                samples=_samples(response),
            )
            if response is not None and _samples(response) > 0
            else None
        ),
        note=HEALTH_NOTE,
    )


def _unavailable(reason: str) -> PipelineHealth:
    return PipelineHealth(available=False, note=f"{reason} {HEALTH_NOTE}")


async def pipeline_health(*, transport: httpx.AsyncBaseTransport | None = None) -> PipelineHealth:
    settings = get_settings()
    secret = settings.voice_runtime_metrics_secret.get_secret_value()
    if not secret:
        return _unavailable(
            "Pipeline health is not set up: set ATLAS_VOICE_RUNTIME_METRICS_SECRET and "
            "restart the voice service."
        )
    url = settings.voice_runtime_base_url.rstrip("/") + METRICS_PATH
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), transport=transport) as http:
            resp = await http.get(url, headers={METRICS_SECRET_HEADER: secret})
    except httpx.HTTPError as exc:
        log.warning("pipeline_health_unreachable", extra={"error_type": type(exc).__name__})
        return _unavailable("The voice service could not be reached.")
    if resp.status_code == 404:
        return _unavailable("Metrics are turned off on the voice service.")
    if resp.status_code in (401, 403, 503):
        return _unavailable(
            "The voice service refused the metrics secret; check that both sides use the "
            "same ATLAS_VOICE_RUNTIME_METRICS_SECRET."
        )
    if not resp.is_success:
        return _unavailable("The voice service did not return metrics.")
    return health_from_text(resp.text[:5_000_000])
