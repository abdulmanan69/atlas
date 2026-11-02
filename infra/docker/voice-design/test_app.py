"""Tests for the voice design server (Qwen3-TTS), with the models replaced by a fake: no GPU,
no download, no torch.

They check designing a voice (the request, what the design model is asked, what is kept on
disk, the preview, speaking with it, reloading it after a restart, deleting it), cloning from
a recording (with and without a transcript, the 3-30 s rule, consent), the capabilities,
`instructions` (ignored, or honoured in design mode), text cleaning and sentence splitting,
streaming order, the phrase cache (hit, opener, a new voice's openers in the background), the
built-in voice made at the first start, the API key, and that logs never hold a text, a
description or a transcript. The streaming test runs the app on 127.0.0.1 (a free port)
without its lifespan; every other test calls it in-process.

Run in a small venv (fastapi, numpy, scipy, safetensors, soxr, httpx, python-multipart,
uvicorn; no torch) from the repository root:

  python -m unittest discover -s infra/docker/voice-design -t infra/docker/voice-design -v

or inside the image (the folder is mounted, not copied into it):

  docker run --rm --entrypoint python -v "$PWD/infra/docker/voice-design:/srv/tests:ro" \
    -w /srv atlas/voice-design:qwen3-tts-0.1.1 \
    -m unittest discover -s /srv/tests -t /srv/tests -v
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import scipy.io.wavfile

os.environ.setdefault("VOICE_DESIGN_WARMUP", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MODEL = "qwen3-tts"
DESCRIPTION = "Warm, upbeat American man in his forties, clear and friendly."


class FakePrompt(SimpleNamespace):
    """Shaped like qwen_tts's VoiceClonePromptItem, with numpy arrays."""


class FakeEngine:
    """Stands in for QwenEngine: records what it is asked and returns short audio whose level
    tells the calls apart (0.05 + 0.01 x call number)."""

    sample_rate = 24000
    device = "cpu"
    dtype = "float32"
    languages = ["english", "chinese", "japanese"]

    def __init__(self, seconds: float = 0.2, lead_seconds: float = 0.0, can_design: bool = True) -> None:
        self.seconds = seconds
        self.lead_seconds = lead_seconds
        self.can_design = can_design
        self.texts: list[str] = []
        self.spoken: list[tuple[str, object, str]] = []  # (text, prompt, language)
        self.designs: list[tuple[str, str, str, int | None]] = []  # (text, instruct, language, seed)
        self.samples: list[tuple[np.ndarray, str | None]] = []  # clone_prompt(audio, transcript)
        self.calls = 0
        self.fail_with: BaseException | None = None
        self.gate: tuple[int, threading.Event] | None = None
        self.started = threading.Event()

    def level(self, call: int) -> float:
        return 0.05 + 0.01 * (call % 90)

    def _audio(self) -> np.ndarray:
        self.calls += 1
        call = self.calls
        if self.fail_with is not None:
            raise self.fail_with
        if self.gate is not None and self.gate[0] == call:
            self.started.set()
            self.gate[1].wait(10)
        lead = np.zeros(int(self.sample_rate * self.lead_seconds), np.float32)
        body = np.full(int(self.sample_rate * self.seconds), self.level(call), np.float32)
        return np.concatenate([lead, body])

    def synthesize(self, text: str, prompt: object, language: str) -> np.ndarray:
        self.texts.append(text)
        self.spoken.append((text, prompt, language))
        return self._audio()

    def design(self, text: str, instruct: str, language: str, seed: int | None) -> np.ndarray:
        self.texts.append(text)
        self.designs.append((text, instruct, language, seed))
        return self._audio()

    def clone_prompt(self, audio: np.ndarray, transcript: str | None) -> FakePrompt:
        self.samples.append((audio, transcript))
        frames = max(1, audio.shape[0] // 1920)
        return FakePrompt(
            ref_code=None if transcript is None else np.arange(frames * 16, dtype=np.int64).reshape(frames, 16),
            ref_spk_embedding=np.full((8,), float(np.max(np.abs(audio))), np.float32),
            x_vector_only_mode=transcript is None,
            icl_mode=transcript is not None,
            ref_text=transcript,
        )

    def prompt_to_arrays(self, prompt: FakePrompt) -> tuple[dict[str, np.ndarray], dict[str, str]]:
        arrays = {"ref_spk_embedding": prompt.ref_spk_embedding}
        if prompt.ref_code is not None:
            arrays["ref_code"] = prompt.ref_code
        return arrays, {"mode": "xvector" if prompt.x_vector_only_mode else "icl", "ref_text": prompt.ref_text or ""}

    def make_prompt(self, arrays: dict[str, np.ndarray], meta: dict[str, str]) -> FakePrompt:
        xvector = meta.get("mode") == "xvector"
        return FakePrompt(
            ref_code=None if xvector else np.array(arrays["ref_code"]),
            ref_spk_embedding=np.array(arrays["ref_spk_embedding"]),
            x_vector_only_mode=xvector, icl_mode=not xvector, ref_text=meta.get("ref_text") or None,
        )


def levels(pcm: bytes) -> list[int]:
    """The distinct sample values of `pcm`, in order (one per fake synthesis)."""
    out: list[int] = []
    for value in np.frombuffer(pcm, dtype="<i2").tolist():
        if value and (not out or out[-1] != value):
            out.append(value)
    return out


def pcm_level(engine: FakeEngine, call: int) -> int:
    return int(engine.level(call) * 32767)


def wav_bytes(seconds: float, rate: int = 44100, channels: int = 2, dtype=np.int16) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    tone = 0.3 * np.sin(2 * np.pi * 220 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
    if dtype == np.int16:
        data = (tone * 32767).astype(np.int16)
    elif dtype == np.int32:
        data = (tone * (2**31 - 1)).astype(np.int32)
    else:
        data = tone.astype(np.float32)
    if channels > 1:
        data = np.stack([data] * channels, axis=1)
    buf = io.BytesIO()
    scipy.io.wavfile.write(buf, rate, data)
    return buf.getvalue()


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


SAVED = (
    "_engine", "_phrase_cache", "API_KEY", "VOICE_DIR", "MAX_VOICES", "CLONING", "VOICE_CACHE",
    "FIRST_SENTENCE_CHARS", "MAX_SENTENCE_CHARS", "WARMUP", "INSTRUCT_MODE", "LANGUAGE",
    "DEFAULT_VOICE_DESCRIPTION", "DEFAULT_VOICE_SEED", "SAMPLE_TEXT", "load_engine",
    "PHRASE_CACHE_ANY_VOICE", "CACHED_PHRASES", "DESIGN_REPO",
)


class ServerTest(unittest.TestCase):
    engine_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: getattr(server, name) for name in SAVED}
        self.saved_gpu = server._gpu
        self.engine = FakeEngine(**self.engine_kwargs)
        server._engine = self.engine
        server._gpu = server.GpuTurns()
        server._phrase_cache = server.PhraseCache(1024 * 1024, 32)
        server.API_KEY = None
        server.VOICE_DIR = Path(self.tmp.name) / "voices"
        server.MAX_VOICES = 50
        server.CLONING = True
        server.INSTRUCT_MODE = "ignore"
        server.LANGUAGE = "English"
        server.SAMPLE_TEXT = "Hi, thanks for calling today."
        server.PHRASE_CACHE_ANY_VOICE = False
        self.saved_made = set(server._phrase_voices_made)
        server._phrase_voices_made.clear()
        server._voices.clear()
        server._prompts.clear()
        # The built-in voice, as the first start makes it; the counts start after it.
        server.design_voice(
            "default", server.DEFAULT_VOICE_DESCRIPTION, "English", server.SAMPLE_TEXT,
            server.DEFAULT_VOICE_SEED, voice_id="default",
        )
        self.engine.calls = 0
        self.engine.texts.clear()
        self.engine.designs.clear()
        self.engine.spoken.clear()
        self.engine.samples.clear()
        self.logs = LogCapture()
        root = logging.getLogger()
        self.saved_level = root.level
        root.addHandler(self.logs)
        root.setLevel(logging.DEBUG)
        self.client = TestClient(server.app)  # no `with`: the real models are never loaded

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(server, name, value)
        server._gpu = self.saved_gpu
        for thread in server._phrase_threads:
            thread.join(timeout=10)
        server._phrase_voices_made.clear()
        server._phrase_voices_made.update(self.saved_made)
        server._voices.clear()
        server._prompts.clear()
        root = logging.getLogger()
        root.removeHandler(self.logs)
        root.setLevel(self.saved_level)
        self.tmp.cleanup()

    def speak(self, text: str, voice: str = "default", fmt: str = "pcm", **extra):
        return self.client.post(
            "/v1/audio/speech",
            json={"model": MODEL, "input": text, "voice": voice, "response_format": fmt, **extra},
        )

    def design(self, **fields):
        body = {"name": "Sam Designed", "description": DESCRIPTION, **fields}
        return self.client.post("/v1/voices/design", json=body)

    def create(self, audio: bytes, **fields: str):
        data = {"name": "Sam Example", "consent": "true", **fields}
        return self.client.post(
            "/v1/voices", data=data, files={"audio": ("sample.wav", audio, "audio/wav")}
        )

    def files(self) -> list[str]:
        return sorted(p.name for p in server.VOICE_DIR.iterdir() if p.name != "default.json"
                      and not p.name.startswith("default."))


# ---------------------------------------------------------------- text


class TextTest(ServerTest):
    def test_cues_markup_and_emoji_are_removed_never_read_out(self) -> None:
        text, removed = server.clean_text(
            'Sure [laugh], I can [smiles warmly] help.<break time="1s"/> Bye [sigh]. '
            "</laugh>[] Okay \U0001F600 then ✅"
        )
        self.assertEqual(text, "Sure, I can help. Bye. Okay then")
        self.assertEqual(removed, 6)
        self.assertEqual(server.clean_text("a [b c] d] e [f")[0], "a d e f")
        self.assertEqual(server.clean_text("if a < b and c > d")[0], "if a < b and c > d")
        r = self.speak("Sure [pause] thing [Laughs].")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.engine.texts, ["Sure thing."])
        for text in ("[pause]", "<break/> ...", "\U0001F600"):
            r = self.speak(text)
            self.assertEqual((r.status_code, r.content), (200, b""), text)
        self.assertEqual(len(self.speak("[pause]", fmt="wav").content), 44)
        self.assertEqual(self.engine.calls, 1)

    def test_sentences(self) -> None:
        server.FIRST_SENTENCE_CHARS = 0
        self.assertEqual(
            server.split_sentences(
                "I spoke with Dr. Lee. She is free at 5 p.m. Tomorrow? J. K. Rowling wrote it. "
                "It costs 3.50 dollars... okay, fine. \"Great!\" She said."
            ),
            [
                "I spoke with Dr. Lee.", "She is free at 5 p.m. Tomorrow?",
                "J. K. Rowling wrote it.", "It costs 3.50 dollars... okay, fine.",
                "\"Great!\"", "She said.",
            ],
        )
        server.FIRST_SENTENCE_CHARS = 60
        line = "Hmm, let me think about that for a second... okay, yes, three months works perfectly."
        self.assertEqual(
            server.split_sentences(line),
            ["Hmm, let me think about that for a second...", "okay, yes, three months works perfectly."],
        )
        server.MAX_SENTENCE_CHARS = 40
        words = " ".join(f"word{i}" for i in range(30))
        parts = server.split_sentences(words)
        self.assertTrue(all(len(p) <= 40 for p in parts), parts)
        self.assertEqual(" ".join(parts), words)

    def test_auto_dtype_is_bfloat16_on_ampere_float32_on_a_t4_and_float16_warns(self) -> None:
        """With a stub torch: no real torch in these tests."""
        capability = [7, 5]
        stub = SimpleNamespace(
            float16="f16", bfloat16="bf16", float32="f32",
            cuda=SimpleNamespace(get_device_capability=lambda _i: tuple(capability)),
        )
        with mock.patch.dict(sys.modules, {"torch": stub}):
            self.assertEqual(server._torch_dtype("auto", "cuda"), "f32")  # a T4
            capability[:] = [8, 6]
            self.assertEqual(server._torch_dtype("auto", "cuda"), "bf16")  # an A10G
            self.assertEqual(server._torch_dtype("auto", "cpu"), "f32")
            self.assertEqual(server._torch_dtype("float16", "cuda"), "f16")
            with self.assertRaises(RuntimeError):
                server._torch_dtype("int8", "cuda")
        self.assertTrue(any("float16: Qwen3-TTS overflows" in line for line in self.logs.lines))

    def test_token_cap_grows_with_the_text(self) -> None:
        self.assertEqual(server.token_cap("Okay."), 75)
        self.assertEqual(server.token_cap("x" * 300), 960)
        self.assertEqual(server.token_cap("x" * 5000), 8192)


# ---------------------------------------------------------------- what the server says it is


class CapabilitiesTest(ServerTest):
    def test_capabilities(self) -> None:
        self.assertEqual(self.client.get("/v1/capabilities").json(), {
            "model": "qwen3-tts", "cloning": True, "design": True, "instruct": False,
            "sound_tags": [], "languages": ["English", "Chinese", "Japanese"],
            "streaming": "sentence", "reference_seconds": {"min": 3, "max": 30},
        })
        server.CLONING = False
        server.INSTRUCT_MODE = "design"
        caps = self.client.get("/v1/capabilities").json()
        self.assertEqual((caps["cloning"], caps["design"], caps["instruct"]), (False, True, True))
        self.engine.can_design = False
        caps = self.client.get("/v1/capabilities").json()
        self.assertEqual((caps["design"], caps["instruct"]), (False, False))

    def test_models_voices_and_sound_tags(self) -> None:
        model = self.client.get("/v1/models").json()["data"][0]
        self.assertEqual((model["id"], model["sample_rate"], model["voices"]), (MODEL, 24000, ["default"]))
        self.assertEqual(model["variant"], "0.6B-Base + 1.7B-VoiceDesign")
        self.assertEqual(model["repos"]["speech"], "Qwen/Qwen3-TTS-12Hz-0.6B-Base")
        self.assertEqual(model["sound_tags"], [])
        self.assertEqual(self.client.get("/v1/audio/sound-tags").json(), {"tags": []})
        voices = self.client.get("/v1/audio/voices").json()["data"]
        self.assertEqual(voices, [{"id": "default", "license": "Apache-2.0", "default": True}])
        listed = self.client.get("/v1/voices").json()["data"]
        self.assertEqual(listed, [{
            "id": "default", "name": "default", "type": "builtin", "license": "Apache-2.0", "default": True,
        }])
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "device": "cpu"})


# ---------------------------------------------------------------- designing a voice


class DesignTest(ServerTest):
    def test_design_speak_preview_reload_and_delete(self) -> None:
        r = self.design(language="english", seed=7, sample_text="Good morning, how are you?")
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        voice_id = body["id"]
        self.assertRegex(voice_id, r"^design-sam-designed-[0-9a-f]{16}$")
        self.assertEqual(body["name"], "Sam Designed")
        self.assertEqual(body["type"], "designed")
        self.assertEqual(body["description"], DESCRIPTION)
        self.assertEqual((body["language"], body["seed"], body["sample_seconds"]), ("English", 7, 0.2))
        self.assertRegex(body["created_at"], r"^\d{4}-\d\d-\d\dT")
        # The design model rendered the sample text with the description, seeded; the speech
        # model's clone prompt was made from that sample and its known transcript.
        self.assertEqual(self.engine.designs, [("Good morning, how are you?", DESCRIPTION, "English", 7)])
        sample, transcript = self.engine.samples[-1]
        self.assertEqual((sample.shape, transcript), ((4800,), "Good morning, how are you?"))

        # Kept: the prompt, the record and the preview (the designed sample itself).
        self.assertEqual(self.files(), [f"{voice_id}.json", f"{voice_id}.safetensors", f"{voice_id}.wav"])
        meta = json.loads((server.VOICE_DIR / f"{voice_id}.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["type"], meta["description"], meta["seed"]), ("designed", DESCRIPTION, 7))
        self.assertEqual(meta["model"], "qwen3-tts-12hz")
        preview = self.client.get(f"/v1/voices/{voice_id}/sample")
        self.assertEqual((preview.status_code, preview.headers["content-type"]), (200, "audio/wav"))
        self.assertEqual(preview.content[:4], b"RIFF")
        self.assertEqual(int.from_bytes(preview.content[24:28], "little"), 24000)
        self.assertEqual(levels(preview.content[44:]), [pcm_level(self.engine, 1)])
        self.assertEqual(len(preview.content), 44 + 9600)

        listed = self.client.get("/v1/voices").json()["data"]
        self.assertEqual([v["id"] for v in listed], ["default", voice_id])
        self.assertEqual(listed[1]["description"], DESCRIPTION)

        speech = self.speak("Hi there.", voice=voice_id)
        self.assertEqual(speech.status_code, 200)
        text, prompt, language = self.engine.spoken[-1]
        self.assertEqual((text, language, prompt.ref_text, prompt.icl_mode), ("Hi there.", "English", "Good morning, how are you?", True))

        # A restart reads the index from disk and loads the saved prompt.
        server._voices.clear()
        server._prompts.clear()
        server.load_voice_index()
        self.assertEqual(set(server._voices), {"default", voice_id})
        loaded = server._prompt_for(voice_id)
        np.testing.assert_array_equal(loaded.ref_code, prompt.ref_code)
        np.testing.assert_array_equal(loaded.ref_spk_embedding, prompt.ref_spk_embedding)
        self.assertEqual((loaded.ref_text, loaded.icl_mode), (prompt.ref_text, True))

        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 204)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/v1/voices/{voice_id}/sample").status_code, 404)
        self.assertEqual(self.speak("Hi.", voice=voice_id).status_code, 422)

    def test_defaults_language_and_sample_text(self) -> None:
        r = self.design()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.engine.designs, [("Hi, thanks for calling today.", DESCRIPTION, "English", None)])
        self.assertEqual(r.json()["language"], "English")
        self.assertIsNone(r.json()["seed"])
        server.LANGUAGE = "Auto"
        r = self.design(name="Other", sample_text="  Hello  [laugh] there. ")
        self.assertEqual(self.engine.designs[-1][:3], ("Hello there.", DESCRIPTION, "Auto"))
        r = self.design(name="Third", language="JAPANESE")
        self.assertEqual(r.json()["language"], "Japanese")
        self.assertEqual(self.speak("Hi.", voice=r.json()["id"]).status_code, 200)
        self.assertEqual(self.engine.spoken[-1][2], "Japanese")

    def test_request_checks(self) -> None:
        self.assertEqual(self.design(name="  ").status_code, 422)
        self.assertEqual(self.design(name="x" * 61).status_code, 422)
        self.assertEqual(self.design(description="ok").status_code, 422)
        self.assertEqual(self.design(description="x" * 1001).status_code, 422)
        self.assertEqual(self.design(description="bad\x07one").status_code, 422)
        r = self.design(language="klingon")
        self.assertEqual(r.status_code, 422)
        self.assertIn("English, Chinese, Japanese, Auto", r.json()["detail"])
        self.assertEqual(self.design(seed=-1).status_code, 422)
        self.assertEqual(self.design(seed=2**31).status_code, 422)
        self.assertEqual(self.design(sample_text="x" * 301).status_code, 422)
        self.assertEqual(self.design(sample_text="[pause]").status_code, 422)
        self.assertEqual(self.client.post("/v1/voices/design", json={"name": "x"}).status_code, 422)
        self.assertEqual(self.engine.designs, [])

    def test_design_off_limit_and_busy(self) -> None:
        self.engine.can_design = False
        r = self.design()
        self.assertEqual(r.status_code, 501)
        self.assertEqual(r.json()["detail"], server.DESIGN_TURNED_OFF)
        self.engine.can_design = True
        server.MAX_VOICES = 1
        self.assertEqual(self.design().status_code, 201)
        self.assertEqual(self.design().status_code, 409)
        self.assertEqual(self.create(wav_bytes(6)).status_code, 409)
        server.MAX_VOICES = 50
        self.engine.fail_with = RuntimeError("the model produced invalid audio")
        r = self.design()
        self.assertEqual(r.status_code, 500)
        self.assertTrue(any("Voice design failed: RuntimeError" in line for line in self.logs.lines))

    def test_a_failed_synthesis_ends_the_audio(self) -> None:
        self.engine.fail_with = RuntimeError("bad audio")
        r = self.speak("Zebra quartz says hello. And more.")
        self.assertEqual((r.status_code, r.content), (200, b""))
        self.assertTrue(any("Synthesis failed: RuntimeError" in line for line in self.logs.lines))


# ---------------------------------------------------------------- instructions


class InstructionsTest(ServerTest):
    def test_ignored_unless_in_design_mode(self) -> None:
        voice_id = self.design().json()["id"]
        r = self.speak("Hello there.", voice=voice_id, instructions="cheerful, a little faster")
        self.assertEqual((r.status_code, r.headers["x-phrase-cache"]), (200, "miss"))
        self.assertEqual(len(self.engine.spoken), 1)
        self.assertEqual(len(self.engine.designs), 1)  # the voice's own design only
        self.assertTrue(any(
            "instructions ignored (VOICE_DESIGN_INSTRUCT=ignore): 25 chars" in line for line in self.logs.lines
        ))

    def test_design_mode_renders_with_the_description_and_the_instructions(self) -> None:
        server.INSTRUCT_MODE = "design"
        voice_id = self.design(seed=3).json()["id"]
        server.prewarm_phrases(["Okay."], [voice_id])
        r = self.speak("Okay. Hello there.", voice=voice_id, instructions="cheerful, a little faster")
        self.assertEqual((r.status_code, r.headers["x-phrase-cache"]), (200, "bypass"))
        self.assertEqual(self.engine.designs[-2:], [
            ("Okay.", f"{DESCRIPTION} cheerful, a little faster", "English", 3),
            ("Hello there.", f"{DESCRIPTION} cheerful, a little faster", "English", 3),
        ])
        # Nothing of it is kept: the next plain request reaches the speech model.
        self.assertEqual(self.speak("Hello there.", voice=voice_id).headers["x-phrase-cache"], "miss")
        self.assertEqual(self.engine.spoken[-1][0], "Hello there.")
        # The built-in voice is a designed one too; a cloned voice takes no instructions.
        self.speak("Hi.", instructions="slowly")
        self.assertEqual(self.engine.designs[-1][0], "Hi.")
        cloned = self.create(wav_bytes(6)).json()["id"]
        self.speak("Hi.", voice=cloned, instructions="slowly")
        self.assertEqual(self.engine.spoken[-1][0], "Hi.")
        self.assertTrue(any("instructions ignored for a cloned voice" in line for line in self.logs.lines))


# ---------------------------------------------------------------- speech and streaming


class SpeechTest(ServerTest):
    def test_pcm_and_wav(self) -> None:
        pcm = self.speak("Hello.")
        self.assertEqual((pcm.status_code, pcm.headers["content-type"]), (200, "audio/pcm"))
        self.assertEqual(len(pcm.content), 9600)  # 0.2 s at 24 kHz, 16-bit mono
        wav = self.speak("Hello there.", fmt="wav")
        self.assertEqual(wav.headers["content-type"], "audio/wav")
        self.assertEqual(wav.content[:4] + wav.content[8:16], b"RIFFWAVEfmt ")
        fmt = np.frombuffer(wav.content[20:36], dtype="<u2")
        self.assertEqual((int(fmt[0]), int(fmt[1])), (1, 1))  # PCM, mono
        self.assertEqual(int.from_bytes(wav.content[24:28], "little"), 24000)
        self.assertEqual(len(wav.content), 44 + 9600)
        self.assertIs(self.engine.spoken[-1][1], server._prompt_for("default"))

    def test_request_checks(self) -> None:
        self.assertEqual(self.client.post("/v1/audio/speech", json={
            "model": "pocket-tts", "input": "Hi.", "voice": "default"}).status_code, 404)
        self.assertEqual(self.speak("Hi.", sample_rate=16000).status_code, 422)
        self.assertEqual(self.speak("Hi.", sample_rate=24000).status_code, 200)
        self.assertEqual(self.speak("Hi.", fmt="mp3").status_code, 422)
        self.assertEqual(self.speak("Hi.", voice="alba").status_code, 422)
        self.assertEqual(self.speak("Hi.", voice="custom-x-0123456789abcdef").status_code, 422)
        self.assertEqual(self.speak("Hi.", voice="design-x-0123456789abcdef").status_code, 422)
        self.assertEqual(self.speak("   ").status_code, 400)
        self.assertEqual(self.speak("Hi.", voice="alloy").status_code, 200)  # OpenAI name
        self.assertEqual(self.speak("Hi.", speed=1.5).status_code, 200)  # no speed control

    def test_sentences_are_spoken_in_order(self) -> None:
        r = self.speak("First one here. Second one here! Third one?")
        self.assertEqual(self.engine.texts, ["First one here.", "Second one here!", "Third one?"])
        self.assertEqual(r.headers["x-phrase-cache"], "miss")
        self.assertEqual(levels(r.content), [pcm_level(self.engine, n) for n in (1, 2, 3)])

    def test_logs_never_hold_the_text_the_description_or_the_transcript(self) -> None:
        secret = "Zebra quartz forty two"
        self.speak(f"{secret}. Another {secret} sentence. [pause]")
        self.speak(f"{secret}.", fmt="wav")
        self.speak(f"{secret}.")  # a kept phrase now
        self.design(description=f"A {secret} voice, deep and slow.", sample_text=f"{secret} speaking.")
        self.create(wav_bytes(6), transcript=f"{secret} was said here")
        self.speak(f"{secret}.", instructions=f"say it {secret}")
        self.engine.fail_with = ValueError(secret)
        self.speak(f"Oh {secret}!")
        self.assertTrue(any("chars=" in line for line in self.logs.lines))
        self.assertTrue(any("Designed voice" in line for line in self.logs.lines))
        for line in self.logs.lines:
            self.assertNotIn("zebra", line.lower())
            self.assertNotIn("quartz", line.lower())


class LiveServer:
    """The app on 127.0.0.1 (a free port), without its lifespan: audio is read as it is sent."""

    def __init__(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            server.app, host="127.0.0.1", port=0, lifespan="off", log_level="warning",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("the test server did not start")
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def post(self, text: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = json.dumps({"model": MODEL, "input": text, "voice": "default"})
        conn.request("POST", "/v1/audio/speech", body=body, headers={"Content-Type": "application/json"})
        return conn, conn.getresponse()

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class StreamingTest(ServerTest):
    def setUp(self) -> None:
        super().setUp()
        self.live = LiveServer()
        self.addCleanup(self.live.close)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def test_each_sentence_is_sent_as_soon_as_it_is_made(self) -> None:
        self.engine.gate = (2, self.release)  # the second sentence waits until released
        conn, response = self.live.post("One sentence here. Two sentences here. Three.")
        self.addCleanup(conn.close)
        self.assertEqual(response.status, 200)
        first = response.read(9600)
        self.assertTrue(self.engine.started.wait(5))
        self.assertEqual(levels(first), [pcm_level(self.engine, 1)])
        self.assertEqual(self.engine.calls, 2)
        self.release.set()
        rest = response.read()
        self.assertEqual(levels(first + rest), [pcm_level(self.engine, n) for n in (1, 2, 3)])

    def test_a_new_reply_goes_before_the_later_sentences_of_one_playing(self) -> None:
        self.engine.gate = (1, self.release)
        results: dict[str, bytes] = {}

        def call(name: str, text: str) -> None:
            conn, response = self.live.post(text)
            results[name] = response.read()
            conn.close()

        first = threading.Thread(target=call, args=("a", "A one here. A two here. A three here."))
        first.start()
        self.assertTrue(self.engine.started.wait(5))
        second = threading.Thread(target=call, args=("b", "B one here."))
        second.start()
        deadline = time.monotonic() + 5
        while server._gpu.waiting() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server._gpu.waiting(), 1)
        self.release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(self.engine.texts, ["A one here.", "B one here.", "A two here.", "A three here."])
        self.assertEqual(levels(results["b"]), [pcm_level(self.engine, 2)])


# ---------------------------------------------------------------- phrase cache


class PhraseCacheTest(ServerTest):
    def test_prewarmed_phrase_and_opener(self) -> None:
        server.prewarm_phrases(["Got it.", "Thanks."], ["default"])
        self.assertEqual(self.engine.calls, 2)
        first = self.speak("got it.")
        self.assertEqual((first.headers["x-phrase-cache"], self.engine.calls), ("hit", 2))
        opener = server._phrase_cache.get("default", "Got it.")
        r = self.speak("Got it, perfect timing then.")
        self.assertEqual(r.headers["x-phrase-cache"], "opener")
        self.assertEqual(self.engine.texts[-1], "Perfect timing then.")
        self.assertTrue(r.content.startswith(b"".join(opener)))
        calls = self.engine.calls
        r = self.speak("Got it! Thanks.")
        self.assertEqual((r.headers["x-phrase-cache"], self.engine.calls), ("opener", calls))
        self.assertEqual(self.speak("Gotta go now.").headers["x-phrase-cache"], "miss")

    def test_short_texts_are_kept_per_voice(self) -> None:
        self.assertEqual(self.speak("Understood.").headers["x-phrase-cache"], "miss")
        self.assertEqual(self.speak("Understood.").headers["x-phrase-cache"], "hit")
        voice_id = self.design().json()["id"]
        self.assertEqual(self.speak("Understood.", voice=voice_id).headers["x-phrase-cache"], "miss")
        long_text = "This sentence is longer than thirty-two characters."
        self.assertEqual(self.speak(long_text).headers["x-phrase-cache"], "miss")
        self.assertEqual(self.speak(long_text).headers["x-phrase-cache"], "miss")
        stats = self.client.get("/v1/audio/phrase-cache").json()
        self.assertEqual((stats["hits"], stats["entries"]), (1, 2))
        self.assertNotIn("understood", json.dumps(stats).lower())

    def test_a_designed_voice_gets_the_kept_phrases_in_the_background(self) -> None:
        voice_id = self.design().json()["id"]
        server.PHRASE_CACHE_ANY_VOICE = True
        server.CACHED_PHRASES = ["Oh nice!"]
        turns = []
        real_turn = server._gpu.turn

        def recording_turn(deadline):
            turns.append(deadline - time.monotonic())
            return real_turn(deadline)

        with mock.patch.object(server._gpu, "turn", recording_turn):
            self.assertEqual(self.speak("Hello there.", voice=voice_id).status_code, 200)
            for thread in server._phrase_threads:
                thread.join(timeout=5)
        self.assertEqual([round(t) for t in turns], [0, 10])
        self.assertEqual(self.engine.texts[-2:], ["Hello there.", "Oh nice!"])
        r = self.speak("Oh nice, a Civic!", voice=voice_id)
        self.assertEqual(r.headers["x-phrase-cache"], "opener")
        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 204)
        self.assertNotIn(voice_id, server._phrase_voices_made)
        self.assertEqual(server._phrase_cache.stats()["entries"], 0)


# ---------------------------------------------------------------- cloning


TOKEN = "hf_TestOnlyNotARealToken0123456789ab"  # noqa: S105


class CloningTest(ServerTest):
    def test_clone_with_and_without_a_transcript(self) -> None:
        r = self.create(wav_bytes(6.5), consent_statement="Signed form on file, 2026-10-30.",
                        transcript="  The quick brown fox  jumps over it. ")
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        voice_id = body["id"]
        self.assertRegex(voice_id, r"^custom-sam-example-[0-9a-f]{16}$")
        self.assertEqual((body["name"], body["type"], body["sample_seconds"]), ("Sam Example", "custom", 6.5))
        self.assertNotIn("description", body)
        audio, transcript = self.engine.samples[-1]
        self.assertEqual((audio.shape, audio.dtype, transcript), ((int(6.5 * 24000),), np.float32, "The quick brown fox jumps over it."))
        # The preview is rendered by the speech model, never the upload.
        self.assertEqual(self.engine.spoken[-1][0], server.SAMPLE_TEXT)
        preview = self.client.get(f"/v1/voices/{voice_id}/sample")
        self.assertEqual(levels(preview.content[44:]), [pcm_level(self.engine, 1)])
        meta = json.loads((server.VOICE_DIR / f"{voice_id}.json").read_text())
        self.assertEqual(meta["consent"], {"attested": True, "statement": "Signed form on file, 2026-10-30."})
        self.assertEqual(meta["clone_mode"], "icl")
        self.assertNotIn("fox", json.dumps(meta))  # the transcript is in the prompt only
        self.assertEqual(self.files(), [f"{voice_id}.json", f"{voice_id}.safetensors", f"{voice_id}.wav"])

        plain = self.create(wav_bytes(5), name="No Transcript").json()
        self.assertIsNone(self.engine.samples[-1][1])
        self.assertEqual(json.loads((server.VOICE_DIR / f"{plain['id']}.json").read_text())["clone_mode"], "xvector")
        self.speak("Hi.", voice=plain["id"])
        self.assertTrue(self.engine.spoken[-1][1].x_vector_only_mode)

        server._voices.clear()
        server._prompts.clear()
        server.load_voice_index()
        server.preload_prompts()
        self.assertEqual(set(server._voices), {"default", voice_id, plain["id"]})
        self.assertFalse(server._prompt_for(plain["id"]).icl_mode)
        self.assertEqual(server._prompt_for(voice_id).ref_text, "The quick brown fox jumps over it.")
        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 204)
        self.assertEqual(self.speak("Hi.", voice=voice_id).status_code, 422)

    def test_three_seconds_is_enough_and_the_rules(self) -> None:
        self.assertEqual(self.create(wav_bytes(3.0, rate=16000)).status_code, 201)
        self.assertIn("between 3 and 30", self.create(wav_bytes(2.5)).json()["detail"])
        self.assertIn("between 3 and 30", self.create(wav_bytes(31, rate=16000)).json()["detail"])
        self.assertIn("sample rate", self.create(wav_bytes(6, rate=12345)).json()["detail"])
        for value in ("", "false", "yes please"):
            r = self.create(wav_bytes(6), consent=value)
            self.assertEqual(r.status_code, 422)
            self.assertIn("permission", r.json()["detail"])
        fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + os.urandom(2048)
        self.assertEqual(self.create(fake_mp3).status_code, 415)
        self.assertEqual(self.create(wav_bytes(6) + b"\x00" * (10 * 1024 * 1024)).status_code, 413)
        self.assertEqual(self.create(wav_bytes(6), name="x" * 61).status_code, 422)
        self.assertEqual(len(self.engine.samples), 1)

    def test_cloning_switch_and_the_cloning_endpoints(self) -> None:
        state = self.client.get("/v1/cloning").json()
        self.assertEqual(state, {
            "state": "on", "message": None,
            "terms_url": "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base", "token_source": None,
        })
        r = self.client.put("/v1/cloning", json={"hf_token": TOKEN})
        self.assertEqual((r.status_code, r.json()["state"]), (200, "on"))
        self.assertIn("needs no Hugging Face token", r.json()["message"])
        self.assertEqual(self.client.delete("/v1/cloning").status_code, 409)
        cloned = self.create(wav_bytes(6)).json()["id"]
        designed = self.design().json()["id"]
        server.CLONING = False
        self.assertEqual(self.client.get("/v1/cloning").json()["state"], "off")
        self.assertEqual(self.create(wav_bytes(6)).status_code, 501)
        self.assertEqual(self.client.put("/v1/cloning", json={}).status_code, 409)
        self.assertEqual(self.client.delete("/v1/cloning").status_code, 204)
        # Designing, listing, speaking and deleting keep working without cloning.
        listed = [v["id"] for v in self.client.get("/v1/voices").json()["data"]]
        self.assertEqual(listed, ["default", cloned, designed])
        self.assertEqual(self.design(name="Later").status_code, 201)
        self.assertEqual(self.speak("Hi.", voice=cloned).status_code, 200)
        self.assertEqual(self.client.delete(f"/v1/voices/{cloned}").status_code, 204)
        for line in self.logs.lines:
            self.assertNotIn(TOKEN, line)

    def test_ids_are_random_and_paths_cannot_escape(self) -> None:
        ids = {server.new_voice_id("Sam", server.DESIGN_PREFIX) for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(server.DESIGN_ID.match(i) for i in ids))
        self.assertRegex(server.new_voice_id("Ünïcôdé  ✨", server.CUSTOM_PREFIX), r"^custom-unicode-[0-9a-f]{16}$")
        for bad in ("design-..-0123456789abcdef", "custom-a-0123", "../etc/passwd", "default"):
            self.assertEqual(self.client.delete(f"/v1/voices/{bad}").status_code, 404, bad)
        self.assertEqual(self.client.get("/v1/voices/../default/sample").status_code, 404)
        self.assertEqual(self.client.get("/v1/voices/default/sample").status_code, 200)
        self.assertIn("default.safetensors", [p.name for p in server.VOICE_DIR.iterdir()])

    def test_lru_keeps_a_bounded_number_of_voices_in_memory(self) -> None:
        server.VOICE_CACHE = 2
        made = [self.design(name=f"V{i}").json()["id"] for i in range(3)]
        self.assertEqual(list(server._prompts), made[1:])
        server._prompt_for(made[0])
        self.assertEqual(list(server._prompts), [made[2], made[0]])

    def test_api_key_protects_everything_but_health(self) -> None:
        server.API_KEY = "secret"
        voice_id = "design-x-0123456789abcdef"
        calls = [
            ("GET", "/v1/models"), ("GET", "/v1/audio/voices"), ("GET", "/v1/audio/sound-tags"),
            ("GET", "/v1/capabilities"), ("GET", "/v1/audio/phrase-cache"), ("GET", "/v1/voices"),
            ("GET", "/v1/cloning"), ("PUT", "/v1/cloning"), ("DELETE", "/v1/cloning"),
            ("DELETE", f"/v1/voices/{voice_id}"), ("GET", f"/v1/voices/{voice_id}/sample"),
            ("GET", "/v1/voices/default/sample"),
        ]
        for method, path in calls:
            self.assertEqual(self.client.request(method, path).status_code, 401, path)
            wrong = self.client.request(method, path, headers={"Authorization": "Bearer nope"})
            self.assertEqual(wrong.status_code, 401, path)
        self.assertEqual(self.speak("Hi.").status_code, 401)
        self.assertEqual(self.create(wav_bytes(6)).status_code, 401)
        self.assertEqual(self.design().status_code, 401)
        self.assertEqual(self.engine.calls, 0)
        ok = {"Authorization": "Bearer secret"}
        self.assertEqual(self.client.get("/v1/capabilities", headers=ok).status_code, 200)
        self.assertEqual(self.client.post("/v1/voices/design", headers=ok, json={
            "name": "Sam", "description": DESCRIPTION}).status_code, 201)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "device": "cpu"})


# ---------------------------------------------------------------- startup


class StartupTest(ServerTest):
    def start(self, engine: FakeEngine):
        server._engine = None
        server._voices.clear()
        server._prompts.clear()
        with mock.patch.object(server, "load_engine", return_value=engine), TestClient(server.app) as client:
            return client.get("/health").json(), client.get("/v1/voices").json()["data"]

    def test_first_start_designs_the_built_in_voice_then_reuses_it(self) -> None:
        server.WARMUP = True
        for path in list(server.VOICE_DIR.iterdir()):
            path.unlink()
        engine = FakeEngine()
        health, voices = self.start(engine)
        self.assertEqual(health, {"status": "ok", "device": "cpu"})
        self.assertEqual([v["id"] for v in voices], ["default"])
        self.assertEqual(engine.designs, [(server.SAMPLE_TEXT, server.DEFAULT_VOICE_DESCRIPTION, "English", server.DEFAULT_VOICE_SEED)])
        self.assertEqual(engine.texts[1], "Hello there.")  # the warm-up, after the design
        self.assertEqual(engine.calls, 2 + len(server.CACHED_PHRASES))
        self.assertEqual(server._phrase_cache.stats()["pinned"], len(server.CACHED_PHRASES))
        self.assertTrue(any("first start" in line for line in self.logs.lines))

        again = FakeEngine()
        self.start(again)
        self.assertEqual(again.designs, [])  # on disk already
        server.DEFAULT_VOICE_DESCRIPTION = "A gravelly old sailor."
        changed = FakeEngine()
        self.start(changed)
        self.assertEqual(changed.designs[0][1], "A gravelly old sailor.")
        self.assertTrue(any("settings changed" in line for line in self.logs.lines))

    def test_without_the_design_model_the_built_in_voice_must_be_on_disk(self) -> None:
        engine = FakeEngine(can_design=False)
        health, voices = self.start(engine)  # made by setUp: fine
        self.assertEqual(health["status"], "ok")
        self.assertEqual(self.client.get("/v1/capabilities").json()["design"], False)
        for path in list(server.VOICE_DIR.iterdir()):
            path.unlink()
        with self.assertRaises(RuntimeError):
            self.start(FakeEngine(can_design=False))

    def test_startup_loads_voices_from_disk_and_checks_settings(self) -> None:
        voice_id = self.design().json()["id"]
        _, voices = self.start(FakeEngine())
        self.assertEqual([v["id"] for v in voices], ["default", voice_id])
        self.assertIn(voice_id, server._prompts)
        server.INSTRUCT_MODE = "loud"
        with self.assertRaises(RuntimeError):
            self.start(FakeEngine())
        server.INSTRUCT_MODE = "ignore"
        server.LANGUAGE = "klingon"
        with self.assertRaises(server.SampleRejectedError):
            self.start(FakeEngine())


if __name__ == "__main__":
    unittest.main()
