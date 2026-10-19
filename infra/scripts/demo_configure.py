"""Configure the local demo through the ATLAS API (used by setup_local_demo.ps1 / .sh).

Two phases, so the shell script can start the streaming speech server in between:

  connections   sign in as the demo user and make sure the self-hosted model connections
                exist (re-used by name or address, never duplicated). With a Deepgram key
                (env ATLAS_DEMO_DEEPGRAM_KEY) and internet, also the live-transcription
                connection to the local streaming server. Last line: STREAMING=yes|no
  finish        apply the speed preset (--preset), check the models, create the demo
                agent if missing and publish it. Last line: PRESET=<id that is active>

Reads the password from env ATLAS_DEMO_PASSWORD. Never prints a secret.
Run from the API project so httpx is available:
  cd services/api && uv run --no-sync python ../../infra/scripts/demo_configure.py connections
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

DEMO_EMAIL = "demo@example.com"
DEMO_ORG_NAME = "Demo Realty (demo data)"
AGENT_NAME = "Mortgage lead qualifier"
AGENT_TEMPLATE = "lead_qualifier"
POCKET_PRESET = "private_pocket_tts"
STREAMING_PRESET = "private_streaming"

# name, provider, base URL. Names match what was set up by hand; any existing connection
# with the same provider and base URL is re-used too, whatever it is called.
SELF_HOSTED = [
    ("Local LLM (Ollama)", "speaches", "http://local-llm:11434/v1"),
    ("Local speech (Whisper)", "speaches", "http://local-speech:8000/v1"),
    ("Pocket TTS (local-tts)", "speaches", "http://local-tts:8000/v1"),
]
STREAMING = ("Live speech (local-stt)", "deepgram", "http://local-stt:8000")


def say(msg: str) -> None:
    print(f"    {msg}", flush=True)


class Api:
    def __init__(self, base: str, secrets: list[str]) -> None:
        self.client = httpx.Client(base_url=base.rstrip("/") + "/api/v1", timeout=120.0)
        self.secrets = [s for s in secrets if s]
        self.headers: dict[str, str] = {}

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def call(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201)) -> Any:
        r = self.client.request(method, path, json=body, headers=self.headers)
        if r.status_code not in ok:
            raise ApiError(r.status_code, self.redact(_error_text(r)), _error_details(r))
        return r.json() if r.content else None

    def login(self, password: str) -> None:
        try:
            data = self.call("POST", "/auth/login", {"email": DEMO_EMAIL, "password": password})
        except ApiError as exc:
            if exc.status in (401, 403):
                raise SystemExit(
                    f"Sign-in as {DEMO_EMAIL} failed ({exc.status}). The demo user exists with a "
                    "different password than ATLAS_DEMO_PASSWORD in .env. Put the right one in "
                    ".env, or reset the database (see docs/university-demo.md, Troubleshooting)."
                ) from exc
            raise
        memberships = data.get("memberships") or []
        org = next(
            (m["organization"] for m in memberships if m["organization"]["name"] == DEMO_ORG_NAME),
            memberships[0]["organization"] if memberships else None,
        )
        if org is None:
            raise SystemExit(f"{DEMO_EMAIL} has no organization; run the seed first.")
        self.headers = {
            "Authorization": f"Bearer {data['access_token']}",
            "X-Organization-Id": org["id"],
        }
        say(f"signed in as {DEMO_EMAIL} ({org['name']})")


class ApiError(Exception):
    def __init__(self, status: int, message: str, details: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status, self.message, self.details = status, message, details


def _error_text(r: httpx.Response) -> str:
    try:
        err = r.json().get("error") or {}
        return str(err.get("message") or r.text)[:500]
    except ValueError:
        return r.text[:500]


def _error_details(r: httpx.Response) -> Any:
    try:
        return (r.json().get("error") or {}).get("details")
    except ValueError:
        return None


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def find_connection(conns: list[dict], name: str, provider: str, base_url: str) -> dict | None:
    for c in conns:
        if c["name"].strip().lower() == name.lower():
            return c
    for c in conns:
        if c["provider"] == provider and _norm(str(c["connection_settings"].get("base_url", ""))) == _norm(base_url):
            return c
    return None


def internet_ok() -> bool:
    try:
        httpx.get("https://api.deepgram.com/v1/projects", timeout=6.0)
        return True  # any HTTP answer (401 without a key) means the host is reachable
    except httpx.HTTPError:
        return False


def phase_connections(api: Api, deepgram_key: str) -> int:
    conns = api.call("GET", "/ai/connections")
    for name, provider, url in SELF_HOSTED:
        existing = find_connection(conns, name, provider, url)
        if existing:
            say(f"connection '{existing['name']}' already exists ({url})")
            continue
        api.call("POST", "/ai/connections", {
            "name": name, "provider": provider, "connection_settings": {"base_url": url},
        })
        say(f"created connection '{name}' ({url})")

    name, provider, url = STREAMING
    conns = api.call("GET", "/ai/connections")
    streaming = find_connection(conns, name, provider, url)
    if streaming:
        say(f"connection '{streaming['name']}' already exists ({url}): live transcription available")
    elif not deepgram_key:
        say("no Deepgram key given: live transcription is skipped (Whisper is used, fully offline)")
    elif not internet_ok():
        say("a Deepgram key was given but there is no internet: the voice service checks the key "
            "online when the connection is saved, so live transcription is skipped this time")
    else:
        try:
            api.call("POST", "/ai/connections", {
                "name": name, "provider": provider,
                "connection_settings": {"base_url": url},
                "credentials": {"api_key": deepgram_key},
            })
            streaming = {"name": name}
            say(f"created connection '{name}' ({url}); calls send audio only to the local server")
        except ApiError as exc:
            say(f"could not save the live transcription connection ({exc.status}: {exc.message}); "
                "falling back to Whisper")
    print(f"STREAMING={'yes' if streaming else 'no'}", flush=True)
    return 0


def apply_preset(api: Api, preset: str) -> str:
    order = [preset] if preset == POCKET_PRESET else [preset, POCKET_PRESET]
    for pid in order:
        try:
            api.call("POST", f"/ai-settings/presets/{pid}/apply")
            say(f"speed preset '{pid}' applied")
            return pid
        except ApiError as exc:
            missing = (exc.details or {}).get("missing") if isinstance(exc.details, dict) else None
            say(f"preset '{pid}' not applied ({exc.status}: {exc.message}"
                + (f"; missing: {', '.join(missing)}" if missing else "") + ")")
    raise SystemExit("No self-hosted preset could be applied; see the messages above.")


def ensure_agent(api: Api) -> dict:
    agents = api.call("GET", "/agents")
    agent = next((a for a in agents if a["name"].strip().lower() == AGENT_NAME.lower()), None)
    if agent is None:
        archived = api.call("GET", "/agents?include_archived=true")
        old = next((a for a in archived if a["name"].strip().lower() == AGENT_NAME.lower()), None)
        if old is not None:
            api.call("POST", f"/agents/{old['id']}/unarchive")
            agent = old
            say(f"agent '{AGENT_NAME}' was archived: restored it")
    if agent is None:
        agent = api.call("POST", "/agents", {
            "name": AGENT_NAME,
            "template": AGENT_TEMPLATE,
            "description": "Demo agent for the self-hosted browser test call.",
        })
        say(f"created agent '{AGENT_NAME}' from the '{AGENT_TEMPLATE}' template")
    else:
        say(f"agent '{AGENT_NAME}' already exists")
    return agent


def browser_only_settings(api: Api, agent: dict) -> None:
    """The demo agent only takes browser test calls, where there is never an answering
    machine. Voicemail detection would still ask the language model to classify the
    caller's first words and wait up to 3 s for it, delaying the first reply (measured).
    Turn it off for this agent only; phone agents keep their own setting."""
    # The list endpoint returns a summary; save from the full agent so the config validates.
    full = api.call("GET", f"/agents/{agent['id']}")
    config = full.get("config") or {}
    voicemail = (config.get("call_settings") or {}).get("voicemail") or {}
    if voicemail.get("enabled") is False:
        say("voicemail detection already off for the demo agent")
        return
    config.setdefault("call_settings", {}).setdefault("voicemail", {})["enabled"] = False
    try:
        api.call("PUT", f"/agents/{agent['id']}/config",
                 {"config": config, "note": "Demo: browser test calls only, voicemail detection off"})
        say("voicemail detection turned off for the demo agent (browser calls never reach one)")
    except ApiError as exc:
        say(f"could not turn off voicemail detection ({exc.status}: {exc.message}); continuing")


def phase_finish(api: Api, preset: str, web_url: str) -> int:
    active = apply_preset(api, preset)
    try:
        check = api.call("POST", "/ai/models/check")
    except ApiError as exc:
        check = {"ok": False, "problems": [f"the check itself failed ({exc.status}: {exc.message})"]}
    if check.get("ok"):
        say("models check: ok")
    else:
        say("models check reported problems (a test call may still work once models are warm):")
        for p in check.get("problems") or []:
            say(f"  - {api.redact(str(p))}")
    agent = ensure_agent(api)
    browser_only_settings(api, agent)
    try:
        published = api.call("POST", f"/agents/{agent['id']}/publish", {"note": "setup_local_demo"})
        say(f"agent published (version {published.get('published_version')}): "
            f"{web_url.rstrip('/')}/agents/{agent['id']}")
    except ApiError as exc:
        say(f"publishing failed ({exc.status}: {exc.message})")
        if exc.details:
            say(f"  details: {api.redact(str(exc.details))[:800]}")
        print(f"PRESET={active}", flush=True)
        return 2
    print(f"PRESET={active}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("phase", choices=["connections", "finish"])
    p.add_argument("--api", default="http://127.0.0.1:8080")
    p.add_argument("--web", default="http://localhost:5173")
    p.add_argument("--preset", default=POCKET_PRESET, choices=[POCKET_PRESET, STREAMING_PRESET])
    args = p.parse_args()

    password = os.environ.get("ATLAS_DEMO_PASSWORD", "")
    if not password:
        raise SystemExit("ATLAS_DEMO_PASSWORD is not set")
    key = os.environ.get("ATLAS_DEMO_DEEPGRAM_KEY", "").strip()
    api = Api(args.api, [password, key])
    try:
        api.login(password)
        if args.phase == "connections":
            return phase_connections(api, key)
        return phase_finish(api, args.preset, args.web)
    except ApiError as exc:
        print(f"ATLAS API error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"ATLAS API not reachable at {args.api}: {exc.__class__.__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
