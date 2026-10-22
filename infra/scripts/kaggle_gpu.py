#!/usr/bin/env python3
"""Testing only: run ATLAS's models on a Kaggle GPU and point ATLAS at them.

  python infra/scripts/kaggle_gpu.py start     start a GPU session on Kaggle, connect ATLAS
  python infra/scripts/kaggle_gpu.py start --voice --stt
                                               the same, with the expressive voice and live
                                               speech-to-text on the GPU too
  python infra/scripts/kaggle_gpu.py connect --api https://atlas.example.com
                                               connect another ATLAS to the running session
  python infra/scripts/kaggle_gpu.py status    session and connections
  python infra/scripts/kaggle_gpu.py stop      put every connected ATLAS back, end the session

`start` pushes a private Kaggle notebook that runs Ollama (the same version as the
self-hosted stack) on the notebook's GPU behind an AI gateway (ADR 0007: a token check,
/health, /llm/v1 and, when asked for, /voice/v1 and /live-stt/<secret>) and opens a
temporary Cloudflare tunnel to it. ATLAS registers that address as the AI server
"GPU test (Kaggle)" and uses it: the language model switches to it, with the model chosen
before as the fallback; `stop` puts the earlier models back and removes the server.

--voice also runs the expressive voice server (infra/docker/expressive-tts, Chatterbox-Turbo:
it can laugh, sigh and gasp) and switches the organisation's voice to it. --stt also runs the
live speech-to-text server (infra/docker/streaming-stt, Parakeet TDT 0.6B v3 on CUDA) and
points the organisation's live speech-to-text connection at it for the session; that
connection keeps its own key, which this script never sees.

For tests, not for real calls: a Kaggle session ends after --hours (at most 12), the address
changes every session, the call's audio and text go through Kaggle and Cloudflare, and
Kaggle's acceptable-use policy does not allow running a service's backend there.

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
SERVER_NAME = "GPU test (Kaggle)"
DEMO_EMAIL = "demo@example.com"
DEMO_ORG_NAME = "Demo Realty (demo data)"
DEFAULT_MODELS = ["qwen3:8b", "qwen2.5:7b"]
STACK_MODELS = ["qwen3:8b"]  # with the voice or speech-to-text on the same T4, one model
FALLBACK_AFTER_MS = 3000
# The servers the notebook runs besides Ollama, embedded unchanged (base64) from these files.
EMBEDDED = {
    "voice": ("infra/docker/expressive-tts", ("app.py", "requirements.txt")),
    "stt": ("infra/docker/streaming-stt", ("app.py", "fetch_model.py", "requirements.gpu.txt")),
    "health": ("infra/docker/ai-gateway", ("app.py",)),
}
# The address line carries the session's id, so a log of an earlier run is never taken for it.
URL_LINE = re.compile(r"ATLAS_GPU_URL=(https://[a-z0-9-]+\.trycloudflare\.com) session=([0-9a-f]{12})")
FAILED_LINE = re.compile(r"ATLAS_GPU_FAILED session=([0-9a-f]{12}): ([^\"\\]*)")

# The notebook. Placeholders are filled by render_kernel(); it never holds the token or the
# live speech-to-text secret, only their SHA-256, so its source is useless to anyone who
# reads it.
KERNEL = r'''# ATLAS GPU test session (testing only), started by infra/scripts/kaggle_gpu.py.
# Ollama (and with VOICE / STT the expressive voice and live speech-to-text servers) on this
# notebook's GPU behind an AI gateway with a token check, reachable through a temporary
# Cloudflare tunnel until the session is stopped or HOURS have passed.
import base64, json, os, re, shutil, subprocess, sys, threading, time, urllib.error, urllib.request

TOKEN_SHA256 = "@TOKEN_SHA256@"
LIVE_SHA256 = "@LIVE_SHA256@"
MODELS = @MODELS@
HOURS = @HOURS@
VOICE = @VOICE@
STT = @STT@
FILES = @FILES@  # {"voice/app.py": base64, ...}: the servers' own files, unchanged
OLLAMA_VERSION = "0.40.6"
PORT = 8000
VOICE_PORT = 8001
STT_PORT = 8010
HEALTH_PORT = 8020
WORK = "/tmp/atlas"
CLOUDFLARED = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"

PROXY = r"""
import asyncio, hashlib, hmac, json, os, subprocess
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

TOKEN_SHA256 = os.environ["ATLAS_TOKEN_SHA256"]
LIVE_SHA256 = os.environ.get("ATLAS_LIVE_SHA256", "")
LLM_UP = "http://127.0.0.1:11434"
VOICE_UP = os.environ.get("ATLAS_VOICE_UP", "")
STT_UP = os.environ.get("ATLAS_STT_UP", "")
HEALTH_UP = os.environ["ATLAS_HEALTH_UP"]
DROP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "host", "content-length", "authorization",
        "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept"}
served = {"requests": 0, "streams": 0}

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def allowed(req):
    got = req.headers.get("Authorization", "")
    return got.startswith("Bearer ") and hmac.compare_digest(digest(got[7:]), TOKEN_SHA256)

async def relay(req):
    # The live speech-to-text WebSocket (Deepgram's protocol): both ways, until either side closes.
    client = web.WebSocketResponse(max_msg_size=0)
    await client.prepare(req)
    served["streams"] += 1
    target = STT_UP.replace("http://", "ws://") + "/v1/listen" + ("?" + req.query_string if req.query_string else "")
    try:
        async with req.app["s"].ws_connect(target, max_msg_size=0) as server:
            async def pump(src, dst):
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    else:
                        break
                await dst.close()
            await asyncio.gather(pump(client, server), pump(server, client))
    except Exception:
        await client.close(code=1011)
    return client

async def handle(req):
    parts = req.path.split("/")
    if len(parts) == 5 and parts[1] == "live-stt" and parts[3:] == ["v1", "listen"]:
        if not (STT_UP and LIVE_SHA256 and hmac.compare_digest(digest(parts[2]), LIVE_SHA256)):
            return web.json_response({"error": "not found"}, status=404)
        return await relay(req)
    if not allowed(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    if req.path == "/atlas/stop" and req.method == "POST":
        open("/tmp/atlas-stop", "w").close()
        return web.json_response({"stopping": True}, status=202)
    if req.path == "/atlas/health":
        gpu = subprocess.run("nvidia-smi --query-gpu=name,utilization.gpu,memory.used --format=csv,noheader",
                             shell=True, capture_output=True, text=True).stdout.strip()
        return web.json_response({"gpu": gpu, **served, "voice": bool(VOICE_UP), "stt": bool(STT_UP)})
    if req.path == "/health":
        target = HEALTH_UP + "/health"
    elif req.path.startswith("/llm/v1/"):
        target = LLM_UP + req.path_qs[len("/llm"):]
    elif VOICE_UP and req.path.startswith("/voice/v1/"):
        target = VOICE_UP + req.path_qs[len("/voice"):]
    else:
        return web.json_response({"error": "not found"}, status=404)
    served["requests"] += 1
    body = await req.read()
    # qwen3 thinks before answering unless asked not to, and the voice service cannot send
    # that field: add it here for chat requests to qwen3 models.
    if target.startswith(LLM_UP) and req.path.endswith("/chat/completions") and body:
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


def unpack(name):
    """The embedded files of one server, written under WORK/<name>/srv."""
    srv = os.path.join(WORK, name, "srv")
    os.makedirs(srv, exist_ok=True)
    for path, b64 in FILES.items():
        if path.startswith(name + "/"):
            with open(os.path.join(srv, path.split("/", 1)[1]), "wb") as f:
                f.write(base64.b64decode(b64))
    return srv


def uv_path():
    """Installed once, before the installs run side by side (two pip runs at once collide)."""
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=True)
    return shutil.which("uv") or os.path.join(os.path.dirname(sys.executable), "uv")


def clean_env(**extra):
    """An environment for one of the servers: no notebook Python, no Hugging Face token."""
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
              "HUGGINGFACE_HUB_TOKEN"):
        env.pop(k, None)
    env.update(HF_HUB_DISABLE_IMPLICIT_TOKEN="1", HF_HUB_DISABLE_PROGRESS_BARS="1", TQDM_DISABLE="1",
               PYTHONUNBUFFERED="1", TOKENIZERS_PARALLELISM="false", CUDA_VISIBLE_DEVICES="0", **extra)
    return env


def voice_env(filter_ld):
    venv = os.path.join(WORK, "voice", "venv")
    env = clean_env(VIRTUAL_ENV=venv, PATH=os.path.join(venv, "bin") + os.pathsep + os.environ.get("PATH", ""),
                    HF_HOME=os.path.join(WORK, "voice", "hf"),
                    EXPRESSIVE_TTS_VOICE_DIR=os.path.join(WORK, "voice", "voices"))
    if filter_ld:  # CUDA libraries of the image that clash with the torch wheels' own
        keep = [p for p in env.get("LD_LIBRARY_PATH", "").split(":")
                if p and ("nvidia/lib" in p or "x86_64-linux-gnu" in p)]
        env["LD_LIBRARY_PATH"] = ":".join(keep)
    return env


setup = {}


def install_voice():
    """The image's `pip install --no-deps -r requirements.txt`, into a venv, with uv for speed."""
    try:
        srv = unpack("voice")
        venv = os.path.join(WORK, "voice", "venv")
        py = os.path.join(venv, "bin", "python")
        uv = setup["uv"]
        env = dict(os.environ, UV_HTTP_TIMEOUT="600", UV_LINK_MODE="copy")
        subprocess.run([uv, "venv", "-q", venv, "--python", sys.executable, "--seed"], check=True, env=env)
        subprocess.run([uv, "pip", "install", "-q", "--python", py, "--no-deps", "-r",
                        os.path.join(srv, "requirements.txt")], check=True, env=env)
        check = "import torch, perth, chatterbox.tts_turbo; assert torch.cuda.is_available()"
        for filter_ld in (False, True):
            if subprocess.run([py, "-c", check], env=voice_env(filter_ld), capture_output=True).returncode == 0:
                setup["voice"] = dict(srv=srv, py=py, filter_ld=filter_ld)
                return
        setup["voice_error"] = "the voice packages do not import"
    except Exception as e:
        setup["voice_error"] = f"the voice install failed ({type(e).__name__})"


def stt_env():
    """The image's environment (Dockerfile.gpu): the CUDA libraries from the pinned NVIDIA
    packages first; the notebook's own path after them, where the driver's libcuda.so.1 is."""
    venv = os.path.join(WORK, "stt", "venv")
    sp = os.path.join(venv, "lib", "python3.12", "site-packages", "nvidia")
    libs = [os.path.join(sp, d, "lib") for d in ("cuda_runtime", "cublas", "cufft", "curand", "nvjitlink", "cudnn")]
    return clean_env(VIRTUAL_ENV=venv, PATH=os.path.join(venv, "bin") + os.pathsep + os.environ.get("PATH", ""),
                     LD_LIBRARY_PATH=":".join(libs + [os.environ.get("LD_LIBRARY_PATH", "")]),
                     STT_MODEL_DIR=os.path.join(WORK, "stt", "models"), STT_MODEL="parakeet-0.6b-v3",
                     STT_PROVIDER="cuda", STT_MAX_SESSIONS="8", PORT=str(STT_PORT), LOG_LEVEL="INFO")


def install_stt():
    """The GPU image's `pip install -r requirements.gpu.txt` (sherpa-onnx's CUDA build, cp312)
    into a Python 3.12 venv, then the model files, pinned and checked by fetch_model.py."""
    try:
        srv = unpack("stt")
        venv = os.path.join(WORK, "stt", "venv")
        py = os.path.join(venv, "bin", "python")
        uv = setup["uv"]
        env = dict(os.environ, UV_HTTP_TIMEOUT="600", UV_LINK_MODE="copy")
        subprocess.run([uv, "venv", "-q", "-p", "3.12", venv], check=True, env=env)
        subprocess.run([uv, "pip", "install", "-q", "-p", py, "-r", os.path.join(srv, "requirements.gpu.txt")],
                       check=True, env=env)
        subprocess.run([py, "fetch_model.py"], cwd=srv, env=stt_env(), check=True)
        setup["stt"] = dict(srv=srv, py=py)
    except Exception as e:
        setup["stt_error"] = f"the speech-to-text install failed ({type(e).__name__})"


def wait_ready(name, proc, port, tries, log):
    for _ in range(tries):
        if proc.poll() is not None:
            print("".join(open(log).readlines()[-5:]), flush=True)
            fail(f"the {name} server exited")
        try:
            health = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3))
            if health.get("status") == "ok":
                print(f"{name} server ready", flush=True)
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    fail(f"the {name} server did not get ready")


gpu = subprocess.run("nvidia-smi --query-gpu=name --format=csv,noheader", shell=True,
                     capture_output=True, text=True).stdout.strip()
print("GPU:", gpu or "none", flush=True)
if not gpu:
    fail("no GPU in this session (accelerator not granted)")
installs = []
if VOICE or STT:
    setup["uv"] = uv_path()
if VOICE:
    installs.append(threading.Thread(target=install_voice, daemon=True))
if STT:
    installs.append(threading.Thread(target=install_stt, daemon=True))
for t in installs:
    t.start()  # meanwhile Ollama installs and downloads its models
sh("command -v zstd >/dev/null || (apt-get update -qq && apt-get install -y -qq zstd >/dev/null)")
sh(f"curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION={OLLAMA_VERSION} sh >/dev/null")
# Three slots, like the GPU models host: a call's two end-of-call requests (outcome,
# variables) take the two least recently used slots, so the agent's prompt stays cached in
# the third and the next call's first reply does not read it again (3-6 s on a T4).
env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_KEEP_ALIVE="-1", OLLAMA_FLASH_ATTENTION="1",
           OLLAMA_CONTEXT_LENGTH="4096", OLLAMA_NUM_PARALLEL="3", OLLAMA_MAX_LOADED_MODELS=str(len(MODELS)),
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

for t in installs:
    t.join()
for key in ("voice_error", "stt_error"):
    if key in setup:
        fail(setup[key])

voice = stt = None
if VOICE:
    # Downloads the weights (about 3 GB), loads and warms the model and makes the kept openers.
    voice = subprocess.Popen([setup["voice"]["py"], "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                              "--port", str(VOICE_PORT), "--workers", "1"], cwd=setup["voice"]["srv"],
                             env=voice_env(setup["voice"]["filter_ld"]), stdout=open("/tmp/voice.log", "w"),
                             stderr=subprocess.STDOUT)
if STT:
    # Loads the model on the GPU and decodes every padded length once (about half a minute).
    stt = subprocess.Popen([setup["stt"]["py"], "app.py"], cwd=setup["stt"]["srv"], env=stt_env(),
                           stdout=open("/tmp/stt.log", "w"), stderr=subprocess.STDOUT)
if voice is not None:
    wait_ready("voice", voice, VOICE_PORT, 900, "/tmp/voice.log")
if stt is not None:
    wait_ready("speech-to-text", stt, STT_PORT, 600, "/tmp/stt.log")

# The AI gateway's health report (infra/docker/ai-gateway/app.py, unchanged), for ATLAS's
# AI servers screen: which services run here, their models, voices and sounds.
health_srv = unpack("health")
services = ["llm"] + (["voice"] if VOICE else []) + (["live_stt"] if STT else [])
health = subprocess.Popen([sys.executable, "app.py"], cwd=health_srv,
                          env=dict(os.environ, PORT=str(HEALTH_PORT), AI_HEALTH_SERVICES=",".join(services),
                                   AI_HEALTH_LLM_URL="http://127.0.0.1:11434",
                                   AI_HEALTH_VOICE_URL=f"http://127.0.0.1:{VOICE_PORT}",
                                   AI_HEALTH_LIVE_STT_URL=f"http://127.0.0.1:{STT_PORT}"),
                          stdout=open("/tmp/health.log", "w"), stderr=subprocess.STDOUT)
if not wait_http(f"http://127.0.0.1:{HEALTH_PORT}/health", 60):
    fail("the health report did not start")

try:
    import aiohttp  # noqa: F401
except ImportError:
    sh(f"{sys.executable} -m pip install -q aiohttp")
with open("/tmp/atlas_proxy.py", "w") as f:
    f.write(PROXY)
proxy = subprocess.Popen([sys.executable, "/tmp/atlas_proxy.py"],
                         env=dict(os.environ, ATLAS_TOKEN_SHA256=TOKEN_SHA256, ATLAS_LIVE_SHA256=LIVE_SHA256,
                                  ATLAS_PORT=str(PORT), ATLAS_HEALTH_UP=f"http://127.0.0.1:{HEALTH_PORT}",
                                  ATLAS_VOICE_UP=f"http://127.0.0.1:{VOICE_PORT}" if VOICE else "",
                                  ATLAS_STT_UP=f"http://127.0.0.1:{STT_PORT}" if STT else ""))
if not wait_http(f"http://127.0.0.1:{PORT}/health", 30):
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
        urllib.request.urlopen(url + "/health", timeout=10)
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
procs = [p for p in (ollama, voice, stt, health, proxy, tunnel) if p is not None]
while time.time() < end:
    if os.path.exists("/tmp/atlas-stop"):
        reason = "stopped by kaggle_gpu.py stop"
        break
    if any(p.poll() is not None for p in procs):
        reason = "a process exited"
        break
    time.sleep(20)
    if int(time.time()) % 300 < 20:
        print(time.strftime("%H:%M:%S"), "session running", flush=True)
for p in reversed(procs):
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
    except OSError as e:  # unreachable, or a session whose address no longer resolves
        return 0, str(getattr(e, "reason", e))[:300]


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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


def embedded_files(voice: bool, stt: bool) -> dict[str, str]:
    files: dict[str, str] = {}
    for name, (folder, names) in EMBEDDED.items():
        if (name == "voice" and not voice) or (name == "stt" and not stt):
            continue
        for n in names:
            files[f"{name}/{n}"] = base64.b64encode((ROOT / folder / n).read_bytes()).decode()
    return files


def render_kernel(token: str, live_secret: str, models: list[str], hours: float, *,
                  voice: bool = False, stt: bool = False) -> str:
    return (KERNEL.replace("@TOKEN_SHA256@", sha256(token)).replace("@LIVE_SHA256@", sha256(live_secret))
            .replace("@MODELS@", json.dumps(models)).replace("@HOURS@", repr(hours))
            .replace("@FILES@", json.dumps(embedded_files(voice, stt)))
            .replace("@VOICE@", repr(voice)).replace("@STT@", repr(stt)))


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
    return sha256(token)[:12]


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


def selection_body(selection: dict[str, Any]) -> dict[str, Any]:
    return {k: selection.get(k) for k in ("llm", "stt", "tts", "embeddings", "llm_fallback")}


def connect(state: dict[str, Any], api_base: str, password: str, model: str) -> None:
    """Register the session as an AI server in this ATLAS and use it for the organisation."""
    atlas = Atlas(api_base, password)
    recorded = state["connected"].get(api_base, {})
    selection = atlas.call("GET", "/ai/models")
    # What to put back: recorded by the first connect of this session, never overwritten.
    previous = recorded.get("previous") or selection_body(selection)
    for server in atlas.call("GET", "/ai/servers"):
        if server["name"] == SERVER_NAME and server["url"] != state["url"]:
            # An earlier session that was not stopped: its server is gone, remove its record.
            its = {s.get("connection_id") for s in server["services"]}
            if previous.get("llm") and (selection.get("llm") or {}).get("connection_id") in its:
                atlas.call("PUT", "/ai/models", previous)
            atlas.call("DELETE", f"/ai/servers/{server['id']}", ok=(200, 204, 404))
    servers = atlas.call("GET", "/ai/servers")
    server = next((s for s in servers if s["url"] == state["url"]), None)
    if server is None:
        body: dict[str, Any] = {"name": SERVER_NAME, "url": state["url"], "token": state["token"]}
        if state.get("stt"):
            body["live_stt_secret"] = state["live_secret"]
        server = atlas.call("POST", "/ai/servers", body)
    services = {s["service"]: s for s in server["services"]}
    up = sorted(name for name, s in services.items() if s.get("connection_id"))
    say(f"{api_base}: AI server {SERVER_NAME} is {server['status']} ({', '.join(up)})")

    # Live speech-to-text: the organisation's own live-speech connection, which keeps its key,
    # points at the session's server meanwhile (ATLAS never gets the key from here).
    stt_patched = recorded.get("stt_patched")
    if state.get("stt") and not stt_patched:
        current_stt = (previous.get("stt") or {}).get("connection_id")
        conn = next((c for c in atlas.call("GET", "/ai/connections") if c["id"] == current_stt), None)
        if conn and conn["provider"] == "deepgram":
            old = str((conn.get("connection_settings") or {}).get("base_url") or "")
            atlas.call("PATCH", f"/ai/connections/{conn['id']}",
                       {"connection_settings": {"base_url": f"{state['url']}/live-stt/{state['live_secret']}"}})
            stt_patched = {"connection_id": conn["id"], "base_url": old}
            say(f"{api_base}: live speech-to-text now runs on the GPU (Parakeet 0.6B v3)")
        else:
            say(f"{api_base}: no live speech-to-text (Deepgram-type) connection in use; "
                "speech-to-text stays where it is")

    use: dict[str, Any] = {"llm_model": model, "use_live_stt": False, "fallback_after_ms": FALLBACK_AFTER_MS}
    fallback = (previous.get("llm") or {}).get("connection_id")
    if fallback and fallback not in {s.get("connection_id") for s in server["services"]}:
        # If the Kaggle session ends or stalls, the model chosen before answers instead.
        use["fallback_llm_connection_id"] = fallback
    if state.get("voice"):
        use["voice"] = "expressive"
    atlas.call("POST", f"/ai/servers/{server['id']}/use", use)
    state["connected"][api_base] = {"server_id": server["id"], "previous": previous, "model": model,
                                    "stt_patched": stt_patched}
    save_state(state)
    check = atlas.call("POST", "/ai/models/check")
    with_voice = " and the voice is the expressive one" if state.get("voice") else ""
    say(f"{api_base}: language model is now {model} on the Kaggle GPU{with_voice} "
        f"(models check: {'ok' if check.get('ok') else check.get('problems')})")


def disconnect(api_base: str, info: dict[str, Any], password: str) -> None:
    atlas = Atlas(api_base, password)
    if info.get("previous"):
        atlas.call("PUT", "/ai/models", info["previous"])
        say(f"{api_base}: models put back")
    patched = info.get("stt_patched")
    if patched and patched.get("base_url"):
        atlas.call("PATCH", f"/ai/connections/{patched['connection_id']}",
                   {"connection_settings": {"base_url": patched["base_url"]}}, ok=(200, 404))
        say(f"{api_base}: live speech-to-text put back")
    if info.get("server_id"):
        atlas.call("DELETE", f"/ai/servers/{info['server_id']}", ok=(200, 204, 404))
        say(f"{api_base}: AI server {SERVER_NAME} removed")


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
    token, live_secret = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    models = args.model or (STACK_MODELS if args.voice or args.stt else DEFAULT_MODELS)
    extras = [n for n, on in (("the expressive voice", args.voice), ("live speech-to-text", args.stt)) if on]
    say(f"starting {ref} on a T4 GPU with {', '.join(models + extras)} for at most {hours:g} h")
    push_kernel(ref, render_kernel(token, live_secret, models, hours, voice=args.voice, stt=args.stt))
    state = {"kernel": ref, "token": token, "live_secret": live_secret, "models": models,
             "voice": args.voice, "stt": args.stt, "hours": hours, "started": now(), "url": None,
             "connected": {}}
    save_state(state)
    return finish_start(state, args, password)


def finish_start(state: dict[str, Any], args: argparse.Namespace, password: str) -> int:
    token = state["token"]
    url = wait_for_url(state["kernel"], session_id(token), timeout_s=args.wait_minutes * 60)
    code_ok, health = http("GET", url + "/health", headers={"Authorization": f"Bearer {token}"})
    code_no, _ = http("GET", url + "/health")
    if code_ok not in (200, 503) or code_no != 401 or not isinstance(health, dict):
        die(f"the GPU address does not answer as expected (with token {code_ok}, without {code_no})")
    down = [n for n, s in (health.get("services") or {}).items() if not s.get("up")]
    if down:
        die(f"the GPU server reports these services down: {', '.join(down)}")
    state["url"] = url
    save_state(state)
    say(f"GPU address is up ({', '.join(health.get('services') or {})}) and refuses requests without the token")
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
    extras = [n for n, on in (("the expressive voice", state.get("voice")), ("live speech-to-text", state.get("stt"))) if on]
    say(f"session {state['kernel']} started {state['started']} for at most {state['hours']:g} h; "
        f"{', '.join(state['models'] + extras)}")
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
            say(f"{api_base}: could not put the models back ({exc}); run stop again")
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
            s.add_argument("--stt", action="store_true",
                           help="also run live speech-to-text (Parakeet 0.6B v3 on CUDA) and point ATLAS's "
                                "live speech connection at it")
    args = p.parse_args()
    return {"start": cmd_start, "connect": cmd_connect, "status": cmd_status, "stop": cmd_stop}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
