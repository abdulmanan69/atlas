"""Self-hosted language model warm-up: what is sent, when, and that it never gets in the way.

A small threaded HTTP server stands in for the model server; the voice service is the
usual respx fake, with the model server's host passed through to the real socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Environment, get_settings
from app.integrations.voice_runtime.workflow_builder import compile_prompt
from app.schemas.voice import AgentConfig, FlowDefinition, ServiceChoice
from app.services import llm_warmup
from app.services.agents import TEMPLATES
from app.services.llm_warmup import (
    WarmupTarget,
    build_warmup_body,
    first_request_prompt,
    is_self_hosted,
    render_template,
    target_for,
    warm_agent_prompt,
)
from app.services.natural_sounds import NO_CUES_RULE
from tests.calls_support import CallsRuntime, create_lead, published_agent, start_call
from tests.conftest import Account
from tests.fake_runtime import VALID_KEY


class FakeModelServer:
    """Records every request; answers like an OpenAI-style chat server after `delay`."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.delay = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                time.sleep(outer.delay)
                payload = json.dumps({"choices": [{"message": {"content": "Hi"}}]}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up (timeout test)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def model_server() -> Iterator[FakeModelServer]:
    server = FakeModelServer()
    yield server
    server.close()


@pytest.fixture
def warmup(monkeypatch: pytest.MonkeyPatch, model_server: FakeModelServer) -> FakeModelServer:
    """Warm-up on, pointed at the fake model server, http allowed (local development)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "local_llm_warmup_enabled", True)
    monkeypatch.setattr(settings, "local_llm_warmup_url", model_server.base_url)
    monkeypatch.setattr(settings, "local_llm_warmup_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "environment", Environment.LOCAL)
    llm_warmup.reset_state()
    return model_server


@pytest.fixture
def runtime() -> Iterator[CallsRuntime]:
    fake = CallsRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        router.route(host="127.0.0.1").pass_through()
        yield fake


async def _finish_warmups() -> list[str]:
    return list(await asyncio.gather(*list(llm_warmup._tasks)))


def _guided() -> AgentConfig:
    return next(t for t in TEMPLATES if t.id == "lead_qualifier").config.model_copy(
        update={"company_name": "Northwind"}
    )


def _target(server: FakeModelServer, *, trusted: bool = True) -> WarmupTarget:
    return WarmupTarget(base_url=server.base_url, model="qwen2.5:1.5b", trusted=trusted)


# ------------------------------------------------------------------ what is sent


def test_template_rendering_matches_the_voice_service() -> None:
    ctx = {"first_name": "Hina", "company": "", "meta": {"city": "Lahore"}, "tags": ["a"]}
    assert render_template("Hi {{first_name}}", ctx) == "Hi Hina"
    assert render_template("Hi {{ last_name | fallback:there }}", ctx) == "Hi there"
    assert render_template("At {{company | your company}}", ctx) == "At your company"
    assert render_template("{{nickname | fallback}}", ctx) == "Nickname"
    assert render_template("{{initial_context.first_name}} in {{meta.city}}", ctx) == (
        "Hina in Lahore"
    )
    assert render_template("{{tags}} {{missing}}!", ctx) == '["a"] !'
    assert render_template("one\\ntwo", ctx) == "one\ntwo"
    assert render_template("{{current_weekday_UTC}}", ctx) in {
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }


def test_first_request_matches_the_compiled_agent() -> None:
    cfg = _guided()
    prompt = first_request_prompt(cfg, {"first_name": "Hina"})
    assert prompt is not None
    assert prompt.system == compile_prompt(cfg)
    assert prompt.greeting is not None and prompt.greeting.startswith("Hi Hina, this is Sarah")
    assert prompt.tools == [
        {
            "type": "function",
            "function": {
                "name": "wrap_up",
                "description": cfg.end_condition,
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]
    body = build_warmup_body(prompt, "qwen2.5:1.5b")
    assert body["max_tokens"] == 1 and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "assistant", "user"]
    assert body["tools"] == prompt.tools


def test_global_rules_and_start_tools_follow_the_flow() -> None:
    cfg = next(t for t in TEMPLATES if t.id == "qualify_and_book").config
    assert cfg.flow is not None and cfg.global_rules
    prompt = first_request_prompt(cfg, {})
    assert prompt is not None and prompt.system.startswith(cfg.global_rules.strip() + "\n\n")
    start = next(n for n in cfg.flow.nodes if n.kind == "start")
    expected = [p.name for p in cfg.flow.paths if p.from_node == start.id]
    assert [t["function"]["name"] for t in prompt.tools or []] == [
        llm_warmup.transition_function_name(n) for n in expected
    ]

    # Rules not applied to this Start; actions on it: tools are left to the voice service.
    flow = FlowDefinition.model_validate(cfg.flow.model_dump())
    first = next(n for n in flow.nodes if n.kind == "start")
    first.use_global_rules = False
    first.action_ids = [str(uuid.uuid4())]
    changed = first_request_prompt(cfg.model_copy(update={"flow": flow}), {})
    assert changed is not None and changed.tools is None
    assert changed.system == first.prompt


def test_only_self_hosted_models_are_warmed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_self_hosted("speaches", None)
    assert is_self_hosted("openai", "http://local-llm:11434/v1")
    assert is_self_hosted("openai", "http://192.168.1.20:8000/v1")
    assert is_self_hosted("openai", "http://localhost:1234/v1")
    assert not is_self_hosted("openai", None)
    assert not is_self_hosted("openai", "https://api.openai.com/v1")
    assert not is_self_hosted("groq", "http://localhost:1234/v1")

    monkeypatch.setattr(get_settings(), "local_llm_warmup_url", None)
    choice = ServiceChoice(connection_id="c1", settings={"model": "qwen2.5:1.5b"})
    target = target_for("speaches", {"base_url": "http://local-llm:11434/v1"}, choice)
    assert target == WarmupTarget(
        base_url="http://local-llm:11434/v1", model="qwen2.5:1.5b", trusted=False
    )
    assert target_for("openai", {}, choice) is None
    assert target_for("speaches", {}, ServiceChoice(connection_id="c1", settings={})) is None
    monkeypatch.setattr(get_settings(), "local_llm_warmup_url", "http://127.0.0.1:11434/v1")
    override = target_for("speaches", {"base_url": "http://local-llm:11434/v1"}, choice)
    assert override is not None and override.trusted
    assert override.chat_url == "http://127.0.0.1:11434/v1/chat/completions"


# ------------------------------------------------------------------ when it is sent


async def test_warmup_is_sent_once_and_deduplicated(warmup: FakeModelServer) -> None:
    async def resolve(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return _target(warmup)

    org = uuid.uuid4()
    assert await warm_agent_prompt(org, _guided(), {"first_name": "Hina"}, resolve=resolve) == (
        "sent"
    )
    assert len(warmup.requests) == 1
    sent = warmup.requests[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["body"]["model"] == "qwen2.5:1.5b" and sent["body"]["max_tokens"] == 1
    assert "Hina" in sent["body"]["messages"][1]["content"]

    # Same prompt again shortly after (a campaign batch): not sent twice.
    assert await warm_agent_prompt(org, _guided(), {"first_name": "Hina"}, resolve=resolve) == (
        "duplicate"
    )
    # A different lead is a different prompt.
    assert await warm_agent_prompt(org, _guided(), {"first_name": "Omar"}, resolve=resolve) == (
        "sent"
    )
    assert len(warmup.requests) == 2


async def test_one_warmup_per_server_at_a_time(warmup: FakeModelServer) -> None:
    warmup.delay = 0.5

    async def resolve(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return _target(warmup)

    first = asyncio.create_task(
        warm_agent_prompt(uuid.uuid4(), _guided(), {"first_name": "A"}, resolve=resolve)
    )
    await asyncio.sleep(0.2)
    # The server reads one prompt at a time: queueing more would delay real replies.
    assert await warm_agent_prompt(
        uuid.uuid4(), _guided(), {"first_name": "B"}, resolve=resolve
    ) == ("busy")
    assert await first == "sent"
    assert len(warmup.requests) == 1


async def test_never_raises_or_waits(
    warmup: FakeModelServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return _target(warmup)

    warmup.delay = 1.0
    monkeypatch.setattr(get_settings(), "local_llm_warmup_timeout_seconds", 0.2)
    assert await warm_agent_prompt(uuid.uuid4(), _guided(), {}, resolve=resolve) == "timeout"

    # Nothing listens on port 9 (Windows retries a refused connect for about 2 s).
    monkeypatch.setattr(get_settings(), "local_llm_warmup_timeout_seconds", 5.0)

    async def unreachable(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return WarmupTarget(base_url="http://127.0.0.1:9/v1", model="m", trusted=True)

    assert await warm_agent_prompt(uuid.uuid4(), _guided(), {}, resolve=unreachable) == (
        "unreachable"
    )

    async def broken(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        raise RuntimeError("voice service down")

    assert await warm_agent_prompt(uuid.uuid4(), _guided(), {}, resolve=broken) == "failed"

    monkeypatch.setattr(get_settings(), "local_llm_warmup_enabled", False)
    assert await warm_agent_prompt(uuid.uuid4(), _guided(), {}, resolve=resolve) == "disabled"
    assert llm_warmup.schedule_published_warmup(uuid.uuid4(), uuid.uuid4(), 1, {}) is None


async def test_destination_policy_applies_to_tenant_urls(
    warmup: FakeModelServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def tenant(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return _target(warmup, trusted=False)  # a connection's own (private) base URL

    assert await warm_agent_prompt(uuid.uuid4(), _guided(), {}, resolve=tenant) == "blocked"
    assert warmup.requests == []
    monkeypatch.setattr(get_settings(), "allow_private_model_urls", True)
    assert await warm_agent_prompt(uuid.uuid4(), _guided(), {}, resolve=tenant) == "sent"

    # Outside local development a tenant's URL must use https, even with private hosts allowed.
    monkeypatch.setattr(get_settings(), "environment", Environment.PRODUCTION)
    assert await warm_agent_prompt(
        uuid.uuid4(), _guided(), {"first_name": "Ann"}, resolve=tenant
    ) == ("blocked")

    # The operator's URL may stay plain http while its host is internal (an API container
    # reaching the model server on the same Docker network) ...
    async def operator(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return _target(warmup)

    assert await warm_agent_prompt(
        uuid.uuid4(), _guided(), {"first_name": "Zed"}, resolve=operator
    ) == ("sent")

    # ... but never across the internet.
    async def operator_public(_org: uuid.UUID, _cfg: AgentConfig) -> WarmupTarget:
        return WarmupTarget(
            base_url="http://models.example.com/v1", model="qwen2.5:1.5b", trusted=True
        )

    assert await warm_agent_prompt(
        uuid.uuid4(), _guided(), {"first_name": "Bo"}, resolve=operator_public
    ) == ("blocked")


# ------------------------------------------------------------------ the call paths


async def _use_self_hosted_llm(client: httpx.AsyncClient, acct: Account) -> None:
    r = await client.post(
        "/api/v1/ai/connections",
        headers=acct.headers,
        json={
            "name": "Local LLM",
            "provider": "speaches",
            "connection_settings": {"base_url": "http://local-llm:11434/v1"},
        },
    )
    assert r.status_code == 201, r.text
    local = r.json()["id"]
    models = (await client.get("/api/v1/ai/models", headers=acct.headers)).json()
    r = await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={**models, "llm": {"connection_id": local, "settings": {"model": "qwen2.5:1.5b"}}},
    )
    assert r.status_code == 200, r.text


async def test_live_call_and_test_call_warm_a_self_hosted_model(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    warmup: FakeModelServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "live_calls_enabled", True)
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner, company="Northwind")

    # A hosted model: nothing is sent to any model server.
    await start_call(client, owner, lead["id"], agent["id"], live=True)
    assert await _finish_warmups() == ["not_self_hosted"]
    assert warmup.requests == []

    await _use_self_hosted_llm(client, owner)
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    assert call["state"] == "dialing"  # the call did not wait for the warm-up
    assert await _finish_warmups() == ["sent"]
    body = warmup.requests[0]["body"]
    assert body["model"] == "qwen2.5:1.5b" and body["max_tokens"] == 1
    assert body["messages"][0]["role"] == "system" and "Hina" in body["messages"][1]["content"]
    # It starts with the global rules the agent was published with, so the prompt cache
    # matches: here only the rule about sound cues (the voice is not expressive).
    published = runtime.last("PUT", r"^/workflow/\d+$")["workflow_definition"]["nodes"]
    rules = next(n["data"]["prompt"] for n in published if n["type"] == "globalNode")
    assert rules == NO_CUES_RULE
    assert body["messages"][0]["content"].startswith(rules + "\n\n")

    # Browser test call from the agent editor.
    r = await client.post(
        f"/api/v1/agents/{agent['id']}/test-session",
        headers=owner.headers,
        json={"lead_id": lead["id"]},
    )
    assert r.status_code == 200, r.text
    # Same agent, same lead, seconds later: the prompt is already warm.
    assert await _finish_warmups() == ["duplicate"]

    # A different lead for the test call is a new prompt.
    other = await create_lead(client, owner, phone="+14155550101", first_name="Omar")
    r = await client.post(
        f"/api/v1/agents/{agent['id']}/test-session",
        headers=owner.headers,
        json={"lead_id": other["id"]},
    )
    assert r.status_code == 200, r.text
    assert await _finish_warmups() == ["sent"]
    assert "Omar" in warmup.requests[-1]["body"]["messages"][1]["content"]
    assert VALID_KEY not in json.dumps(warmup.requests)
