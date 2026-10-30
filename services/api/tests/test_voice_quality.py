"""Reference-recording quality for voice cloning: measurements, the part chosen, findings."""

from __future__ import annotations

import io
import math
import random
import wave
from array import array

import pytest

from app.schemas.voice_cloning import QualityFinding
from app.services import voice_quality as quality
from app.services.voice_samples import Sample

RATE = 24000


def speech_wav(
    seconds: float = 12.0,
    *,
    rate: int = RATE,
    channels: int = 1,
    level: float = 0.25,
    noise: float = 0.0,
    gain: float = 1.0,
    dc: float = 0.0,
    lead: float = 0.0,
    tail: float = 0.0,
    pause: float = 0.3,
    fade: float | None = None,
) -> bytes:
    """A speech-like 16-bit WAV: one-second phrases of a buzzy voice (120 Hz with harmonics
    and a syllable envelope) each followed by a pause, with optional noise, silence at the
    ends, clipping (a gain over 1), a DC offset, and `fade` scaling the second half."""
    rng = random.Random(7)  # noqa: S311 - repeatable test noise, nothing secret
    phrase = [
        (0.6 + 0.4 * math.sin(2 * math.pi * 4 * i / rate))
        * (
            math.sin(2 * math.pi * 120 * i / rate)
            + 0.5 * math.sin(2 * math.pi * 240 * i / rate)
            + 0.25 * math.sin(2 * math.pi * 360 * i / rate)
        )
        / 1.75
        if i < (1.0 - pause) * rate
        else 0.0
        for i in range(rate)
    ]
    samples = array("h")
    begin = int(lead * rate)
    for i in range(int(seconds * rate)):
        t = i / rate
        amp = level if lead <= t < seconds - tail else 0.0
        if fade is not None and t > seconds / 2:
            amp *= fade
        v = (amp * phrase[(i - begin) % rate] + rng.uniform(-noise, noise)) * gain + dc
        s = max(-32768, min(32767, int(v * 32767)))
        for _ in range(channels):
            samples.append(s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def pcm(wav: bytes) -> array[int]:
    with wave.open(io.BytesIO(wav)) as w:
        samples = array("h")
        samples.frombytes(w.readframes(w.getnframes()))
    return samples


def sample(wav: bytes, *, rate: int = RATE, channels: int = 1, codec: str = "pcm_s16le") -> Sample:
    """As the decoder hands a sample on: the WAV, plus what the upload was."""
    return Sample(
        wav=wav, source_format="wav", source_rate=rate, source_channels=channels, codec=codec
    )


def codes(ref: quality.Reference) -> list[tuple[str, str]]:
    return [(f.level, f.code) for f in ref.quality.findings]


def test_clean_speech_has_nothing_to_report() -> None:
    ref = quality.assess(sample(speech_wav(12.0)))
    assert ref.quality.score == 100 and ref.quality.findings == []
    assert ref.start == 0.0 and ref.upload_seconds == 12.0
    # The pause after the last phrase is cut, but for a 0.2 s pad; too small a cut to mention.
    assert ref.seconds == pytest.approx(11.9, abs=0.05)
    with wave.open(io.BytesIO(ref.wav)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, RATE, 2)
        assert w.getnframes() == pytest.approx(11.9 * RATE, abs=0.05 * RATE)


def test_measurements_of_a_known_signal() -> None:
    # Five one-second bursts of a 1 kHz sine at half scale, each followed by a silent second.
    loud = [int(0.5 * 32767 * math.sin(2 * math.pi * 1000 * i / RATE)) for i in range(RATE)]
    samples = array("h")
    for second in range(10):
        samples.extend(loud if second % 2 == 0 else [0] * RATE)
    f = quality.frames(samples, RATE)
    m = quality.measure(samples, f, 0, len(f.levels))
    assert m.seconds == 10.0
    assert m.peak == pytest.approx(0.5, abs=0.001)
    assert m.clipping == 0.0
    assert m.rms_dbfs == pytest.approx(-12.04, abs=0.1)  # half the time at -9 dBFS
    assert m.speech_dbfs == pytest.approx(-9.03, abs=0.1)
    assert m.noise_floor_dbfs == quality.NOISE_FLOOR_MIN_DBFS
    assert m.snr_db == pytest.approx(81, abs=1)
    assert m.dc_offset == pytest.approx(0.0, abs=0.001)
    assert (m.speech_ratio, m.speech_seconds) == (0.5, 5.0)
    assert (m.leading_silence, m.trailing_silence, m.longest_pause) == (0.0, 1.0, 1.0)
    assert m.level_spread_db == pytest.approx(0.0, abs=0.01)

    # Clipped samples are counted, and the DC offset measured and left out of the levels.
    for i in range(100):
        samples[i * 7] = 32767
    offset = array("h", (min(s + 1000, 32767) for s in samples))
    f = quality.frames(offset, RATE)
    m = quality.measure(offset, f, 0, len(f.levels))
    assert m.clipping == pytest.approx(100 / len(samples), rel=0.05)
    assert m.dc_offset == pytest.approx(1000 / 32768, abs=0.001)
    assert m.noise_floor_dbfs == quality.NOISE_FLOOR_MIN_DBFS, "the offset is not noise"
    assert m.speech_dbfs == pytest.approx(-9.03, abs=0.15)

    with pytest.raises(ValueError):
        quality.frames(array("h", [0] * 10), RATE)


def test_the_part_to_clone_from() -> None:
    # Silence before and after is cut, a pad of 0.2 s left; too short to choose from.
    f = quality.frames(pcm(speech_wav(13.0, lead=4.0, tail=3.0)), RATE)
    start, end, chosen = quality.select(f)
    assert start * 0.02 == pytest.approx(3.8) and end * 0.02 == pytest.approx(9.9)
    assert not chosen
    # Never under the 5 s the voice server needs: the ends come back in.
    f = quality.frames(pcm(speech_wav(5.5, lead=1.0)), RATE)
    start, end, chosen = quality.select(f)
    assert (start, end, chosen) == (0, 275, False)

    # A long upload: 15 s with the most speech, from the first word; ties go to the earliest.
    long = pcm(speech_wav(40.0, lead=3.0))
    f = quality.frames(long, RATE)
    start, end, chosen = quality.select(f)
    assert start * 0.02 == pytest.approx(2.8) and (end - start) * 0.02 == 15.0
    assert chosen
    # Clipping in the middle: the clean part before it is chosen; with the whole first half
    # clipped, the least clipped window.
    clipped = array("h", long)
    for i in range(20 * RATE, 30 * RATE):
        clipped[i] = max(-32768, min(32767, clipped[i] * 20))
    start, end, chosen = quality.select(quality.frames(clipped, RATE))
    assert start * 0.02 == pytest.approx(2.8) and end * 0.02 <= 20.0
    for i in range(0, 20 * RATE):
        clipped[i] = max(-32768, min(32767, clipped[i] * 20))
    start, end, chosen = quality.select(quality.frames(clipped, RATE))
    assert start * 0.02 >= 24.5 and chosen
    # Pauses count against a window: the one over the densest speech wins.
    sparse = pcm(speech_wav(40.0, pause=0.6))
    dense = pcm(speech_wav(15.0, pause=0.1))
    sparse[10 * RATE : 25 * RATE] = dense
    start, end, chosen = quality.select(quality.frames(sparse, RATE))
    assert 9.5 <= start * 0.02 <= 10.5 and chosen


def test_findings_name_each_fault_and_what_to_do() -> None:
    def found(
        wav: bytes, *, rate: int = RATE, channels: int = 1, codec: str = "pcm_s16le"
    ) -> dict[str, tuple[str, str]]:
        ref = quality.assess(sample(wav, rate=rate, channels=channels, codec=codec))
        return {f.code: (f.level, f.message) for f in ref.quality.findings}

    heavy = found(speech_wav(gain=20))
    assert heavy["clipping"][0] == "problem"
    assert "% of samples: lower the input gain" in heavy["clipping"][1]
    light = found(speech_wav(gain=5))
    assert light["clipping"][0] == "warning" and "A few peaks clip" in light["clipping"][1]

    noisy = found(speech_wav(noise=0.03))
    assert noisy["noise"][0] == "warning"
    assert "dB below the voice" in noisy["noise"][1]
    drowned = found(speech_wav(noise=0.15))
    assert drowned["noise"][0] == "problem" and "almost as loud" in drowned["noise"][1]
    assert "little_speech" not in drowned, "speech cannot be told from such noise"

    silent = found(speech_wav(6.0, level=0.0))
    assert silent["silent"][0] == "problem" and "noise" not in silent

    ends = found(speech_wav(13.0, lead=4.0, tail=3.0))
    assert ends["window"] == (
        "ok",
        "Using 6.1 s from 3.8 s to 9.9 s: the silence before and after it was cut.",
    )
    assert ends["little_speech"][0] == "warning" and ends["short"][0] == "warning"
    assert "silence_start" not in ends and "silence_end" not in ends

    short = found(speech_wav(6.0))
    assert short["short"] == (
        "warning",
        "Short (5.9 s): 10 to 20 seconds of speech gives a better clone.",
    )
    assert short["little_speech"][1].startswith("Only 4.2 s of speech")
    pauses = found(speech_wav(pause=0.6))
    assert pauses["speech_ratio"][0] == "warning"
    assert pauses["speech_ratio"][1].startswith("Only 41 % of the recording is speech")
    # The gap, and the pause that ends the phrase before it.
    assert found(_with_gap())["long_pause"][1].startswith("A 4.3 s pause in the middle")

    assert found(speech_wav(fade=0.1))["uneven_level"][0] == "warning"
    assert found(speech_wav(level=0.01))["quiet"][1].startswith("The recording is quiet (-")
    offset = found(speech_wav(dc=0.05))
    assert offset["dc_offset"][1].startswith("The recording has a DC offset (5 % of full scale)")
    rate = found(speech_wav(), rate=8000)
    assert rate["low_sample_rate"][1].startswith("Sampled at 8 kHz")
    assert found(speech_wav(), channels=2)["stereo"] == (
        "ok",
        "Stereo: the two channels were mixed to one.",
    )
    assert found(speech_wav(), channels=6)["stereo"][1] == "6 channels were mixed to one."
    assert found(speech_wav(), codec="opus")["compressed"][1].startswith(
        "OPUS is a compressed format"
    )

    chosen = quality.assess(sample(speech_wav(40.0, lead=3.0)))
    assert codes(chosen) == [("ok", "window")]
    assert (
        chosen.quality.findings[0].message == "Using 15 s from 2.8 s to 17.8 s, the clearest part."
    )
    assert (chosen.start, chosen.seconds, chosen.upload_seconds) == (2.8, 15.0, 40.0)


def _with_gap() -> bytes:
    """14 s of speech with a 4 s gap after the first 5 s."""
    samples = pcm(speech_wav(14.0))
    samples[5 * RATE : 9 * RATE] = array("h", [0] * (4 * RATE))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def test_the_score_and_the_order_of_findings() -> None:
    ref = quality.assess(sample(speech_wav(6.0, gain=20, dc=0.05), channels=2))
    assert [f.level for f in ref.quality.findings] == [
        "problem",
        "warning",
        "warning",
        "warning",
        "ok",
    ]
    assert ref.quality.score == 100 - 35 - 3 * 12
    ruined = quality.assess(sample(speech_wav(6.0, gain=20, noise=0.15)))
    assert [f.level for f in ruined.quality.findings] == ["problem", "problem", "warning"]
    assert ruined.quality.score == 100 - 2 * 35 - 12
    # Never below zero.
    assert quality.score([QualityFinding(level="problem", code="x", message="x")] * 3) == 0
