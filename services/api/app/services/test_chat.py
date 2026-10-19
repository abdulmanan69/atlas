"""Typed test chats with the published agent (agent editor, "Type" mode).

The browser never talks to the voice service here. ATLAS opens the voice service's text
chat with the organization's own credentials and relays each agent turn to the browser
as server-sent events in ATLAS's own shape (`TestChatEvent`): no runtime credentials,
ids or wording reach the page. Like a browser test call (`agents.test_session`), a chat
is recorded under Calls (mode `test`, `usage.channel = "text"`) before it starts; its
run completes like a call (the completion webhook fills transcript and outcome when the
chat ends) and, being mode `test`, it never counts in analytics or touches the lead.
"""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.exceptions import AppError, InvalidStateTransitionError, NotFoundError
from app.integrations.voice_runtime.client import (
    RuntimeRejectedError,
    RuntimeUnavailableError,
    TextChatStream,
)
from app.models.calls import TERMINAL_STATES, Call, CallMode
from app.models.voice import AgentStatus
from app.schemas.test_chat import TestChatEvent
from app.services import audit, llm_warmup
from app.services import calls as call_service
from app.services import leads as lead_service
from app.services.agents import SAMPLE_VARIABLES, get_agent, lead_variables
from app.services.latency import KNOWLEDGE_TOOL
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

TURN_FAILED = "The agent could not finish its reply. Try sending your message again."
CHAT_ENDED = "This test chat has ended. Start a new one."


class ChatEndedError(InvalidStateTransitionError):
    """The chat is over (ended by the tester, the agent or inactivity): start a new one."""

    code = "test_chat_ended"

    def __init__(self) -> None:
        super().__init__(CHAT_ENDED)


@dataclass
class ChatRelay:
    """A chat turn being relayed: the record it belongs to and the SSE body for the browser."""

    call_id: uuid.UUID
    body: AsyncIterator[bytes]


def _sse(event: TestChatEvent) -> bytes:
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n".encode()


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def translate(raw: dict[str, Any], at_ms: int, *, replied: bool) -> list[TestChatEvent]:
    """The voice service's text-chat stream event as ATLAS chat events (often none).

    `replied`: an `agent_text` was already sent for this turn. When a turn completes
    without having streamed its message (older runs), the stored reply is sent instead.
    Error details from the voice service are never forwarded.
    """
    at = max(0, at_ms)
    kind = raw.get("type")
    if kind == "turn_event":
        event = _dict(raw.get("event"))
        payload = _dict(event.get("payload"))
        name = event.get("type")
        if name == "bot_speech" and (text := _text(payload.get("text"))):
            return [TestChatEvent(type="agent_text", at_ms=at, text=text)]
        if name in ("tool_call_started", "tool_call_result"):
            function = _text(payload.get("function_name")) or "tool"
            return [
                TestChatEvent(
                    type="tool_start" if name == "tool_call_started" else "tool_end",
                    at_ms=at,
                    tool_id=_text(payload.get("tool_call_id")),
                    name=function,
                    kind="knowledge" if function == KNOWLEDGE_TOOL else "action",
                )
            ]
        if name == "node_transition" and (step := _text(payload.get("node_name"))):
            return [TestChatEvent(type="step", at_ms=at, name=step)]
        if name == "execution_error":
            return [TestChatEvent(type="error", at_ms=at, message=TURN_FAILED)]
        return []
    if kind == "complete":
        session = _dict(raw.get("session"))
        out: list[TestChatEvent] = []
        if not replied and (text := _last_reply(session)):
            out.append(TestChatEvent(type="agent_text", at_ms=at, text=text))
        out.append(TestChatEvent(type="done", at_ms=at, ended=bool(session.get("is_completed"))))
        return out
    if kind == "error":
        return [TestChatEvent(type="error", at_ms=at, message=TURN_FAILED)]
    return []


def _last_reply(session: dict[str, Any]) -> str | None:
    turns = _dict(session.get("session_data")).get("turns")
    last = _dict(turns[-1]) if isinstance(turns, list) and turns else {}
    return _text(_dict(last.get("assistant_message")).get("text"))


async def _relay(
    stack: contextlib.AsyncExitStack,
    stream: TextChatStream,
    started: float,
    *,
    call_id: uuid.UUID | None,
) -> AsyncIterator[bytes]:
    """Translate one streamed turn; always ends with `done` or `error`, then closes it."""

    def elapsed() -> int:
        return round((time.monotonic() - started) * 1000)

    async with stack:
        if call_id is not None:
            yield _sse(TestChatEvent(type="started", at_ms=elapsed(), call_id=call_id))
        replied = finished = False
        try:
            async for raw in stream.events():
                if raw.get("type") == "ping":
                    yield b": keep-alive\n\n"
                    continue
                for event in translate(raw, elapsed(), replied=replied):
                    replied = replied or event.type == "agent_text"
                    finished = finished or event.type in ("done", "error")
                    yield _sse(event)
                if raw.get("type") in ("complete", "error"):
                    break
        except (httpx.HTTPError, AppError) as exc:
            log.warning("test_chat_stream_lost", extra={"error_type": type(exc).__name__})
        if not finished:
            message = "The connection to the agent was lost. Send your message again."
            yield _sse(TestChatEvent(type="error", at_ms=elapsed(), message=message))


def _check_testable(agent: Any) -> None:
    if agent.status == AgentStatus.ARCHIVED:
        raise InvalidStateTransitionError("Archived agents cannot be tested")
    if not agent.runtime_workflow_id or agent.published_version is None:
        raise InvalidStateTransitionError("Publish the agent before starting a test chat")


async def start(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, lead_id: uuid.UUID | None
) -> ChatRelay:
    """Record the chat, open it with the voice service and relay the greeting turn.

    The record is committed before the greeting streams: the browser sends its next
    message with the call id. If the voice service refuses the chat, the request rolls
    back and no record is left.
    """
    agent = await get_agent(db, ctx, agent_id)
    _check_testable(agent)
    lead = await lead_service.get_lead(db, ctx, lead_id) if lead_id else None
    variables = lead_variables(lead) if lead is not None else dict(SAMPLE_VARIABLES)
    call = await call_service.create_test_call(
        db, ctx, agent, lead_id=lead.id if lead is not None else None, channel="text"
    )
    context = {**variables, "atlas_call_id": str(call.id), "atlas_test": "true"}
    stack = contextlib.AsyncExitStack()
    try:
        client = await stack.enter_async_context(runtime_client(db, ctx.org_id))
        started = time.monotonic()
        try:
            stream = await stack.enter_async_context(
                client.open_text_chat(
                    str(agent.runtime_workflow_id),
                    context=context,
                    name=f"Test chat {call.id.hex[:8]}",
                )
            )
        except RuntimeRejectedError as exc:
            # Its wording (quota, model errors) is the voice service's, not for the page.
            log.warning("test_chat_refused", extra={"runtime_status": exc.runtime_status})
            raise RuntimeRejectedError(
                "The test chat could not start. Check the agent's AI providers, publish "
                "again and retry.",
                runtime_status=exc.runtime_status,
            ) from exc
        if stream.run_id is None:
            raise RuntimeUnavailableError("The voice service did not start the chat.")
        call_service.mark_test_call_started(db, call, stream.run_id)
        # Self-hosted models only: read the prompt while the tester reads the greeting.
        llm_warmup.schedule_published_warmup(ctx.org_id, agent.id, agent.published_version, context)
        await audit.record(
            db,
            action="agent.test_chat_started",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="agent",
            resource_id=agent.id,
            changes={"lead_id": str(lead_id) if lead_id else None, "call_id": str(call.id)},
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
        await db.commit()
    except BaseException:
        await stack.aclose()
        raise
    return ChatRelay(call.id, _relay(stack, stream, started, call_id=call.id))


async def _chat_call(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, call_id: uuid.UUID
) -> Call:
    call = await db.get(Call, call_id)
    if (
        call is None
        or call.organization_id != ctx.org_id
        or call.agent_id != agent_id
        or call.mode != CallMode.TEST
        or call_service.call_channel(call) != "text"
    ):
        raise NotFoundError("Test chat not found")
    return call


async def send(
    db: AsyncSession,
    ctx: TenantContext,
    agent_id: uuid.UUID,
    call_id: uuid.UUID,
    text: str,
) -> ChatRelay:
    """Pass the tester's message to the chat and relay the agent's reply turn."""
    await get_agent(db, ctx, agent_id)  # same visibility rules as the agent itself
    call = await _chat_call(db, ctx, agent_id, call_id)
    if call.state in TERMINAL_STATES or not call.runtime_run_id or not call.runtime_workflow_id:
        raise ChatEndedError
    stack = contextlib.AsyncExitStack()
    try:
        client = await stack.enter_async_context(runtime_client(db, ctx.org_id))
        started = time.monotonic()
        try:
            stream = await stack.enter_async_context(
                client.send_text_chat(call.runtime_workflow_id, call.runtime_run_id, text)
            )
        except RuntimeRejectedError as exc:
            if exc.runtime_status == 409:
                raise InvalidStateTransitionError(
                    "The agent is still replying. Wait for its answer, then send again."
                ) from exc
            if exc.runtime_status in (400, 404):
                raise ChatEndedError from exc
            log.warning("test_chat_message_refused", extra={"runtime_status": exc.runtime_status})
            raise RuntimeRejectedError(
                "The agent could not take this message. Try again shortly.",
                runtime_status=exc.runtime_status,
            ) from exc
        # Nothing to write: end the read-only transaction so no connection is held
        # while the reply streams.
        await db.commit()
    except BaseException:
        await stack.aclose()
        raise
    return ChatRelay(call.id, _relay(stack, stream, started, call_id=None))


async def end(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, call_id: uuid.UUID
) -> Call:
    """End the chat. Its run completes, so the result (transcript, outcome) arrives as for
    a call; the record is also re-synced shortly in case the webhook is not set up.
    Idempotent; a chat the voice service already ended is just synced.
    """
    call = await _chat_call(db, ctx, agent_id, call_id)
    if call.state not in TERMINAL_STATES and call.runtime_run_id and call.runtime_workflow_id:
        try:
            async with runtime_client(db, ctx.org_id) as client:
                await client.end_text_chat(call.runtime_workflow_id, call.runtime_run_id)
        except (RuntimeRejectedError, RuntimeUnavailableError) as exc:
            # The voice service ends idle chats itself; reconciliation closes the rest.
            log.warning(
                "test_chat_end_failed",
                extra={"call_id": str(call.id), "error_type": type(exc).__name__},
            )
    return await call_service.end_test_call(
        db, ctx, agent_id=agent_id, call_id=call_id, connected=True
    )
