"""Quality of a reference recording for voice cloning: the part to clone from, and findings.

A cloned voice copies whatever its reference carries, so the sample is measured before it
goes anywhere near the voice server: clipping, background noise, level, silence and DC
offset from the decoded audio, and the sample rate, channel count and codec the decoder saw.
Each finding has a level: `problem` (the sample is refused: the clone would be bad),
`warning` (the clone may suffer; the voice is still created) or `ok` (a note, nothing to
do). The thresholds are the named constants below. Music, echo and a second voice are not
measured: the form's guidance covers them.

An upload may be up to 75 s. The silence before the first and after the last word is cut,
and from anything longer than 15 s the 15 s with the most speech and no clipping are chosen,
then trimmed to their own first and last word (not under 10 s): the voice server always gets
5 to 15 s, and the findings describe that part. The noise floor is measured over the whole
upload, where the pauses are.

Pure Python over the decoded sample (mono 16-bit at the model's rate; math.sumprod does the
sums), well under a second in a worker thread: numpy is not a dependency of the API.
"""

from __future__ import annotations

import io
import math
import wave
from array import array
from dataclasses import dataclass

from app.schemas.voice_cloning import FindingLevel, QualityFinding, SampleQuality
from app.services.voice_samples import MIN_SECONDS, Sample

FULL_SCALE = 32768  # 16-bit samples
WINDOW_SECONDS = 0.02  # short-time level: 20 ms, a few pitch periods
SEGMENT_SECONDS = 1.0  # level steadiness is compared between one-second stretches
REFERENCE_SECONDS = 15.0  # this much of a longer upload goes to the voice server
REFERENCE_MIN_SECONDS = 10.0  # the chosen part is not trimmed shorter than this
TRIM_PAD_SECONDS = 0.2  # kept before the first and after the last word
SELECT_STEP_SECONDS = 0.5  # between the candidate starts over a longer upload
NOTE_CUT_SECONDS = 0.5  # a cut of at least this is mentioned in the findings
NOISE_PERCENTILE = 0.05  # the quietest windows: the pauses between words and phrases
NOISE_FLOOR_MIN_DBFS = -90.0  # digital silence still counts as a floor (16-bit dither level)
SILENT_DBFS = -80.0  # a whole part below this is silence: nothing was recorded
SILENCE_MARGIN_DB = 6.0  # a window this close to the noise floor is a pause, not speech
CLIP_LEVEL = 0.985  # of full scale: resampling moves clipped flat tops off the exact maximum
CLIP_WARN_FRACTION = 0.0002  # of samples: a few clipped peaks (about 3 ms in 15 s)
CLIP_PROBLEM_FRACTION = 0.005  # of samples: audible distortion throughout
SNR_WARN_DB = 20.0  # voice above the noise floor; a quiet room and a close microphone give 35+
SNR_PROBLEM_DB = 10.0  # the noise is almost as loud as the voice
QUIET_DBFS = -35.0  # speech level below this: far from the microphone, or the gain is low
LEVEL_SPREAD_WARN_DB = 10.0  # between the quieter and the louder seconds of speech
DC_OFFSET_WARN = 0.02  # of full scale: a faulty interface; it costs headroom
LEADING_SILENCE_WARN_SECONDS = 2.0  # before the first word (left to reach the minimum length)
TRAILING_SILENCE_WARN_SECONDS = 2.0  # after the last word (likewise)
PAUSE_WARN_SECONDS = 3.0  # the longest pause between words
SPEECH_RATIO_WARN = 0.6  # speech time over the whole part; natural reading gives 0.7 to 0.9
SPEECH_WARN_SECONDS = 5.0  # the voice is heard for less than this
SPEECH_PROBLEM_SECONDS = 3.0  # too little voice to clone from
SHORT_WARN_SECONDS = 8.0  # 10 to 20 seconds gives a better clone
MIN_RATE_HZ = 16_000  # below, the recording lacks the top of the voice (telephone quality)
LOSSY_CODECS = frozenset({"mp3", "aac", "opus", "vorbis"})
PROBLEM_POINTS, WARNING_POINTS = 35, 12  # taken off the score of 100 per finding
_RANK: dict[FindingLevel, int] = {"problem": 0, "warning": 1, "ok": 2}


@dataclass(frozen=True, slots=True)
class Frames:
    """The upload in 20 ms windows."""

    rate: int
    window: int  # samples per window
    mean: float  # the DC offset, in sample units
    levels: list[float]  # dBFS, DC removed
    silent: list[bool]  # at the noise floor: a pause
    clipped: list[int]  # samples at or near full scale
    floor: float  # the noise floor, dBFS


@dataclass(frozen=True, slots=True)
class Measurements:
    seconds: float
    peak: float  # of full scale
    clipping: float  # fraction of samples at or near full scale
    rms_dbfs: float  # whole part, DC removed
    speech_dbfs: float  # the windows with speech
    noise_floor_dbfs: float
    snr_db: float  # speech above the noise floor
    dc_offset: float  # mean sample, as a fraction of full scale
    speech_ratio: float  # speech time over the whole part
    leading_silence: float  # seconds before the first speech
    trailing_silence: float  # seconds after the last speech
    longest_pause: float  # seconds, between speech
    speech_seconds: float
    level_spread_db: float  # between the quieter and the louder seconds of speech


@dataclass(frozen=True, slots=True)
class Reference:
    """The part of an upload to clone from, and what was found about it."""

    wav: bytes  # mono 16-bit PCM WAV at the upload's decoded rate, 5 to 15 s
    start: float  # seconds into the upload
    seconds: float
    upload_seconds: float
    quality: SampleQuality


def _dbfs(level: float) -> float:
    return 20 * math.log10(max(level, 1e-9))


def _power_mean_db(levels: list[float]) -> float:
    return 10 * math.log10(sum(10 ** (lv / 10) for lv in levels) / len(levels))


def _percentile(ordered: list[float], fraction: float) -> float:
    return ordered[int(fraction * (len(ordered) - 1))]


def _runs(flags: list[bool]) -> tuple[int, int, int]:
    """Leading, trailing and longest inner run of True, in items."""
    first = next((i for i, f in enumerate(flags) if not f), len(flags))
    last = next((i for i in range(len(flags) - 1, -1, -1) if not flags[i]), -1)
    longest = run = 0
    for flag in flags[first : last + 1]:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    return first, len(flags) - 1 - last if last >= 0 else 0, longest


def _energy(w: array[int]) -> float:
    """Sum of squares about the window's own mean: sum((x - m)^2) = sum(x^2) - sum(x)^2 / k.

    Each window's mean, not the upload's: asymmetric speech shifts the latter, and that
    shift would show up as a floor in every quiet window.
    """
    return max(math.sumprod(w, w) - sum(w) ** 2 / len(w), 0.0)


def frames(samples: array[int], rate: int) -> Frames:
    """Level, pause and clipping of every 20 ms window; at least one window of audio."""
    window = max(1, int(WINDOW_SECONDS * rate))
    n = len(samples) - len(samples) % window
    if n < window:
        raise ValueError("too short to measure")
    mean = sum(samples[:n]) / n
    clip_at = int(CLIP_LEVEL * FULL_SCALE)
    levels: list[float] = []
    clipped: list[int] = []
    for start in range(0, n, window):
        w = samples[start : start + window]
        levels.append(_dbfs(math.sqrt(_energy(w) / window) / FULL_SCALE))
        hits = 0
        if max(w) >= clip_at or min(w) <= -clip_at:  # the C-level scan first: most windows
            hits = sum(1 for s in w if s >= clip_at or s <= -clip_at)
        clipped.append(hits)
    floor = max(_percentile(sorted(levels), NOISE_PERCENTILE), NOISE_FLOOR_MIN_DBFS)
    silent = [lv < floor + SILENCE_MARGIN_DB for lv in levels]
    return Frames(rate, window, mean, levels, silent, clipped, floor)


def _trimmed(f: Frames, lo: int, hi: int, least: int) -> tuple[int, int]:
    """[lo, hi) without the pauses at its ends (a pad kept), unless that leaves under `least`."""
    pad = int(TRIM_PAD_SECONDS / WINDOW_SECONDS)
    leading, trailing, _ = _runs(f.silent[lo:hi])
    if leading == hi - lo:  # no speech at all
        return lo, hi
    a, b = max(lo, lo + leading - pad), min(hi, hi - trailing + pad)
    return (a, b) if b - a >= least else (lo, hi)


def select(f: Frames) -> tuple[int, int, bool]:
    """The windows [start, end) to clone from, and whether they were chosen from a longer upload.

    At most 15 s: the upload without the silence at its ends when that fits, otherwise the
    15 s with the most speech and no clipping, trimmed to their own first and last word.
    """
    total = len(f.levels)
    least = int(MIN_SECONDS / WINDOW_SECONDS)
    lo, hi = _trimmed(f, 0, total, least)
    if hi - lo < least:  # a short upload: the ends come back in, to the minimum length
        lo = max(0, min(lo, total - least))
        hi = min(total, lo + least)
    length = int(REFERENCE_SECONDS / WINDOW_SECONDS)
    if hi - lo <= length:
        return lo, hi, False
    step = max(1, int(SELECT_STEP_SECONDS / WINDOW_SECONDS))
    starts = [*range(lo, hi - length, step), hi - length]
    warn_at = CLIP_WARN_FRACTION * length * f.window

    def key(start: int) -> tuple[bool, float, int]:
        end = start + length
        speech = length - sum(f.silent[start:end])
        clipped = sum(f.clipped[start:end])
        # A clean window with the most speech; failing a clean one, the least clipped;
        # ties go to the earliest.
        return clipped < warn_at, speech / length if clipped < warn_at else -clipped, -start

    best = max(starts, key=key)
    least = int(REFERENCE_MIN_SECONDS / WINDOW_SECONDS)
    return *_trimmed(f, best, best + length, least), True


def measure(samples: array[int], f: Frames, start: int, end: int) -> Measurements:
    """The part [start, end) of the upload, in windows."""
    part = samples[start * f.window : end * f.window]
    n = len(part)
    levels, silent = f.levels[start:end], f.silent[start:end]
    rms = math.sqrt(_energy(part) / n)
    speech = [lv for lv, quiet in zip(levels, silent, strict=True) if not quiet]
    speech_dbfs = _power_mean_db(speech) if speech else _dbfs(rms / FULL_SCALE)
    leading, trailing, pause = _runs(silent)
    per_segment = max(1, int(SEGMENT_SECONDS / WINDOW_SECONDS))
    segment_levels = []
    for at in range(0, len(levels) - per_segment + 1, per_segment):
        talking = [
            lv
            for lv, quiet in zip(
                levels[at : at + per_segment], silent[at : at + per_segment], strict=True
            )
            if not quiet
        ]
        if len(talking) * 2 >= per_segment:
            segment_levels.append(_power_mean_db(talking))
    spread = 0.0
    if len(segment_levels) >= 5:
        ordered = sorted(segment_levels)
        spread = _percentile(ordered, 0.9) - _percentile(ordered, 0.1)
    per_window = f.window / f.rate
    return Measurements(
        seconds=n / f.rate,
        peak=max(max(part), -min(part)) / FULL_SCALE,
        clipping=sum(f.clipped[start:end]) / n,
        rms_dbfs=_dbfs(rms / FULL_SCALE),
        speech_dbfs=speech_dbfs,
        noise_floor_dbfs=f.floor,
        snr_db=speech_dbfs - f.floor,
        dc_offset=f.mean / FULL_SCALE,
        speech_ratio=len(speech) / len(levels),
        leading_silence=leading * per_window,
        trailing_silence=trailing * per_window,
        longest_pause=pause * per_window,
        speech_seconds=len(speech) * per_window,
        level_spread_db=spread,
    )


def _secs(seconds: float) -> str:
    return f"{seconds:.1f}".rstrip("0").rstrip(".") + " s"


def _pct(fraction: float, digits: int = 0) -> str:
    return f"{fraction * 100:.{digits}f}".rstrip("0").rstrip(".") + " %"


def findings(m: Measurements, *, rate: int, channels: int, codec: str) -> list[QualityFinding]:
    """Plain-English findings about a part, most serious first."""
    found: list[tuple[FindingLevel, str, str]] = []
    silent = m.rms_dbfs < SILENT_DBFS
    # Under the noise, or in silence, speech cannot be told from the rest: say so, once.
    speech_known = not silent and m.snr_db >= SNR_PROBLEM_DB
    if m.clipping >= CLIP_PROBLEM_FRACTION:
        found.append(
            (
                "problem",
                "clipping",
                f"The recording clips in {_pct(m.clipping, 2)} of samples: lower the input gain "
                "or move back from the microphone, and record again.",
            )
        )
    elif m.clipping >= CLIP_WARN_FRACTION:
        found.append(
            (
                "warning",
                "clipping",
                f"A few peaks clip ({_pct(m.clipping, 2)} of samples): lower the input gain a "
                "little.",
            )
        )
    if silent:
        found.append(
            (
                "problem",
                "silent",
                "The recording is silent: check that the microphone works and is selected, and "
                "record again.",
            )
        )
    elif m.snr_db < SNR_PROBLEM_DB:
        found.append(
            (
                "problem",
                "noise",
                f"Background noise is almost as loud as the voice (estimated {m.snr_db:.0f} dB "
                "below it): record in a quieter place, closer to the microphone.",
            )
        )
    elif m.snr_db < SNR_WARN_DB:
        found.append(
            (
                "warning",
                "noise",
                f"Background noise is high (estimated {m.snr_db:.0f} dB below the voice): a "
                "quieter room or a closer microphone gives a cleaner clone.",
            )
        )
    if speech_known and m.speech_seconds < SPEECH_PROBLEM_SECONDS:
        found.append(
            (
                "problem",
                "little_speech",
                f"Only {_secs(m.speech_seconds)} of speech in the recording: it needs at least "
                "5 seconds of someone talking.",
            )
        )
    elif speech_known and m.speech_seconds < SPEECH_WARN_SECONDS:
        found.append(
            (
                "warning",
                "little_speech",
                f"Only {_secs(m.speech_seconds)} of speech: 10 to 20 seconds gives a better clone.",
            )
        )
    if speech_known and m.speech_dbfs < QUIET_DBFS:
        found.append(
            (
                "warning",
                "quiet",
                f"The recording is quiet ({m.speech_dbfs:.0f} dBFS): raise the input gain or move "
                "closer to the microphone.",
            )
        )
    if speech_known and m.level_spread_db > LEVEL_SPREAD_WARN_DB:
        found.append(
            (
                "warning",
                "uneven_level",
                f"The level varies a lot (some seconds are {m.level_spread_db:.0f} dB louder than "
                "others): keep the same distance from the microphone, and turn automatic gain "
                "control off.",
            )
        )
    if speech_known and m.leading_silence > LEADING_SILENCE_WARN_SECONDS:
        found.append(
            (
                "warning",
                "silence_start",
                f"{_secs(m.leading_silence)} of silence at the start: start speaking sooner.",
            )
        )
    if speech_known and m.trailing_silence > TRAILING_SILENCE_WARN_SECONDS:
        found.append(
            (
                "warning",
                "silence_end",
                f"{_secs(m.trailing_silence)} of silence at the end: stop recording sooner.",
            )
        )
    if speech_known and m.longest_pause > PAUSE_WARN_SECONDS:
        found.append(
            (
                "warning",
                "long_pause",
                f"A {_secs(m.longest_pause)} pause in the middle: trim it, or read without "
                "long gaps.",
            )
        )
    if speech_known and m.speech_ratio < SPEECH_RATIO_WARN:
        found.append(
            (
                "warning",
                "speech_ratio",
                f"Only {_pct(m.speech_ratio)} of the recording is speech: fewer and shorter pauses "
                "give a better clone.",
            )
        )
    if m.seconds < SHORT_WARN_SECONDS:
        found.append(
            (
                "warning",
                "short",
                f"Short ({_secs(m.seconds)}): 10 to 20 seconds of speech gives a better clone.",
            )
        )
    if abs(m.dc_offset) > DC_OFFSET_WARN:
        found.append(
            (
                "warning",
                "dc_offset",
                f"The recording has a DC offset ({_pct(abs(m.dc_offset), 1)} of full scale): try "
                "another microphone or interface, or remove it in an audio editor.",
            )
        )
    if rate < MIN_RATE_HZ:
        found.append(
            (
                "warning",
                "low_sample_rate",
                f"Sampled at {rate / 1000:g} kHz: telephone-quality audio loses the brightness of "
                "the voice. Record at 16 kHz or more.",
            )
        )
    if channels > 1:
        found.append(
            (
                "ok",
                "stereo",
                "Stereo: the two channels were mixed to one."
                if channels == 2
                else f"{channels} channels were mixed to one.",
            )
        )
    if codec in LOSSY_CODECS:
        found.append(
            (
                "ok",
                "compressed",
                f"{codec.upper()} is a compressed format: the original WAV or FLAC, if there is "
                "one, keeps more of the voice.",
            )
        )
    found.sort(key=lambda f: _RANK[f[0]])
    return [QualityFinding(level=lv, code=code, message=message) for lv, code, message in found]


def score(found: list[QualityFinding]) -> int:
    points = 100
    for finding in found:
        if finding.level == "problem":
            points -= PROBLEM_POINTS
        elif finding.level == "warning":
            points -= WARNING_POINTS
    return max(points, 0)


def _wav(pcm: bytes, rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def assess(sample: Sample) -> Reference:
    """The part of a decoded upload to clone from, with its findings and score."""
    with wave.open(io.BytesIO(sample.wav)) as w:
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    f = frames(samples, rate)
    start, end, chosen = select(f)
    m = measure(samples, f, start, end)
    found = findings(
        m, rate=sample.source_rate, channels=sample.source_channels, codec=sample.codec
    )
    upload_seconds = len(samples) / rate
    begins = start * f.window / rate
    if upload_seconds - m.seconds >= NOTE_CUT_SECONDS:
        span = f"Using {_secs(m.seconds)} from {_secs(begins)} to {_secs(begins + m.seconds)}"
        found.append(
            QualityFinding(
                level="ok",
                code="window",
                message=f"{span}, the clearest part."
                if chosen
                else f"{span}: the silence before and after it was cut.",
            )
        )
    return Reference(
        wav=_wav(samples[start * f.window : end * f.window].tobytes(), rate),
        start=begins,
        seconds=m.seconds,
        upload_seconds=upload_seconds,
        quality=SampleQuality(score=score(found), findings=found),
    )
