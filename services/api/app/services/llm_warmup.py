"""Warm a self-hosted language model's prompt cache while a call is connecting.

On a CPU, a self-hosted model spends seconds reading the agent's prompt before it can
start the first reply of a call (about 8 s for a 740-token prompt on the 6-core dev
machine), then serves later turns from its prompt cache in about 0.1 s, because each
request only has to read what changed since the last one. The voice service sends its
first request after the caller's first words, so the ringing and the greeting are idle
time. This module sends the same prompt ahead of time, so that first request finds it
already cached. Measurements are in docs/local-models.md ("Warming the prompt").

The cache only helps up to the first token that differs, so the warm-up reproduces the
voice service's first request (verified against its code and a captured request body):

    system     global rules (with the rule about sound cues, natural_sounds.py) + blank
               line + the Start prompt, each rendered with the call's variables
               (`render_template` mirrors the voice service's renderer)
    tools      one function per path that leaves the Start, in path order
    assistant  the greeting, once it has been spoken
    user       the caller's first words (unknown yet; the warm-up stops before them)

The default model's chat template writes the system text first and the tool list after
it, in the same system block. When the Start uses actions or documents, their tool
definitions live in the voice service, so the warm-up leaves tools out and still caches
the system text.

Everything here is best effort and runs in a background task: it never delays a call or
makes one fail. Only self-hosted models are warmed (registered AI servers with their
token), at most one warm-up per server at a time, and the same prompt is not sent twice
within a short window.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import ipaddress
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.core.config import get_settings
from app.core.exceptions import ValidationFailedError
from app.core.net_policy import validate_destination_url
from app.integrations.voice_runtime.workflow_builder import effective_flow, global_rules_text
from app.schemas.voice import AgentConfig, ServiceChoice

log = logging.getLogger(__name__)

SELF_HOSTED_PROVIDERS = frozenset({"speaches"})
# Providers that speak the OpenAI chat API and accept a custom base URL. A base URL on a
# private address or a bare hostname (a container name) means a self-hosted server.
OPENAI_COMPATIBLE_PROVIDERS = frozenset({"openai"})
# The voice service's own default base URL for the self-hosted provider.
SELF_HOSTED_DEFAULT_BASE_URL = "http://localhost:11434/v1"
# Stands in for the caller's first words, which are not known yet. Everything before
# them is what the voice service will send.
WARMUP_USER_TEXT = "Hello?"
DEDUPE_SECONDS = 120.0

# ------------------------------------------------------------------ template rendering
# Same syntax and results as the voice service's api/utils/template_renderer.py:
# {{name}}, {{a.b.c}}, {{name | default}}, {{name | fallback:default}}, the built-in
# current_time / current_weekday variables, and literal "\n" turned into a newline.

_VAR = re.compile(r"\{\{\s*([^|\s}]+)(?:\s*\|\s*([^:}]+)(?::([^}]+))?)?\s*\}\}")
_CURRENT_TIME = "current_time"
_CURRENT_WEEKDAY = "current_weekday"
_INITIAL_CONTEXT = "initial_context."
_TZ_IN_TEMPLATE = re.compile(
    r"\{\{\s*(?:" + _CURRENT_TIME + "|" + _CURRENT_WEEKDAY + r")_([^|\s}]+)"
)


def _nested(obj: Any, path: str) -> Any:
    current = obj
    for key in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _builtin(path: str, default_tz: str | None) -> str | None:
    def now(tz: str | None) -> datetime | None:
        try:
            return datetime.now(ZoneInfo(tz or "UTC"))
        except (KeyError, ValueError):
            return None

    if path in (_CURRENT_TIME, _CURRENT_WEEKDAY):
        moment = now(default_tz)
    elif path.startswith((_CURRENT_TIME + "_", _CURRENT_WEEKDAY + "_")):
        moment = now(path.split("_", 2)[2])
    else:
        return None
    if moment is None:
        return None
    if path.startswith(_CURRENT_TIME):
        return moment.strftime("%Y-%m-%d %H:%M:%S %Z")
    return moment.strftime("%A")


def render_template(template: str | None, context: Mapping[str, Any]) -> str:
    """Fill `{{...}}` placeholders exactly as the voice service does for prompts."""
    if not template:
        return template or ""
    tz_match = _TZ_IN_TEMPLATE.search(template)
    default_tz = tz_match.group(1).strip() if tz_match else None

    def replace(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        filter_name = match.group(2).strip() if match.group(2) else None
        filter_value = match.group(3).strip() if match.group(3) else None
        value: Any = _builtin(path, default_tz)
        if value is None:
            value = _nested(context, path)
            if value is None and path.startswith(_INITIAL_CONTEXT):
                value = _nested(context, path[len(_INITIAL_CONTEXT) :])
            if filter_name is not None and value in (None, ""):
                if filter_name == "fallback":
                    value = filter_value if filter_value is not None else path.title()
                else:
                    value = filter_name
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return str(value)

    return _VAR.sub(replace, template).replace("\\n", "\n")


# ------------------------------------------------------------------ the request


def transition_function_name(path_name: str) -> str:
    """The function name the voice service gives a path (workflow_graph.py)."""
    return re.sub(r"[^a-z0-9]", "_", path_name.lower())


@dataclass(frozen=True, slots=True)
class WarmupPrompt:
    system: str
    greeting: str | None
    tools: list[dict[str, Any]] | None  # None: the Start has tools ATLAS cannot rebuild


def first_request_prompt(
    cfg: AgentConfig, variables: Mapping[str, Any], *, sound_rule: str = ""
) -> WarmupPrompt | None:
    """What the voice service's first model request starts with, for these variables.

    `sound_rule` is the rule about sound cues the agent was published with
    (natural_sounds.agent_sound_rule); it ends the global rules.
    """
    flow = effective_flow(cfg)
    start = next((n for n in flow.nodes if n.kind == "start"), None)
    if start is None:
        return None
    rules = global_rules_text(cfg, sound_rule)
    parts = [
        render_template(rules, variables) if rules and start.use_global_rules else "",
        render_template(start.prompt, variables),
    ]
    system = "\n\n".join(p for p in parts if p)
    if not system:
        return None
    greeting = render_template(start.greeting, variables) or None
    tools: list[dict[str, Any]] | None = None
    if not start.action_ids and not start.knowledge_ids:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": transition_function_name(p.name),
                    "description": p.when,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }
            for p in flow.paths
            if p.from_node == start.id
        ]
    return WarmupPrompt(system=system, greeting=greeting, tools=tools)


def build_warmup_body(prompt: WarmupPrompt, model: str) -> dict[str, Any]:
    """An OpenAI-style chat request that reads the prompt and generates one token."""
    messages: list[dict[str, str]] = [{"role": "system", "content": prompt.system}]
    if prompt.greeting:
        messages.append({"role": "assistant", "content": prompt.greeting})
    messages.append({"role": "user", "content": WARMUP_USER_TEXT})
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": 1,
        "stream": False,
    }
    if prompt.tools:
        body["tools"] = prompt.tools
    return body


# ------------------------------------------------------------------ where to send it


@dataclass(frozen=True, slots=True)
class WarmupTarget:
    base_url: str  # OpenAI-style base, e.g. http://127.0.0.1:11434/v1
    model: str
    trusted: bool  # operator-configured (ATLAS_LOCAL_LLM_WARMUP_URL), not tenant-supplied
    # A registered AI server (ADR 0007): reached at its own address with its token.
    api_key: str | None = field(default=None, repr=False)
    # The rule about sound cues in the agent's global rules (natural_sounds.py), which
    # depends on its voice.
    sound_rule: str = ""

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _is_local_host(host: str) -> bool:
    if host == "localhost" or "." not in host:  # bare names are container/service names
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith((".local", ".internal", ".localhost"))
    return not address.is_global


def is_self_hosted(provider: str, base_url: str | None) -> bool:
    """A self-hosted provider, or an OpenAI-compatible base URL on a local server."""
    if provider in SELF_HOSTED_PROVIDERS:
        return True
    if provider in OPENAI_COMPATIBLE_PROVIDERS and base_url:
        try:
            host = urlsplit(base_url.strip()).hostname
        except ValueError:
            return False
        return bool(host) and _is_local_host(host or "")
    return False


def target_for(
    provider: str, connection_settings: Mapping[str, Any], choice: ServiceChoice
) -> WarmupTarget | None:
    """The warm-up destination for a language-model choice, or None when not self-hosted."""
    base_url = str(connection_settings.get("base_url") or "").strip() or None
    if not is_self_hosted(provider, base_url):
        return None
    model = str(choice.settings.get("model") or "").strip()
    if not model:
        return None
    override = (get_settings().local_llm_warmup_url or "").strip()
    if override:
        return WarmupTarget(base_url=override, model=model, trusted=True)
    if provider in SELF_HOSTED_PROVIDERS and not base_url:
        base_url = SELF_HOSTED_DEFAULT_BASE_URL
    if not base_url:
        return None
    return WarmupTarget(base_url=base_url, model=model, trusted=False)


async def check_destination(target: WarmupTarget) -> bool:
    """Apply the outbound destination policy (app/core/net_policy.py).

    The operator's ATLAS_LOCAL_LLM_WARMUP_URL may be private (that is its purpose), and
    may use plain http when its host is internal (a model server on the same Docker
    network); it still gets the credential check, and https otherwise. A tenant's
    connection URL must resolve to a public address unless ATLAS_ALLOW_PRIVATE_MODEL_URLS
    is true, and must use https outside local development. A registered AI server gets
    the rules it was registered under: with ATLAS_ALLOW_PRIVATE_MODEL_URLS also plain http
    on a private network.
    """
    allow_private = target.trusted or get_settings().allow_private_model_urls
    registered = target.api_key is not None
    try:
        await validate_destination_url(
            target.chat_url,
            label="Self-hosted model",
            allow_private=allow_private,
            allow_internal_http=target.trusted or (registered and allow_private),
        )
    except ValidationFailedError as exc:
        log.info("llm_warmup_skipped", extra={"reason": "destination_policy", "detail": str(exc)})
        return False
    return True


# ------------------------------------------------------------------ sending


_in_flight: set[str] = set()
_recent: dict[str, float] = {}
_tasks: set[asyncio.Task[str]] = set()


def _fingerprint(target: WarmupTarget, body: Mapping[str, Any]) -> str:
    raw = json.dumps([target.chat_url, body], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def reset_state() -> None:
    """Forget in-flight and recent warm-ups (tests)."""
    _in_flight.clear()
    _recent.clear()


async def send_warmup(target: WarmupTarget, body: dict[str, Any]) -> str:
    """POST one warm-up request. Returns an outcome code; never raises.

    One warm-up per server at a time: the server answers one request at a time, so a
    queue of warm-ups would delay the real first request of an earlier call.
    """
    key = target.chat_url
    fingerprint = _fingerprint(target, body)
    now = time.monotonic()
    if now - _recent.get(fingerprint, -DEDUPE_SECONDS) < DEDUPE_SECONDS:
        return "duplicate"
    if key in _in_flight:
        return "busy"
    if not await check_destination(target):
        return "blocked"
    _in_flight.add(key)
    _recent[fingerprint] = now
    for stale in [k for k, t in _recent.items() if now - t >= DEDUPE_SECONDS]:
        _recent.pop(stale, None)
    timeout = get_settings().local_llm_warmup_timeout_seconds
    started = time.perf_counter()
    try:
        headers = {"Authorization": f"Bearer {target.api_key}"} if target.api_key else None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)), follow_redirects=False
        ) as client:
            response = await client.post(key, json=body, headers=headers)
        outcome = "sent" if response.is_success else f"http_{response.status_code}"
    except httpx.TimeoutException:
        outcome = "timeout"
    except httpx.HTTPError as exc:
        outcome = "unreachable"
        log.debug("llm_warmup_error", extra={"error": type(exc).__name__})
    finally:
        _in_flight.discard(key)
    log.info(
        "llm_warmup",
        extra={
            "outcome": outcome,
            "model": target.model,
            "ms": round((time.perf_counter() - started) * 1000),
        },
    )
    return outcome


TargetResolver = Callable[[uuid.UUID, AgentConfig], Awaitable[WarmupTarget | None]]


async def resolve_target(org_id: uuid.UUID, cfg: AgentConfig) -> WarmupTarget | None:
    """The language model this agent's calls use, if it is self-hosted.

    The agent's own model override wins over the organization's default selection,
    as in the voice service. A connection ATLAS created for a registered AI server is
    warmed at that server's own address with its token, never at the operator's
    ATLAS_LOCAL_LLM_WARMUP_URL (that is a different server). The target carries the rule
    about sound cues for the agent's voice, which is part of the prompt.
    """
    from app.db.session import get_sessionmaker  # local imports: keep this module light
    from app.services.natural_sounds import agent_sound_rule

    target = await _llm_target(org_id, cfg)
    if target is None:
        return None
    async with get_sessionmaker()() as db:
        rule = await agent_sound_rule(db, org_id, cfg)
    return dataclasses.replace(target, sound_rule=rule)


async def _llm_target(org_id: uuid.UUID, cfg: AgentConfig) -> WarmupTarget | None:
    from app.db.session import get_sessionmaker
    from app.services.ai_servers import gateway_for
    from app.services.ai_settings import get_selection_for_org
    from app.services.runtime_accounts import runtime_client

    async with get_sessionmaker()() as db:
        choice = cfg.model_override.llm if cfg.model_override else None
        if choice is None:
            choice = (await get_selection_for_org(db, org_id)).llm
        if choice is None:
            return None
        gateway = await gateway_for(db, org_id, choice.connection_id, "llm")
        if gateway is not None:
            model = str(choice.settings.get("model") or "").strip()
            if not model:
                return None
            return WarmupTarget(
                base_url=gateway.base_url, model=model, trusted=False, api_key=gateway.token
            )
        async with runtime_client(db, org_id) as client:
            connections = await client.list_connections()
    connection = next((c for c in connections if c.uuid == choice.connection_id), None)
    if connection is None or not connection.is_active:
        return None
    return target_for(connection.provider, connection.connection_settings, choice)


async def warm_agent_prompt(
    org_id: uuid.UUID,
    cfg: AgentConfig,
    variables: Mapping[str, Any],
    *,
    resolve: TargetResolver | None = None,
) -> str:
    """Warm the model for one call. Returns an outcome code; never raises."""
    if not get_settings().local_llm_warmup_enabled:
        return "disabled"
    try:
        target = await (resolve or resolve_target)(org_id, cfg)
        if target is None:
            return "not_self_hosted"
        prompt = first_request_prompt(cfg, variables, sound_rule=target.sound_rule)
        if prompt is None:
            return "no_prompt"
        return await send_warmup(target, build_warmup_body(prompt, target.model))
    except Exception as exc:  # best effort: a warm-up must never affect the call
        log.info("llm_warmup_failed", extra={"error": type(exc).__name__})
        return "failed"


async def warm_published_agent(
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    version: int,
    variables: Mapping[str, Any],
    *,
    resolve: TargetResolver | None = None,
) -> str:
    """Load the published configuration in its own session, then warm. Never raises."""
    from sqlalchemy import select

    from app.db.session import get_sessionmaker
    from app.models.voice import AgentVersion

    try:
        async with get_sessionmaker()() as db:
            raw = (
                await db.execute(
                    select(AgentVersion.config).where(
                        AgentVersion.organization_id == org_id,
                        AgentVersion.agent_id == agent_id,
                        AgentVersion.version == version,
                    )
                )
            ).scalar_one_or_none()
        if raw is None:
            return "no_prompt"
        cfg = AgentConfig.model_validate(raw)
    except Exception as exc:
        log.info("llm_warmup_failed", extra={"error": type(exc).__name__})
        return "failed"
    return await warm_agent_prompt(org_id, cfg, variables, resolve=resolve)


def schedule_published_warmup(
    org_id: uuid.UUID,
    agent_id: uuid.UUID,
    version: int | None,
    variables: Mapping[str, Any],
    *,
    resolve: TargetResolver | None = None,
) -> asyncio.Task[str] | None:
    """Warm the model for a call that has just started, in the background.

    Returns at once; the task never raises. Called after the voice service accepted the
    call (outbound) or the browser test session, so it cannot delay or fail either.
    """
    if version is None or not get_settings().local_llm_warmup_enabled:
        return None
    task = asyncio.create_task(
        warm_published_agent(org_id, agent_id, version, dict(variables), resolve=resolve),
        name="llm-warmup",
    )
    _tasks.add(task)  # keep a reference until it finishes
    task.add_done_callback(_tasks.discard)
    return task
