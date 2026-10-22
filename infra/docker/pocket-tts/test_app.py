"""Tests for the adapter's voice-cloning code path and phrase cache, with the model
replaced by a fake.

The real cloning weights are gated, so these tests check everything around them: upload
limits, WAV decoding and resampling, consent, ids, saving and reloading the voice state,
the 501 answer when cloning is off, and speech with a cloned voice. The phrase-cache tests
check replay without the model, keys per voice, what is kept, and the memory bound. The
cloning-switch tests (/v1/cloning) replace Hugging Face and the weights download with fakes:
no request leaves the machine and no real token is used.

Run inside the image (stdlib unittest; the folder is mounted, not copied into the image):

  docker compose --profile local-ai run --rm --no-deps \
    -v "$PWD/infra/docker/pocket-tts:/srv/tests:ro" -w /srv --entrypoint python \
    local-tts -m unittest discover -s /srv/tests -t /srv/tests -v
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
import numpy as np
import safetensors.torch
import scipy.io.wavfile
import torch
from huggingface_hub import constants as hf_constants
from huggingface_hub import get_token
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

os.environ.setdefault("POCKET_TTS_WARMUP", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Voices get their kept phrases in the background only in the test that checks it.
server.PHRASE_CACHE_ANY_VOICE = False


class FakeModel:
    """Stands in for TTSModel: records prompts and returns a small, real-shaped state."""

    sample_rate = 24000
    has_voice_cloning = True

    def __init__(self) -> None:
        self.prompts: list[torch.Tensor] = []
        self.spoken_with: list[object] = []

    def get_state_for_audio_prompt(self, audio, truncate: bool = False):
        if isinstance(audio, torch.Tensor):
            self.prompts.append(audio)
            frames = max(1, audio.shape[-1] // 1920)
            return {
                "transformer.layers.0.self_attn": {
                    "cache": torch.full((2, 1, frames, 2, 4), 0.5),
                    "offset": torch.tensor([frames]),
                }
            }
        if isinstance(audio, Path):
            from pocket_tts.models.model_state import _import_model_state

            return _import_model_state(audio, torch.device("cpu"))
        return {"builtin": {"offset": torch.tensor([1])}}

    def generate_audio_stream(self, model_state, text_to_generate, stop=None, **_):
        self.spoken_with.append(model_state)
        yield torch.zeros(2400)


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


class CloningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (server.VOICE_DIR, server._model, server._cloning_enabled, server.API_KEY)
        server.VOICE_DIR = Path(self.tmp.name) / "voices"
        self.model = FakeModel()
        server._model = self.model  # type: ignore[assignment]
        server._cloning_enabled = True
        server.API_KEY = None
        server._custom_voices.clear()
        server._custom_states.clear()
        server._voice_states.clear()
        # Each test starts with an empty phrase cache, so every request reaches the model.
        self.saved_cache = server._phrase_cache
        server._phrase_cache = server.PhraseCache(1024 * 1024, 32)
        self.client = TestClient(server.app)  # no `with`: the real model is never loaded

    def tearDown(self) -> None:
        server.VOICE_DIR, server._model, server._cloning_enabled, server.API_KEY = self.saved
        server._phrase_cache = self.saved_cache
        server._custom_voices.clear()
        server._custom_states.clear()
        self.tmp.cleanup()

    def create(self, audio: bytes, **fields: str):
        data = {"name": "Sam Example", "consent": "true", **fields}
        return self.client.post(
            "/v1/voices", data=data, files={"audio": ("sample.wav", audio, "audio/wav")}
        )

    # ---------------------------------------------------------------- cloning off

    def test_endpoints_answer_501_with_setup_steps_when_cloning_is_off(self) -> None:
        server._cloning_enabled = False
        for response in (
            self.client.get("/v1/voices"),
            self.create(wav_bytes(6)),
            self.client.delete("/v1/voices/custom-sam-0123456789abcdef"),
        ):
            self.assertEqual(response.status_code, 501, response.text)
            detail = response.json()["detail"]
            self.assertIn("https://huggingface.co/kyutai/pocket-tts", detail)
            self.assertIn("HF_TOKEN", detail)
        speech = self.client.post(
            "/v1/audio/speech",
            json={"model": "pocket-tts", "input": "Hi", "voice": "custom-sam-0123456789abcdef"},
        )
        self.assertEqual(speech.status_code, 422)
        self.assertIn("cloned voice", speech.json()["detail"])
        self.assertEqual(self.model.prompts, [])

    def test_built_in_voices_still_work(self) -> None:
        self.assertEqual(self.client.get("/v1/audio/voices").status_code, 200)
        r = self.client.post(
            "/v1/audio/speech", json={"model": "pocket-tts", "input": "Hello.", "voice": "alba"}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content), 4800)

    # ---------------------------------------------------------------- validation

    def test_consent_is_required(self) -> None:
        for value in ("", "false", "yes please"):
            r = self.create(wav_bytes(6), consent=value)
            self.assertEqual(r.status_code, 422)
            self.assertIn("permission", r.json()["detail"])
        self.assertEqual(self.model.prompts, [])

    def test_only_wav_is_accepted(self) -> None:
        fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + os.urandom(2048)
        r = self.create(fake_mp3)
        self.assertEqual(r.status_code, 415)
        r = self.create(b"#!/bin/sh\nrm -rf /\n" * 10)
        self.assertEqual(r.status_code, 415)
        broken = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
        self.assertEqual(self.create(broken).status_code, 422)

    def test_duration_rate_and_silence_limits(self) -> None:
        self.assertIn("between 5 and 30", self.create(wav_bytes(3)).json()["detail"])
        self.assertIn("between 5 and 30", self.create(wav_bytes(31, rate=16000)).json()["detail"])
        self.assertIn("sample rate", self.create(wav_bytes(6, rate=12345)).json()["detail"])
        silent = io.BytesIO()
        scipy.io.wavfile.write(silent, 16000, np.zeros(16000 * 6, dtype=np.int16))
        self.assertIn("silent", self.create(silent.getvalue()).json()["detail"])
        self.assertEqual(self.model.prompts, [])

    def test_size_limit(self) -> None:
        big = wav_bytes(6) + b"\x00" * (10 * 1024 * 1024)
        r = self.create(big)
        self.assertEqual(r.status_code, 413)

    def test_name_is_required_and_bounded(self) -> None:
        self.assertEqual(self.create(wav_bytes(6), name="  ").status_code, 422)
        self.assertEqual(self.create(wav_bytes(6), name="x" * 61).status_code, 422)
        self.assertEqual(self.create(wav_bytes(6), name="bad\x07name").status_code, 422)

    def test_decodes_24_bit_and_float_wav(self) -> None:
        for dtype in (np.int32, np.float32):
            audio, rate = server.decode_wav(wav_bytes(6, rate=48000, channels=1, dtype=dtype))
            self.assertEqual(rate, 48000)
            self.assertEqual(audio.dtype, np.float32)
            self.assertLessEqual(float(np.max(np.abs(audio))), 1.0)
            self.assertGreater(float(np.max(np.abs(audio))), 0.2)

    # ---------------------------------------------------------------- create, use, delete

    def test_clone_speak_reload_and_delete(self) -> None:
        r = self.create(wav_bytes(6.5), consent_statement="Signed form on file, 2026-10-19.")
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        voice_id = body["id"]
        self.assertRegex(voice_id, r"^custom-sam-example-[0-9a-f]{16}$")
        self.assertEqual(body["name"], "Sam Example")
        self.assertEqual(body["sample_seconds"], 6.5)

        # Mono, resampled to the model's 24 kHz.
        prompt = self.model.prompts[-1]
        self.assertEqual(tuple(prompt.shape), (1, int(6.5 * 24000)))
        self.assertEqual(prompt.dtype, torch.float32)

        # Only the state and metadata are kept: never the uploaded audio.
        files = sorted(p.name for p in server.VOICE_DIR.iterdir())
        self.assertEqual(files, [f"{voice_id}.json", f"{voice_id}.safetensors"])
        meta = json.loads((server.VOICE_DIR / f"{voice_id}.json").read_text())
        self.assertEqual(meta["consent"]["statement"], "Signed form on file, 2026-10-19.")
        self.assertTrue(meta["consent"]["attested"])
        self.assertEqual(meta["language"], server.LANGUAGE)

        listed = self.client.get("/v1/voices").json()["data"]
        self.assertIn("alba", [v["id"] for v in listed if v["type"] == "builtin"])
        self.assertEqual([v["id"] for v in listed if v["type"] == "custom"], [voice_id])

        speech = self.client.post(
            "/v1/audio/speech", json={"model": "pocket-tts", "input": "Hi.", "voice": voice_id}
        )
        self.assertEqual(speech.status_code, 200)
        self.assertIn("transformer.layers.0.self_attn", self.model.spoken_with[-1])

        # A restart reads the index from disk and reloads the saved state.
        server._custom_voices.clear()
        server._custom_states.clear()
        server.load_voice_index()
        self.assertIn(voice_id, server._custom_voices)
        state = server._voice_state(voice_id)
        cache = state["transformer.layers.0.self_attn"]["cache"]  # type: ignore[index]
        self.assertTrue(torch.equal(cache, torch.full_like(cache, 0.5)))

        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 204)
        self.assertEqual(list(server.VOICE_DIR.iterdir()), [])
        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 404)
        gone = self.client.post(
            "/v1/audio/speech", json={"model": "pocket-tts", "input": "Hi.", "voice": voice_id}
        )
        self.assertEqual(gone.status_code, 422)

    def test_ids_are_random_and_paths_cannot_escape(self) -> None:
        ids = {server.new_voice_id("Sam") for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(server.CUSTOM_ID.match(i) for i in ids))
        self.assertRegex(server.new_voice_id("Ünïcôdé  ✨"), r"^custom-unicode-[0-9a-f]{16}$")
        self.assertRegex(server.new_voice_id("✨✨"), r"^custom-voice-[0-9a-f]{16}$")
        for bad in ("custom-..-0123456789abcdef", "custom-a-0123", "../etc/passwd"):
            self.assertFalse(server.CUSTOM_ID.match(bad))
        self.assertEqual(self.client.delete("/v1/voices/custom-x-..").status_code, 404)

    def test_lru_keeps_a_bounded_number_of_states(self) -> None:
        saved = server.CUSTOM_VOICE_CACHE
        server.CUSTOM_VOICE_CACHE = 2
        try:
            made = [self.create(wav_bytes(5.5), name=f"V{i}").json()["id"] for i in range(3)]
            self.assertEqual(list(server._custom_states), made[1:])
            server._voice_state(made[0])  # reloaded from disk, evicts the oldest
            self.assertEqual(list(server._custom_states), [made[2], made[0]])
        finally:
            server.CUSTOM_VOICE_CACHE = saved

    def test_voice_limit(self) -> None:
        saved = server.MAX_CUSTOM_VOICES
        server.MAX_CUSTOM_VOICES = 1
        try:
            self.assertEqual(self.create(wav_bytes(6)).status_code, 201)
            self.assertEqual(self.create(wav_bytes(6)).status_code, 409)
        finally:
            server.MAX_CUSTOM_VOICES = saved

    def test_api_key_protects_the_voice_endpoints(self) -> None:
        server.API_KEY = "secret"
        self.assertEqual(self.client.get("/v1/voices").status_code, 401)
        self.assertEqual(self.create(wav_bytes(6)).status_code, 401)
        ok = self.client.get("/v1/voices", headers={"Authorization": "Bearer secret"})
        self.assertEqual(ok.status_code, 200)


class CountingModel(FakeModel):
    """Three distinct chunks per synthesis, numbered, so replays can be told apart."""

    has_voice_cloning = False

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.texts: list[str] = []

    def generate_audio_stream(self, model_state, text_to_generate, stop=None, **_):
        self.calls += 1
        self.texts.append(text_to_generate)
        for i in range(3):
            yield torch.full((1920,), 0.1 * (self.calls + i))


class PhraseCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (server._model, server._phrase_cache, server._cloning_enabled, server.API_KEY)
        self.model = CountingModel()
        server._model = self.model  # type: ignore[assignment]
        server._phrase_cache = server.PhraseCache(1024 * 1024, 32)
        server._cloning_enabled = False
        server.API_KEY = None
        server._voice_states.clear()
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server._model, server._phrase_cache, server._cloning_enabled, server.API_KEY = self.saved
        server._voice_states.clear()

    def speak(self, text: str, voice: str = "alba", fmt: str = "pcm"):
        return self.client.post(
            "/v1/audio/speech",
            json={"model": "pocket-tts", "input": text, "voice": voice, "response_format": fmt},
        )

    def test_normalisation(self) -> None:
        self.assertEqual(server.normalize_phrase("  Got\u00a0 it. "), "got it.")
        self.assertEqual(server.normalize_phrase("That\u2019s right."), "that's right.")
        self.assertNotEqual(server.normalize_phrase("Great."), server.normalize_phrase("Great!"))

    def test_prewarmed_phrase_is_replayed_without_the_model(self) -> None:
        server.prewarm_phrases(["Got it."], ["alba"])
        self.assertEqual(self.model.calls, 1)
        first = self.speak("got it.")
        again = self.speak("Got  it.", fmt="wav")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(first.headers["x-phrase-cache"], "hit")
        self.assertEqual(len(first.content), 3 * 1920 * 2)
        # Same audio as generated; WAV adds only the streaming header.
        self.assertEqual(again.content[44:], first.content)
        self.assertEqual(again.content[:4], b"RIFF")
        self.assertEqual(server._phrase_cache.stats()["pinned"], 1)

    def test_cache_is_per_voice(self) -> None:
        server.prewarm_phrases(["Okay."], ["alba"])
        r = self.speak("Okay.", voice="marius")
        self.assertEqual(r.headers["x-phrase-cache"], "miss")
        self.assertEqual(self.model.calls, 2)

    def test_short_texts_are_kept_once_spoken_long_ones_never(self) -> None:
        self.assertEqual(self.speak("Understood.").headers["x-phrase-cache"], "miss")
        self.assertEqual(self.speak("Understood.").headers["x-phrase-cache"], "hit")
        long_text = "This sentence is longer than thirty-two characters."
        self.assertEqual(self.speak(long_text).headers["x-phrase-cache"], "miss")
        self.assertEqual(self.speak(long_text).headers["x-phrase-cache"], "miss")
        self.assertEqual(self.model.calls, 3)

    def test_memory_bound_evicts_unpinned_first(self) -> None:
        chunk = b"\x01" * 1000
        cache = server.PhraseCache(3000, 32)
        self.assertTrue(cache.put("alba", "Pinned.", [chunk], pin=True))
        self.assertTrue(cache.put("alba", "One.", [chunk]))
        self.assertTrue(cache.put("alba", "Two.", [chunk]))
        self.assertIsNotNone(cache.get("alba", "One."))  # Two. is now least recently used
        self.assertTrue(cache.put("alba", "Three.", [chunk]))
        self.assertIsNone(cache.get("alba", "Two."))
        self.assertIsNotNone(cache.get("alba", "Pinned."))
        self.assertLessEqual(cache.stats()["bytes"], 3000)
        self.assertFalse(cache.put("alba", "Too big.", [chunk * 4]))
        cache.drop_voice("alba")
        self.assertEqual(cache.stats()["entries"], 0)

    def test_kept_audio_starts_just_before_the_first_sound(self) -> None:
        rate = 24000
        silence = np.zeros(4800, dtype=np.int16).tobytes()  # 100 ms
        tone = (np.full(1920, 0.2) * 32767).astype(np.int16).tobytes()
        trimmed = server.trim_leading_silence([silence] + [tone] * 6, rate)
        joined = b"".join(trimmed)
        lead = rate * server.KEEP_LEAD_MS // 1000 * 2
        self.assertEqual(len(joined), lead + 6 * len(tone))
        self.assertEqual(joined[:lead], bytes(lead))
        self.assertEqual(joined[lead:], tone * 6)
        # Re-cut into chunks of the first chunk's size.
        sizes = [len(silence), len(silence), len(joined) - 2 * len(silence)]
        self.assertEqual([len(c) for c in trimmed], sizes)
        # Nothing audible: unchanged.
        self.assertEqual(server.trim_leading_silence([silence], rate), [silence])

    def test_kept_opener_starts_a_longer_sentence(self) -> None:
        server.prewarm_phrases(["Got it.", "Thanks."], ["alba"])
        opener = server._phrase_cache.get("alba", "Got it.")
        r = self.speak("Got it, perfect timing then.")
        self.assertEqual(r.headers["x-phrase-cache"], "opener")
        self.assertEqual(self.model.texts[-1], "Perfect timing then.")
        self.assertTrue(r.content.startswith(b"".join(opener)))
        self.assertGreater(len(r.content), len(b"".join(opener)))
        # Opener and rest both kept: nothing to synthesise.
        calls = self.model.calls
        r = self.speak("Got it! Thanks.")
        self.assertEqual((r.headers["x-phrase-cache"], self.model.calls), ("opener", calls))
        # Not an opener: no separator after it, or a different word.
        for text in ("Got it right this time.", "Gotta go now.", "I got it, thanks."):
            self.assertEqual(self.speak(text).headers["x-phrase-cache"], "miss", text)

    def test_bare_opener_with_other_end_mark_is_the_kept_phrase(self) -> None:
        server.prewarm_phrases(["Great."], ["alba"])
        calls = self.model.calls
        r = self.speak("Great!")
        self.assertEqual((r.headers["x-phrase-cache"], self.model.calls), ("opener", calls))
        self.assertEqual(r.content, b"".join(server._phrase_cache.get("alba", "Great.")))
        self.assertEqual(self.speak("Great.Really").headers["x-phrase-cache"], "miss")

    def test_longest_opener_wins_and_apostrophes_match(self) -> None:
        server.prewarm_phrases(["Sure.", "Sure thing.", "You're welcome."], ["alba"])
        self.speak("Sure thing, see you then.")
        self.assertEqual(self.model.texts[-1], "See you then.")
        self.speak("You\u2019re welcome, Sarah.")
        self.assertEqual(self.model.texts[-1], "Sarah.")

    def test_stats_endpoint_gives_counts_only(self) -> None:
        self.speak("Sure.")
        self.speak("Sure.")
        stats = self.client.get("/v1/audio/phrase-cache").json()
        self.assertEqual((stats["hits"], stats["misses"], stats["entries"]), (1, 1, 1))
        self.assertNotIn("sure", json.dumps(stats).lower())

    def test_emoji_are_not_read_out(self) -> None:
        self.assertEqual(server.strip_sound_cues("Have a great day! 🚗💨"), "Have a great day!")
        self.assertEqual(server.strip_sound_cues("Done ✅ thanks"), "Done thanks")
        self.assertEqual(server.strip_sound_cues("No emoji here."), "No emoji here.")

    def test_a_new_voice_gets_the_kept_phrases_in_the_background(self) -> None:
        saved = (server.PHRASE_CACHE_ANY_VOICE, server.CACHED_PHRASES, set(server._phrase_voices_made))
        try:
            server.PHRASE_CACHE_ANY_VOICE = True
            server.CACHED_PHRASES = ["Oh nice!"]
            server._phrase_voices_made.clear()
            self.assertEqual(self.speak("Hello there.", voice="marius").status_code, 200)
            for thread in server._phrase_threads:
                thread.join(timeout=5)
            r = self.speak("Oh nice, a Civic!", voice="marius")
            self.assertEqual(r.headers["x-phrase-cache"], "opener")
            self.assertIn("marius", server._phrase_voices_made)
            calls = self.model.calls
            self.speak("Second request.", voice="marius")  # no second batch for the same voice
            for thread in server._phrase_threads:
                thread.join(timeout=5)
            self.assertEqual(self.model.calls, calls + 1)
        finally:
            server.PHRASE_CACHE_ANY_VOICE, server.CACHED_PHRASES = saved[0], saved[1]
            server._phrase_voices_made.clear()
            server._phrase_voices_made.update(saved[2])

    def test_sound_cues_are_removed_not_read_out(self) -> None:
        cases = {
            "[laugh] That's great!": "That's great!",
            "Oh, [Chuckles] that is funny.": "Oh, that is funny.",
            "That's funny [laugh].": "That's funny.",
            "Well [sigh], [gasp] okay": "Well, okay",
            "[clears his throat] Right.": "Right.",
            "Bless you [sniffles] [cough]!": "Bless you!",
            "[shh] Listen.": "Listen.",
            # Not a sound cue: kept, and nothing else changes.
            "Press [1] for sales.  Twice": "Press [1] for sales.  Twice",
            "No cues here.": "No cues here.",
        }
        for text, spoken in cases.items():
            self.assertEqual(server.strip_sound_cues(text), spoken, text)
        r = self.speak("[laugh] Thanks for calling.")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.model.texts[-1], "Thanks for calling.")
        # The rest of the reply still matches a kept phrase.
        server.prewarm_phrases(["Got it."], ["alba"])
        calls = self.model.calls
        r = self.speak("[chuckle] Got it.")
        self.assertEqual((r.headers["x-phrase-cache"], self.model.calls), ("hit", calls))
        # Only a cue: nothing is said, and nothing fails.
        r = self.speak(" [sigh] ", fmt="wav")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.content), 44)
        self.assertEqual(self.model.calls, calls)
        self.assertEqual(self.speak("[laugh]").content, b"")
        self.assertEqual(self.speak("   ").status_code, 400)


# Shaped like a Hugging Face user access token; not one.
TOKEN = "hf_TestOnlyNotARealToken0123456789ab"  # noqa: S105
OTHER_TOKEN = "hf_AnotherFakeTokenForTheEnvironment99"  # noqa: S105


class OpenWeightsModel(FakeModel):
    """TTSModel as loaded without a usable token: the open weights, no voice cloning."""

    config = types.SimpleNamespace(
        weights_path="hf://kyutai/pocket-tts/languages/english/model.safetensors@rev"
    )

    def __init__(self, cloning: bool = False) -> None:
        super().__init__()
        self.has_voice_cloning = cloning
        self.mimi = types.SimpleNamespace()
        self.loaded: list[list[str]] = []

    def load_state_dict(self, state, strict: bool = True):
        self.loaded.append(sorted(state))


def hub_error(kind: type[HfHubHTTPError], status: int, text: str = "") -> HfHubHTTPError:
    url = "https://huggingface.co/api/models/kyutai/pocket-tts/auth-check"
    response = httpx.Response(status, request=httpx.Request("GET", url))
    return kind(f"{status} Client Error. {text}", response=response)


class LogCapture(logging.Handler):
    """Every record of every logger, formatted with tracebacks (what pytest's caplog keeps)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class CloningSwitchTest(unittest.TestCase):
    """GET/PUT/DELETE /v1/cloning with Hugging Face and the weights download faked."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.token_path = root / "huggingface" / "token"  # $HF_HOME/token
        self.weights = root / "model.safetensors"
        safetensors.torch.save_file({"flow_lm.speaker_proj_weight": torch.zeros(2)}, self.weights)
        self.downloads: list[str] = []
        self.download_error: BaseException | None = None
        self.release = threading.Event()
        self.release.set()
        self.hub = mock.Mock()
        self.hub.whoami.return_value = {"name": "voice-owner"}
        self.hub.auth_check.return_value = None
        self.hf_api = mock.Mock(return_value=self.hub)
        for patcher in (
            mock.patch.dict(os.environ),
            mock.patch.object(hf_constants, "HF_TOKEN_PATH", str(self.token_path)),
            mock.patch.object(server, "HfApi", self.hf_api),
            mock.patch.object(server, "download_if_necessary", self.download),
            mock.patch.object(server, "VOICE_DIR", root / "voices"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("HF_TOKEN", None)
        os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
        self.saved = (
            server._model, server._cloning_enabled, server._cloning_status,
            server._cloning_problem, server._phrase_cache, server.API_KEY,
        )
        self.model = OpenWeightsModel()
        server._model = self.model  # type: ignore[assignment]
        server._cloning_enabled = False
        server._cloning_status, server._cloning_problem = "off", None
        server._phrase_cache = server.PhraseCache(1024 * 1024, 32)
        server.API_KEY = None
        server._voice_states.clear()
        server._custom_voices.clear()
        server._custom_states.clear()
        self.logs = LogCapture()
        root_logger = logging.getLogger()
        self.saved_level = root_logger.level
        root_logger.addHandler(self.logs)
        root_logger.setLevel(logging.DEBUG)
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        self.release.set()
        self.settle()
        root_logger = logging.getLogger()
        root_logger.removeHandler(self.logs)
        root_logger.setLevel(self.saved_level)
        (
            server._model, server._cloning_enabled, server._cloning_status,
            server._cloning_problem, server._phrase_cache, server.API_KEY,
        ) = self.saved
        server._voice_states.clear()
        server._custom_voices.clear()
        server._custom_states.clear()
        self.tmp.cleanup()
        # Whatever the test did: the token is never logged.
        for line in self.logs.lines:
            self.assertNotIn(TOKEN, line)
            self.assertNotIn(OTHER_TOKEN, line)

    def download(self, path: str) -> Path:
        self.downloads.append(path)
        self.release.wait(10)
        if self.download_error is not None:
            raise self.download_error
        return self.weights

    def settle(self) -> None:
        """Wait for the background load started by the last PUT."""
        thread = server._cloning_thread
        if thread is not None:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "the background load did not finish")

    def put(self, token: str = TOKEN):
        return self.client.put("/v1/cloning", json={"hf_token": token})

    def state(self) -> dict:
        r = self.client.get("/v1/cloning")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)
        return r.json()

    def voice_on_disk(self) -> str:
        """A cloned voice saved earlier (kept on disk while cloning was off)."""
        voice_id = server.new_voice_id("Disk Voice")
        server.VOICE_DIR.mkdir(parents=True, exist_ok=True)
        state = FakeModel().get_state_for_audio_prompt(torch.zeros(1, 24000))
        server.export_model_state(state, server.VOICE_DIR / f"{voice_id}.safetensors")
        meta = {
            "id": voice_id, "name": "Disk Voice", "created_at": "2026-10-20T10:00:00+00:00",
            "sample_seconds": 6.0, "language": server.LANGUAGE,
            "consent": {"attested": True, "statement": server.DEFAULT_CONSENT_STATEMENT},
        }
        (server.VOICE_DIR / f"{voice_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        return voice_id

    def speak(self, voice: str):
        return self.client.post(
            "/v1/audio/speech", json={"model": "pocket-tts", "input": "Hello.", "voice": voice}
        )

    # ---------------------------------------------------------------- off

    def test_state_when_off(self) -> None:
        self.assertEqual(self.state(), {
            "state": "off", "message": server.CLONING_UNAVAILABLE,
            "terms_url": "https://huggingface.co/kyutai/pocket-tts", "token_source": None,
        })

    # ---------------------------------------------------------------- PUT: refused

    def test_put_refuses_what_is_not_a_token(self) -> None:
        for bad in (
            "not-a-token", "hf_short", "hf_" + "a" * 300, "hf_has spaces in the middle 0123",
            "api_org_0123456789abcdefghijklmnop", "hf_quote\"0123456789abcdefghij",
        ):
            r = self.put(bad)
            self.assertEqual(r.status_code, 422, bad)
            self.assertIsInstance(r.json()["detail"], str)
            self.assertNotIn(bad, r.text)
        for body in ({"token": TOKEN}, {"hf_token": 12345}, [TOKEN], TOKEN):
            r = self.client.put("/v1/cloning", json=body)
            self.assertEqual(r.status_code, 422, body)
            self.assertNotIn(TOKEN, r.text)
        r = self.client.put("/v1/cloning", content=TOKEN.encode())  # not JSON
        self.assertEqual(r.status_code, 422)
        self.assertNotIn(TOKEN, r.text)
        self.hf_api.assert_not_called()
        self.assertFalse(self.token_path.exists())

    def test_put_maps_hugging_face_answers_and_saves_nothing(self) -> None:
        cases = [
            ("whoami", hub_error(HfHubHTTPError, 401), 401, "Hugging Face did not accept"),
            ("auth_check", hub_error(GatedRepoError, 403), 403, server.CLONING_TERMS_URL),
            ("auth_check", hub_error(RepositoryNotFoundError, 404), 403, "accept the terms"),
            ("whoami", httpx.ConnectError("no route to host"), 502, "could not be reached"),
            ("auth_check", httpx.ReadTimeout("timed out"), 502, "could not be reached"),
            ("whoami", hub_error(HfHubHTTPError, 503), 502, "could not be reached"),
        ]
        for call, error, status, text in cases:
            with self.subTest(call=call, error=type(error).__name__, status=status):
                self.hub.reset_mock()
                getattr(self.hub, call).side_effect = error
                r = self.put()
                getattr(self.hub, call).side_effect = None
                self.assertEqual(r.status_code, status, r.text)
                self.assertIn(text, r.json()["detail"])
                self.assertNotIn(TOKEN, r.text)
                self.hf_api.assert_called_with(token=TOKEN)
                self.assertFalse(self.token_path.exists())
                self.assertEqual(self.state()["state"], "off")
        # The 403 tells the owner what to do.
        self.hub.auth_check.side_effect = hub_error(GatedRepoError, 403)
        detail = self.put().json()["detail"]
        self.assertIn("with the account that owns the token, then try again", detail)
        self.hub.auth_check.assert_called_with("kyutai/pocket-tts")
        self.assertEqual(self.downloads, [])

    def test_put_gives_up_when_hugging_face_hangs(self) -> None:
        hang = threading.Event()
        self.addCleanup(hang.set)
        self.hub.whoami.side_effect = lambda: hang.wait(10)
        with mock.patch.object(server, "HF_CHECK_SECONDS", 0.2):
            r = self.put()
        self.assertEqual(r.status_code, 502)
        self.assertFalse(self.token_path.exists())

    # ---------------------------------------------------------------- PUT: on

    def test_put_saves_the_token_and_turns_cloning_on(self) -> None:
        voice_id = self.voice_on_disk()
        server._voice_states["marius"] = object()  # made before the weights change
        old_cache = server._phrase_cache
        r = self.put()
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["state"], "enabling")
        self.assertEqual(r.json()["token_source"], "saved")
        self.assertNotIn(TOKEN, r.text)
        self.settle()

        state = self.state()
        self.assertEqual(
            (state["state"], state["message"], state["token_source"]), ("on", None, "saved")
        )
        # Saved where huggingface_hub reads it at the next start, for this user only.
        self.assertEqual(self.token_path.read_text(encoding="utf-8"), TOKEN)
        self.assertEqual(self.token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(get_token(), TOKEN)
        # The gated weights went into the model in use; states and kept audio were renewed.
        self.assertEqual(self.downloads, [OpenWeightsModel.config.weights_path])
        self.assertEqual(self.model.loaded, [["flow_lm.speaker_proj_weight"]])
        self.assertTrue(self.model.has_voice_cloning)
        self.assertTrue(callable(self.model.mimi.encode_to_latent))
        self.assertNotIn("marius", server._voice_states)
        self.assertIn(server.DEFAULT_VOICE, server._voice_states)
        self.assertIsNot(server._phrase_cache, old_cache)
        self.assertEqual(server._phrase_cache.stats()["pinned"], len(server.CACHED_PHRASES))
        # Cloned voices on disk are back.
        listed = self.client.get("/v1/voices")
        self.assertEqual(listed.status_code, 200)
        self.assertIn(voice_id, [v["id"] for v in listed.json()["data"]])
        self.assertEqual(self.speak(voice_id).status_code, 200)
        self.assertTrue(any("Voice cloning enabled" in line for line in self.logs.lines))

    def test_put_while_enabling_is_refused_and_built_in_voices_keep_working(self) -> None:
        self.release.clear()  # the weights "download" until released
        self.assertEqual(self.put().status_code, 202)
        again = self.put()
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"], server.CLONING_BUSY)
        state = self.state()
        self.assertEqual((state["state"], state["message"]), ("enabling", server.CLONING_ENABLING))
        voices = self.client.get("/v1/voices")
        self.assertEqual(voices.status_code, 501)
        self.assertEqual(voices.json()["detail"], server.CLONING_ENABLING)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        spoken = self.speak("alba")
        self.assertEqual((spoken.status_code, len(spoken.content)), (200, 4800))
        self.release.set()
        self.settle()
        self.assertEqual(self.state()["state"], "on")

    def test_failed_load_says_why_without_the_token(self) -> None:
        # Hugging Face refuses the download: an error text that holds the token is redacted.
        self.download_error = hub_error(GatedRepoError, 403, f"token {TOKEN} refused")
        self.assertEqual(self.put().status_code, 202)
        self.settle()
        state = self.state()
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["message"], server.CLONING_SAVED_TOKEN_REJECTED)
        self.assertFalse(server._cloning_enabled)
        voices = self.client.get("/v1/voices")
        self.assertEqual(voices.status_code, 501)
        self.assertEqual(voices.json()["detail"], server.CLONING_SAVED_TOKEN_REJECTED)
        logged = [line for line in self.logs.lines if "could not be turned on" in line]
        self.assertEqual(len(logged), 1)
        self.assertIn("GatedRepoError", logged[0])
        # Hugging Face unreachable during the download.
        self.download_error = httpx.ConnectError("no route to host")
        self.assertEqual(self.put().status_code, 202)
        self.settle()
        self.assertEqual(self.state()["message"], server.CLONING_DOWNLOAD_FAILED)
        self.assertEqual(self.model.loaded, [])

    # ---------------------------------------------------------------- DELETE

    def test_delete_removes_the_saved_token_and_turns_cloning_off(self) -> None:
        voice_id = self.voice_on_disk()
        self.assertEqual(self.put().status_code, 202)
        self.settle()
        self.assertEqual(self.client.get("/v1/voices").status_code, 200)

        r = self.client.delete("/v1/cloning")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(self.token_path.exists())
        self.assertIsNone(get_token())
        self.assertEqual(self.state(), {
            "state": "off", "message": server.CLONING_UNAVAILABLE,
            "terms_url": server.CLONING_TERMS_URL, "token_source": None,
        })
        for response in (
            self.client.get("/v1/voices"),
            self.client.delete(f"/v1/voices/{voice_id}"),
        ):
            self.assertEqual(response.status_code, 501)
        spoken = self.speak(voice_id)
        self.assertEqual(spoken.status_code, 422)
        self.assertIn("cloned voice", spoken.json()["detail"])
        self.assertEqual(self.speak("alba").status_code, 200)
        # Weights and cloned voices stay for next time.
        self.assertTrue(self.model.has_voice_cloning)
        self.assertTrue((server.VOICE_DIR / f"{voice_id}.safetensors").is_file())
        self.assertEqual(self.client.delete("/v1/cloning").status_code, 204)

        # On again: nothing to download, the voice is back.
        self.assertEqual(self.put().status_code, 202)
        self.settle()
        self.assertEqual(self.state()["state"], "on")
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.speak(voice_id).status_code, 200)

    def test_delete_during_enabling_wins(self) -> None:
        self.release.clear()
        self.assertEqual(self.put().status_code, 202)
        self.assertEqual(self.client.delete("/v1/cloning").status_code, 204)
        self.release.set()
        self.settle()
        self.assertEqual(self.state()["state"], "off")
        self.assertFalse(server._cloning_enabled)
        self.assertFalse(self.token_path.exists())

    def test_token_from_the_environment_is_managed_there(self) -> None:
        os.environ["HF_TOKEN"] = OTHER_TOKEN
        self.assertEqual(self.state()["token_source"], "environment")
        r = self.client.delete("/v1/cloning")
        self.assertEqual(r.status_code, 409)
        self.assertIn("POCKET_TTS_HF_TOKEN / HF_TOKEN", r.json()["detail"])
        r = self.put()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"], server.CLONING_ENV_PUT)
        self.assertNotIn(OTHER_TOKEN, r.text)
        self.hf_api.assert_not_called()
        self.assertFalse(self.token_path.exists())

    def test_api_key_protects_the_switch(self) -> None:
        server.API_KEY = "secret"
        self.assertEqual(self.client.get("/v1/cloning").status_code, 401)
        self.assertEqual(self.put().status_code, 401)
        self.assertEqual(self.client.delete("/v1/cloning").status_code, 401)
        self.hf_api.assert_not_called()

    # ---------------------------------------------------------------- startup

    def start(self, model: OpenWeightsModel) -> dict:
        loader = mock.Mock(**{"load_model.return_value": model})
        with mock.patch.object(server, "TTSModel", loader), TestClient(server.app) as client:
            return client.get("/v1/cloning").json()  # the lifespan ran

    def test_startup_with_a_saved_token(self) -> None:
        voice_id = self.voice_on_disk()
        server.save_token(TOKEN)
        state = self.start(OpenWeightsModel(cloning=True))
        self.assertEqual((state["state"], state["token_source"]), ("on", "saved"))
        self.assertTrue(server._cloning_enabled)
        self.assertIn(voice_id, server._custom_voices)

    def test_startup_states_without_cloning(self) -> None:
        self.assertEqual(self.start(OpenWeightsModel())["state"], "off")
        server.save_token(TOKEN)  # saved, but the gated weights did not load
        state = self.start(OpenWeightsModel())
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["message"], server.CLONING_SAVED_TOKEN_REJECTED)
        os.environ["HF_TOKEN"] = OTHER_TOKEN
        state = self.start(OpenWeightsModel())
        self.assertEqual((state["state"], state["token_source"]), ("failed", "environment"))
        self.assertEqual(state["message"], server.CLONING_TOKEN_REJECTED)
        state = self.start(OpenWeightsModel(cloning=True))
        self.assertEqual((state["state"], state["token_source"]), ("on", "environment"))


@unittest.skipUnless(
    os.environ.get("POCKET_TTS_MODEL_TESTS") == "1",
    "loads the real model (about 1 GB of RAM): set POCKET_TTS_MODEL_TESTS=1",
)
class BoundedEncoderTest(unittest.TestCase):
    """The bounded encoder gives the library's result, with the open (non-cloning) weights.

    Uses a synthetic signal (no person's voice). The encoder weights are what matters here;
    the gated weights are not needed to check that the two computations agree.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from pocket_tts import TTSModel

        cls.model = TTSModel.load_model(language=server.LANGUAGE)

    def signal(self, seconds: float) -> torch.Tensor:
        rate = self.model.sample_rate
        t = torch.arange(int(seconds * rate)) / rate
        noise = torch.randn(t.shape, generator=torch.Generator().manual_seed(1))
        wave_ = 0.2 * torch.sin(2 * torch.pi * 180 * t) * (1 + 0.5 * torch.sin(2 * torch.pi * 3 * t))
        return (wave_ + 0.02 * noise).view(1, 1, -1)

    def test_bounded_encoder_matches_library(self) -> None:
        mimi = self.model.mimi
        library = type(mimi).encode_to_latent
        for seconds in (9.7, 13.3):
            x = self.signal(seconds)
            with torch.no_grad():
                expected = library(mimi, x)
                actual = server.encode_to_latent_bounded(mimi, x)
            self.assertEqual(actual.shape, expected.shape)
            scale = float(expected.abs().max())
            diff = float((actual - expected).abs().max())
            self.assertLess(diff, 1e-4 * max(scale, 1.0), f"{seconds} s: max diff {diff}")


if __name__ == "__main__":
    unittest.main()
