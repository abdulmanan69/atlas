"""Shared fakes for the calls and webhooks tests.

`CallsRuntime` extends the contract-faithful fake runtime with the parts the calls
domain uses: the outbound trigger, run detail, and the artifact download redirect to
object storage (served by `CallsRuntime.storage`).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from app.core.config import get_settings
from tests.conftest import Account
from tests.fake_runtime import VALID_KEY, FakeRuntime, Org

STORAGE = "http://storage.test"
PHONE = "+14155550100"


class CallsRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.runs: dict[int, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}  # storage key -> bytes
        self.capacity_full = False

    # ------------------------------------------------------------ run fixtures

    def add_run(
        self,
        org_id: int,
        workflow_id: int,
        *,
        initial_context: dict[str, Any] | None = None,
        call_type: str = "outbound",
        completed: bool = False,
    ) -> int:
        rid = next(self._ids)
        self.runs[rid] = {
            "_org": org_id,
            "id": rid,
            "workflow_id": workflow_id,
            "name": f"WR-TEL-{rid:08d}",
            "mode": "twilio",
            "created_at": "2026-10-18T10:00:00+00:00",
            "is_completed": completed,
            "call_type": call_type,
            "transcript_url": None,
            "recording_url": None,
            "public_access_token": None,
            "cost_info": None,
            "usage_info": None,
            "definition_id": 1,
            "initial_context": initial_context or {},
            "gathered_context": {},
            "logs": None,
            "annotations": None,
        }
        return rid

    def finish(
        self,
        run_id: int,
        *,
        disposition: str | None = "qualified",
        call_status: str | None = "completed",
        extracted: dict[str, Any] | None = None,
        duration: float = 42.0,
        transcript: str | None = None,
        recording: bytes | None = None,
    ) -> None:
        run = self.runs[run_id]
        gathered: dict[str, Any] = {}
        if disposition:
            gathered["call_disposition"] = disposition
        if call_status:
            gathered["call_status"] = call_status
        if extracted is not None:
            gathered["extracted_variables"] = extracted
        run["gathered_context"] = gathered
        run["is_completed"] = True
        run["usage_info"] = {
            "llm": {"gpt-4.1-mini": {"prompt_tokens": 900, "completion_tokens": 120}},
            "stt": {},
            "tts": {},
            "call_duration_seconds": duration,
        }
        run["cost_info"] = {"call_duration_seconds": int(duration)}
        if transcript is not None:
            key = f"transcripts/{run_id}.txt"
            self.objects[key] = transcript.encode("utf-8")
            run["transcript_url"] = key
        if recording is not None:
            key = f"recordings/{run_id}.wav"
            self.objects[key] = recording
            run["recording_url"] = key
        if run["transcript_url"] or run["recording_url"]:
            run["public_access_token"] = f"pat-{uuid.uuid4().hex}"

    # ------------------------------------------------------------ endpoints

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if m == "POST" and (mm := re.fullmatch(r"/public/agent/workflow/([\w-]+)", path)):
            wf = next((w for w in org.workflows.values() if w["workflow_uuid"] == mm[1]), None)
            if wf is None:
                return httpx.Response(404, json={"detail": "Workflow not found"})
            if self.capacity_full:
                return httpx.Response(429, json={"detail": "Concurrent call limit reached"})
            rid = self.add_run(
                org.id,
                wf["id"],
                initial_context={
                    **body["initial_context"],
                    "phone_number": body["phone_number"],
                    "called_number": body["phone_number"],
                    "direction": "outbound",
                },
            )
            return httpx.Response(
                200,
                json={
                    "status": "queued",
                    "workflow_run_id": rid,
                    "workflow_run_name": self.runs[rid]["name"],
                },
            )
        if m == "GET" and (mm := re.fullmatch(r"/workflow/(\d+)/runs/(\d+)", path)):
            run = self.runs.get(int(mm[2]))
            if run is None or run["_org"] != org.id or run["workflow_id"] != int(mm[1]):
                return httpx.Response(404, json={"detail": "Workflow run not found"})
            return httpx.Response(200, json={k: v for k, v in run.items() if k[0] != "_"})
        if m == "GET" and (mm := re.fullmatch(r"/public/download/workflow/([\w-]+)/(\w+)", path)):
            run = next((r for r in self.runs.values() if r["public_access_token"] == mm[1]), None)
            if run is None:
                return httpx.Response(404, json={"detail": "Invalid or expired token"})
            key = {"transcript": run["transcript_url"], "recording": run["recording_url"]}.get(
                mm[2]
            )
            if not key:
                return httpx.Response(404, json={"detail": f"No {mm[2]} available"})
            return httpx.Response(302, headers={"location": f"{STORAGE}/voice-audio/{key}"})
        return super()._authed(org, m, path, body, request)

    def storage(self, request: httpx.Request) -> httpx.Response:
        key = request.url.path.removeprefix("/voice-audio/")
        data = self.objects.get(key)
        if data is None:
            return httpx.Response(404, content=b"not found")
        content_type = "audio/wav" if key.endswith(".wav") else "text/plain"
        return httpx.Response(200, content=data, headers={"content-type": content_type})


def make_runtime() -> Iterator[CallsRuntime]:
    """Mount a CallsRuntime for one test; wrap it in a module-level fixture."""
    fake = CallsRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        router.route(host="storage.test").mock(side_effect=fake.storage)
        yield fake


def enable_live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "live_calls_enabled", True)


def runtime_org(fake: CallsRuntime) -> Org:
    orgs = [o for o in fake.orgs.values() if o.workflows]
    assert len(orgs) == 1, "expected exactly one organization with a published agent"
    return orgs[0]


def webhook_secret(org: Org) -> str:
    credential = next(iter(org.credentials.values()))
    return str(credential["credential_data"]["header_value"])


def workflow_id(org: Org) -> int:
    return next(iter(org.workflows))


async def published_agent(client: httpx.AsyncClient, acct: Account) -> dict[str, Any]:
    ids = {}
    for provider in ("openai", "deepgram", "elevenlabs"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=acct.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}},
        )
        assert r.status_code == 201, r.text
        ids[provider] = r.json()["id"]
    r = await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={
            "llm": {"connection_id": ids["openai"], "settings": {"model": "gpt-4.1-mini"}},
            "stt": {"connection_id": ids["deepgram"], "settings": {}},
            "tts": {"connection_id": ids["elevenlabs"], "settings": {"voice": "v1"}},
        },
    )
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/agents", headers=acct.headers, json={"name": "Qualifier"})
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    r = await client.post(f"/api/v1/agents/{agent_id}/publish", headers=acct.headers, json={})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def create_lead(
    client: httpx.AsyncClient, acct: Account, phone: str = PHONE, **extra: Any
) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/leads",
        headers=acct.headers,
        json={"first_name": "Hina", "phone": phone, **extra},
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def start_call(
    client: httpx.AsyncClient, acct: Account, lead_id: str, agent_id: str, *, live: bool = False
) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/calls",
        headers=acct.headers,
        json={"lead_id": lead_id, "agent_id": agent_id, "live": live},
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]
