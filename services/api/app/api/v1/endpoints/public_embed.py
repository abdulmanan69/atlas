"""Public website widget: the script tenants embed and the sessions it opens.

No authentication: a widget is identified by its public key and admitted only from the
sites its owner allowed (Origin, else Referer), with per-visitor and per-widget rate
limits. Cross-origin access to these routes is handled by `EmbedCorsMiddleware`.
"""

from __future__ import annotations

import json
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Request, Response, status
from fastapi import Path as PathParam
from pydantic import ValidationError

from app.api.v1.deps import DB, Meta
from app.core.exceptions import ValidationFailedError
from app.schemas.agent_settings import WebCallEnd, WebCallSession
from app.schemas.common import ErrorResponse
from app.services import web_embed

router = APIRouter(prefix="/public/embed", tags=["Website widget"])

WIDGET_FILE = Path(__file__).resolve().parents[3] / "static" / "widget.js"
Key = Annotated[str, PathParam(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
_ERRORS: dict[int | str, dict[str, object]] = {
    403: {"model": ErrorResponse, "description": "The page's site is not allowed"},
    404: {"model": ErrorResponse, "description": "Unknown or switched-off widget"},
    409: {"model": ErrorResponse, "description": "The agent is not published or not active"},
    429: {"model": ErrorResponse, "description": "Too many sessions from this visitor or site"},
}


@lru_cache(maxsize=1)
def _widget_source() -> bytes:
    return WIDGET_FILE.read_bytes()


@router.get(
    "/widget.js",
    response_class=Response,
    summary="The website widget script",
    description=(
        'Embed with `<script src=".../widget.js" data-agent-key="wk_..." async></script>`. '
        "Optional attributes: `data-label` (button text), `data-position` (`right` or "
        "`left`), `data-color` (a CSS color)."
    ),
    responses={200: {"content": {"application/javascript": {}}}},
)
async def widget_script() -> Response:
    return Response(
        content=_widget_source(),
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "Cross-Origin-Resource-Policy": "cross-origin",
        },
    )


@router.post(
    "/{key}/sessions",
    response_model=WebCallSession,
    status_code=status.HTTP_201_CREATED,
    summary="Start a voice conversation from an allowed website",
    description=(
        "Records an inbound call and returns a one-time signaling address and ICE servers "
        "for the browser's audio connection."
    ),
    responses=_ERRORS,
)
async def start_session(key: Key, request: Request, db: DB, meta: Meta) -> WebCallSession:
    origin = web_embed.request_origin(request.headers.get("origin"), request.headers.get("referer"))
    return await web_embed.start_web_call(
        db, key, origin=origin, ip=meta.ip, user_agent=meta.user_agent
    )


@router.post(
    "/{key}/sessions/{call_id}/end",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Report that a widget conversation ended",
    description=(
        "Body `{call_token, connected}` as JSON; also accepted as text/plain so the widget "
        "can send it with `navigator.sendBeacon` when the page closes."
    ),
    responses={404: {"model": ErrorResponse, "description": "Unknown call or token"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WebCallEnd.model_json_schema()}},
        }
    },
)
async def end_session(key: Key, call_id: uuid.UUID, request: Request, db: DB, meta: Meta) -> None:
    raw = await request.body()
    try:
        data = WebCallEnd.model_validate(json.loads(raw or b"{}"))
    except (ValueError, ValidationError) as exc:
        raise ValidationFailedError("Send {call_token, connected} as JSON") from exc
    await web_embed.end_web_call(
        db, key, call_id, token=data.call_token, connected=data.connected, ip=meta.ip
    )
