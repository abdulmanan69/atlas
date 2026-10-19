"""Typed test chats: the agent editor's "Type" mode.

`POST /agents/{id}/test-chat` records a test call (mode test, channel text), opens the
voice service's text chat with the organization's own credentials and relays the turns
as ATLAS's own server-sent events. The browser never sees runtime credentials, ids or
error text. Ending the chat completes the run, whose result lands on the record like a
call's; analytics never count it.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.webhooks import DELIVERY_ID_HEADER
from app.integrations.voice_runtime.workflow_builder import WEBHOOK_SECRET_HEADER
from app.models.audit import AuditLog
from app.models.calls import Call, CallMode, CallState
from app.services import jobs, test_chat
from tests.calls_support import (
    CallsRuntime,
    create_lead,
    published_agent,
    runtime_org,
    webhook_secret,
    workflow_id,
)
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import Org

WEBHOOK = "/api/v1/webhooks/voice-runtime"
SECRET_ERROR = "OpenAI LLMService#2 failed: invalid api key sk-live-123"


def _sse(*events: dict[str, Any], keep_alive: bool = False) -> bytes:
    out = b": keep-alive\n\n" if keep_alive else b""
    for e in events:
        out += f"data: {json.dumps(e)}\n\n".encode()
    return out


def _turn(turn_id: str, type_: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = {"type": type_, "created_at": "2026-10-19T10:00:00+00:00", "payload": payload}
    return {"type": "turn_event", "turn_id": turn_id, "event": event}


class ChatRuntime(CallsRuntime):
    """CallsRuntime with the authenticated text-chat routes (routes/workflow_text_chat.py).

    Creating a chat creates a run in mode "textchat" with the given initial context and
    streams its greeting turn; each message streams one reply turn; `end` completes the
    run. Streams follow `_stream_pending_turn_response`: `session` first, `turn_event`s,
    then `complete` (or `error`), with the run id in `X-Workflow-Run-Id`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.chats: list[dict[str, Any]] = []
        self.messages: list[str] = []
        self.greeting: list[dict[str, Any]] = [
            _turn("t0", "node_transition", {"node_id": "n1", "node_name": "Conversation"}),
            _turn("t0", "bot_speech", {"text": "Hi Hina, this is Sarah."}),
        ]
        self.reply: list[dict[str, Any]] | None = None
        self.busy = False
        self.refuse_create: int | None = None
        self.message_refusal: int | None = None

    def _session(self, rid: int, *, reply: str | None = None) -> dict[str, Any]:
        run = self.runs[rid]
        turn = {"id": "t", "status": "completed", "assistant_message": {"text": reply}}
        return {
            "workflow_run_id": rid,
            "is_completed": run["is_completed"],
            "revision": 1,
            "session_data": {"turns": [turn] if reply else []},
        }

    def _stream(self, rid: int, events: list[dict[str, Any]]) -> httpx.Response:
        body = _sse({"type": "session", "session": self._session(rid)}, *events, keep_alive=True)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream", "X-Workflow-Run-Id": str(rid)},
        )

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if m == "POST" and (mm := re.fullmatch(r"/workflow/(\d+)/text-chat/sessions/stream", path)):
            if self.refuse_create is not None:
                return httpx.Response(self.refuse_create, json={"detail": SECRET_ERROR})
            wf = int(mm[1])
            if wf not in org.workflows:
                return httpx.Response(404, json={"detail": "Workflow not found"})
            self.chats.append(body)
            rid = self.add_run(
                org.id, wf, initial_context=dict(body["initial_context"]), call_type="inbound"
            )
            self.runs[rid]["mode"] = "textchat"
            done = {"type": "complete", "session": self._session(rid)}
            return self._stream(rid, [*self.greeting, done])
        sessions = r"/workflow/(\d+)/text-chat/sessions/(\d+)"
        if m == "POST" and (mm := re.fullmatch(rf"{sessions}/messages/stream", path)):
            run = self.runs.get(int(mm[2]))
            if run is None or run["_org"] != org.id:
                return httpx.Response(404, json={"detail": "Text chat session not found"})
            if run["is_completed"]:
                return httpx.Response(400, json={"detail": "Text chat session has ended"})
            if self.busy:
                return httpx.Response(409, json={"detail": "A turn is in progress"})
            if self.message_refusal is not None:
                return httpx.Response(self.message_refusal, json={"detail": SECRET_ERROR})
            self.messages.append(body["text"])
            rid = int(mm[2])
            events = self.reply
            if events is None:
                events = [
                    _turn(
                        "t1",
                        "tool_call_started",
                        {
                            "function_name": "retrieve_from_knowledge_base",
                            "tool_call_id": "call_1",
                            "arguments": {"query": "pricing"},
                        },
                    ),
                    _turn(
                        "t1",
                        "tool_call_result",
                        {
                            "function_name": "retrieve_from_knowledge_base",
                            "tool_call_id": "call_1",
                            "result": {"chunks": ["secret internal pricing"]},
                        },
                    ),
                    _turn("t1", "node_transition", {"node_id": "n2", "node_name": "Qualify"}),
                    _turn("t1", "bot_speech", {"text": "Plans start at 49 dollars."}),
                    {"type": "complete", "session": self._session(rid)},
                ]
            return self._stream(rid, events)
        if m == "POST" and (mm := re.fullmatch(rf"{sessions}/end", path)):
            rid = int(mm[2])
            if rid not in self.runs or self.runs[rid]["_org"] != org.id:
                return httpx.Response(404, json={"detail": "Text chat session not found"})
            if not self.runs[rid]["is_completed"]:
                self.finish(
                    rid, disposition="user_hangup", call_status=None, transcript="Agent: Hi"
                )
            return httpx.Response(200, json=self._session(rid))
        return super()._authed(org, m, path, body, request)


@pytest.fixture
def runtime() -> Iterator[ChatRuntime]:
    fake = ChatRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        router.route(host="storage.test").mock(side_effect=fake.storage)
        yield fake


@pytest.fixture
def queue_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhooks are processed inline when the job queue is unavailable."""

    async def _unavailable(*_: Any, **__: Any) -> bool:
        raise jobs.QueueUnavailableError("redis down")

    monkeypatch.setattr(jobs, "enqueue", _unavailable)


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in response.text.split("\n\n"):
        data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


async def _start(
    client: httpx.AsyncClient, acct: Account, agent_id: str, lead_id: str | None = None
) -> tuple[str, list[dict[str, Any]]]:
    r = await client.post(
        f"/api/v1/agents/{agent_id}/test-chat", headers=acct.headers, json={"lead_id": lead_id}
    )
    events = _events(r)
    assert events[0]["type"] == "started"
    assert r.headers["x-call-id"] == events[0]["call_id"]
    return events[0]["call_id"], events


async def _say(
    client: httpx.AsyncClient, acct: Account, agent_id: str, call_id: str, text: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/agents/{agent_id}/test-chat/{call_id}/messages",
        headers=acct.headers,
        json={"text": text},
    )


async def _call(client: httpx.AsyncClient, acct: Account, call_id: str) -> dict[str, Any]:
    r = await client.get(f"/api/v1/calls/{call_id}", headers=acct.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# ------------------------------------------------------------------ starting


async def test_start_records_a_text_test_call_and_streams_the_greeting(
    client: httpx.AsyncClient, owner: Account, runtime: ChatRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call_id, events = await _start(client, owner, agent["id"], lead["id"])

    assert [e["type"] for e in events] == ["started", "step", "agent_text", "done"]
    assert events[1]["name"] == "Conversation"
    assert events[2]["text"] == "Hi Hina, this is Sarah."
    assert events[3]["ended"] is False
    assert all(isinstance(e["at_ms"], int) and e["at_ms"] >= 0 for e in events)

    # The chat carries the call id and the lead's variables, like a test call's session.
    sent = runtime.chats[-1]["initial_context"]
    assert sent["atlas_call_id"] == call_id and sent["atlas_test"] == "true"
    assert sent["first_name"] == "Hina"
    assert runtime.last("POST", r"/workflow/\d+/text-chat/sessions/stream") is not None

    call = await _call(client, owner, call_id)
    assert call["mode"] == "test" and call["channel"] == "text"
    assert call["state"] == "in_progress" and call["started_at"] is not None
    assert call["lead_id"] == lead["id"] and call["agent_version"] == agent["published_version"]
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call_id))
        assert row is not None
        assert row.runtime_run_id == str(max(runtime.runs))
        assert row.runtime_workflow_id == str(workflow_id(runtime_org(runtime)))
        audit = (
            await db.execute(select(AuditLog).where(AuditLog.action == "agent.test_chat_started"))
        ).scalar_one()
    assert audit.changes == {"lead_id": lead["id"], "call_id": call_id}
    timeline = (await client.get(f"/api/v1/calls/{call_id}/events", headers=owner.headers)).json()
    assert timeline[0]["data"]["channel"] == "text"

    # Voice test calls stay voice.
    listed = (await client.get("/api/v1/calls", headers=owner.headers)).json()
    assert [c["channel"] for c in listed["items"]] == ["text"]


async def test_a_chat_that_cannot_start_leaves_no_call(
    client: httpx.AsyncClient, owner: Account, runtime: ChatRuntime
) -> None:
    agent = await published_agent(client, owner)
    runtime.refuse_create = 402
    r = await client.post(f"/api/v1/agents/{agent['id']}/test-chat", headers=owner.headers, json={})
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/json")
    assert "sk-live" not in r.text and "LLMService" not in r.text
    runtime.refuse_create = 500
    r = await client.post(f"/api/v1/agents/{agent['id']}/test-chat", headers=owner.headers, json={})
    assert r.status_code == 503
    assert "sk-live" not in r.text and "LLMService" not in r.text
    assert (await client.get("/api/v1/calls", headers=owner.headers)).json()["total"] == 0

    draft = await client.post("/api/v1/agents", headers=owner.headers, json={"name": "Draft"})
    r = await client.post(
        f"/api/v1/agents/{draft.json()['id']}/test-chat", headers=owner.headers, json={}
    )
    assert r.status_code == 409 and "Publish the agent" in r.text


# ------------------------------------------------------------------ messages


async def test_a_message_streams_the_reply_with_tools_steps_and_reply_time(
    client: httpx.AsyncClient, owner: Account, runtime: ChatRuntime
) -> None:
    agent = await published_agent(client, owner)
    call_id, _ = await _start(client, owner, agent["id"])

    events = _events(await _say(client, owner, agent["id"], call_id, "How much is it?"))
    assert runtime.messages == ["How much is it?"]
    assert [e["type"] for e in events] == ["tool_start", "tool_end", "step", "agent_text", "done"]
    tool_start, tool_end, step, reply, done = events
    assert tool_start == {
        "type": "tool_start",
        "at_ms": tool_start["at_ms"],
        "tool_id": "call_1",
        "name": "retrieve_from_knowledge_base",
        "kind": "knowledge",
    }
    assert tool_end["tool_id"] == "call_1" and tool_end["kind"] == "knowledge"
    assert step["name"] == "Qualify"
    assert reply["text"] == "Plans start at 49 dollars." and reply["at_ms"] >= 0
    assert done["ended"] is False
    # Tool arguments and results stay on the server.
    raw = json.dumps(events)
    assert "pricing" not in raw

    # A reply that did not stream its text still arrives, from the stored turn.
    rid = max(runtime.runs)
    runtime.reply = [{"type": "complete", "session": runtime._session(rid, reply="Sure.")}]
    events = _events(await _say(client, owner, agent["id"], call_id, "Thanks"))
    assert [(e["type"], e.get("text")) for e in events] == [
        ("agent_text", "Sure."),
        ("done", None),
    ]


async def test_failed_turns_and_lost_streams_end_with_a_plain_error(
    client: httpx.AsyncClient, owner: Account, runtime: ChatRuntime
) -> None:
    agent = await published_agent(client, owner)
    call_id, _ = await _start(client, owner, agent["id"])

    runtime.reply = [
        _turn("t1", "execution_error", {"message": SECRET_ERROR}),
        {"type": "error", "message": SECRET_ERROR},
    ]
    r = await _say(client, owner, agent["id"], call_id, "Hello?")
    events = _events(r)
    assert [e["type"] for e in events] == ["error", "error"]
    assert events[0]["message"] == test_chat.TURN_FAILED
    assert "sk-live" not in r.text and "LLMService" not in r.text

    runtime.reply = [_turn("t2", "bot_speech", {"text": "Half a rep"})]  # no complete
    events = _events(await _say(client, owner, agent["id"], call_id, "Still there?"))
    assert [e["type"] for e in events] == ["agent_text", "error"]
    assert "connection to the agent was lost" in events[-1]["message"]

    # The voice service refuses a message (e.g. its usage limit): plain words only.
    runtime.message_refusal = 402
    r = await _say(client, owner, agent["id"], call_id, "Anyone?")
    assert r.status_code == 422 and "could not take this message" in r.text
    assert "sk-live" not in r.text and "LLMService" not in r.text
    runtime.message_refusal = None

    # The voice service is still working on the last message.
    runtime.busy = True
    r = await _say(client, owner, agent["id"], call_id, "Hurry")
    assert r.status_code == 409 and "still replying" in r.text
    assert r.json()["error"]["code"] == "invalid_state_transition"


# ------------------------------------------------------------------ ending and result


@pytest.mark.usefixtures("queue_down")
async def test_ending_completes_the_run_and_the_result_keeps_the_channel(
    client: httpx.AsyncClient, owner: Account, runtime: ChatRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call_id, _ = await _start(client, owner, agent["id"], lead["id"])
    run_id = max(runtime.runs)
    end_url = f"/api/v1/agents/{agent['id']}/test-chat/{call_id}/end"

    r = await client.post(end_url, headers=owner.headers)
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "text" and r.json()["state"] == "in_progress"
    assert runtime.runs[run_id]["is_completed"] is True
    assert runtime.count("POST", rf"/workflow/\d+/text-chat/sessions/{run_id}/end") == 1

    # The chat is over: a late message is refused in plain words.
    r = await _say(client, owner, agent["id"], call_id, "One more thing")
    assert r.status_code == 409 and r.json()["error"]["code"] == "test_chat_ended"

    # The completion webhook fills the record, as for a call; the channel survives.
    org = runtime_org(runtime)
    headers = {DELIVERY_ID_HEADER: str(uuid.uuid4()), WEBHOOK_SECRET_HEADER: webhook_secret(org)}
    payload = {
        "atlas_call_id": call_id,
        "workflow_run_id": str(run_id),
        "workflow_id": str(workflow_id(org)),
        "direction": "inbound",
    }
    r = await client.post(WEBHOOK, headers=headers, json=payload)
    assert r.status_code == 202 and r.json()["status"] == "processed", r.text
    call = await _call(client, owner, call_id)
    assert call["state"] == "completed" and call["channel"] == "text"
    assert call["transcript_available"] is True
    assert call["usage"]["channel"] == "text" and "llm" in call["usage"]

    # Ending again is harmless; a finished chat refuses messages.
    assert (await client.post(end_url, headers=owner.headers)).json()["state"] == "completed"
    r = await _say(client, owner, agent["id"], call_id, "Hello again")
    assert r.status_code == 409 and r.json()["error"]["code"] == "test_chat_ended"

    # Never counted, never on the lead.
    o = (await client.get("/api/v1/analytics/calls-overview", headers=owner.headers)).json()
    assert o["totals"]["calls"] == 0 and o["mode_split"]["test"] == 1
    lead_calls = (
        await client.get(f"/api/v1/leads/{lead['id']}/calls", headers=owner.headers)
    ).json()
    assert lead_calls["total"] == 0


# ------------------------------------------------------------------ tenancy and roles


async def test_chats_are_tenant_isolated_role_gated_and_text_only(
    client: httpx.AsyncClient, owner: Account, runtime: ChatRuntime
) -> None:
    agent = await published_agent(client, owner)
    call_id, _ = await _start(client, owner, agent["id"])
    base = f"/api/v1/agents/{agent['id']}/test-chat"

    other = await signup(client, "other@example.com", org="Other Co")
    assert (await _say(client, other, agent["id"], call_id, "hi")).status_code == 404
    r = await client.post(f"{base}/{call_id}/end", headers=other.headers)
    assert r.status_code == 404

    viewer = await invite_and_join(client, owner, "rep@example.com", "agent")
    assert (await _say(client, viewer, agent["id"], call_id, "hi")).status_code == 403
    r = await client.post(base, headers=viewer.headers, json={})
    assert r.status_code == 403

    # A voice test call is not a chat.
    voice = await client.post(
        f"/api/v1/agents/{agent['id']}/test-session", headers=owner.headers, json={}
    )
    assert voice.status_code == 200, voice.text
    voice_id = voice.json()["call_id"]
    assert (await _call(client, owner, voice_id))["channel"] == "voice"
    assert (await _say(client, owner, agent["id"], voice_id, "hi")).status_code == 404

    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call_id))
        assert row is not None and row.mode == CallMode.TEST
        assert row.state == CallState.IN_PROGRESS


def test_translate_never_forwards_unknown_or_internal_events() -> None:
    assert test_chat.translate({"type": "session", "session": {}}, 5, replied=False) == []
    ignored = _turn("t", "session_cancelled", {"reason": "x"})
    assert test_chat.translate(ignored, 5, replied=False) == []
    blank = _turn("t", "bot_speech", {"text": "   "})
    assert test_chat.translate(blank, 5, replied=False) == []
    done = test_chat.translate(
        {"type": "complete", "session": {"is_completed": True}}, -3, replied=True
    )
    assert [(e.type, e.ended, e.at_ms) for e in done] == [("done", True, 0)]
    action = test_chat.translate(
        _turn("t", "tool_call_started", {"function_name": "book_meeting"}), 9, replied=False
    )
    assert action[0].kind == "action" and action[0].name == "book_meeting"
