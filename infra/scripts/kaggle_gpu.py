#!/usr/bin/env python3
"""Testing only: run the language model on a Kaggle GPU and point ATLAS at it.

  python infra/scripts/kaggle_gpu.py start     start a GPU session on Kaggle, connect ATLAS
  python infra/scripts/kaggle_gpu.py start --voice
                                               the same, with the expressive voice too
  python infra/scripts/kaggle_gpu.py connect --api https://atlas.example.com
                                               connect another ATLAS to the running session
  python infra/scripts/kaggle_gpu.py status    session and connections
  python infra/scripts/kaggle_gpu.py stop      put every connected ATLAS back, end the session

`start` pushes a private Kaggle notebook that runs Ollama (the same version as the
self-hosted stack) on the notebook's GPU behind a token check and opens a temporary
Cloudflare tunnel to it. ATLAS gets a connection "GPU test (Kaggle)" with that address and
token, and the organisation's language model is switched to it; the model chosen before is
kept and put back by `stop`. With --voice the session also runs the expressive voice server
(infra/docker/expressive-tts, Chatterbox-Turbo: it can laugh, sigh and gasp) on the same GPU
at <address>/voice/v1; ATLAS gets a connection "Expressive voice GPU test (Kaggle)" and the
organisation's voice is switched to it; the voice chosen before is put back by `stop`.
Speech-to-text stays where it is.

For tests, not for real calls: a Kaggle session ends after --hours (at most 12), the address
changes every session, the call's text goes through Kaggle and Cloudflare, and Kaggle's
acceptable-use policy does not allow running a service's backend there.

Needs: the Kaggle CLI (`pip install kaggle`; set KAGGLE_CLI if it is not on PATH) signed in
with an API token (KAGGLE_API_TOKEN or ~/.kaggle/access_token) of a phone-verified account;
the ATLAS demo password in ATLAS_DEMO_PASSWORD or the repository's .env. Session state, with
the token, is kept in .cache/kaggle-gpu/session.json (git-ignored) and never printed.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATE = ROOT / ".cache" / "kaggle-gpu" / "session.json"
KERNEL_SLUG = "atlas-gpu-llm"
CONNECTION_NAME = "GPU test (Kaggle)"
VOICE_CONNECTION_NAME = "Expressive voice GPU test (Kaggle)"
VOICE_SRC = ROOT / "infra" / "docker" / "expressive-tts"
VOICE_MODEL = "chatterbox-turbo"
DEMO_EMAIL = "demo@example.com"
DEMO_ORG_NAME = "Demo Realty (demo data)"
DEFAULT_MODELS = ["qwen3:8b", "qwen2.5:7b"]
VOICE_MODELS = ["qwen3:8b"]  # with the voice on the same T4 (16 GB), one language model
# The address line carries the session's id, so a log of an earlier run is never taken for it.
URL_LINE = re.compile(r"ATLAS_GPU_URL=(https://[a-z0-9-]+\.trycloudflare\.com) session=([0-9a-f]{12})")
FAILED_LINE = re.compile(r"ATLAS_GPU_FAILED session=([0-9a-f]{12}): ([^\"\\]*)")

# The notebook. Placeholders are filled by render_kernel(); it never holds the token itself,
# only its SHA-256, so the notebook's source is useless to anyone who reads it.
KERNEL = r'''# ATLAS GPU test session (testing only), started by infra/scripts/kaggle_gpu.py.
# Ollama (and with VOICE the expressive voice server) on this notebook's GPU behind a token
# check, reachable through a temporary Cloudflare tunnel until the session is stopped or
# HOURS have passed.
import base64, json, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.request

TOKEN_SHA256 = "@TOKEN_SHA256@"
MODELS = @MODELS@
HOURS = @HOURS@
VOICE = @VOICE@
# infra/docker/expressive-tts/app.py and requirements.txt, unchanged (base64).
VOICE_FILES = @VOICE_FILES@
OLLAMA_VERSION = "0.40.6"
PORT = 8000
VOICE_PORT = 8001
VOICE_WORK = "/tmp/atlas-voice"
CLOUDFLARED = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

PROXY = r"""
import hashlib, hmac, json, os, subprocess
from aiohttp import ClientSession, ClientTimeout, web

TOKEN_SHA256 = os.environ["ATLAS_TOKEN_SHA256"]
UP = "http://127.0.0.1:11434"
VOICE_UP = os.environ.get("ATLAS_VOICE_UP", "")
DROP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length", "authorization"}
served = {"requests": 0}

def allowed(req):
    got = req.headers.get("Authorization", "")
    digest = hashlib.sha256(got[7:].encode()).hexdigest() if got.startswith("Bearer ") else ""
    return hmac.compare_digest(digest, TOKEN_SHA256)

async def handle(req):
    if not allowed(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    if req.path == "/atlas/stop" and req.method == "POST":
        open("/tmp/atlas-stop", "w").close()
        return web.json_response({"stopping": True}, status=202)
    if req.path == "/atlas/health":
        gpu = subprocess.run("nvidia-smi --query-gpu=name,utilization.gpu,memory.used --format=csv,noheader",
                             shell=True, capture_output=True, text=True).stdout.strip()
        return web.json_response({"gpu": gpu, "requests": served["requests"], "voice": bool(VOICE_UP)})
    if VOICE_UP and (req.path.startswith("/voice/v1/") or req.path == "/voice/health"):
        target = VOICE_UP + req.path_qs[len("/voice"):]
    elif req.path.startswith("/v1/"):
        target = UP + req.path_qs
    else:
        return web.json_response({"error": "not found"}, status=404)
    served["requests"] += 1
    body = await req.read()
    # qwen3 thinks before answering unless asked not to, and the voice service cannot send
    # that field: add it here for chat requests to qwen3 models.
    if target.startswith(UP) and req.path.endswith("/chat/completions") and body:
        try:
            payload = json.loads(body)
            if str(payload.get("model", "")).startswith("qwen3") and "reasoning_effort" not in payload:
                payload["reasoning_effort"] = "none"
                body = json.dumps(payload).encode()
        except ValueError:
            pass
    headers = {k: v for k, v in req.headers.items() if k.lower() not in DROP}
    async with req.app["s"].request(req.method, target, data=body, headers=headers) as up:
        resp = web.StreamResponse(
            status=up.status, headers={k: v for k, v in up.headers.items() if k.lower() not in DROP})
        await resp.prepare(req)
        async for chunk in up.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp

async def start(app):
    app["s"] = ClientSession(timeout=ClientTimeout(total=None, sock_read=300))

async def stop(app):
    await app["s"].close()

app = web.Application(client_max_size=32 * 1024 * 1024)  # voice samples for cloning
app.on_startup.append(start)
app.on_cleanup.append(stop)
app.router.add_route("*", "/{tail:.*}", handle)
web.run_app(app, host="127.0.0.1", port=int(os.environ["ATLAS_PORT"]), access_log=None, print=None)
"""


def sh(cmd):
    print("+", cmd, flush=True)
    subprocess.run(cmd, shell=True, check=True)


def fail(msg):
    print(f"ATLAS_GPU_FAILED session={TOKEN_SHA256[:12]}: {msg}", flush=True)
    sys.exit(1)


def wait_http(url, tries):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True  # it answers
        except OSError:
            time.sleep(1)
    return False


def voice_env(filter_ld):
    """The voice server's environment: its own venv and caches, no Hugging Face token."""
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
              "HUGGINGFACE_HUB_TOKEN"):
        env.pop(k, None)
    venv = os.path.join(VOICE_WORK, "venv")
    env.update(VIRTUAL_ENV=venv, PATH=os.path.join(venv, "bin") + os.pathsep + env.get("PATH", ""),
               HF_HOME=os.path.join(VOICE_WORK, "hf"), HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
               HF_HUB_DISABLE_PROGRESS_BARS="1", TQDM_DISABLE="1", PYTHONUNBUFFERED="1",
               TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="0",
               EXPRESSIVE_TTS_VOICE_DIR=os.path.join(VOICE_WORK, "voices"))
    if filter_ld:  # CUDA libraries of the image that clash with the torch wheels' own
        keep = [p for p in env.get("LD_LIBRARY_PATH", "").split(":")
                if p and ("nvidia/lib" in p or "x86_64-linux-gnu" in p)]
        env["LD_LIBRARY_PATH"] = ":".join(keep)
    return env


voice_setup = {}


def install_voice():
    """The image's `pip install --no-deps -r requirements.txt`, into a venv, with uv for speed."""
    try:
        srv = os.path.join(VOICE_WORK, "srv")
        os.makedirs(srv, exist_ok=True)
        for name, b64 in VOICE_FILES.items():
            with open(os.path.join(srv, name), "wb") as f:
                f.write(base64.b64decode(b64))
        venv = os.path.join(VOICE_WORK, "venv")
        py = os.path.join(venv, "bin", "python")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=True)
        uv = shutil.which("uv") or os.path.join(os.path.dirname(sys.executable), "uv")
        env = dict(os.environ, UV_HTTP_TIMEOUT="600", UV_LINK_MODE="copy")
        subprocess.run([uv, "venv", "-q", venv, "--python", sys.executable, "--seed"], check=True, env=env)
        subprocess.run([uv, "pip", "install", "-q", "--python", py, "--no-deps", "-r",
                        os.path.join(srv, "requirements.txt")], check=True, env=env)
        check = "import torch, perth, chatterbox.tts_turbo; assert torch.cuda.is_available()"
        for filter_ld in (False, True):
            if subprocess.run([py, "-c", check], env=voice_env(filter_ld), capture_output=True).returncode == 0:
                voice_setup.update(srv=srv, py=py, filter_ld=filter_ld)
                return
        voice_setup["error"] = "the voice packages do not import"
    except Exception as e:
        voice_setup["error"] = f"the voice install failed ({type(e).__name__})"


gpu = subprocess.run("nvidia-smi --query-gpu=name --format=csv,noheader", shell=True,
                     capture_output=True, text=True).stdout.strip()
print("GPU:", gpu or "none", flush=True)
if not gpu:
    fail("no GPU in this session (accelerator not granted)")
installing = threading.Thread(target=install_voice, daemon=True)
if VOICE:
    installing.start()  # meanwhile Ollama installs and downloads its models
sh("command -v zstd >/dev/null || (apt-get update -qq && apt-get install -y -qq zstd >/dev/null)")
sh(f"curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION={OLLAMA_VERSION} sh >/dev/null")
env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_KEEP_ALIVE="-1", OLLAMA_FLASH_ATTENTION="1",
           OLLAMA_CONTEXT_LENGTH="4096", OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS=str(len(MODELS)),
           CUDA_VISIBLE_DEVICES="0")
ollama = subprocess.Popen(["ollama", "serve"], env=env, stdout=open("/tmp/ollama.log", "w"), stderr=subprocess.STDOUT)
if not wait_http("http://127.0.0.1:11434/api/version", 120):
    fail("Ollama did not start")
for m in MODELS:
    sh(f"ollama pull {m} 2>&1 | tail -n 1")
    # Load it onto the GPU now, so the first call does not wait for it.
    req = urllib.request.Request("http://127.0.0.1:11434/api/generate", method="POST",
                                 data=('{"model": "%s", "prompt": "hi", "stream": false, "options": {"num_predict": 1}}' % m).encode())
    urllib.request.urlopen(req, timeout=600).read()

voice = None
if VOICE:
    installing.join()
    if "error" in voice_setup:
        fail(voice_setup["error"])
    # Downloads the weights (about 3 GB), loads and warms the model and makes the kept openers.
    voice = subprocess.Popen([voice_setup["py"], "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                              "--port", str(VOICE_PORT), "--workers", "1"], cwd=voice_setup["srv"],
                             env=voice_env(voice_setup["filter_ld"]), stdout=open("/tmp/voice.log", "w"),
                             stderr=subprocess.STDOUT)
    ready = False
    for _ in range(900):
        if voice.poll() is not None:
            print("".join(open("/tmp/voice.log").readlines()[-5:]), flush=True)
            fail("the voice server exited")
        try:
            health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{VOICE_PORT}/health", timeout=3))
            ready = health.get("status") == "ok"
        except (OSError, ValueError):
            pass
        if ready:
            break
        time.sleep(1)
    if not ready:
        fail("the voice server did not get ready")
    print("voice server ready", flush=True)

try:
    import aiohttp  # noqa: F401
except ImportError:
    sh(f"{sys.executable} -m pip install -q aiohttp")
with open("/tmp/atlas_proxy.py", "w") as f:
    f.write(PROXY)
proxy = subprocess.Popen([sys.executable, "/tmp/atlas_proxy.py"],
                         env=dict(os.environ, ATLAS_TOKEN_SHA256=TOKEN_SHA256, ATLAS_PORT=str(PORT),
                                  ATLAS_VOICE_UP=f"http://127.0.0.1:{VOICE_PORT}" if VOICE else ""))
if not wait_http(f"http://127.0.0.1:{PORT}/v1/models", 30):
    fail("the token check did not start")

sh(f"curl -fsSL -o /tmp/cloudflared {CLOUDFLARED} && chmod +x /tmp/cloudflared")
# Its log goes to a file: a pipe nobody reads would fill up and stall the tunnel.
tunnel = subprocess.Popen(["/tmp/cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                          stdout=open("/tmp/cloudflared.log", "w"), stderr=subprocess.STDOUT)
url = None
for _ in range(90):
    time.sleep(1)
    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open("/tmp/cloudflared.log").read())
    if m:
        url = m.group(0)
        break
if url is None:
    fail("the tunnel did not start")
# Reachable from outside: an unauthenticated request must come back 401 from the token check.
reachable = False
for _ in range(40):
    try:
        urllib.request.urlopen(url + "/v1/models", timeout=10)
    except urllib.error.HTTPError as e:
        reachable = e.code == 401
    except OSError:
        pass
    if reachable:
        break
    time.sleep(3)
if not reachable:
    fail("the tunnel does not answer")
print(f"ATLAS_GPU_URL={url} session={TOKEN_SHA256[:12]}", flush=True)

end = time.time() + HOURS * 3600
reason = "time limit reached"
while time.time() < end:
    if os.path.exists("/tmp/atlas-stop"):
        reason = "stopped by kaggle_gpu.py stop"
        break
    if any(p.poll() is not None for p in (ollama, proxy, tunnel, voice) if p is not None):
        reason = "a process exited"
        break
    time.sleep(20)
    if int(time.time()) % 300 < 20:
        print(time.strftime("%H:%M:%S"), "session running", flush=True)
for p in (tunnel, proxy, voice, ollama):
    if p is not None:
        p.terminate()
print("ATLAS GPU session ended:", reason, flush=True)
'''


# ------------------------------------------------------------------ helpers


def say(msg: str) -> None:
    print(f"    {msg}", flush=True)


def die(msg: str) -> None:
    raise SystemExit(f"error: {msg}")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, Any] | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(state: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE)


def read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return out


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
         timeout: float = 60) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:300]


# ------------------------------------------------------------------ Kaggle


def kaggle_cli() -> str:
    cli = os.environ.get("KAGGLE_CLI") or shutil.which("kaggle")
    if not cli:
        die("the Kaggle CLI was not found: `pip install kaggle`, or set KAGGLE_CLI to its path")
    return cli


# The CLI writes the notebook's log as UTF-8; on Windows it would otherwise use the console's
# code page and stop at the first character outside it (Ollama's installer prints some).
CLI_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


def kaggle(*args: str, timeout: float = 120) -> str:
    r = subprocess.run([kaggle_cli(), *args], capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", env=CLI_ENV)
    if r.returncode != 0:
        die(f"kaggle {args[0]} {args[1] if len(args) > 1 else ''} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


def kaggle_user() -> str:
    user = os.environ.get("KAGGLE_USERNAME")
    if user:
        return user
    m = re.search(r"username:\s*(\S+)", kaggle("config", "view"))
    if not m or m.group(1) == "None":
        die("could not read the Kaggle username (`kaggle config view`); set KAGGLE_USERNAME")
    return m.group(1)


def gpu_hours_left() -> float | None:
    m = re.search(r"^GPU\s+[\d.]+h\s+([\d.]+)h", kaggle("quota"), re.M)
    return float(m.group(1)) if m else None


def render_kernel(token: str, models: list[str], hours: float, voice: bool = False) -> str:
    files = ({n: base64.b64encode((VOICE_SRC / n).read_bytes()).decode() for n in ("app.py", "requirements.txt")}
             if voice else {})
    return (KERNEL.replace("@TOKEN_SHA256@", hashlib.sha256(token.encode()).hexdigest())
            .replace("@MODELS@", json.dumps(models)).replace("@HOURS@", repr(hours))
            .replace("@VOICE_FILES@", json.dumps(files)).replace("@VOICE@", repr(voice)))


def push_kernel(ref: str, source: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "atlas_gpu_llm.py").write_text(source, encoding="utf-8")
        meta = {"id": ref, "title": KERNEL_SLUG, "code_file": "atlas_gpu_llm.py", "language": "python",
                "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_tpu": False,
                "enable_internet": True, "dataset_sources": [], "competition_sources": [],
                "kernel_sources": [], "model_sources": []}
        Path(tmp, "kernel-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        kaggle("kernels", "push", "-p", tmp, "--accelerator", "NvidiaTeslaT4", timeout=300)


def session_id(token: str) -> str:
    """The id a session prints with its address: the start of its token's SHA-256."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def wait_for_url(ref: str, session: str, timeout_s: float) -> str:
    """Follow the notebook's live log until this session prints its tunnel address."""
    proc = subprocess.Popen([kaggle_cli(), "kernels", "logs", "-f", ref], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                            env=CLI_ENV)
    deadline = time.time() + timeout_s
    last_note = 0.0
    try:
        while time.time() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                if proc.poll() is not None:  # the log stream ended (not started yet, or finished)
                    try:
                        status = kaggle("kernels", "status", ref)
                    except SystemExit:  # Kaggle's API failed this once; ask again next round
                        status = ""
                    if re.search(r"error|cancel|complete", status, re.I):
                        die(f"the Kaggle session ended before it was ready ({status.strip()}); "
                            f"see https://www.kaggle.com/code/{ref}")
                    time.sleep(10)
                    proc = subprocess.Popen([kaggle_cli(), "kernels", "logs", "-f", ref], stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                            env=CLI_ENV)
                continue
            if (m := URL_LINE.search(line)) is not None and m.group(2) == session:
                return m.group(1)
            if (m := FAILED_LINE.search(line)) is not None and m.group(1) == session:
                die(f"the Kaggle session failed: {m.group(2)}")
            if time.time() - last_note > 60:
                last_note = time.time()
                say("still starting on Kaggle (installing, downloading the models)...")
        die(f"no address from Kaggle after {timeout_s / 60:.0f} minutes; see https://www.kaggle.com/code/{ref}")
    finally:
        if proc.poll() is None:
            proc.terminate()
    return ""  # unreachable


# ------------------------------------------------------------------ ATLAS


class Atlas:
    def __init__(self, base: str, password: str) -> None:
        self.base = base.rstrip("/") + "/api/v1"
        code, data = http("POST", self.base + "/auth/login", {"email": DEMO_EMAIL, "password": password})
        if code != 200:
            die(f"sign-in to {base} as {DEMO_EMAIL} failed (HTTP {code})")
        orgs = [m["organization"] for m in data.get("memberships") or []]
        org = next((o for o in orgs if o["name"] == DEMO_ORG_NAME), orgs[0] if orgs else None)
        if org is None:
            die(f"{DEMO_EMAIL} has no organization at {base}")
        self.headers = {"Authorization": f"Bearer {data['access_token']}", "X-Organization-Id": org["id"]}

    def call(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        code, data = http(method, self.base + path, body, self.headers, timeout=120)
        if code not in ok:
            msg = data.get("error", {}).get("message") if isinstance(data, dict) else data
            die(f"{method} {path}: HTTP {code} {msg}")
        return data


def atlas_password(explicit: str | None) -> str:
    pw = explicit or os.environ.get("ATLAS_DEMO_PASSWORD") or read_dotenv(ROOT / ".env").get("ATLAS_DEMO_PASSWORD")
    if not pw:
        die("no ATLAS demo password: set ATLAS_DEMO_PASSWORD or put it in .env")
    return pw


def upsert_connection(atlas: Atlas, conns: list[dict[str, Any]], name: str, base_url: str,
                      token: str) -> dict[str, Any]:
    existing = next((c for c in conns if c["name"] == name), None)
    settings, credentials = {"base_url": base_url}, {"api_key": token}
    if existing:
        return atlas.call("PATCH", f"/ai/connections/{existing['id']}",
                          {"connection_settings": settings, "credentials": credentials})
    return atlas.call("POST", "/ai/connections", {"name": name, "provider": "speaches",
                                                  "connection_settings": settings, "credentials": credentials})


def connect(state: dict[str, Any], api_base: str, password: str, model: str) -> None:
    atlas = Atlas(api_base, password)
    conns = atlas.call("GET", "/ai/connections")
    conn = upsert_connection(atlas, conns, CONNECTION_NAME, state["url"] + "/v1", state["token"])
    selection = atlas.call("GET", "/ai/models")
    recorded = state["connected"].get(api_base, {})
    previous = recorded.get("previous_llm")
    if previous is None:
        previous = selection.get("llm")
        if previous and previous.get("connection_id") == conn["id"]:
            previous = None  # left over from an earlier session that was not stopped
    selection["llm"] = {"connection_id": conn["id"], "settings": {"model": model, "temperature": 0.3}}
    if previous and previous.get("connection_id") and previous["connection_id"] != conn["id"]:
        # If the Kaggle session ends or stalls, the model chosen before answers instead.
        selection["llm_fallback"] = {"connection_id": previous["connection_id"],
                                     "settings": previous.get("settings") or {}, "after_ms": 3000, "on_error": True}
    entry: dict[str, Any] = {"connection_id": conn["id"], "previous_llm": previous, "model": model}
    if state.get("voice"):
        vconn = upsert_connection(atlas, conns, VOICE_CONNECTION_NAME, state["url"] + "/voice/v1", state["token"])
        previous_tts = recorded.get("previous_tts")
        if previous_tts is None:
            previous_tts = selection.get("tts")
            if previous_tts and previous_tts.get("connection_id") == vconn["id"]:
                previous_tts = None
        selection["tts"] = {"connection_id": vconn["id"], "settings": {"model": VOICE_MODEL, "voice": "default"}}
        entry.update(voice_connection_id=vconn["id"], previous_tts=previous_tts)
    atlas.call("PUT", "/ai/models", selection)
    state["connected"][api_base] = entry
    save_state(state)
    check = atlas.call("POST", "/ai/models/check")
    voice = " and the voice is the expressive one" if state.get("voice") else ""
    say(f"{api_base}: language model is now {model} on the Kaggle GPU{voice} "
        f"(models check: {'ok' if check.get('ok') else check.get('problems')})")


def disconnect(api_base: str, info: dict[str, Any], password: str) -> None:
    atlas = Atlas(api_base, password)
    selection = atlas.call("GET", "/ai/models")
    put_back = []
    if info.get("previous_llm"):
        selection["llm"] = info["previous_llm"]
        selection["llm_fallback"] = None  # it pointed at the model that is now the main one again
        put_back.append("language model")
    elif (selection.get("llm") or {}).get("connection_id") == info["connection_id"]:
        say(f"{api_base}: no earlier language model was recorded; choose one under AI providers")
    voice_id = info.get("voice_connection_id")
    if voice_id and info.get("previous_tts"):
        selection["tts"] = info["previous_tts"]
        put_back.append("voice")
    elif voice_id and (selection.get("tts") or {}).get("connection_id") == voice_id:
        say(f"{api_base}: no earlier voice was recorded; choose one under AI providers")
    if put_back:
        atlas.call("PUT", "/ai/models", selection)
        say(f"{api_base}: {' and '.join(put_back)} put back")
    for conn_id in (info["connection_id"], voice_id):
        if conn_id:
            atlas.call("DELETE", f"/ai/connections/{conn_id}", ok=(200, 204, 404, 409))


# ------------------------------------------------------------------ commands


def cmd_start(args: argparse.Namespace) -> int:
    password = atlas_password(args.password)
    state = load_state()
    if state and not state.get("url"):
        # An earlier `start` stopped waiting (timeout, Ctrl+C, an error): wait for that session.
        say(f"waiting for {state['kernel']}, which was still starting")
        return finish_start(state, args, password)
    if state:
        die("a GPU session is already recorded: run `stop` first (or `status`)")
    hours = min(max(args.hours, 0.25), 11.5)
    Atlas(args.api, password)  # fail early if ATLAS is not reachable
    left = gpu_hours_left()
    if left is not None:
        say(f"Kaggle GPU hours left this week: {left:.1f}")
        if left < hours:
            die(f"only {left:.1f} GPU hours left; use --hours {max(left - 0.1, 0.25):.1f} or less")
    user = kaggle_user()
    ref = f"{user}/{KERNEL_SLUG}"
    token = secrets.token_urlsafe(32)
    models = args.model or (VOICE_MODELS if args.voice else DEFAULT_MODELS)
    with_voice = " and the expressive voice" if args.voice else ""
    say(f"starting {ref} on a T4 GPU with {', '.join(models)}{with_voice} for at most {hours:g} h")
    push_kernel(ref, render_kernel(token, models, hours, args.voice))
    state = {"kernel": ref, "token": token, "models": models, "voice": args.voice, "hours": hours,
             "started": now(), "url": None, "connected": {}}
    save_state(state)
    return finish_start(state, args, password)


def finish_start(state: dict[str, Any], args: argparse.Namespace, password: str) -> int:
    token = state["token"]
    url = wait_for_url(state["kernel"], session_id(token), timeout_s=args.wait_minutes * 60)
    code_ok, _ = http("GET", url + "/v1/models", headers={"Authorization": f"Bearer {token}"})
    code_no, _ = http("GET", url + "/v1/models")
    if code_ok != 200 or code_no != 401:
        die(f"the GPU address does not answer as expected (with token {code_ok}, without {code_no})")
    if state.get("voice"):
        code_v, body_v = http("GET", url + "/voice/health", headers={"Authorization": f"Bearer {token}"})
        if code_v != 200 or not isinstance(body_v, dict) or body_v.get("status") != "ok":
            die(f"the voice server does not answer as expected (HTTP {code_v})")
    state["url"] = url
    save_state(state)
    say("GPU address is up and refuses requests without the token")
    connect(state, args.api, password, state["models"][0])
    say(f"Done. Test calls now use the GPU. When finished: python {Path(__file__).name} stop")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    state = load_state()
    if not state or not state.get("url"):
        die("no running GPU session: run `start` first")
    connect(state, args.api, atlas_password(args.password), args.model[0] if args.model else state["models"][0])
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = load_state()
    if not state:
        say("no GPU session recorded")
        return 0
    with_voice = " and the expressive voice" if state.get("voice") else ""
    say(f"session {state['kernel']} started {state['started']} for at most {state['hours']:g} h; "
        f"models {', '.join(state['models'])}{with_voice}")
    say("Kaggle: " + kaggle("kernels", "status", state["kernel"]).strip())
    if state.get("url"):
        code, data = http("GET", state["url"] + "/atlas/health", headers={"Authorization": f"Bearer {state['token']}"},
                          timeout=20)
        say(f"GPU address: {'up, ' + json.dumps(data) if code == 200 else f'not answering (HTTP {code})'}")
    for api_base, info in state["connected"].items():
        say(f"connected: {api_base} (model {info.get('model')})")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    state = load_state()
    if not state:
        say("no GPU session recorded")
        return 0
    password = atlas_password(args.password)
    for api_base, info in list(state["connected"].items()):
        try:
            disconnect(api_base, info, password)
            del state["connected"][api_base]
            save_state(state)
        except SystemExit as exc:
            say(f"{api_base}: could not put the model back ({exc}); run stop again")
            return 1
    if state.get("url"):
        code, _ = http("POST", state["url"] + "/atlas/stop", headers={"Authorization": f"Bearer {state['token']}"},
                       timeout=20)
        say("GPU session asked to stop" if code == 202 else
            f"the GPU address did not answer (HTTP {code}); the session ends by itself after {state['hours']:g} h, "
            f"or stop it at https://www.kaggle.com/code/{state['kernel']}")
    STATE.unlink(missing_ok=True)
    say("session record removed")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("start", "connect", "status", "stop"):
        s = sub.add_parser(name)
        s.add_argument("--password", default=None, help=argparse.SUPPRESS)
        if name in ("start", "connect"):
            s.add_argument("--api", default="http://127.0.0.1:8080", help="ATLAS base URL (default: local)")
            s.add_argument("--model", action="append",
                           help=f"model(s) to load; the first is used (default: {', '.join(DEFAULT_MODELS)})")
        if name == "start":
            s.add_argument("--hours", type=float, default=2.0, help="end the session after this long (default 2)")
            s.add_argument("--wait-minutes", type=float, default=20.0, help="give up if not ready by then")
            s.add_argument("--voice", action="store_true",
                           help="also run the expressive voice (Chatterbox-Turbo) and switch ATLAS's voice to it")
    args = p.parse_args()
    return {"start": cmd_start, "connect": cmd_connect, "status": cmd_status, "stop": cmd_stop}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
