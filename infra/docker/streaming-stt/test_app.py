"""Tests for the live transcription server's protocol logic, with the model and the voice
activity detector replaced by fakes (no model download needed).

They check what the voice service relies on: interim results while words change, a final
with from_finalize=true right after Finalize (an empty one when nothing was said), finals
at a pause unless endpointing=false, the audio kept from before the detected start of
speech, the cap on live re-reads for whole-utterance models, and the request options.

Run inside the image (stdlib unittest; the folder is mounted, not copied into the image):

  docker compose --profile local-ai run --rm --no-deps \
    -v "$PWD/infra/docker/streaming-stt:/srv/tests:ro" -w /srv --entrypoint python \
    local-stt -m unittest discover -s /srv/tests -t /srv/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as server


class FakeVad:
    """Speech wherever the 32 ms window is louder than a threshold."""

    def __init__(self, narrowband: bool = False) -> None:
        self.window = server.VAD_WINDOW
        self.pending = np.zeros(0, np.float32)

    def speech(self, x: np.ndarray) -> list[bool]:
        self.pending = np.concatenate([self.pending, x])
        out = []
        while self.pending.size >= self.window:
            w, self.pending = self.pending[: self.window], self.pending[self.window :]
            out.append(bool(np.sqrt(np.mean(w**2)) > 0.01))
        return out


class FakeOnline:
    """A streaming model that 'recognises' one word per 0.2 s of loud audio."""

    streaming = True

    def start(self, rate: int) -> dict[str, Any]:
        return {"rate": rate, "audio": [], "text": "", "finished": False}

    def accept(self, seg: dict[str, Any], samples: np.ndarray) -> None:
        assert not seg["finished"], "audio after the final"
        seg["audio"].append(samples)

    def _hyp(self, seg: dict[str, Any]) -> server.Hypothesis:
        audio = np.concatenate(seg["audio"]) if seg["audio"] else np.zeros(0)
        loud = int(np.sum(np.abs(audio) > 0.05) / (0.2 * seg["rate"]))
        words = " ".join(["word"] * loud)
        return server.Hypothesis(words, [], [])

    def partial(self, seg: dict[str, Any]) -> server.Hypothesis | None:
        hyp = self._hyp(seg)
        if hyp.text == seg["text"]:
            return None
        seg["text"] = hyp.text
        return hyp

    def prepare(self, seg: dict[str, Any], mark: float) -> None:
        seg["prepared"] = mark

    def final(
        self, seg: dict[str, Any], keep_s: float | None, mark: float | None = None
    ) -> server.Hypothesis:
        seg["finished"] = True
        return self._hyp(seg)


def tone(seconds: float, rate: int, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float, rate: int) -> np.ndarray:
    return np.zeros(int(seconds * rate), np.float32)


def pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


class SessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._vad, self._engine = server.Vad, server._engine
        server.Vad = FakeVad  # type: ignore[misc,assignment]
        server._engine = FakeOnline()  # type: ignore[assignment]

    def tearDown(self) -> None:
        server.Vad = self._vad  # type: ignore[misc]
        server._engine = self._engine

    def session(self, query: str = "interim_results=true", rate: int = 16000) -> server.Session:
        return server.Session(server.Options.from_path(f"/v1/listen?sample_rate={rate}&{query}"))

    def feed(self, s: server.Session, x: np.ndarray) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        step = int(s.rate * 0.02)  # 20 ms frames, like the voice service
        for i in range(0, x.size, step):
            events.extend(s.feed(pcm(x[i : i + step])))
        return events

    def test_interims_then_final_on_finalize(self) -> None:
        for rate in (16000, 8000):
            s = self.session(rate=rate)
            audio = np.concatenate([silence(0.5, rate), tone(1.0, rate), silence(0.2, rate)])
            events = self.feed(s, audio)
            interims = [e for e in events if e["type"] == "Results" and not e["is_final"]]
            self.assertTrue(interims, "live words while speaking")
            self.assertFalse([e for e in events if e["type"] == "Results" and e["is_final"]])
            (final,) = s.finalize(from_finalize=True)
            self.assertTrue(final["is_final"] and final["speech_final"] and final["from_finalize"])
            alt = final["channel"]["alternatives"][0]
            self.assertTrue(alt["transcript"].startswith("word"))
            self.assertEqual(len(alt["words"]), len(alt["transcript"].split()))
            # The segment starts before the detected speech (pre-roll), never before 0.
            self.assertLess(final["start"], 0.5)
            self.assertGreaterEqual(final["start"], 0.5 - server.PREROLL_S - 0.11)

    def test_finalize_with_nothing_said_is_an_empty_final(self) -> None:
        s = self.session()
        self.feed(s, silence(1.0, 16000))
        (final,) = s.finalize(from_finalize=True)
        self.assertTrue(final["is_final"] and final["from_finalize"])
        self.assertEqual(final["channel"]["alternatives"][0]["transcript"], "")
        self.assertEqual(s.finalize(from_finalize=False), [], "CloseStream adds no empty final")

    def test_pause_ends_the_segment(self) -> None:
        s = self.session()
        pause = silence(server.ENDPOINT_SILENCE_S + 0.2, 16000)
        events = self.feed(s, np.concatenate([tone(0.8, 16000), pause]))
        finals = [e for e in events if e["type"] == "Results" and e["is_final"]]
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0]["speech_final"] and not finals[0]["from_finalize"])
        (late,) = s.finalize(from_finalize=True)
        self.assertEqual(late["channel"]["alternatives"][0]["transcript"], "", "already sent")
        # The next words start a new segment.
        events = self.feed(s, np.concatenate([tone(0.6, 16000), silence(0.2, 16000)]))
        (final,) = s.finalize(from_finalize=True)
        self.assertTrue(final["channel"]["alternatives"][0]["transcript"])
        self.assertGreater(final["start"], 1.0)

    def test_endpointing_false_waits_for_finalize(self) -> None:
        s = self.session("interim_results=true&endpointing=false")
        events = self.feed(s, np.concatenate([tone(0.8, 16000), silence(1.0, 16000)]))
        self.assertFalse([e for e in events if e["type"] == "Results" and e["is_final"]])
        (final,) = s.finalize(from_finalize=True)
        self.assertTrue(final["channel"]["alternatives"][0]["transcript"])

    def test_vad_events_and_utterance_end(self) -> None:
        s = self.session("interim_results=true&vad_events=true&utterance_end_ms=1000")
        audio = np.concatenate([silence(0.3, 16000), tone(0.6, 16000), silence(1.6, 16000)])
        events = self.feed(s, audio)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds.count("SpeechStarted"), 1)
        self.assertIn("UtteranceEnd", kinds)
        self.assertLess(kinds.index("SpeechStarted"), kinds.index("UtteranceEnd"))

    def test_long_speech_is_cut(self) -> None:
        s = self.session("interim_results=false")
        events = self.feed(s, tone(server.MAX_UTTERANCE_S + 1.0, 16000))
        finals = [e for e in events if e["type"] == "Results" and e["is_final"]]
        self.assertEqual(len(finals), 1)
        self.assertFalse(finals[0]["speech_final"])
        self.assertIsNotNone(s.seg, "speech goes on in a new segment")

    def test_unsure_text_is_dropped(self) -> None:
        unsure = server.Hypothesis("words the caller never said", [], [], confidence=0.3)
        sure = server.Hypothesis("Hello.", [], [], confidence=0.9)
        old = server.MIN_CONFIDENCE
        server.MIN_CONFIDENCE = 0.6
        try:
            self.assertEqual(server._trusted(unsure).text, "")
            self.assertEqual(server._trusted(sure).text, "Hello.")
        finally:
            server.MIN_CONFIDENCE = old
        self.assertAlmostEqual(server._confidence([np.log(0.5), np.log(0.5)]), 0.5)
        self.assertEqual(server._confidence([]), 1.0)

    def test_metadata(self) -> None:
        s = self.session()
        self.feed(s, tone(0.3, 16000))
        meta = s.metadata()
        self.assertEqual(meta["type"], "Metadata")
        self.assertAlmostEqual(meta["duration"], 0.3, places=2)


class OfflineEngineTest(unittest.TestCase):
    """The whole-utterance engine's live re-reads: spaced, and dropped when too slow."""

    def engine(self) -> server.OfflineEngine:
        calls: list[int] = []

        class Stream:
            def accept_waveform(self, rate: int, x: np.ndarray) -> None:
                self.n = x.size
                self.result = SimpleNamespace(text=f"n{x.size}", tokens=[], timestamps=[])

        class Rec:
            def create_stream(self) -> Stream:
                return Stream()

            def decode_stream(self, stream: Stream) -> None:
                calls.append(stream.n)

        eng = object.__new__(server.OfflineEngine)
        eng.rec = Rec()  # type: ignore[attr-defined]
        eng.calls = calls  # type: ignore[attr-defined]
        return eng

    def test_rereads_every_interval_and_final_trims(self) -> None:
        eng = self.engine()
        seg = eng.start(16000)
        hyps = []
        for _ in range(20):  # 2 s in 0.1 s pieces
            eng.accept(seg, np.ones(1600, np.float32))
            hyps.append(eng.partial(seg))
        reads = [h for h in hyps if h is not None]
        self.assertEqual(len(reads), int(2.0 / server.INTERIM_S))
        final = eng.final(seg, 1.0)
        self.assertEqual(final.text, "n16000", "trailing audio past keep_s is not transcribed")

    def test_slow_rereads_are_spaced_out(self) -> None:
        eng = self.engine()
        seg = eng.start(16000)
        eng.accept(seg, np.ones(16000, np.float32))
        self.assertIsNotNone(eng.partial(seg))
        seg["cost"] = 0.4  # the last read took 0.4 s: the next one waits for 1 s of audio
        eng.accept(seg, np.ones(8000, np.float32))
        self.assertIsNone(eng.partial(seg))
        eng.accept(seg, np.ones(8000, np.float32))
        self.assertEqual(eng.partial(seg).text, "n32000")  # type: ignore[union-attr]
        self.assertEqual(eng.final(seg, None).text, "n32000")

    def test_pause_is_transcribed_before_finalize(self) -> None:
        eng = self.engine()
        seg = eng.start(16000)
        eng.accept(seg, np.ones(16000, np.float32))
        eng.prepare(seg, 1.0)
        reads = len(eng.calls)  # type: ignore[attr-defined]
        eng.accept(seg, np.zeros(1600, np.float32))  # more silence, no new speech
        self.assertEqual(eng.final(seg, 1.3, 1.0).text, "n16000")
        self.assertEqual(len(eng.calls), reads, "no second read")  # type: ignore[attr-defined]
        eng.prepare(seg, 1.0)
        self.assertEqual(len(eng.calls), reads, "one read per pause")  # type: ignore[attr-defined]
        # New speech after the pause (another mark): the segment is read again.
        self.assertEqual(eng.final(seg, None, 2.0).text, "n17600")


class OptionsTest(unittest.TestCase):
    def test_options(self) -> None:
        o = server.Options.from_path(
            "/v1/listen?encoding=linear16&sample_rate=8000&interim_results=true&endpointing=100"
            "&utterance_end_ms=500&model=x&keyterm=a"
        )
        self.assertEqual((o.sample_rate, o.interim_results, o.endpointing), (8000, True, True))
        self.assertEqual(o.utterance_end_s, 1.0, "Deepgram's minimum")
        for bad in ("encoding=mulaw", "channels=2", "sample_rate=96000", "sample_rate=x"):
            with self.assertRaises(server.BadRequestError):
                server.Options.from_path(f"/v1/listen?{bad}")

    def test_resampler(self) -> None:
        for rate in (8000, 11025, 24000, 44100, 48000):
            r = server.Resampler(rate, 16000)
            x = tone(1.0, rate, 0.5)  # 220 Hz
            frame = int(rate * 0.02)
            y = np.concatenate([r.process(x[i : i + frame]) for i in range(0, x.size, frame)])
            self.assertLessEqual(abs(y.size - 16000), 1, rate)
            body = y[800:15000]  # past the filter's start-up
            self.assertAlmostEqual(float(np.sqrt(np.mean(body**2))), 0.5 / np.sqrt(2), delta=0.01)
            spectrum = np.abs(np.fft.rfft(body))
            self.assertAlmostEqual(np.argmax(spectrum) * 16000 / body.size, 220, delta=3)


if __name__ == "__main__":
    unittest.main()
