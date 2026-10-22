"""Tests for the live transcription server's protocol logic, with the model and the voice
activity detector replaced by fakes (no model download needed).

They check what the voice service relies on: interim results while words change, a final
with from_finalize=true right after Finalize (an empty one when nothing was said), finals
at a pause unless endpointing=false, the audio kept from before the detected start of
speech, the cap on live re-reads for whole-utterance models, and the request options; and
the model and GPU settings (STT_MODEL, STT_PROVIDER), without a GPU.

Run inside the image (stdlib unittest; the folder is mounted, not copied into the image):

  docker compose --profile local-ai run --rm --no-deps \
    -v "$PWD/infra/docker/streaming-stt:/srv/tests:ro" -w /srv --entrypoint python \
    local-stt -m unittest discover -s /srv/tests -t /srv/tests -v
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as server
import fetch_model


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

    def test_gpu_rereads_wait_while_another_call_is_decoded(self) -> None:
        eng = self.engine()
        seg = eng.start(16000)
        eng.accept(seg, np.ones(16000, np.float32))
        with mock.patch.object(server, "PROVIDER", "cuda"), \
                mock.patch.object(server.OfflineEngine, "_busy", 1):
            self.assertIsNone(eng.partial(seg), "another call's decode is running")
            self.assertEqual(seg["read_at"], 0, "tried again with the next audio")
            self.assertEqual(eng.calls, [])  # type: ignore[attr-defined]
        with mock.patch.object(server, "PROVIDER", "cpu"), \
                mock.patch.object(server.OfflineEngine, "_busy", 1):
            self.assertIsNotNone(eng.partial(seg), "the CPU keeps its behaviour")
        self.assertEqual(server.OfflineEngine._busy, 0)
        eng.final(seg, None)
        self.assertEqual(server.OfflineEngine._busy, 0, "counted down after every decode")

    def test_segments_are_padded_to_the_bucket_on_a_gpu(self) -> None:
        eng = self.engine()
        rate = 16000
        loud, quiet = tone(1.0, rate, 0.5), np.full(int(0.25 * rate), 0.001, np.float32)
        with mock.patch.object(server, "PAD_S", 0.5):
            seg = eng.start(rate)
            eng.accept(seg, np.concatenate([loud, quiet]))  # 1.25 s
            eng.final(seg, None)
            self.assertEqual(eng.calls[-1], int(1.5 * rate))  # type: ignore[attr-defined]
            padded = server.pad_to_bucket(np.concatenate([loud, quiet]), rate)
            self.assertEqual(padded.size, int(1.5 * rate))
            self.assertTrue(np.all(np.abs(padded[int(1.25 * rate):]) <= 0.001), "background, not speech")
            same = np.ones(rate, np.float32)
            self.assertIs(server.pad_to_bucket(same, rate), same, "already a whole bucket")
            short = np.full(100, 0.2, np.float32)
            self.assertEqual(server.pad_to_bucket(short, rate).size, int(0.5 * rate))
            self.assertEqual(server.pad_to_bucket(np.zeros(0, np.float32), rate).size, 0)
        with mock.patch.object(server, "PAD_S", 0.0):
            x = np.ones(12345, np.float32)
            self.assertIs(server.pad_to_bucket(x, rate), x, "no padding on the CPU")

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


class ModelSettingsTest(unittest.TestCase):
    """STT_MODEL and STT_PROVIDER: which files are fetched and where the model runs."""

    def spec(self, **env: str) -> fetch_model.ModelSpec:
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("STT_MODEL", "STT_PROVIDER"):
                if name not in env:
                    os.environ.pop(name, None)
            return fetch_model.spec_from_env()

    def test_every_model_is_pinned(self) -> None:
        for key, spec in fetch_model.MODELS.items():
            with self.subTest(model=key):
                self.assertEqual(spec.key, key)
                self.assertTrue({"encoder", "decoder", "joiner", "tokens"} <= set(spec.files))
                for name, sha in spec.files.values():
                    self.assertRegex(sha, r"^[0-9a-f]{64}$", name)
                if spec.archive:
                    self.assertRegex(spec.archive_sha256, r"^[0-9a-f]{64}$")
                else:
                    self.assertRegex(spec.revision, r"^[0-9a-f]{40}$", "a commit, not a branch")

    def test_parakeet_v3(self) -> None:
        spec = self.spec(STT_MODEL="parakeet-0.6b-v3")
        self.assertEqual((spec.key, spec.model_id), ("parakeet-0.6b-v3", "parakeet-en-0.6b-v3"))
        self.assertEqual((spec.kind, spec.model_type, spec.licence), ("offline", "nemo_transducer", "CC BY 4.0"))
        self.assertTrue(spec.cased)
        self.assertTrue(spec.files["encoder"][0].endswith(".int8.onnx"), "int8 on the CPU")
        self.assertEqual(spec.version, "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8@2bda32ec")

    def test_provider_default_and_values(self) -> None:
        for value, expected in (("", "cpu"), ("cpu", "cpu"), ("CUDA", "cuda"), (" cuda ", "cuda")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"STT_PROVIDER": value}):
                self.assertEqual(fetch_model.provider_from_env(), expected)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STT_PROVIDER", None)
            self.assertEqual(fetch_model.provider_from_env(), "cpu")
        for bad in ("gpu", "tensorrt", "rocm"):
            with self.subTest(value=bad), mock.patch.dict(os.environ, {"STT_PROVIDER": bad}):
                with self.assertRaises(SystemExit) as ctx:
                    fetch_model.provider_from_env()
                self.assertIn("cpu, cuda", str(ctx.exception))

    def test_cuda_uses_the_full_precision_export(self) -> None:
        spec = self.spec(STT_MODEL="parakeet-0.6b-v3", STT_PROVIDER="cuda")
        self.assertEqual(spec.key, "parakeet-0.6b-v3-fp32")
        self.assertEqual(spec.model_id, "parakeet-en-0.6b-v3", "the same name in results and /health")
        self.assertEqual(spec.files["encoder"][0], "encoder.onnx")
        self.assertEqual(spec.files["encoder_weights"][0], "encoder.weights", "beside the encoder")
        self.assertEqual(
            spec.files["tokens"], fetch_model.MODELS["parakeet-0.6b-v3"].files["tokens"]
        )
        # Models without a GPU export keep their files on either provider.
        self.assertEqual(self.spec(STT_MODEL="parakeet-110m", STT_PROVIDER="cuda").key, "parakeet-110m")
        self.assertEqual(self.spec().key, fetch_model.DEFAULT_MODEL)
        with self.assertRaises(SystemExit):
            self.spec(STT_MODEL="parakeet-0.6b-v4", STT_PROVIDER="cuda")

    def test_data_files_are_fetched_beside_the_model(self) -> None:
        spec = fetch_model.MODELS["parakeet-0.6b-v3-fp32"]
        fetched: list[str] = []

        def fake_fetch(url: str, target: Path, expected: str) -> None:
            fetched.append(url)
            target.write_bytes(b"x")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(fetch_model, "_fetch", fake_fetch):
            paths = fetch_model.ensure_model(Path(tmp), spec)
            self.assertEqual(paths["encoder_weights"].parent, paths["encoder"].parent)
            self.assertEqual(paths["encoder_weights"].name, "encoder.weights")
        self.assertIn(f"{spec.base_url}/encoder.weights", fetched)
        self.assertTrue(all(re.search(r"/resolve/[0-9a-f]{40}/", u) for u in fetched if "huggingface" in u))

    def test_recognizers_get_the_provider(self) -> None:
        calls: list[dict[str, Any]] = []
        fake = SimpleNamespace(
            OfflineRecognizer=SimpleNamespace(from_transducer=lambda **kw: calls.append(kw)),
            OnlineRecognizer=SimpleNamespace(from_transducer=lambda **kw: calls.append(kw)),
        )
        paths = {role: Path(f"/models/x/{role}") for role in ("encoder", "decoder", "joiner", "tokens")}
        for provider in ("cpu", "cuda"):
            with self.subTest(provider=provider), mock.patch.object(server, "sherpa_onnx", fake), \
                    mock.patch.object(server, "PROVIDER", provider), \
                    mock.patch.object(server, "_paths", paths):
                calls.clear()
                server.OfflineEngine()
                server.OnlineEngine()
                self.assertEqual([c["provider"] for c in calls], [provider, provider])

    def test_cuda_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "sherpa_onnx"
            (package / "lib").mkdir(parents=True)
            fake = SimpleNamespace(__file__=str(package / "__init__.py"))
            gpus: list[str] = []
            with mock.patch.object(server, "sherpa_onnx", fake), \
                    mock.patch.object(server.glob, "glob", lambda pattern: list(gpus)):
                self.assertIn("Dockerfile.gpu", server.cuda_problem() or "", "CPU build")
                (package / "lib" / "libonnxruntime_providers_cuda.so").write_bytes(b"")
                self.assertIn("no NVIDIA GPU", server.cuda_problem() or "", "no GPU passed in")
                gpus.append("/dev/nvidia0")
                self.assertIsNone(server.cuda_problem())

    def test_health_reports_the_provider(self) -> None:
        with mock.patch.object(server, "PROVIDER", "cuda"):
            health = server._health()
        self.assertEqual(health["provider"], "cuda")
        self.assertEqual(health["model"], server.MODEL_ID)


if __name__ == "__main__":
    unittest.main()
