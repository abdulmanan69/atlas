"""Typed test chats with an agent (agent editor, "Type" mode): requests and the event stream."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from app.schemas.common import APIModel

TestChatEventType = Literal[
    "started", "agent_text", "tool_start", "tool_end", "step", "done", "error"
]


class TestChatStart(APIModel):
    lead_id: uuid.UUID | None = Field(
        default=None, description="Fill template variables from this lead"
    )


class TestChatMessage(APIModel):
    text: str = Field(min_length=1, max_length=2000, description="What the tester typed")


class TestChatEvent(APIModel):
    """One server-sent event of a typed test chat (`data: <this JSON>`).

    `started` (first event of a new chat, carries `call_id`), `agent_text` (one message
    from the agent), `tool_start` / `tool_end` (a knowledge lookup or action, matched by
    `tool_id`), `step` (the conversation moved to another step, `name`), `error` (the
    turn failed, `message`) and finally `done` (`ended` is true when the agent ended the
    conversation). Comment lines (`: keep-alive`) only keep the connection open.
    """

    type: TestChatEventType
    at_ms: int = Field(
        ge=0,
        description=(
            "Milliseconds since ATLAS passed the message (or the start of the chat) to the "
            "voice service. On the first `agent_text` of a turn it is the reply time"
        ),
    )
    call_id: uuid.UUID | None = Field(
        default=None, description="`started`: the chat's record under Calls (mode test)"
    )
    text: str | None = Field(default=None, description="`agent_text`: what the agent said")
    tool_id: str | None = Field(default=None, description="`tool_start` / `tool_end`")
    name: str | None = Field(
        default=None, description="`tool_start` / `tool_end`: the tool; `step`: the step name"
    )
    kind: Literal["knowledge", "action"] | None = Field(
        default=None, description="`tool_start` / `tool_end`: a knowledge lookup or an action"
    )
    ended: bool | None = Field(default=None, description="`done`: the agent ended the conversation")
    message: str | None = Field(default=None, description="`error`: what went wrong")
