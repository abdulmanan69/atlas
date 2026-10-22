"""Tests of the AI gateway's health service against fake model servers (standard library).

    python -m unittest -v test_app          (in this folder; Python 3.11 or newer)
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import app

# What the real servers answered on 2026-10-22 (ollama 0.40.6, speaches 0.8.3, the Pocket
# TTS adapter, the live speech-to-text server), trimmed.
LLM = {
    "/api/version": (200, {"version": "0.40.6"}),
    "/v1/models": (200, {"object": "list", "data": [
        {"id": "qwen2.5:1.5b", "object": "model"}, {"id": "qwen2.5:0.5b", "object": "model"}]}),
    "/api/ps": (200, {"models": [{"name": "qwen2.5:1.5b", "size": 1}]}),
    "/api/tags": (200, {"models": [{"name": "secret-model"}]}),
}
WHISPER = {
    "/health": (200, "OK"),
    "/v1/models": (200, {"data": [
        {"id": "speaches-ai/Kokoro-82M-v1.0-ONNX", "task": "text-to-speech"},
        {"id": "Systran/faster-whisper-base", "task": "automatic-speech-recognition"},
        {"id": "Systran/faster-whisper-tiny.en", "task": "automatic-speech-recognition"},
    ]}),
    "/openapi.json": (200, {"info": {"title": "Speaches", "version": "v0.8.2"}}),
}
TTS = {
    "/health": (200, {"status": "ok"}),
    "/v1/models": (200, {"object": "list", "data": [{"id": "pocket-tts", "object": "model"}]}),
    "/v1/audio/voices": (200, {"object": "list", "data": [
        {"id": "alba", "default": True}, {"id": "marius", "default": False},
        {"id": "javert", "default": False}]}),
    "/v1/cloning": (200, {"state": "on", "message": None, "token_source": "saved"}),
    "/openapi.json": (200, {"info": {"version": "1.0.0"}}),
}
LIVE_STT = {
    "/health": (200, {
        "status": "ok", "model": "parakeet-en-110m", "model_key": "parakeet-110m",
        "model_version": "parakeet-110m-int8@f628312e", "provider": "cpu", "streaming": False,
        "language": "en", "sample_rate": 16000, "sessions": 1, "max_sessions": 4,
        "min_confidence": 0.6,
    }),
}
# The expressive voice (infra/docker/expressive-tts) and a GPU host's servers, trimmed.
VOICE = {
    "/health": (200, {"status": "ok", "device": "cuda"}),
    "/v1/models": (200, {"object": "list", "data": [
        {"id": "chatterbox-turbo", "object": "model", "sample_rate": 24000, "voices": ["default"]}]}),
    "/v1/audio/voices": (200, {"object": "list", "data": [
        {"id": "default", "license": "MIT", "default": True}]}),
    "/v1/audio/sound-tags": (200, {"tags": ["[laugh]", "[sigh]", "[gasp]", "[chuckle]"]}),
    "/v1/cloning": (200, {"state": "on", "message": None}),
    "/openapi.json": (200, {"info": {"title": "Chatterbox-Turbo (OpenAI-compatible)", "version": "1.0.0"}}),
}
GPU_LLM = {
    **LLM,
    "/v1/models": (200, {"object": "list", "data": [
        {"id": "qwen3:8b-nothink", "object": "model"}, {"id": "qwen3:8b", "object": "model"},
        {"id": "qwen2.5:1.5b", "object": "model"}]}),
    "/api/ps": (200, {"models": [{"name": "qwen3:8b-nothink", "size": 1}]}),
}
GPU_LIVE_STT = {
    "/health": (200, {
        **LIVE_STT["/health"][1], "model": "parakeet-en-0.6b-v3", "model_key": "parakeet-0.6b-v3-fp32",
        "model_version": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3@1a468a35", "provider": "cuda",
        "max_sessions": 16,
    }),
}


class FakeServer:
    """An HTTP server answering fixed routes: {path: (status, body)}; body str = plain text.
    `delay` (seconds) is waited before every answer."""

    def __init__(self, routes: dict[str, tuple[int, Any]], delay: float = 0.0) -> None:
        self.routes = dict(routes)
        self.delay = delay
        self.requests: list[tuple[str, dict[str, str]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                fake.requests.append((self.path, dict(self.headers)))
                if fake.delay:
                    time.sleep(fake.delay)
                status, body = fake.routes.get(self.path, (404, {"detail": "Not Found"}))
                data = (body if isinstance(body, str) else json.dumps(body)).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the health check gave up waiting (timeout tests)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


class HealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.servers: dict[str, FakeServer] = {}
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.addCleanup(self.pool.shutdown)

    def serve(self, name: str, routes: dict[str, tuple[int, Any]], delay: float = 0.0) -> str:
        server = FakeServer(routes, delay)
        self.addCleanup(server.close)
        self.servers[name] = server
        return server.url

    def config(self, budget: float = 2.0, **urls: str) -> app.Config:
        env = {f"AI_HEALTH_{k.upper()}_URL": v for k, v in urls.items()}
        env["AI_HEALTH_BUDGET_S"] = str(budget)
        return app.Config(env)

    def all_up(self, **overrides: dict[str, tuple[int, Any]]) -> app.Config:
        routes = {"llm": LLM, "whisper": WHISPER, "tts": TTS, "live_stt": LIVE_STT}
        routes.update(overrides)
        return self.config(**{name: self.serve(name, r) for name, r in routes.items()})

    def test_all_up(self) -> None:
        status, body = app.health(self.all_up(), self.pool)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertRegex(body["checked_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        s = body["services"]
        self.assertEqual(list(s), ["llm", "whisper", "tts", "live_stt"])
        for service in s.values():
            self.assertIs(service["up"], True)
            self.assertEqual(service["state"], "up")
            self.assertIsInstance(service["latency_ms"], int)
        self.assertEqual(s["llm"]["version"], "0.40.6")
        self.assertEqual(s["llm"]["models"], ["qwen2.5:1.5b", "qwen2.5:0.5b"])
        self.assertEqual(s["llm"]["loaded"], ["qwen2.5:1.5b"])
        self.assertEqual(s["whisper"]["version"], "v0.8.2")
        self.assertEqual(
            s["whisper"]["models"],
            ["Systran/faster-whisper-base", "Systran/faster-whisper-tiny.en"],
        )
        self.assertEqual(s["whisper"]["voice_models"], ["speaches-ai/Kokoro-82M-v1.0-ONNX"])
        self.assertEqual(s["tts"]["version"], "1.0.0")
        self.assertEqual(s["tts"]["models"], ["pocket-tts"])
        self.assertEqual(s["tts"]["voices"], 3)
        self.assertEqual(s["tts"]["default_voice"], "alba")
        self.assertEqual(s["tts"]["cloning"], "on")
        self.assertEqual(s["live_stt"]["model"], "parakeet-en-110m")
        self.assertEqual(s["live_stt"]["version"], "parakeet-110m-int8@f628312e")
        self.assertEqual(s["live_stt"]["provider"], "cpu")
        self.assertIs(s["live_stt"]["streaming"], False)
        self.assertEqual((s["live_stt"]["sessions"], s["live_stt"]["max_sessions"]), (1, 4))

    def test_only_the_v1_and_status_endpoints_are_asked(self) -> None:
        app.health(self.all_up(), self.pool)
        asked = {path for path, _ in self.servers["llm"].requests}
        self.assertEqual(asked, {"/api/version", "/v1/models", "/api/ps"})
        for server in self.servers.values():
            for _, headers in server.requests:
                self.assertNotIn("Authorization", headers)

    def test_cloning_states(self) -> None:
        for state in ("off", "enabling", "on", "failed"):
            with self.subTest(state=state):
                tts = {**TTS, "/v1/cloning": (200, {"state": state, "message": "x"})}
                cfg = self.config(tts=self.serve("tts", tts))
                cfg.services = ["tts"]
                _, body = app.health(cfg, self.pool)
                self.assertEqual(body["services"]["tts"]["cloning"], state)

    def test_cloning_endpoint_missing_is_unknown(self) -> None:
        tts = {k: v for k, v in TTS.items() if k != "/v1/cloning"}
        _, body = app.health(self.all_up(tts=tts), self.pool)
        self.assertEqual(body["services"]["tts"]["cloning"], "unknown")
        self.assertEqual(body["status"], "ok")

    def test_cloning_unexpected_answer_is_null(self) -> None:
        tts = {**TTS, "/v1/cloning": (200, {"state": "maybe"})}
        _, body = app.health(self.all_up(tts=tts), self.pool)
        self.assertIsNone(body["services"]["tts"]["cloning"])

    def test_one_service_down_is_degraded(self) -> None:
        cfg = self.all_up()
        cfg.urls["live_stt"] = closed_port_url()
        status, body = app.health(cfg, self.pool)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(
            body["services"]["live_stt"],
            {"up": False, "state": "down", "latency_ms": None, "error": "refused"},
        )
        self.assertIs(body["services"]["llm"]["up"], True)

    def test_all_down_is_503(self) -> None:
        url = closed_port_url()
        status, body = app.health(self.config(llm=url, whisper=url, tts=url, live_stt=url), self.pool)
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "down")
        self.assertTrue(all(s["error"] == "refused" for s in body["services"].values()))

    def test_unreachable_host(self) -> None:
        cfg = self.config(llm="http://no-such-container.invalid:11434")
        cfg.services = ["llm"]
        _, body = app.health(cfg, self.pool)
        self.assertEqual(body["services"]["llm"]["error"], "unreachable")

    def test_slow_service_times_out_within_the_budget(self) -> None:
        cfg = self.all_up()
        cfg.urls["llm"] = self.serve("slow", LLM, delay=2.0)
        cfg.budget_s = 0.5
        started = time.monotonic()
        _, body = app.health(cfg, self.pool)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(body["services"]["llm"]["error"], "timeout")
        self.assertEqual(body["status"], "degraded")

    def test_http_error_is_reported_without_upstream_text(self) -> None:
        llm = {**LLM, "/api/version": (500, {"error": "token=abc123 leaked"})}
        _, body = app.health(self.all_up(llm=llm), self.pool)
        self.assertEqual(body["services"]["llm"]["error"], "HTTP 500")
        self.assertNotIn("abc123", json.dumps(body))

    def test_no_urls_in_the_answer(self) -> None:
        cfg = self.all_up()
        cfg.urls["whisper"] = closed_port_url()
        _, body = app.health(cfg, self.pool)
        text = json.dumps(body)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("http", text)

    def test_loading_services(self) -> None:
        tts = {"/health": (200, {"status": "loading"})}
        live = {"/health": (200, {**LIVE_STT["/health"][1], "status": "loading"})}
        _, body = app.health(self.all_up(tts=tts, live_stt=live), self.pool)
        for name in ("tts", "live_stt"):
            self.assertIs(body["services"][name]["up"], False)
            self.assertEqual(body["services"][name]["state"], "loading")
            self.assertIsInstance(body["services"][name]["latency_ms"], int)
        self.assertEqual(body["status"], "degraded")

    def test_detail_failure_keeps_the_service_up(self) -> None:
        llm = {**LLM, "/v1/models": (200, "not json"), "/api/ps": (500, {})}
        whisper = {**WHISPER, "/openapi.json": (404, {})}
        _, body = app.health(self.all_up(llm=llm, whisper=whisper), self.pool)
        self.assertIs(body["services"]["llm"]["up"], True)
        self.assertIsNone(body["services"]["llm"]["models"])
        self.assertIsNone(body["services"]["llm"]["loaded"])
        self.assertIsNone(body["services"]["whisper"]["version"])
        self.assertEqual(body["status"], "ok")

    def test_live_stt_non_json_is_down(self) -> None:
        _, body = app.health(self.all_up(live_stt={"/health": (200, "OK")}), self.pool)
        self.assertEqual(body["services"]["live_stt"]["error"], "invalid response")

    def test_long_lists_are_capped(self) -> None:
        many = {"data": [{"id": f"m{i}"} for i in range(500)]}
        _, body = app.health(self.all_up(llm={**LLM, "/v1/models": (200, many)}), self.pool)
        self.assertEqual(len(body["services"]["llm"]["models"]), app.MAX_ITEMS)

    def test_services_subset(self) -> None:
        cfg = app.Config({"AI_HEALTH_SERVICES": "llm, tts"})
        self.assertEqual(cfg.services, ["llm", "tts"])
        with self.assertRaises(ValueError):
            app.Config({"AI_HEALTH_SERVICES": "llm,gpu"})

    def test_default_urls_are_the_compose_service_names(self) -> None:
        cfg = app.Config({})
        self.assertEqual(cfg.urls["llm"], "http://local-llm:11434")
        self.assertEqual(cfg.urls["live_stt"], "http://local-stt:8000")
        self.assertEqual(cfg.urls["voice"], "http://expressive-tts:8000")
        # The expressive voice is asked only on GPU hosts (deploy.sh --gpu adds it).
        self.assertEqual(cfg.services, ["llm", "whisper", "tts", "live_stt"])

    def test_gpu_host(self) -> None:
        """What a GPU models server answers: the expressive voice with its sounds, the no-think
        alias among the language models, live speech-to-text on the GPU."""
        cfg = self.config(
            llm=self.serve("llm", GPU_LLM), whisper=self.serve("whisper", WHISPER),
            tts=self.serve("tts", TTS), live_stt=self.serve("live_stt", GPU_LIVE_STT),
            voice=self.serve("voice", VOICE),
        )
        self.assertEqual(cfg.services, ["llm", "whisper", "tts", "live_stt"])
        cfg = app.Config({**{f"AI_HEALTH_{k.upper()}_URL": v for k, v in cfg.urls.items()},
                          "AI_HEALTH_SERVICES": "llm,whisper,tts,live_stt,voice"})
        status, body = app.health(cfg, self.pool)
        self.assertEqual((status, body["status"]), (200, "ok"))
        s = body["services"]
        self.assertEqual(list(s), ["llm", "whisper", "tts", "live_stt", "voice"])
        self.assertEqual(s["llm"]["models"], ["qwen3:8b-nothink", "qwen3:8b", "qwen2.5:1.5b"])
        self.assertEqual(s["llm"]["loaded"], ["qwen3:8b-nothink"])
        self.assertEqual((s["live_stt"]["model"], s["live_stt"]["provider"]), ("parakeet-en-0.6b-v3", "cuda"))
        v = s["voice"]
        self.assertEqual((v["up"], v["state"], v["device"]), (True, "up", "cuda"))
        self.assertEqual(v["models"], ["chatterbox-turbo"])
        self.assertEqual((v["voices"], v["default_voice"], v["cloning"]), (1, "default", "on"))
        self.assertEqual(v["sound_tags"], ["[laugh]", "[sigh]", "[gasp]", "[chuckle]"])
        self.assertEqual(v["version"], "1.0.0")
        asked = {path for path, _ in self.servers["voice"].requests}
        self.assertEqual(asked, set(VOICE))
        self.assertNotIn("http", json.dumps(v))

    def test_voice_loading_down_and_odd_answers(self) -> None:
        cfg = self.config(voice=self.serve("voice", {"/health": (200, {"status": "loading"})}))
        cfg.services = ["voice"]
        status, body = app.health(cfg, self.pool)
        self.assertEqual((status, body["services"]["voice"]["state"]), (503, "loading"))
        self.assertNotIn("sound_tags", body["services"]["voice"])
        odd = {**VOICE, "/v1/audio/sound-tags": (200, {"tags": ["[laugh]", 7, "x" * 500] + ["[sigh]"] * 300}),
               "/health": (200, {"status": "ok"})}
        cfg = self.config(voice=self.serve("odd", odd))
        cfg.services = ["voice"]
        v = app.health(cfg, self.pool)[1]["services"]["voice"]
        self.assertIsNone(v["device"])
        self.assertEqual(v["sound_tags"][:2], ["[laugh]", "x" * app.MAX_TEXT])
        self.assertEqual(len(v["sound_tags"]), app.MAX_ITEMS)
        cfg = self.config(voice=self.serve("no-tags", {k: x for k, x in VOICE.items() if k != "/v1/audio/sound-tags"}))
        cfg.services = ["voice"]
        v = app.health(cfg, self.pool)[1]["services"]["voice"]
        self.assertIsNone(v["sound_tags"])
        self.assertIs(v["up"], True)
        cfg = self.config(voice=closed_port_url())
        cfg.services = ["voice"]
        self.assertEqual(app.health(cfg, self.pool)[1]["services"]["voice"]["error"], "refused")


class ServerTest(unittest.TestCase):
    """The HTTP layer the gateway talks to."""

    def setUp(self) -> None:
        cfg = app.Config({})
        cfg.urls = {name: closed_port_url() for name in cfg.urls}
        pool = ThreadPoolExecutor(max_workers=4)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(cfg, pool))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(pool.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], Any]:
        req = urllib.request.Request(self.url + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), json.loads(r.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, dict(exc.headers), json.loads(exc.read())

    def test_health(self) -> None:
        status, headers, body = self.request("/health")
        self.assertEqual(status, 503)  # every fake upstream is down
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Server"], "ai-health")
        self.assertEqual(body["status"], "down")

    def test_livez_does_not_ask_the_models(self) -> None:
        self.assertEqual(self.request("/livez")[::2], (200, {"status": "ok"}))

    def test_other_paths_and_methods(self) -> None:
        self.assertEqual(self.request("/v1/models")[0], 404)
        self.assertEqual(self.request("/health", "POST")[0], 405)


if __name__ == "__main__":
    unittest.main()
