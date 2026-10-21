"""Voice samples for cloning: common audio formats in, the WAV Pocket TTS reads best out.

Every upload (WAV included) is decoded with FFmpeg through PyAV, downmixed to mono and
resampled to 24 kHz 16-bit PCM WAV, the model's own rate, so the Pocket TTS server gets one
well-formed format whatever the caller sent and the 5 to 30 second rule is checked here,
before anything is forwarded.

Decoding runs in a short-lived child process: FFmpeg parses untrusted bytes, and a crash,
hang or memory blow-up there must not take an API worker (and every tenant's requests on
it) down. PyAV 19.0.1, for one, aborts the process on audio with a custom channel order
(PyAV issue 2444). The child gets the bytes on stdin, none of the API's environment, and a
time limit; it picks the demuxer from the file's first bytes (never FFmpeg's probing),
decodes only allowed codecs, and stops as soon as the audio is longer than 30 seconds.

This file is also that child (`python -I voice_samples.py <demuxer>`), so it imports only
the standard library at module level and PyAV only inside the decoder: the API process
itself never loads FFmpeg.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import subprocess
import sys
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_RATE = 24_000  # Pocket TTS's model rate, so the server does not resample again
MIN_SECONDS = 5.0
MAX_SECONDS = 30.0
MAX_CHANNELS = 8
MIN_RATE, MAX_RATE = 8_000, 192_000
# Decoded frames; 30 s of the smallest real frames (2.5 ms Opus) is 12,000.
MAX_FRAMES = 100_000
DECODE_TIMEOUT_SECONDS = 30.0
_DECODE_SLOTS = threading.BoundedSemaphore(2)  # child processes per API worker
_CHILD_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
_EXIT_REJECTED = 2

FORMATS = "WAV, MP3, M4A (AAC), OGG (Opus or Vorbis), WebM (Opus) or FLAC"
UNSUPPORTED = f"Upload the sample as {FORMATS}."
UNREADABLE = f"Could not read this audio. Upload a {FORMATS} file."
TOO_LONG = (
    f"The sample is longer than {MAX_SECONDS:.0f} seconds: it must be between "
    f"{MIN_SECONDS:.0f} and {MAX_SECONDS:.0f} seconds."
)

# FFmpeg codec names decoded; anything else in an accepted container is refused unread.
_PCM = {f"pcm_{k}{e}" for k in ("s16", "s24", "s32", "f32", "f64") for e in ("le", "be")}
CODECS = frozenset(
    _PCM
    | {"pcm_u8", "pcm_mulaw", "pcm_alaw", "adpcm_ima_wav", "adpcm_ms"}
    | {"mp3", "aac", "alac", "flac", "opus", "vorbis"}
)
DEMUXERS = frozenset({"wav", "flac", "ogg", "matroska", "mov", "mp3", "aac"})


class SampleError(Exception):
    """The sample cannot be used; `message` is safe to show to the caller."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status  # 415: not readable audio; 422: readable but not usable
        self.message = message


@dataclass(frozen=True, slots=True)
class Sample:
    wav: bytes  # mono 16-bit PCM WAV at TARGET_RATE
    source_format: str  # the demuxer the upload was read with


def sniff(data: bytes) -> str | None:
    """The FFmpeg demuxer for an upload, from its first bytes; None for other formats."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"  # WebM is Matroska
    if data[4:8] == b"ftyp":
        return "mov"  # the MP4 family: M4A, MP4, 3GP
    start = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        # An ID3v2 tag (size as a 28-bit "syncsafe" integer) before MP3 or AAC frames.
        size = data[6] << 21 | data[7] << 14 | data[8] << 7 | data[9]
        start = 10 + size + (10 if data[5] & 0x10 else 0)
    head = data[start : start + 2]
    if len(head) == 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        # MPEG audio frame sync; layer bits 00 mark AAC in ADTS framing.
        return "aac" if head[1] & 0x06 == 0 else "mp3"
    return "mp3" if data[:3] == b"ID3" else None


# ---------------------------------------------------------------- the API side


async def normalize(data: bytes) -> Sample:
    """Decode an upload in a child process to mono 16-bit 24 kHz WAV of 5 to 30 seconds."""
    demuxer = sniff(data)
    if demuxer is None:
        raise SampleError(415, UNSUPPORTED)
    wav = await asyncio.to_thread(_run_child, data, demuxer)
    return Sample(wav=wav, source_format=demuxer)


def _child_argv(demuxer: str) -> list[str]:
    # -I: no PYTHON* variables, user site or current directory on the import path.
    return [sys.executable, "-I", str(Path(__file__).resolve()), demuxer]


def _run_child(data: bytes, demuxer: str) -> bytes:
    env = {key: value for key in _CHILD_ENV if (value := os.environ.get(key))}
    with _DECODE_SLOTS:
        try:
            done = subprocess.run(  # noqa: S603 - this interpreter and file, a known demuxer
                _child_argv(demuxer),
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=DECODE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:  # the child is killed
            log.warning("voice_sample_decode_timeout", extra={"demuxer": demuxer})
            raise SampleError(415, UNREADABLE) from None
    if done.returncode == 0 and _is_sample_wav(done.stdout):
        return done.stdout
    if done.returncode == _EXIT_REJECTED and (rejected := _rejection(done.stdout)):
        raise rejected
    # A crash (FFmpeg bug, out of memory) or an unexpected reply.
    log.warning(
        "voice_sample_decode_failed", extra={"demuxer": demuxer, "returncode": done.returncode}
    )
    raise SampleError(415, UNREADABLE)


def _rejection(reply: bytes) -> SampleError | None:
    try:
        body = json.loads(reply)
        status, message = int(body["status"]), str(body["message"])
    except (ValueError, KeyError, TypeError):
        return None
    return SampleError(status, message[:300]) if status in (415, 422) else None


def _is_sample_wav(wav: bytes) -> bool:
    frames = (len(wav) - 44) / 2
    return (
        wav[:4] == b"RIFF"
        and wav[8:12] == b"WAVE"
        and MIN_SECONDS * TARGET_RATE <= frames <= MAX_SECONDS * TARGET_RATE
    )


# ---------------------------------------------------------------- the child side


def decode(data: bytes, demuxer: str) -> bytes:
    """Decode, downmix and resample to mono 16-bit PCM WAV; SampleError when unusable."""
    import av  # here only: the API process never loads FFmpeg

    if demuxer not in DEMUXERS:
        raise SampleError(415, UNSUPPORTED)
    pcm = bytearray()
    limit = int(MAX_SECONDS * TARGET_RATE) * 2
    try:
        with av.open(io.BytesIO(data), "r", format=demuxer) as container:
            if not container.streams.audio:
                raise SampleError(415, f"The file has no audio. {UNSUPPORTED}")
            stream = container.streams.audio[0]
            codec = stream.codec_context.codec.canonical_name
            if codec not in CODECS:
                raise SampleError(415, f"{codec} audio is not supported. {UNSUPPORTED}")
            channels = stream.codec_context.channels
            if channels < 1:
                raise SampleError(415, UNREADABLE)
            if channels > MAX_CHANNELS:
                raise SampleError(415, f"Use a recording with at most {MAX_CHANNELS} channels.")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
            for count, frame in enumerate(container.decode(stream), 1):
                if count > MAX_FRAMES:
                    raise SampleError(415, UNREADABLE)
                if not MIN_RATE <= frame.sample_rate <= MAX_RATE:
                    raise SampleError(
                        415,
                        f"Use a recording sampled at {MIN_RATE // 1000} to {MAX_RATE // 1000} kHz.",
                    )
                for out in resampler.resample(frame):
                    pcm += memoryview(out.planes[0])[: out.samples * 2]
                if len(pcm) > limit:
                    raise SampleError(422, TOO_LONG)
            for out in resampler.resample(None):
                pcm += memoryview(out.planes[0])[: out.samples * 2]
    except SampleError:
        raise
    except Exception as exc:  # FFmpeg and PyAV errors on malformed or unsupported input
        raise SampleError(415, UNREADABLE) from exc
    seconds = len(pcm) / 2 / TARGET_RATE
    if seconds > MAX_SECONDS:
        raise SampleError(422, TOO_LONG)
    if seconds < MIN_SECONDS:
        raise SampleError(
            422,
            f"The sample is {math.floor(seconds * 10) / 10:.1f} s long: it must be between "
            f"{MIN_SECONDS:.0f} and {MAX_SECONDS:.0f} seconds.",
        )
    out_file = io.BytesIO()
    with wave.open(out_file, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(bytes(pcm))
    return out_file.getvalue()


def main() -> int:
    demuxer = sys.argv[1] if len(sys.argv) == 2 else ""
    # All of it before PyAV loads: the parent's write to stdin must never wait on decoding.
    data = sys.stdin.buffer.read()
    try:
        wav = decode(data, demuxer)
    except SampleError as exc:
        sys.stdout.write(json.dumps({"status": exc.status, "message": exc.message}))
        return _EXIT_REJECTED
    sys.stdout.buffer.write(wav)
    return 0


if __name__ == "__main__":
    sys.exit(main())
