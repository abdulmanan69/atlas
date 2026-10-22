"""Tests for the expressive speech server (Chatterbox-Turbo), with the model replaced by a fake:
no GPU, no download, no torch.

They check the sound tags (kept and written the model's way; any other bracketed text removed),
sentence splitting with tags, that each sentence is sent as soon as it is made and in order,
the order in which waiting sentences get the model, the phrase cache (hit, opener, miss, what is
kept, the memory bound), voices and cloning (consent, sample checks, limits, saving and reloading
a voice's conditioning, the /v1/cloning answers), the API key, and that logs never hold the
text. The streaming tests run the app on 127.0.0.1 (a free port) without its lifespan; every
other test calls it in-process.

Run in a small venv (fastapi, numpy, scipy, safetensors, soxr, httpx, python-multipart,
uvicorn; no torch) from the repository root:

  python -m unittest discover -s infra/docker/expressive-tts -t infra/docker/expressive-tts -v

or inside the image (the folder is mounted, not copied into it):

  docker run --rm --entrypoint python -v "$PWD/infra/docker/expressive-tts:/srv/tests:ro" \
    -w /srv atlas/expressive-tts:chatterbox-turbo-0.1.7 \
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

os.environ.setdefault("EXPRESSIVE_TTS_WARMUP", "0")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app as server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

MODEL = "chatterbox-turbo"


def conds_like(value: float, frames: int = 3) -> SimpleNamespace:
    """Shaped like chatterbox's Conditionals: T3Cond fields (two of them empty) and the
    decoder's reference dict (one empty entry)."""
    return SimpleNamespace(
        t3=SimpleNamespace(
            speaker_emb=np.full((1, 4), value, np.float32),
            clap_emb=None,
            cond_prompt_speech_tokens=np.arange(6, dtype=np.int64).reshape(1, 6),
            cond_prompt_speech_emb=None,
            emotion_adv=np.full((1, 1, 1), 0.5, np.float32),
        ),
        gen={
            "prompt_token": np.arange(frames, dtype=np.int64).reshape(1, frames),
            "prompt_token_len": np.array([frames], dtype=np.int64),
            "prompt_feat": np.full((1, 2 * frames, 80), value, np.float32),
            "prompt_feat_len": None,
            "embedding": np.full((1, 192), value, np.float32),
        },
    )


class FakeEngine:
    """Stands in for TurboEngine: records what it is asked and returns short audio whose level
    tells the calls apart (0.05 + 0.01 x call number)."""

    sample_rate = 24000
    device = "cpu"

    def __init__(self, seconds: float = 0.2, lead_seconds: float = 0.0) -> None:
        self.builtin = conds_like(0.1)
        self.seconds = seconds
        self.lead_seconds = lead_seconds
        self.texts: list[str] = []
        self.voices: list[object] = []
        self.prompts: list[np.ndarray] = []
        self.calls = 0
        self.fail_with: BaseException | None = None
        # (call number, event): that synthesis waits until the event is set.
        self.gate: tuple[int, threading.Event] | None = None
        self.started = threading.Event()  # set when the gated synthesis begins

    def level(self, call: int) -> float:
        return 0.05 + 0.01 * (call % 90)

    def synthesize(self, text: str, conds: object) -> np.ndarray:
        self.calls += 1
        call = self.calls
        self.texts.append(text)
        self.voices.append(conds)
        if self.fail_with is not None:
            raise self.fail_with
        if self.gate is not None and self.gate[0] == call:
            self.started.set()
            self.gate[1].wait(10)
        lead = np.zeros(int(self.sample_rate * self.lead_seconds), np.float32)
        body = np.full(int(self.sample_rate * self.seconds), self.level(call), np.float32)
        return np.concatenate([lead, body])

    def conditionals(self, audio: np.ndarray) -> SimpleNamespace:
        self.prompts.append(audio)
        return conds_like(0.5, frames=max(1, audio.shape[0] // 960))

    def make_conds(self, t3: dict, gen: dict) -> SimpleNamespace:
        return SimpleNamespace(t3=SimpleNamespace(**t3), gen=dict(gen))

    def added_tokens(self) -> set[str]:
        tags = {f"[{t}]" for t in server.DEFAULT_SOUND_TAGS}
        return tags | {"[happy]", "[angry]"}


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
    """Every record of every logger, formatted with tracebacks."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


SAVED = (
    "_engine", "_phrase_cache", "API_KEY", "VOICE_DIR", "MAX_CUSTOM_VOICES",
    "CUSTOM_VOICE_CACHE", "FIRST_SENTENCE_CHARS", "MAX_SENTENCE_CHARS", "WARMUP",
    "DEFAULT_VOICE", "load_engine", "PHRASE_CACHE_ANY_VOICE", "CACHED_PHRASES",
)


class ServerTest(unittest.TestCase):
    engine_kwargs: dict = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: getattr(server, name) for name in SAVED}
        self.saved_tags = list(server.SOUND_TAGS)
        self.saved_gpu = server._gpu
        self.engine = FakeEngine(**self.engine_kwargs)
        server._engine = self.engine
        server._gpu = server.GpuTurns()
        server._phrase_cache = server.PhraseCache(1024 * 1024, 32)
        server.API_KEY = None
        server.VOICE_DIR = Path(self.tmp.name) / "voices"
        server.MAX_CUSTOM_VOICES = 50
        # Off: a voice's first request would otherwise start making its phrases in the
        # background (tested on its own below).
        server.PHRASE_CACHE_ANY_VOICE = False
        self.saved_made = set(server._phrase_voices_made)
        server._phrase_voices_made.clear()
        server._custom_voices.clear()
        server._custom_conds.clear()
        self.logs = LogCapture()
        root = logging.getLogger()
        self.saved_level = root.level
        root.addHandler(self.logs)
        root.setLevel(logging.DEBUG)
        self.client = TestClient(server.app)  # no `with`: the real model is never loaded

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            setattr(server, name, value)
        server.SOUND_TAGS[:] = self.saved_tags
        server._gpu = self.saved_gpu
        for thread in server._phrase_threads:
            thread.join(timeout=10)
        server._phrase_voices_made.clear()
        server._phrase_voices_made.update(self.saved_made)
        server._custom_voices.clear()
        server._custom_conds.clear()
        root = logging.getLogger()
        root.removeHandler(self.logs)
        root.setLevel(self.saved_level)
        self.tmp.cleanup()

    def speak(self, text: str, voice: str = "default", fmt: str = "pcm", **extra):
        return self.client.post(
            "/v1/audio/speech",
            json={"model": MODEL, "input": text, "voice": voice, "response_format": fmt, **extra},
        )

    def create(self, audio: bytes, **fields: str):
        data = {"name": "Sam Example", "consent": "true", **fields}
        return self.client.post(
            "/v1/voices", data=data, files={"audio": ("sample.wav", audio, "audio/wav")}
        )


# ---------------------------------------------------------------- sound tags


class SoundTagTest(ServerTest):
    def test_supported_tags_are_kept_and_written_the_models_way(self) -> None:
        text, kept, removed = server.clean_text(
            "Wow! [Gasp] that's [LAUGHS] great <laugh/> and [ clear_throat ] [clears throat]"
        )
        self.assertEqual(
            text, "Wow! [gasp] that's [laugh] great [laugh] and [clear throat] [clear throat]"
        )
        self.assertEqual((kept, removed), (5, 0))
        # The forms the Pocket adapter removes are this voice's sounds.
        forms = {
            "[laughed]": "laugh", "[laughter]": "laugh", "[chuckled]": "chuckle",
            "[Clears his throat]": "clear throat", "[clearing her throat]": "clear throat",
            "[sniffling]": "sniff", "[shhh]": "shush", "[groaning]": "groan", "[sighs]": "sigh",
        }
        for written, tag in forms.items():
            self.assertEqual(server.clean_text(f"Okay {written} then.")[0], f"Okay [{tag}] then.", written)
        self.assertEqual(server.clean_text("Okay [laughable] then.")[0], "Okay then.")

    def test_emoji_are_not_read_out(self) -> None:
        self.assertEqual(server.clean_text("Have a great day! \U0001F697\U0001F4A8")[0], "Have a great day!")
        self.assertEqual(server.clean_text("Done ✅ thanks")[0], "Done thanks")
        self.assertEqual(self.speak("\U0001F600").content, b"")
        self.assertEqual(self.engine.calls, 0)

    def test_any_other_bracketed_text_is_removed(self) -> None:
        text, kept, removed = server.clean_text(
            'Sure [pause], I can [smiles warmly] help.<break time="1s"/> Bye [whispering]. '
            "</laugh>[] [happy]Okay"
        )
        self.assertEqual(text, "Sure, I can help. Bye. Okay")
        self.assertEqual((kept, removed), (0, 7))
        # Stray square brackets go too; a comparison stays as written.
        self.assertEqual(server.clean_text("a [b c] d] e [f")[0], "a d e f")
        self.assertEqual(server.clean_text("if a < b and c > d")[0], "if a < b and c > d")

    def test_only_configured_tags_are_performed(self) -> None:
        server.SOUND_TAGS[:] = ["laugh"]
        self.assertEqual(server.clean_text("[sigh] Okay [laugh].")[0], "Okay [laugh].")

    def test_tags_the_tokenizer_lacks_are_dropped_at_startup(self) -> None:
        server.SOUND_TAGS[:] = ["laugh", "giggle"]
        server.check_sound_tags({"[laugh]", "[sigh]"})
        self.assertEqual(server.SOUND_TAGS, ["laugh"])
        self.assertTrue(any("[giggle] is not a token" in line for line in self.logs.lines))
        self.assertEqual(server.clean_text("Ha [giggle] ha.")[0], "Ha ha.")

    def test_sound_tags_endpoint_and_models_list_them(self) -> None:
        tags = self.client.get("/v1/audio/sound-tags").json()["tags"]
        self.assertEqual(tags, [
            "[laugh]", "[chuckle]", "[sigh]", "[gasp]", "[cough]", "[clear throat]", "[groan]",
            "[sniff]", "[shush]",
        ])
        model = self.client.get("/v1/models").json()["data"][0]
        self.assertEqual((model["id"], model["sample_rate"], model["sound_tags"]), (MODEL, 24000, tags))

    def test_unknown_tags_never_reach_the_model(self) -> None:
        r = self.speak("Sure [pause] thing [Laughs].")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.engine.texts, ["Sure thing [laugh]."])

    def test_nothing_left_to_say_gives_empty_audio(self) -> None:
        for text in ("[pause]", "<break/> ...", "[smiles]."):
            r = self.speak(text)
            self.assertEqual((r.status_code, r.content), (200, b""), text)
        self.assertEqual(len(self.speak("[pause]", fmt="wav").content), 44)
        self.assertEqual(self.engine.calls, 0)
        # A sound alone is something to say.
        self.speak("[laugh]")
        self.assertEqual(self.engine.texts, ["[laugh]"])


# ---------------------------------------------------------------- sentences


class SentenceTest(ServerTest):
    def setUp(self) -> None:
        super().setUp()
        server.FIRST_SENTENCE_CHARS = 0  # tested on its own below

    def test_a_tag_after_a_sentence_opens_the_next(self) -> None:
        self.assertEqual(
            server.split_sentences(
                "Hi there! This is Sarah from Demo Realty. [laugh] Sorry, I'm in a great mood "
                "today. Is now a good time for two quick questions?"
            ),
            [
                "Hi there!", "This is Sarah from Demo Realty.",
                "[laugh] Sorry, I'm in a great mood today.",
                "Is now a good time for two quick questions?",
            ],
        )
        self.assertEqual(
            server.split_sentences("Wow! [gasp] Four hundred thousand? That opens up options."),
            ["Wow!", "[gasp] Four hundred thousand?", "That opens up options."],
        )

    def test_tags_at_the_end_or_alone_stay_with_a_sentence(self) -> None:
        self.assertEqual(
            server.split_sentences("Have a wonderful day! [laugh]"), ["Have a wonderful day! [laugh]"]
        )
        self.assertEqual(server.split_sentences("Okay. [sigh]. Fine."), ["Okay.", "[sigh]. Fine."])
        self.assertEqual(server.split_sentences("Right. [sigh] [cough]"), ["Right. [sigh] [cough]"])
        self.assertEqual(server.split_sentences("[laugh]"), ["[laugh]"])
        self.assertEqual(
            server.split_sentences("[chuckle] Fair enough, I'd want to think it over too."),
            ["[chuckle] Fair enough, I'd want to think it over too."],
        )

    def test_abbreviations_numbers_and_lowercase_do_not_end_a_sentence(self) -> None:
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

    def test_a_long_first_sentence_is_cut_after_its_first_clause(self) -> None:
        server.FIRST_SENTENCE_CHARS = 60
        line = "Hmm, let me think about that for a second... okay, yes, three months works perfectly."
        self.assertEqual(
            server.split_sentences(line),
            ["Hmm, let me think about that for a second...", "okay, yes, three months works perfectly."],
        )
        # Short first sentences and later sentences are left whole.
        line = (
            "Oh no, I'm really sorry to hear that. [sigh] Well, that sounds really stressful, "
            "and I would like to help with it today."
        )
        self.assertEqual(
            server.split_sentences(line),
            [
                "Oh no, I'm really sorry to hear that.",
                "[sigh] Well, that sounds really stressful, and I would like to help with it today.",
            ],
        )
        # No clause mark after the first 20 characters: left whole.
        line = "Uh, thank you so much for calling us back about the apartment on the corner."
        self.assertEqual(server.split_sentences(line), [line])

    def test_long_sentences_are_cut_at_clauses_then_words_never_inside_a_tag(self) -> None:
        server.MAX_SENTENCE_CHARS = 40
        words = " ".join(f"word{i}" for i in range(30))
        parts = server.split_sentences(words)
        self.assertTrue(all(len(p) <= 40 for p in parts), parts)
        self.assertEqual(" ".join(parts), words)
        tagged = "aaaa bbbb cccc dddd eeee ffff gggg [clear throat] hhhh iiii jjjj kkkk"
        parts = server.split_sentences(tagged)
        self.assertIn("[clear throat]", " ".join(parts))
        self.assertTrue(all(p.count("[") == p.count("]") for p in parts), parts)
        clauses = "one two three four five, six seven eight nine ten eleven twelve thirteen"
        self.assertEqual(server.split_sentences(clauses)[0], "one two three four five,")


# ---------------------------------------------------------------- speech


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
        self.assertEqual(int(fmt[7]), 16)
        self.assertEqual(len(wav.content), 44 + 9600)

    def test_request_checks(self) -> None:
        self.assertEqual(
            self.client.post("/v1/audio/speech", json={
                "model": "pocket-tts", "input": "Hi.", "voice": "default"}).status_code,
            404,
        )
        self.assertEqual(self.speak("Hi.", sample_rate=16000).status_code, 422)
        self.assertEqual(self.speak("Hi.", sample_rate=24000).status_code, 200)
        self.assertEqual(self.speak("Hi.", fmt="mp3").status_code, 422)
        self.assertEqual(self.speak("Hi.", voice="alba").status_code, 422)
        self.assertEqual(self.speak("Hi.", voice="custom-x-0123456789abcdef").status_code, 422)
        self.assertEqual(self.speak("   ").status_code, 400)
        self.assertEqual(self.speak("Hi.", voice="alloy").status_code, 200)  # OpenAI name
        self.assertEqual(self.speak("Hi.", speed=1.5).status_code, 200)  # no speed control
        self.assertIs(self.engine.voices[-1], self.engine.builtin)

    def test_sentences_are_spoken_in_order(self) -> None:
        r = self.speak("First one here. [sigh] Second one here! Third one?")
        self.assertEqual(
            self.engine.texts, ["First one here.", "[sigh] Second one here!", "Third one?"]
        )
        self.assertEqual(r.headers["x-phrase-cache"], "miss")
        self.assertEqual(levels(r.content), [pcm_level(self.engine, n) for n in (1, 2, 3)])

    def test_a_failed_synthesis_ends_the_audio_and_logs_no_text(self) -> None:
        self.engine.fail_with = ValueError("Zebra quartz is not allowed")
        r = self.speak("Zebra quartz says hello. And more.")
        self.assertEqual((r.status_code, r.content), (200, b""))
        self.assertTrue(any("Synthesis failed: ValueError" in line for line in self.logs.lines))

    def test_logs_never_hold_the_text(self) -> None:
        secret = "Zebra quartz forty two"
        self.speak(f"{secret}. [laugh] Another {secret} sentence. [pause]")
        self.speak(f"{secret}.", fmt="wav")
        self.speak(f"{secret}.")  # a kept phrase now
        self.engine.fail_with = ValueError(secret)
        self.speak(f"Oh {secret}!")
        self.assertTrue(any("chars=" in line for line in self.logs.lines))
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
        first = response.read(9600)  # all of the first sentence's audio
        self.assertTrue(self.engine.started.wait(5))
        self.assertEqual(levels(first), [pcm_level(self.engine, 1)])
        self.assertEqual(self.engine.calls, 2)  # the second is still being made
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
        self.assertTrue(self.engine.started.wait(5))  # A's first sentence holds the model
        second = threading.Thread(target=call, args=("b", "B one here."))
        second.start()
        deadline = time.monotonic() + 5
        while server._gpu.waiting() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server._gpu.waiting(), 1)  # B waits for its turn
        self.release.set()
        first.join(10)
        second.join(10)
        # A plain lock would usually give A's next sentence the model first.
        self.assertEqual(
            self.engine.texts, ["A one here.", "B one here.", "A two here.", "A three here."]
        )
        self.assertEqual(levels(results["b"]), [pcm_level(self.engine, 2)])


class GpuTurnsTest(unittest.TestCase):
    def test_waiting_turns_go_earliest_deadline_first(self) -> None:
        turns = server.GpuTurns()
        order: list[float] = []

        def wait_for(deadline: float) -> None:
            with turns.turn(deadline):
                order.append(deadline)

        with turns.turn(0.0):
            threads = [threading.Thread(target=wait_for, args=(d,)) for d in (5.0, 1.0, 3.0)]
            for t in threads:
                t.start()
            deadline = time.monotonic() + 5
            while turns.waiting() < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(turns.waiting(), 3)
        for t in threads:
            t.join(5)
        self.assertEqual(order, [1.0, 3.0, 5.0])
        self.assertEqual(turns.waiting(), 0)


# ---------------------------------------------------------------- phrase cache


class PhraseCacheTest(ServerTest):
    def test_normalisation(self) -> None:
        self.assertEqual(server.normalize_phrase("  Got  it. "), "got it.")
        self.assertEqual(server.normalize_phrase("That’s right."), "that's right.")
        self.assertNotEqual(server.normalize_phrase("Great."), server.normalize_phrase("Great!"))

    def test_prewarmed_phrase_is_replayed_without_the_model(self) -> None:
        server.prewarm_phrases(["Got it."], ["default"])
        self.assertEqual(self.engine.calls, 1)
        first = self.speak("got it.")
        again = self.speak("Got  it.", fmt="wav")
        self.assertEqual(self.engine.calls, 1)
        self.assertEqual(first.headers["x-phrase-cache"], "hit")
        self.assertEqual(len(first.content), 9600)
        self.assertEqual(again.content[44:], first.content)
        self.assertEqual(again.content[:4], b"RIFF")
        self.assertEqual(server._phrase_cache.stats()["pinned"], 1)

    def test_cache_is_per_voice(self) -> None:
        server.prewarm_phrases(["Okay."], ["default"])
        voice_id = self.create(wav_bytes(6)).json()["id"]
        r = self.speak("Okay.", voice=voice_id)
        self.assertEqual(r.headers["x-phrase-cache"], "miss")
        self.assertEqual(self.engine.calls, 2)

    def test_short_texts_are_kept_once_spoken_long_ones_never(self) -> None:
        self.assertEqual(self.speak("Understood.").headers["x-phrase-cache"], "miss")
        self.assertEqual(self.speak("Understood.").headers["x-phrase-cache"], "hit")
        long_text = "This sentence is longer than thirty-two characters."
        self.assertEqual(self.speak(long_text).headers["x-phrase-cache"], "miss")
        self.assertEqual(self.speak(long_text).headers["x-phrase-cache"], "miss")
        self.assertEqual(self.engine.calls, 3)

    def test_short_sentences_of_a_longer_reply_are_kept_and_reused(self) -> None:
        reply = "Thanks. This sentence is too long to keep in the cache."
        self.speak(reply)
        self.assertEqual(self.engine.calls, 2)
        again = self.speak(reply)
        self.assertEqual(self.engine.texts[2:], ["This sentence is too long to keep in the cache."])
        self.assertEqual(levels(again.content), [pcm_level(self.engine, 1), pcm_level(self.engine, 3)])
        self.assertEqual(self.speak("Thanks.").headers["x-phrase-cache"], "hit")

    def test_memory_bound_evicts_unpinned_first(self) -> None:
        chunk = b"\x01" * 1000
        cache = server.PhraseCache(3000, 32)
        self.assertTrue(cache.put("default", "Pinned.", [chunk], pin=True))
        self.assertTrue(cache.put("default", "One.", [chunk]))
        self.assertTrue(cache.put("default", "Two.", [chunk]))
        self.assertIsNotNone(cache.get("default", "One."))  # Two. is now least recently used
        self.assertTrue(cache.put("default", "Three.", [chunk]))
        self.assertIsNone(cache.get("default", "Two."))
        self.assertIsNotNone(cache.get("default", "Pinned."))
        self.assertLessEqual(cache.stats()["bytes"], 3000)
        self.assertFalse(cache.put("default", "Too big.", [chunk * 4]))
        cache.drop_voice("default")
        self.assertEqual(cache.stats()["entries"], 0)

    def test_kept_audio_starts_just_before_the_first_sound(self) -> None:
        rate = 24000
        silence = np.zeros(4800, dtype=np.int16).tobytes()  # 100 ms
        tone = (np.full(2400, 0.2) * 32767).astype(np.int16).tobytes()
        trimmed = server.trim_leading_silence([silence] + [tone] * 6, rate)
        joined = b"".join(trimmed)
        lead = rate * server.KEEP_LEAD_MS // 1000 * 2
        self.assertEqual(len(joined), lead + 6 * len(tone))
        self.assertEqual(joined[:lead], bytes(lead))
        self.assertEqual(joined[lead:], tone * 6)
        self.assertEqual(server.trim_leading_silence([silence], rate), [silence])

    def test_kept_opener_starts_a_longer_sentence(self) -> None:
        server.prewarm_phrases(["Got it.", "Thanks."], ["default"])
        opener = server._phrase_cache.get("default", "Got it.")
        r = self.speak("Got it, perfect timing then.")
        self.assertEqual(r.headers["x-phrase-cache"], "opener")
        self.assertEqual(self.engine.texts[-1], "Perfect timing then.")
        self.assertTrue(r.content.startswith(b"".join(opener)))
        self.assertGreater(len(r.content), len(b"".join(opener)))
        calls = self.engine.calls
        r = self.speak("Got it! Thanks.")  # opener and rest both kept: nothing to make
        self.assertEqual((r.headers["x-phrase-cache"], self.engine.calls), ("opener", calls))
        for text in ("Got it right this time.", "Gotta go now.", "I got it, thanks."):
            self.assertEqual(self.speak(text).headers["x-phrase-cache"], "miss", text)

    def test_opener_then_a_sound(self) -> None:
        server.prewarm_phrases(["Got it."], ["default"])
        r = self.speak("Got it! [laugh] that's great news.")
        self.assertEqual(r.headers["x-phrase-cache"], "opener")
        self.assertEqual(self.engine.texts[-1], "[laugh] That's great news.")

    def test_bare_opener_with_other_end_mark_is_the_kept_phrase(self) -> None:
        server.prewarm_phrases(["Great."], ["default"])
        calls = self.engine.calls
        r = self.speak("Great!")
        self.assertEqual((r.headers["x-phrase-cache"], self.engine.calls), ("opener", calls))
        self.assertEqual(r.content, b"".join(server._phrase_cache.get("default", "Great.")))
        self.assertEqual(self.speak("Great.Really").headers["x-phrase-cache"], "miss")

    def test_longest_opener_wins_and_apostrophes_match(self) -> None:
        server.prewarm_phrases(["Sure.", "Sure thing.", "You're welcome."], ["default"])
        self.speak("Sure thing, see you then.")
        self.assertEqual(self.engine.texts[-1], "See you then.")
        self.speak("You’re welcome, Sarah.")
        self.assertEqual(self.engine.texts[-1], "Sarah.")

    def test_a_new_voice_gets_the_kept_phrases_in_the_background(self) -> None:
        voice_id = self.create(wav_bytes(6)).json()["id"]
        server.PHRASE_CACHE_ANY_VOICE = True
        server.CACHED_PHRASES = ["Oh nice!"]
        turns = []
        real_turn = server._gpu.turn

        def recording_turn(deadline):  # when each synthesis asked for the model
            turns.append(deadline - time.monotonic())
            return real_turn(deadline)

        with mock.patch.object(server._gpu, "turn", recording_turn):
            self.assertEqual(self.speak("Hello there.", voice=voice_id).status_code, 200)
            for thread in server._phrase_threads:
                thread.join(timeout=5)
        self.assertIn(voice_id, server._phrase_voices_made)
        # The reply that asked is made first; the background phrase then waits like audio 10 s
        # from running out.
        self.assertEqual([round(t) for t in turns], [0, 10])
        self.assertEqual(self.engine.texts, ["Hello there.", "Oh nice!"])
        r = self.speak("Oh nice, a Civic!", voice=voice_id)
        self.assertEqual(r.headers["x-phrase-cache"], "opener")
        calls = self.engine.calls
        self.speak("Second request.", voice=voice_id)  # no second batch for the same voice
        for thread in server._phrase_threads:
            thread.join(timeout=5)
        self.assertEqual(self.engine.calls, calls + 1)
        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 204)
        self.assertNotIn(voice_id, server._phrase_voices_made)

    def test_stats_endpoint_gives_counts_only(self) -> None:
        self.speak("Sure.")
        self.speak("Sure.")
        stats = self.client.get("/v1/audio/phrase-cache").json()
        self.assertEqual((stats["hits"], stats["misses"], stats["entries"]), (1, 1, 1))
        self.assertNotIn("sure", json.dumps(stats).lower())


class OpenerLeadTest(ServerTest):
    engine_kwargs = {"lead_seconds": 0.1}

    def test_after_a_kept_opener_the_rest_starts_at_its_first_sound(self) -> None:
        server.prewarm_phrases(["Got it."], ["default"])
        head = b"".join(server._phrase_cache.get("default", "Got it."))
        lead = 24000 * server.KEEP_LEAD_MS // 1000 * 2
        self.assertEqual(len(head), lead + 9600)  # 100 ms of lead-in trimmed to 30 ms
        r = self.speak("Got it, perfect timing then.")
        rest = r.content[len(head):]
        self.assertEqual(len(rest), lead + 9600)
        self.assertEqual(rest[:lead], bytes(lead))
        self.assertNotEqual(rest[lead : lead + 2], b"\x00\x00")


# ---------------------------------------------------------------- voices and cloning


# Shaped like a Hugging Face user access token; not one.
TOKEN = "hf_TestOnlyNotARealToken0123456789ab"  # noqa: S105


class VoicesTest(ServerTest):
    def test_built_in_voice(self) -> None:
        voices = self.client.get("/v1/audio/voices").json()["data"]
        self.assertEqual(voices, [{"id": "default", "license": "MIT", "default": True}])
        listed = self.client.get("/v1/voices").json()["data"]
        self.assertEqual(listed, [{
            "id": "default", "name": "default", "type": "builtin", "license": "MIT", "default": True,
        }])

    def test_consent_is_required(self) -> None:
        for value in ("", "false", "yes please"):
            r = self.create(wav_bytes(6), consent=value)
            self.assertEqual(r.status_code, 422)
            self.assertIn("permission", r.json()["detail"])
        self.assertEqual(self.engine.prompts, [])

    def test_only_wav_is_accepted(self) -> None:
        fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + os.urandom(2048)
        self.assertEqual(self.create(fake_mp3).status_code, 415)
        self.assertEqual(self.create(b"#!/bin/sh\nrm -rf /\n" * 10).status_code, 415)
        broken = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
        self.assertEqual(self.create(broken).status_code, 422)

    def test_duration_rate_and_silence_limits(self) -> None:
        self.assertIn("between 5 and 30", self.create(wav_bytes(3)).json()["detail"])
        self.assertIn("between 5 and 30", self.create(wav_bytes(31, rate=16000)).json()["detail"])
        self.assertIn("sample rate", self.create(wav_bytes(6, rate=12345)).json()["detail"])
        silent = io.BytesIO()
        scipy.io.wavfile.write(silent, 16000, np.zeros(16000 * 6, dtype=np.int16))
        self.assertIn("silent", self.create(silent.getvalue()).json()["detail"])
        self.assertEqual(self.engine.prompts, [])

    def test_size_limit(self) -> None:
        big = wav_bytes(6) + b"\x00" * (10 * 1024 * 1024)
        self.assertEqual(self.create(big).status_code, 413)

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

    def test_clone_speak_reload_and_delete(self) -> None:
        r = self.create(wav_bytes(6.5), consent_statement="Signed form on file, 2026-10-22.")
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        voice_id = body["id"]
        self.assertRegex(voice_id, r"^custom-sam-example-[0-9a-f]{16}$")
        self.assertEqual((body["name"], body["sample_seconds"]), ("Sam Example", 6.5))

        # Mono, resampled from 44.1 kHz to the model's 24 kHz.
        prompt = self.engine.prompts[-1]
        self.assertEqual((prompt.shape, prompt.dtype), ((int(6.5 * 24000),), np.float32))

        # Only the conditioning and metadata are kept: never the uploaded audio.
        files = sorted(p.name for p in server.VOICE_DIR.iterdir())
        self.assertEqual(files, [f"{voice_id}.json", f"{voice_id}.safetensors"])
        meta = json.loads((server.VOICE_DIR / f"{voice_id}.json").read_text())
        self.assertEqual(meta["consent"], {"attested": True, "statement": "Signed form on file, 2026-10-22."})
        self.assertEqual(meta["model"], "chatterbox-turbo")

        listed = self.client.get("/v1/voices").json()["data"]
        self.assertEqual([v["id"] for v in listed if v["type"] == "custom"], [voice_id])

        speech = self.speak("Hi.", voice=voice_id)
        self.assertEqual(speech.status_code, 200)
        made = self.engine.voices[-1]
        self.assertEqual(float(made.t3.speaker_emb[0, 0]), 0.5)

        # A restart reads the index from disk and loads the saved conditioning, empty parts too.
        server._custom_voices.clear()
        server._custom_conds.clear()
        server.load_voice_index()
        self.assertIn(voice_id, server._custom_voices)
        loaded = server._voice_conds(voice_id)
        for key, value in vars(made.t3).items():
            if value is None:
                self.assertIsNone(getattr(loaded.t3, key), key)
            else:
                np.testing.assert_array_equal(getattr(loaded.t3, key), value)
        self.assertEqual(set(loaded.gen), set(made.gen))
        self.assertIsNone(loaded.gen["prompt_feat_len"])
        np.testing.assert_array_equal(loaded.gen["prompt_feat"], made.gen["prompt_feat"])
        self.assertEqual(loaded.gen["prompt_token"].dtype, np.int64)

        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 204)
        self.assertEqual(list(server.VOICE_DIR.iterdir()), [])
        self.assertEqual(self.client.delete(f"/v1/voices/{voice_id}").status_code, 404)
        self.assertEqual(self.speak("Hi.", voice=voice_id).status_code, 422)

    def test_voices_of_another_model_or_unreadable_ones_are_skipped(self) -> None:
        good = self.create(wav_bytes(6), name="Good").json()["id"]
        other = self.create(wav_bytes(6), name="Other").json()["id"]
        broken = self.create(wav_bytes(6), name="Broken").json()["id"]
        meta_path = server.VOICE_DIR / f"{other}.json"
        meta = json.loads(meta_path.read_text())
        meta["model"] = "pocket-tts"
        meta_path.write_text(json.dumps(meta))
        (server.VOICE_DIR / f"{broken}.safetensors").write_bytes(b"not a safetensors file")
        server.load_voice_index()
        self.assertEqual(set(server._custom_voices), {good, broken})
        server.preload_custom_conds()
        self.assertEqual(set(server._custom_voices), {good})
        self.assertTrue(any(f"Cloned voice {broken} could not be loaded" in line for line in self.logs.lines))

    def test_ids_are_random_and_paths_cannot_escape(self) -> None:
        ids = {server.new_voice_id("Sam") for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(server.CUSTOM_ID.match(i) for i in ids))
        self.assertRegex(server.new_voice_id("Ünïcôdé  ✨"), r"^custom-unicode-[0-9a-f]{16}$")
        for bad in ("custom-..-0123456789abcdef", "custom-a-0123", "../etc/passwd"):
            self.assertFalse(server.CUSTOM_ID.match(bad))
        self.assertEqual(self.client.delete("/v1/voices/custom-x-..").status_code, 404)

    def test_lru_keeps_a_bounded_number_of_voices_in_memory(self) -> None:
        server.CUSTOM_VOICE_CACHE = 2
        made = [self.create(wav_bytes(5.5), name=f"V{i}").json()["id"] for i in range(3)]
        self.assertEqual(list(server._custom_conds), made[1:])
        server._voice_conds(made[0])  # loaded from disk again, evicts the oldest
        self.assertEqual(list(server._custom_conds), [made[2], made[0]])

    def test_voice_limit(self) -> None:
        server.MAX_CUSTOM_VOICES = 1
        self.assertEqual(self.create(wav_bytes(6)).status_code, 201)
        self.assertEqual(self.create(wav_bytes(6)).status_code, 409)

    def test_cloning_is_always_on_and_a_token_is_never_read(self) -> None:
        state = self.client.get("/v1/cloning").json()
        self.assertEqual(state, {
            "state": "on", "message": None,
            "terms_url": "https://huggingface.co/ResembleAI/chatterbox-turbo", "token_source": None,
        })
        r = self.client.put("/v1/cloning", json={"hf_token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "on")
        self.assertIn("needs no Hugging Face token", r.json()["message"])
        self.assertNotIn(TOKEN, r.text)
        r = self.client.delete("/v1/cloning")
        self.assertEqual(r.status_code, 409)
        self.assertIn("cannot be turned off", r.json()["detail"])
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])  # nothing saved
        for line in self.logs.lines:
            self.assertNotIn(TOKEN, line)

    def test_operator_can_turn_cloning_off(self) -> None:
        voice_id = self.create(wav_bytes(6)).json()["id"]
        server.MAX_CUSTOM_VOICES = 0
        state = self.client.get("/v1/cloning").json()
        self.assertEqual((state["state"], state["message"]), ("off", server.CLONING_TURNED_OFF))
        for response in (
            self.client.get("/v1/voices"),
            self.create(wav_bytes(6)),
            self.client.delete(f"/v1/voices/{voice_id}"),
        ):
            self.assertEqual(response.status_code, 501)
            self.assertEqual(response.json()["detail"], server.CLONING_TURNED_OFF)
        spoken = self.speak("Hi.", voice=voice_id)
        self.assertEqual(spoken.status_code, 422)
        self.assertIn("cloned voice", spoken.json()["detail"])
        self.assertEqual(self.client.put("/v1/cloning", json={}).status_code, 409)
        self.assertEqual(self.client.delete("/v1/cloning").status_code, 204)
        self.assertEqual(self.speak("Hi.").status_code, 200)

    def test_api_key_protects_everything_but_health(self) -> None:
        server.API_KEY = "secret"
        calls = [
            ("GET", "/v1/models"), ("GET", "/v1/audio/voices"), ("GET", "/v1/audio/sound-tags"),
            ("GET", "/v1/audio/phrase-cache"), ("GET", "/v1/voices"), ("GET", "/v1/cloning"),
            ("PUT", "/v1/cloning"), ("DELETE", "/v1/cloning"),
            ("DELETE", "/v1/voices/custom-x-0123456789abcdef"),
        ]
        for method, path in calls:
            self.assertEqual(self.client.request(method, path).status_code, 401, path)
            wrong = self.client.request(method, path, headers={"Authorization": "Bearer nope"})
            self.assertEqual(wrong.status_code, 401, path)
        self.assertEqual(self.speak("Hi.").status_code, 401)
        self.assertEqual(self.create(wav_bytes(6)).status_code, 401)
        self.assertEqual(self.engine.calls, 0)
        ok = {"Authorization": "Bearer secret"}
        self.assertEqual(self.client.get("/v1/voices", headers=ok).status_code, 200)
        speech = self.client.post(
            "/v1/audio/speech", headers=ok, json={"model": MODEL, "input": "Hi.", "voice": "default"}
        )
        self.assertEqual(speech.status_code, 200)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "device": "cpu"})


# ---------------------------------------------------------------- startup


class StartupTest(ServerTest):
    def start(self, engine: FakeEngine):
        server._engine = None
        with mock.patch.object(server, "load_engine", return_value=engine), TestClient(server.app) as client:
            return client.get("/health").json(), client.get("/v1/audio/sound-tags").json()["tags"]

    def test_startup_warms_up_keeps_the_openers_and_checks_the_tags(self) -> None:
        server.WARMUP = True
        engine = FakeEngine()
        engine.added_tokens = lambda: {"[laugh]", "[sigh]"}  # type: ignore[method-assign]
        health, tags = self.start(engine)
        self.assertEqual(health, {"status": "ok", "device": "cpu"})
        self.assertEqual(tags, ["[laugh]", "[sigh]"])
        self.assertEqual(engine.texts[0], "Hello there.")
        self.assertEqual(engine.calls, 1 + len(server.CACHED_PHRASES))
        self.assertEqual(server._phrase_cache.stats()["pinned"], len(server.CACHED_PHRASES))

    def test_startup_loads_cloned_voices_from_disk(self) -> None:
        voice_id = self.create(wav_bytes(6)).json()["id"]
        server._custom_voices.clear()
        server._custom_conds.clear()
        self.start(FakeEngine())
        self.assertIn(voice_id, server._custom_voices)
        self.assertIn(voice_id, server._custom_conds)

    def test_default_voice_must_be_built_in(self) -> None:
        server.DEFAULT_VOICE = "alba"
        with self.assertRaises(RuntimeError):
            self.start(FakeEngine())


if __name__ == "__main__":
    unittest.main()
