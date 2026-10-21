"""Health of the model servers behind the AI gateway (ADR 0007, docs/deployment-layouts.md).

The gateway (Caddy) checks the token and forwards `GET /health` here; this server publishes
no port. On every request it asks each model server on the Docker network, with a short
time budget, whether it answers and what it offers, all four at once:

  llm       language model (Ollama): /api/version, /v1/models, /api/ps (models in memory)
  whisper   Whisper speech-to-text (speaches): /health, /v1/models, /openapi.json (version)
  tts       Pocket TTS adapter: /health, /v1/models, /v1/audio/voices, /v1/cloning,
            /openapi.json (version)
  live_stt  live speech-to-text, Deepgram protocol: /health

Answer (200 when at least one service is up, 503 when none is):

  {"status": "ok" | "degraded" | "down", "checked_at": "...Z",
   "services": {"llm": {"up": true, "state": "up", "latency_ms": 4, "version": "0.40.6",
                        "models": ["qwen2.5:1.5b"], "loaded": []}, ...}}

`latency_ms` is the round trip of the service's first, cheapest request. A service that
does not answer has `"up": false`, `"state": "down"` and a short `error` (timeout,
refused, unreachable, HTTP <code>, invalid response); one that answers but is still
loading its model has `"state": "loading"`. A detail that fails after the first request
is null; Pocket TTS without the /v1/cloning endpoint reports cloning "unknown".

Nothing secret is returned or logged: no URLs, tokens or upstream error text. The
servers are asked without credentials (none of them requires one on the Docker network).

Environment (defaults in brackets):
  AI_HEALTH_LLM_URL [http://local-llm:11434]
  AI_HEALTH_WHISPER_URL [http://local-speech:8000]
  AI_HEALTH_TTS_URL [http://local-tts:8000]
  AI_HEALTH_LIVE_STT_URL [http://local-stt:8000]
  AI_HEALTH_SERVICES [llm,whisper,tts,live_stt]  the services this host offers; others
                                                 are left out instead of reported down
  AI_HEALTH_BUDGET_S [3]       time for all requests to one service
  PORT [8080]

Standard library only, so the image stays small and has nothing to update but Python.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("ai-health")

DEFAULT_URLS = {
    "llm": "http://local-llm:11434",
    "whisper": "http://local-speech:8000",
    "tts": "http://local-tts:8000",
    "live_stt": "http://local-stt:8000",
}
SERVICES = tuple(DEFAULT_URLS)
# speaches' OpenAPI document is about 40 kB; anything far larger is not a model server.
MAX_BODY_BYTES = 2 * 1024 * 1024
# Model names and voice lists come from the servers; cap them so a misbehaving one cannot
# make the answer large.
MAX_ITEMS = 100
MAX_TEXT = 200


class CheckError(Exception):
    """A request failed; the message is one of a few fixed words, never upstream text."""


# Bypass any proxy settings of the environment: the model servers are on the Docker network.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Budget:
    """Time left for one service's requests."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return self.deadline - time.monotonic()


def get_json(base: str, path: str, budget: Budget) -> tuple[int, Any]:
    """GET base+path and parse JSON. Returns (status, body); a 4xx/5xx is (status, None).
    Raises CheckError when there is no usable answer in the time left."""
    timeout = budget.left()
    if timeout <= 0:
        raise CheckError("timeout")
    request = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    try:
        with _opener.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, None
    except (TimeoutError, socket.timeout):
        raise CheckError("timeout") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise CheckError("timeout") from None
        if isinstance(reason, ConnectionRefusedError):
            raise CheckError("refused") from None
        raise CheckError("unreachable") from None  # DNS: the container is not running
    except OSError:  # reset or closed while answering
        raise CheckError("unreachable") from None
    except http.client.HTTPException:  # not HTTP, or cut off mid-body
        raise CheckError("invalid response") from None
    if len(raw) > MAX_BODY_BYTES:
        raise CheckError("invalid response")
    try:
        return status, json.loads(raw)
    except ValueError:
        # speaches answers its /health with plain text "OK".
        return status, raw.decode("utf-8", "replace")


def get_ok(base: str, path: str, budget: Budget) -> Any:
    """GET that must answer 200; returns the body."""
    status, body = get_json(base, path, budget)
    if status != 200:
        raise CheckError(f"HTTP {status}")
    return body


def optional(fn: Callable[[], Any]) -> Any:
    """A detail after the service answered: None when it fails or has an unexpected shape."""
    try:
        return fn()
    except (CheckError, KeyError, TypeError, AttributeError, ValueError, IndexError):
        return None


def text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) else None


def names(items: Any, key: str = "id") -> list[str]:
    out: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get(key), str):
            out.append(item[key][:MAX_TEXT])
        if len(out) >= MAX_ITEMS:
            break
    return out


def openapi_version(base: str, budget: Budget) -> str | None:
    return text(get_ok(base, "/openapi.json", budget)["info"]["version"])


def timed(fn: Callable[[], Any]) -> tuple[Any, int]:
    started = time.perf_counter()
    result = fn()
    return result, round((time.perf_counter() - started) * 1000)


# ------------------------------------------------------------------ per service checks


def check_llm(base: str, budget: Budget) -> dict[str, Any]:
    version, latency = timed(lambda: get_ok(base, "/api/version", budget))
    return {
        "up": True,
        "state": "up",
        "latency_ms": latency,
        "version": text(version.get("version")) if isinstance(version, dict) else None,
        "models": optional(lambda: names(get_ok(base, "/v1/models", budget)["data"])),
        # Models in memory now: a request for any other one waits for it to load first.
        "loaded": optional(lambda: names(get_ok(base, "/api/ps", budget)["models"], "name")),
    }


def check_whisper(base: str, budget: Budget) -> dict[str, Any]:
    _, latency = timed(lambda: get_ok(base, "/health", budget))
    models = optional(lambda: get_ok(base, "/v1/models", budget)["data"])

    def of_task(task: str) -> list[str] | None:
        if not isinstance(models, list):
            return None
        return names(m for m in models if isinstance(m, dict) and m.get("task") == task)

    return {
        "up": True,
        "state": "up",
        "latency_ms": latency,
        "version": optional(lambda: openapi_version(base, budget)),
        # Downloaded models; the first request for one loads it into memory.
        "models": of_task("automatic-speech-recognition"),
        # The same server also speaks with Kokoro.
        "voice_models": of_task("text-to-speech"),
    }


def cloning_state(base: str, budget: Budget) -> str | None:
    status, body = get_json(base, "/v1/cloning", budget)
    if status == 404:
        return "unknown"  # an adapter version without voice cloning settings
    if status != 200 or not isinstance(body, dict):
        return None
    state = body.get("state")
    return state if state in ("off", "enabling", "on", "failed") else None


def check_tts(base: str, budget: Budget) -> dict[str, Any]:
    health, latency = timed(lambda: get_ok(base, "/health", budget))
    loading = isinstance(health, dict) and health.get("status") == "loading"
    out: dict[str, Any] = {
        "up": not loading,
        "state": "loading" if loading else "up",
        "latency_ms": latency,
        "version": optional(lambda: openapi_version(base, budget)),
    }
    if loading:
        return out
    voices = optional(lambda: get_ok(base, "/v1/audio/voices", budget)["data"])
    default = (
        next((v.get("id") for v in voices if isinstance(v, dict) and v.get("default")), None)
        if isinstance(voices, list)
        else None
    )
    out.update(
        models=optional(lambda: names(get_ok(base, "/v1/models", budget)["data"])),
        voices=len(voices) if isinstance(voices, list) else None,
        default_voice=text(default),
        cloning=optional(lambda: cloning_state(base, budget)),
    )
    return out


def check_live_stt(base: str, budget: Budget) -> dict[str, Any]:
    health, latency = timed(lambda: get_ok(base, "/health", budget))
    if not isinstance(health, dict):
        raise CheckError("invalid response")
    ready = health.get("status") == "ok"

    def number(key: str) -> int | None:
        value = health.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return {
        "up": ready,
        "state": "up" if ready else "loading",
        "latency_ms": latency,
        "version": text(health.get("model_version")),
        "model": text(health.get("model")),
        "streaming": health.get("streaming") if isinstance(health.get("streaming"), bool) else None,
        "language": text(health.get("language")),
        "sessions": number("sessions"),
        "max_sessions": number("max_sessions"),
    }


CHECKS: dict[str, Callable[[str, Budget], dict[str, Any]]] = {
    "llm": check_llm,
    "whisper": check_whisper,
    "tts": check_tts,
    "live_stt": check_live_stt,
}


def check_service(name: str, base: str, budget_s: float) -> dict[str, Any]:
    try:
        return CHECKS[name](base, Budget(budget_s))
    except CheckError as exc:
        return {"up": False, "state": "down", "latency_ms": None, "error": str(exc)}
    except Exception:  # an unexpected answer shape must not take the whole check down
        logger.exception("checking %s failed", name)
        return {"up": False, "state": "down", "latency_ms": None, "error": "invalid response"}


class Config:
    def __init__(self, env: dict[str, str]) -> None:
        self.urls = {
            name: env.get(f"AI_HEALTH_{name.upper()}_URL", default).rstrip("/")
            for name, default in DEFAULT_URLS.items()
        }
        wanted = [s.strip() for s in env.get("AI_HEALTH_SERVICES", ",".join(SERVICES)).split(",")]
        unknown = [s for s in wanted if s and s not in SERVICES]
        if unknown:
            raise ValueError(f"AI_HEALTH_SERVICES: unknown service(s) {', '.join(unknown)}")
        self.services = [s for s in SERVICES if s in wanted]
        self.budget_s = float(env.get("AI_HEALTH_BUDGET_S", "3"))
        self.port = int(env.get("PORT", "8080"))


def health(config: Config, pool: ThreadPoolExecutor) -> tuple[int, dict[str, Any]]:
    futures = {
        name: pool.submit(check_service, name, config.urls[name], config.budget_s)
        for name in config.services
    }
    services = {name: future.result() for name, future in futures.items()}
    up = sum(1 for s in services.values() if s["up"])
    if services and up == len(services):
        status = "ok"
    elif up:
        status = "degraded"
    else:
        status = "down"
    body = {
        "status": status,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "services": services,
    }
    return (HTTPStatus.SERVICE_UNAVAILABLE if status == "down" else HTTPStatus.OK), body


# ------------------------------------------------------------------ HTTP server


def make_handler(config: Config, pool: ThreadPoolExecutor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def version_string(self) -> str:
            return "ai-health"  # not the Python version

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = (json.dumps(body, separators=(",", ":")) + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send(*health(config, pool))
            elif path == "/livez":  # the container's own health check: no upstream requests
                self._send(HTTPStatus.OK, {"status": "ok"})
            else:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        do_HEAD = do_GET  # noqa: N815

        def _not_allowed(self) -> None:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _not_allowed  # noqa: N815

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # no access log, like the gateway

    return Handler


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config(dict(os.environ))
    except ValueError as exc:
        sys.exit(str(exc))
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="check")
    server = ThreadingHTTPServer(("0.0.0.0", config.port), make_handler(config, pool))  # noqa: S104 - Docker network only
    server.daemon_threads = True
    # PID 1 in the container: without a handler SIGTERM is ignored and `docker stop` waits.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    logger.info(
        "listening on :%d, checking %s (budget %.1fs each)",
        config.port, ", ".join(config.services), config.budget_s,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
