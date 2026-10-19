"""Mimic self-hosted voice turns with all three local model servers running at once.

The voice service runs speech-to-text, the language model and the voice on the same CPU.
While the first sentence is being spoken, the language model is still writing the rest of
the reply, so the two compete for cores. This script reproduces that, measures each part,
and checks whether the voice keeps up with real-time playback (choppy audio otherwise).

Subcommands (servers: local-speech :8001, local-llm :11434, local-tts :8002):

  wavs  Speak known sentences with Pocket TTS, resample to 16 kHz and pad them the way the
        voice service cuts a segment: up to 1 s kept from before the speech start (here
        0.8 s of silence), the speech, 0.2 s of silence until the end of speech is
        detected and 0.5 s of padding (0.7 s after). Writes WAVs and texts.json.
  stt   Time and word error rate of Whisper models on those WAVs, nothing else running.
  tts   Pocket TTS on its own: time to first audio and real-time factor.
  turn  N turns of one call. Each turn: transcribe a 3 s WAV, stream the transcript to the
        language model with a prompt of about 950 tokens that was warmed first, cut the
        reply at the first sentence end (as the voice service does: punctuation followed
        by the next character), send that sentence to Pocket TTS while the model keeps
        generating, then each further sentence once the previous one is synthesised.
        Earlier replies stay in the conversation, like a real call.

Every result is appended to --out (JSON) under --label, so runs with different server
settings (thread counts) can be compared. Run from the API project so httpx is available:

  cd services/api
  uv run python ../../infra/scripts/bench_local_turn.py wavs --dir ../../.bench/wavs
  uv run python ../../infra/scripts/bench_local_turn.py stt --dir ../../.bench/wavs \
      --models Systran/faster-whisper-base,Systran/faster-whisper-base.en --out r.json
  uv run python ../../infra/scripts/bench_local_turn.py turn --dir ../../.bench/wavs \
      --label llm4-tts2-stt4 --turns 5 --out r.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import queue
import re
import statistics
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

SPEECH_URL = "http://127.0.0.1:8001/v1"
LLM_URL = "http://127.0.0.1:11434/v1"
TTS_URL = "http://127.0.0.1:8002/v1"
TTS_RATE = 24000  # Pocket TTS output, what the voice service plays
STT_RATE = 16000  # what the voice service sends to speech-to-text
LEAD_S, TRAIL_S = 0.8, 0.7
VAD_STOP_S = 0.2  # the voice service ends a turn after 0.2 s of silence (run_pipeline.py)
AUDIBLE_GAP_S = 0.05  # a pause in playback longer than this is heard as a stutter
# The voice service reads speech audio in 0.5 s pieces and plays a piece only once it is
# complete (pipecat TTSService.chunk_size, CHUNK_SECONDS = 0.5), so its "first audio" is
# the moment 0.5 s of audio has arrived, not the first byte.
PIECE_S = 0.5

# Known sentences: five of about 3 s (one per turn) and two of about 5 s. No digits, so
# the word error rate does not depend on how numbers are written.
SENTENCES: dict[str, str] = {
    "s3_1": "We want to buy a family house near a good primary school.",
    "s3_2": "Our budget really depends on the price of the house.",
    "s3_3": "My wife and I will make the final decision together.",
    "s3_4": "We would like to move in some time before the end of June.",
    "s3_5": "Could one of your specialists call me back tomorrow morning?",
    "s5_1": "Hi, I would like to move my appointment from Tuesday to Thursday afternoon, "
    "if that still works for you.",
    "s5_2": "We have been looking for a while now, and we would like to see a few more "
    "houses this weekend.",
}
TURN_WAVS = ["s3_1", "s3_2", "s3_3", "s3_4", "s3_5"]
TTS_ALONE_SENTENCE = "Thanks for calling, I can help book an appointment for tomorrow morning."

# A realistic agent: the demo qualifier's Start prompt (seeded demo agent), global rules
# of the kind tenants write, the greeting, and the transition tools the voice service
# sends. About 950 prompt tokens with the first caller line (checked from usage).
GLOBAL_RULES = """\
You work for Demo Realty (demo data), a residential estate agency with offices in the
city centre and the northern suburbs. Office hours are Monday to Friday, nine to six, and
Saturday, ten to two. The agency helps people buy, sell and rent homes: flats, terraced
and detached houses, and new-build developments. Valuations are free and take about an
hour at the property. A specialist, not you, gives prices, valuations and legal advice.

Style:
- Speak naturally, like a friendly person on the phone. Use short sentences.
- Never read out lists, links, email addresses or long numbers; offer to send them.
- Do not use emojis, markdown or special characters: everything you write is spoken.
- If you did not understand, say so and ask the person to repeat.
- Use the person's first name at most once or twice in the call.

Compliance:
- Calls may be recorded for quality and training. If asked, say so plainly.
- Do not collect card details, bank details or identity document numbers.
- If the person sounds upset or vulnerable, be patient and offer a call back from a
  specialist instead of pushing on.
- Do not promise that a property is available, or that an offer will be accepted.

Booking a call back:
- A specialist can call back on weekdays between nine and six.
- Confirm the day and a rough time, and the best number if it is not this one.
- Repeat the day and time back to the person before you finish.

Common questions:
- Fees: the agency's fees depend on the service; a specialist explains them on the call
  back. Do not quote a percentage or an amount.
- Mortgages: the agency works with independent mortgage advisers. Offer an introduction,
  but do not give mortgage advice or say what the person can borrow.
- Viewings: a specialist arranges viewings after the call. Do not book a viewing yourself.
- Selling and buying at the same time: this is common; the specialist can plan both.
- Renting: the lettings team handles rentals. Note the interest and offer a call back.
- Areas: the agency covers the city and the towns within about twenty miles of it. If the
  person is looking further away, say so politely and offer to note their details."""

START_PROMPT = """\
You are Sarah, speaking on a phone call on behalf of Demo Realty (demo data).

Your goal is to find out whether this lead is a good fit and, if so, agree on a next step
with a specialist. Be warm, brief and respectful of their time. If now is not a good time,
ask when to call back and end the call.

## Questions to cover
Ask these naturally, one at a time, and listen to each answer before moving on:
1. What are you looking to achieve?
2. What timeline are you working with?
3. Do you have a budget in mind?
4. Who else is involved in the decision?

## Rules
- Keep each reply short (one or two sentences): this is a live phone call.
- Speak in English unless the person clearly prefers another language.
- Never invent prices, availability, policies or facts that are not in these instructions.
- If the person asks not to be contacted again, apologise, confirm they will not be called
  again, and end the call politely.
- If asked whether you are a person or a machine, say you are an AI assistant."""

GREETING = (
    "Hi Alex, this is Sarah from Demo Realty (demo data). You recently asked about our "
    "services. Is now a good time for two quick questions?"
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "wrap_up",
            "description": "The goal of the call is reached, the person wants to end the "
            "call, or the person is not interested.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_specialist",
            "description": "The person agreed a day and time for a specialist to call back.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
SYSTEM = f"{GLOBAL_RULES}\n\n{START_PROMPT}"
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+(?=\S)")


# ------------------------------------------------------------------ audio helpers
def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """2:3 resampling with a 3-tap smoothing filter; plenty for timing and accuracy."""
    src = array("h")
    src.frombytes(pcm)
    n = len(src)
    smooth = [
        (src[max(i - 1, 0)] + 2 * src[i] + src[min(i + 1, n - 1)]) / 4 for i in range(n)
    ]
    out = array("h")
    for j in range(n * 2 // 3):
        p = j * 1.5
        i = int(p)
        f = p - i
        a = smooth[i]
        b = smooth[i + 1] if i + 1 < n else a
        out.append(max(-32768, min(32767, round(a * (1 - f) + b * f))))
    return out.tobytes()


def tts_pcm(client: httpx.Client, text: str, voice: str) -> bytes:
    body = {"model": "pocket-tts", "input": text, "voice": voice, "response_format": "pcm"}
    r = client.post(f"{TTS_URL}/audio/speech", json=body, timeout=120)
    r.raise_for_status()
    return r.content


def normalise(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z' ]+", " ", text)
    return text.split()


def word_errors(ref: str, hyp: str) -> tuple[int, int]:
    r, h = normalise(ref), normalise(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)], len(r)


# ------------------------------------------------------------------ results file
def save(out: str | None, section: str, label: str, data: dict[str, Any]) -> None:
    print(json.dumps({section: {label: data}}, indent=2))
    if not out:
        return
    path = Path(out)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    doc.setdefault(section, {})[label] = data
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def meta(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "machine": platform.processor() or platform.machine(),
        "logical_cpus": os.cpu_count(),
        **(extra or {}),
    }


def med(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


# ------------------------------------------------------------------ wavs
def cmd_wavs(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    texts: dict[str, dict[str, Any]] = {}
    with httpx.Client(timeout=120) as client:
        for key, text in SENTENCES.items():
            pcm16 = resample_24k_to_16k(tts_pcm(client, text, args.voice))
            speech_s = len(pcm16) / 2 / STT_RATE
            silence = lambda s: b"\x00\x00" * int(s * STT_RATE)  # noqa: E731
            (out_dir / f"{key}.wav").write_bytes(
                wav_bytes(silence(LEAD_S) + pcm16 + silence(TRAIL_S), STT_RATE)
            )
            texts[key] = {"text": text, "speech_s": round(speech_s, 2),
                          "wav_s": round(speech_s + LEAD_S + TRAIL_S, 2)}
            print(f"{key}: {speech_s:.2f} s of speech  {text}")
    (out_dir / "texts.json").write_text(json.dumps(texts, indent=2), encoding="utf-8")
    return 0


def load_wavs(directory: str) -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    d = Path(directory)
    texts = json.loads((d / "texts.json").read_text(encoding="utf-8"))
    return {k: (d / f"{k}.wav").read_bytes() for k in texts}, texts


def transcribe(client: httpx.Client, model: str, wav: bytes) -> tuple[float, str]:
    """The voice service's request: file audio.wav, model, language (pipecat openai/stt.py)."""
    start = time.perf_counter()
    r = client.post(
        f"{SPEECH_URL}/audio/transcriptions",
        data={"model": model, "language": "en"},
        files={"file": ("audio.wav", wav, "audio/wav")},
        timeout=120,
    )
    elapsed = (time.perf_counter() - start) * 1000
    r.raise_for_status()
    return elapsed, r.json().get("text", "").strip()


# ------------------------------------------------------------------ stt
def cmd_stt(args: argparse.Namespace) -> int:
    wavs, texts = load_wavs(args.dir)
    with httpx.Client(timeout=120) as client:
        for model in [m.strip() for m in args.models.split(",") if m.strip()]:
            transcribe(client, model, wavs["s3_1"])  # load + warm-up, not counted
            per_wav: dict[str, Any] = {}
            errors = words = 0
            for key, info in texts.items():
                times, hyp = [], ""
                for _ in range(args.runs):
                    ms, hyp = transcribe(client, model, wavs[key])
                    times.append(ms)
                e, n = word_errors(info["text"], hyp)
                errors, words = errors + e, words + n
                per_wav[key] = {"speech_s": info["speech_s"], "wav_s": info["wav_s"],
                                "ms_median": med(times), "ms": [round(t) for t in times],
                                "transcript": hyp, "word_errors": e, "words": n}
            by_len = {
                f"{s}s": med([v["ms_median"] for k, v in per_wav.items() if k.startswith(f"s{s}_")])
                for s in (3, 5)
            }
            save(args.out, "stt", f"{args.label}:{model}" if args.label else model, {
                "meta": meta({"runs": args.runs, "threads": args.threads_note}),
                "median_ms_by_length": by_len,
                "word_error_rate": round(errors / words, 3) if words else None,
                "word_errors": errors, "words": words, "wavs": per_wav,
            })
            if args.unload:
                client.delete(f"{SPEECH_URL.removesuffix('/v1')}/api/ps/{model}")
    return 0


# ------------------------------------------------------------------ tts
@dataclass
class Synthesis:
    text: str
    ready_at: float  # when the sentence was complete and handed to the voice
    sent_at: float = 0.0
    chunks: list[tuple[float, int]] = field(default_factory=list)  # (arrival, bytes)

    @property
    def audio_s(self) -> float:
        return sum(n for _, n in self.chunks) / 2 / TTS_RATE

    @property
    def first_audio_ms(self) -> float | None:
        return (self.chunks[0][0] - self.sent_at) * 1000 if self.chunks else None

    @property
    def rtf(self) -> float | None:
        if not self.chunks or not self.audio_s:
            return None
        return (self.chunks[-1][0] - self.sent_at) / self.audio_s

    def pieces(self, seconds: float) -> list[tuple[float, int]]:
        """The audio regrouped into pieces of `seconds`, each available when complete."""
        size, out, have = int(seconds * TTS_RATE * 2), [], 0
        for arrival, n in self.chunks:
            have += n
            while have >= size:
                out.append((arrival, size))
                have -= size
        if have and self.chunks:
            out.append((self.chunks[-1][0], have))
        return out

    @property
    def first_piece_ms(self) -> float | None:
        p = self.pieces(PIECE_S)
        return (p[0][0] - self.sent_at) * 1000 if p else None


def synthesise(client: httpx.Client, s: Synthesis, voice: str) -> None:
    body = {"model": "pocket-tts", "input": s.text, "voice": voice, "response_format": "pcm"}
    s.sent_at = time.perf_counter()
    with client.stream("POST", f"{TTS_URL}/audio/speech", json=body, timeout=120) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            if chunk:
                s.chunks.append((time.perf_counter(), len(chunk)))


def playback(syntheses: list[Synthesis], piece_s: float | None = None) -> dict[str, Any]:
    """Play the reply in real time from its first audio; count where audio arrived late.

    A stall inside a sentence is choppy audio (the voice is slower than real time); one at
    a sentence start is a pause between sentences (waiting for the model or the voice).
    With `piece_s`, audio is played in pieces of that length, as the voice service does.
    """
    chunks = [(t, n, i == 0) for s in syntheses
              for i, (t, n) in enumerate(s.pieces(piece_s) if piece_s else s.chunks)]
    if not chunks:
        return {}
    clock = chunks[0][0]
    inside, between = [], []
    for arrival, n, first_of_sentence in chunks:
        if arrival > clock:
            (between if first_of_sentence else inside).append(arrival - clock)
            clock = arrival
        clock += n / 2 / TTS_RATE

    def summary(gaps: list[float]) -> dict[str, Any]:
        audible = [g for g in gaps if g > AUDIBLE_GAP_S]
        return {"total_ms": round(sum(gaps) * 1000), "audible_count": len(audible),
                "longest_ms": round(max(gaps, default=0) * 1000)}

    return {"inside_sentences": summary(inside), "between_sentences": summary(between)}


def cmd_tts(args: argparse.Namespace) -> int:
    with httpx.Client(timeout=120) as client:
        synthesise(client, Synthesis(TTS_ALONE_SENTENCE, 0), args.voice)  # warm-up
        runs = []
        for _ in range(args.runs):
            s = Synthesis(TTS_ALONE_SENTENCE, time.perf_counter())
            synthesise(client, s, args.voice)
            runs.append({"first_audio_ms": round(s.first_audio_ms or 0),
                         "rtf": round(s.rtf or 0, 3), "audio_s": round(s.audio_s, 2),
                         "stalls": playback([s])["inside_sentences"]})
    save(args.out, "tts_alone", args.label, {
        "meta": meta({"runs": args.runs, "threads": args.threads_note,
                      "sentence": TTS_ALONE_SENTENCE}),
        "first_audio_ms_median": med([r["first_audio_ms"] for r in runs]),
        "rtf_median": round(statistics.median(r["rtf"] for r in runs), 3),
        "runs": runs,
    })
    return 0


# ------------------------------------------------------------------ turn
def warm_prompt(client: httpx.Client, model: str) -> tuple[float, int]:
    """What ATLAS sends while the call connects (llm_warmup.build_warmup_body)."""
    body = {"model": model, "max_tokens": 1, "stream": False, "tools": TOOLS,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "assistant", "content": GREETING},
                         {"role": "user", "content": "Hello?"}]}
    start = time.perf_counter()
    r = client.post(f"{LLM_URL}/chat/completions", json=body, timeout=300)
    r.raise_for_status()
    return (time.perf_counter() - start) * 1000, r.json()["usage"]["prompt_tokens"]


def run_turn(client: httpx.Client, tts_client: httpx.Client, args: argparse.Namespace,
             history: list[dict[str, str]], wav: bytes) -> dict[str, Any]:
    t0 = time.perf_counter()  # STT request = the moment the voice service detects silence
    stt_ms, transcript = transcribe(client, args.stt_model, wav)
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "assistant", "content": GREETING}, *history,
                {"role": "user", "content": transcript}]
    body = {"model": args.llm_model, "stream": True, "stream_options": {"include_usage": True},
            "messages": messages, "tools": TOOLS, "temperature": 0, "seed": 7,
            "max_tokens": args.max_tokens}

    sentences: queue.Queue[Synthesis | None] = queue.Queue()
    done: list[Synthesis] = []

    def voice_worker() -> None:
        while (s := sentences.get()) is not None:
            synthesise(tts_client, s, args.voice)
            done.append(s)

    worker = threading.Thread(target=voice_worker, daemon=True)
    worker.start()
    llm_start = time.perf_counter()
    first_token = None
    token_times: list[float] = []
    text, pending, usage, tool_called = "", "", {}, False
    with client.stream("POST", f"{LLM_URL}/chat/completions", json=body, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            event = json.loads(payload)
            usage = event.get("usage") or usage
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                tool_called = tool_called or bool(delta.get("tool_calls"))
                piece = delta.get("content") or ""
                if not piece:
                    continue
                now = time.perf_counter()
                first_token = first_token or now
                token_times.append(now)
                text += piece
                pending += piece
                while (m := _SENTENCE_END.search(pending)) is not None:
                    sentence, pending = pending[: m.end()].strip(), pending[m.end():]
                    sentences.put(Synthesis(sentence, time.perf_counter()))
    llm_end = time.perf_counter()
    if pending.strip():
        sentences.put(Synthesis(pending.strip(), llm_end))
    sentences.put(None)
    worker.join()

    history += [{"role": "user", "content": transcript}, {"role": "assistant", "content": text}]
    first = done[0] if done else None
    tts_busy_from = first.sent_at if first else llm_end

    def rate(times: list[float]) -> float | None:
        return round((len(times) - 1) / (times[-1] - times[0]), 1) if len(times) > 2 else None

    rel = lambda t: round((t - t0) * 1000)  # noqa: E731
    return {
        "transcript": transcript,
        "reply": text,
        "tool_called": tool_called,
        "stt_ms": round(stt_ms),
        "llm_first_token_ms": round((first_token - llm_start) * 1000) if first_token else None,
        "llm_first_sentence_ms": round((first.ready_at - llm_start) * 1000) if first else None,
        "tts_first_audio_ms": round(first.first_audio_ms) if first and first.chunks else None,
        # From the end of the caller's speech: + the 0.2 s silence before the STT request.
        "reply_start_ms": rel(first.chunks[0][0]) + round(VAD_STOP_S * 1000)
        if first and first.chunks else None,
        # The same, waiting for 0.5 s of audio as the voice service does before it plays.
        "tts_first_piece_ms": round(first.first_piece_ms) if first and first.chunks else None,
        "reply_start_voice_service_ms": rel(first.pieces(PIECE_S)[0][0])
        + round(VAD_STOP_S * 1000) if first and first.chunks else None,
        "llm_total_ms": round((llm_end - llm_start) * 1000),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        # Content pieces per second: before the voice started (no contention) and after.
        "llm_rate_before_tts": rate([t for t in token_times if t <= tts_busy_from]),
        "llm_rate_during_tts": rate([t for t in token_times if t > tts_busy_from]),
        "llm_overlaps_tts_ms": round(max(0.0, llm_end - tts_busy_from) * 1000),
        "sentences": [{"text": s.text, "first_audio_ms": round(s.first_audio_ms or 0),
                       "rtf": round(s.rtf, 3) if s.rtf else None,
                       "audio_s": round(s.audio_s, 2),
                       "ready_ms": rel(s.ready_at), "sent_ms": rel(s.sent_at),
                       # raw arrivals: [ms after the request, bytes]
                       "chunks": [[round((t - s.sent_at) * 1000), n] for t, n in s.chunks]}
                      for s in done],
        "tts_rtf_first_sentence": round(first.rtf, 3) if first and first.rtf else None,
        "stalls": playback(done),
        "stalls_voice_service": playback(done, PIECE_S),
    }


def cmd_turn(args: argparse.Namespace) -> int:
    wavs, texts = load_wavs(args.dir)
    with httpx.Client(timeout=120) as client, httpx.Client(timeout=120) as tts_client:
        transcribe(client, args.stt_model, wavs["s3_1"])  # load Whisper, not counted
        synthesise(tts_client, Synthesis("Hello there.", 0), args.voice)
        # A different first line so the warm-up does not cache the first real request.
        warm_ms, prompt_tokens = warm_prompt(client, args.llm_model)
        time.sleep(1.0)
        history: list[dict[str, str]] = []
        turns = []
        for i in range(args.turns):
            key = TURN_WAVS[i % len(TURN_WAVS)]
            turn = run_turn(client, tts_client, args, history, wavs[key])
            turn["wav"] = key
            turn["word_errors"] = word_errors(texts[key]["text"], turn["transcript"])[0]
            turns.append(turn)
            print(json.dumps({k: turn[k] for k in (
                "stt_ms", "llm_first_token_ms", "llm_first_sentence_ms", "tts_first_audio_ms",
                "reply_start_ms", "tts_rtf_first_sentence", "stalls")}), file=sys.stderr)
            time.sleep(args.pause)

    def m(key: str) -> float | None:
        return med([t[key] for t in turns if t.get(key) is not None])

    def stalls(kind: str, part: str, key: str) -> int:
        return sum(t[kind].get(part, {}).get(key, 0) for t in turns)

    save(args.out, "turns", args.label, {
        "meta": meta({"turns": args.turns, "threads": args.threads_note,
                      "stt_model": args.stt_model, "llm_model": args.llm_model,
                      "warmup_ms": round(warm_ms), "warm_prompt_tokens": prompt_tokens,
                      "max_tokens": args.max_tokens}),
        "median": {k: m(k) for k in (
            "stt_ms", "llm_first_token_ms", "llm_first_sentence_ms", "tts_first_audio_ms",
            "reply_start_ms", "tts_first_piece_ms", "reply_start_voice_service_ms",
            "tts_rtf_first_sentence", "llm_rate_before_tts",
            "llm_rate_during_tts", "llm_total_ms")},
        "audible_stalls_inside_sentences": sum(
            t["stalls"].get("inside_sentences", {}).get("audible_count", 0) for t in turns),
        "stall_ms_inside_sentences": sum(
            t["stalls"].get("inside_sentences", {}).get("total_ms", 0) for t in turns),
        "turns": turns,
    })
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("wavs", "stt", "tts", "turn"):
        s = sub.add_parser(name)
        s.add_argument("--dir", required=True, help="directory for the test WAVs")
        s.add_argument("--voice", default="alba")
        s.add_argument("--out", help="JSON file to add the results to")
        s.add_argument("--label", default="", help="name of this configuration")
        s.add_argument("--threads-note", default="", help="thread settings, for the record")
        s.add_argument("--runs", type=int, default=5)
    sub.choices["stt"].add_argument("--models", default="Systran/faster-whisper-base")
    sub.choices["stt"].add_argument("--unload", action="store_true",
                                    help="unload each model afterwards to free RAM")
    t = sub.choices["turn"]
    t.add_argument("--turns", type=int, default=5)
    t.add_argument("--stt-model", default="Systran/faster-whisper-base")
    t.add_argument("--llm-model", default="qwen2.5:1.5b")
    t.add_argument("--max-tokens", type=int, default=120)
    t.add_argument("--pause", type=float, default=1.0, help="seconds between turns")
    args = p.parse_args()
    return {"wavs": cmd_wavs, "stt": cmd_stt, "tts": cmd_tts, "turn": cmd_turn}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
