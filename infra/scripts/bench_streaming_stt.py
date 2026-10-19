"""Measure the self-hosted streaming speech-to-text server through the voice service's own
Deepgram client, exactly as a call uses it.

Runs inside the voice-runtime container (it has the client library and is on the same
Docker network as `local-stt`). Speech WAVs (any rate, 16-bit mono) are fed in 20 ms frames
in real time, at 16 kHz and at the 8 kHz phone rate (8 kHz also goes through a G.711 mu-law
round trip, like a phone line). The client is built with the same arguments the voice
service uses for a non-Flux Deepgram model.

Two modes per run:
  finalize  the voice service's behaviour: its VAD reports "stopped speaking" 0.2 s after
            the speech ends, and the client sends Finalize then.
  endpoint  no Finalize: the server's own end-of-speech detection has to finish the segment.

Measured per run (wall clock, perf_counter):
  first_interim_ms        speech start -> first live (interim) words
  final_after_speech_ms   speech end -> final transcript reaches the pipeline
  final_after_finalize_ms Finalize sent -> final transcript (finalize mode)
  finalized               whether the voice service marked the transcript finalized
                          (that lets the turn end without its safety-net wait)

Usage (from the repo root):
  docker cp speech.wav <voice-runtime-container>:/tmp/stt-bench/s1.wav
  docker compose --profile voice exec -T voice-runtime /opt/venv/bin/python - \
      --wav /tmp/stt-bench/s1.wav --text "the sentence that was spoken" \
      < infra/scripts/bench_streaming_stt.py > result.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import statistics
import sys
import time
import wave
from datetime import UTC, datetime
from typing import Any

import numpy as np
import soxr
from loguru import logger
from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.workers.runner import WorkerRunner

FRAME_S = 0.02
LEAD_S = 0.5  # silence before the speech
TAIL_S = 2.0  # silence after it (the line stays open, as in a call)
VAD_STOP_S = 0.2  # the voice service's VAD stop_secs (run_pipeline.py)


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wav:
        if wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16-bit PCM")
        rate, channels = wav.getframerate(), wav.getnchannels()
        data = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data / 32768.0, rate


def mulaw_round_trip(x: np.ndarray) -> np.ndarray:
    """G.711 mu-law encode/decode (8-bit companding), as on a phone line."""
    mu = 255.0
    y = np.sign(x) * np.log1p(mu * np.abs(x)) / np.log1p(mu)
    q = np.round((y + 1) / 2 * 255) / 255 * 2 - 1
    return np.sign(q) * (np.expm1(np.abs(q) * np.log1p(mu)) / mu)


def speech_span(x: np.ndarray, rate: int) -> tuple[float, float]:
    """First and last 10 ms frame louder than -35 dB below the peak."""
    hop = rate // 100
    frames = x[: len(x) // hop * hop].reshape(-1, hop)
    rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)
    loud = np.nonzero(rms > rms.max() * 10 ** (-35 / 20))[0]
    return loud[0] * 0.01, (loud[-1] + 1) * 0.01


def prepare(speech: np.ndarray, src_rate: int, rate: int) -> tuple[bytes, float, float]:
    audio = soxr.resample(speech, src_rate, rate) if src_rate != rate else speech
    if rate == 8000:
        audio = mulaw_round_trip(np.clip(audio, -1, 1))
    start, end = speech_span(audio, rate)
    clip = np.concatenate(
        [np.zeros(int(LEAD_S * rate)), audio[: int(end * rate)], np.zeros(int(TAIL_S * rate))]
    )
    pcm = (np.clip(clip, -1, 1) * 32767).astype("<i2").tobytes()
    return pcm, LEAD_S + start, LEAD_S + end


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def wer(ref: str, hyp: str) -> float:
    r, h = words(ref), words(hyp)
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(len(r), 1)


class Collector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, float, str, bool]] = []

    async def process_frame(self, frame: Any, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.perf_counter()
        if isinstance(frame, InterimTranscriptionFrame):
            self.events.append(("interim", now, frame.text, False))
        elif isinstance(frame, TranscriptionFrame):
            self.events.append(("final", now, frame.text, bool(frame.finalized)))
        await self.push_frame(frame, direction)


async def run_once(
    base_url: str,
    model: str,
    pcm: bytes,
    rate: int,
    speech_start: float,
    speech_end: float,
    *,
    finalize: bool,
) -> dict[str, Any]:
    # Same construction as the voice service for a non-Flux Deepgram model.
    stt = DeepgramSTTService(
        api_key="local-not-checked",
        base_url=base_url,
        settings=DeepgramSTTSettings(
            language="en",
            profanity_filter=False,
            endpointing=100,
            model=model,
            keyterm=[],
        ),
        should_interrupt=False,
        sample_rate=rate,
    )
    collector = Collector()
    worker = PipelineWorker(
        Pipeline([stt, collector]),
        params=PipelineParams(audio_in_sample_rate=rate),
        enable_rtvi=False,
        enable_turn_tracking=False,
        cancel_on_idle_timeout=False,
        idle_timeout_secs=None,
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    started = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def _started(_worker: Any, _frame: Any) -> None:
        started.set()

    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    await asyncio.wait_for(started.wait(), 30)
    for _ in range(200):  # connection opens during setup; wait until it is usable
        if stt._connection is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("Deepgram client did not connect")

    step = int(rate * FRAME_S) * 2
    t0 = time.perf_counter()
    finalize_at: float | None = None
    for i in range(0, len(pcm), step):
        n = i // step
        if finalize and finalize_at is None and n * FRAME_S >= speech_end + VAD_STOP_S:
            await worker.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=VAD_STOP_S))
            finalize_at = time.perf_counter()
        await worker.queue_frame(
            InputAudioRawFrame(audio=pcm[i : i + step], sample_rate=rate, num_channels=1)
        )
        delay = t0 + (n + 1) * FRAME_S - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
    await asyncio.sleep(0.5)
    await worker.queue_frame(EndFrame())
    try:
        await asyncio.wait_for(asyncio.shield(run_task), 15)
    finally:
        await runner.cancel()
        await run_task

    speech_start_t, speech_end_t = t0 + speech_start, t0 + speech_end
    interims = [e for e in collector.events if e[0] == "interim"]
    finals = [e for e in collector.events if e[0] == "final"]
    closing = next((e for e in finals if e[1] >= speech_end_t), None)
    ms = lambda a, b: round((a - b) * 1000, 1)  # noqa: E731
    return {
        "mode": "finalize" if finalize else "endpoint",
        "first_interim_ms": ms(interims[0][1], speech_start_t) if interims else None,
        "first_interim_after_audio_ms": ms(interims[0][1], t0) if interims else None,
        "interim_updates": len(interims),
        "final_after_speech_ms": ms(closing[1], speech_end_t) if closing else None,
        "final_after_finalize_ms": (
            ms(closing[1], finalize_at) if closing and finalize_at is not None else None
        ),
        "finalized": closing[3] if closing else None,
        "finals": len(finals),
        "transcript": " ".join(e[2] for e in finals),
    }


def median(values: list[float | None], digits: int = 1) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), digits) if clean else None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://local-stt:8000")
    parser.add_argument("--model", default="zipformer-en-streaming")
    parser.add_argument("--wav", action="append", required=True)
    parser.add_argument("--text", action="append", required=True, help="reference, per --wav")
    parser.add_argument("--rates", default="16000,8000")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if len(args.wav) != len(args.text):
        raise SystemExit("one --text per --wav")
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    results = []
    for path, ref in zip(args.wav, args.text, strict=False):
        speech, src_rate = read_wav(path)
        for rate in (int(r) for r in args.rates.split(",")):
            pcm, start, end = prepare(speech, src_rate, rate)
            await run_once(args.base_url, args.model, pcm, rate, start, end, finalize=True)  # warm
            runs = [
                await run_once(args.base_url, args.model, pcm, rate, start, end, finalize=True)
                for _ in range(args.runs)
            ]
            endpoint = await run_once(
                args.base_url, args.model, pcm, rate, start, end, finalize=False
            )
            for r in [*runs, endpoint]:
                r["wer"] = round(wer(ref, r["transcript"]), 3)
            results.append(
                {
                    "wav": os.path.basename(path),
                    "reference": ref,
                    "sample_rate": rate,
                    "speech_s": round(end - start, 2),
                    "median": {
                        k: median([r[k] for r in runs], 3 if k == "wer" else 1)
                        for k in (
                            "first_interim_ms",
                            "final_after_speech_ms",
                            "final_after_finalize_ms",
                            "wer",
                        )
                    },
                    "runs": runs,
                    "endpoint_run": endpoint,
                }
            )
    print(
        json.dumps(
            {
                "meta": {
                    "date": datetime.now(UTC).date().isoformat(),
                    "client": "voice service Deepgram STT client (same constructor arguments)",
                    "server": args.base_url,
                    "model": args.model,
                    "cpus": os.cpu_count(),
                    "machine": platform.machine(),
                    "frame_ms": FRAME_S * 1000,
                    "vad_stop_s": VAD_STOP_S,
                    "runs": args.runs,
                    "phone_audio": "8 kHz runs use a G.711 mu-law round trip",
                },
                "results": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
