"""HTTP client for the internal voice runtime.

Every call to the runtime goes through this module. It owns timeouts, bounded
retries, and the mapping of runtime failures to ATLAS errors. Payload shapes
follow the runtime's API as verified against its source (v1.49), see ADR 0003.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from app.core.exceptions import AppError, IntegrationUnavailableError

log = logging.getLogger(__name__)

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
_IDEMPOTENT: frozenset[str] = frozenset({"GET", "PUT", "DELETE"})
_MAX_ATTEMPTS = 3


class RuntimeUnavailableError(IntegrationUnavailableError):
    def __init__(
        self, message: str = "The voice service is unavailable. Try again shortly."
    ) -> None:
        super().__init__(message)


class RuntimeRejectedError(AppError):
    """The runtime refused a request (validation, conflict, missing resource)."""

    status_code = 422
    code = "voice_service_rejected"

    def __init__(self, message: str, *, runtime_status: int, details: Any = None) -> None:
        super().__init__(message, details=details)
        self.runtime_status = runtime_status


class RuntimeCapacityError(AppError):
    status_code = 429
    code = "voice_capacity_reached"


# ------------------------------------------------------------------ payload models


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ProviderConnection(_Payload):
    uuid: str
    name: str
    provider: str
    connection_settings: dict[str, Any]
    configured_credentials: list[str]
    revision: int
    is_active: bool


class ServiceSelection(_Payload):
    provider_connection_uuid: str
    settings: dict[str, Any] = {}


class ModelConfiguration(_Payload):
    uuid: str
    name: str
    configuration: dict[str, Any]
    revision: int
    is_active: bool


class Workflow(_Payload):
    id: int
    name: str
    status: str
    workflow_uuid: str | None = None


class WorkflowRun(_Payload):
    """Run detail (GET /workflow/{id}/runs/{run_id}); fields verified in routes/workflow.py."""

    id: int
    workflow_id: int
    is_completed: bool
    name: str | None = None
    call_type: str | None = None  # "inbound" | "outbound"
    created_at: str | None = None
    # Storage keys of the artifacts (e.g. transcripts/{run}.txt, recordings/{run}.wav).
    transcript_url: str | None = None
    recording_url: str | None = None
    # Token for the runtime's public download route; present once an artifact exists.
    public_access_token: str | None = None
    usage_info: dict[str, Any] | None = None
    cost_info: dict[str, Any] | None = None
    initial_context: dict[str, Any] | None = None
    gathered_context: dict[str, Any] | None = None
    # Saved at call end; includes `realtime_feedback_events`, the per-turn event stream
    # (routes/workflow.py:1502, services/pipecat/event_handlers.py:524-528).
    logs: dict[str, Any] | None = None


class EmbedSession(_Payload):
    session_token: str
    workflow_run_id: int


class StorageUpload(_Payload):
    """AmbientNoiseUploadResponse (routes/workflow.py)."""

    upload_url: str
    storage_key: str
    storage_backend: str


class RecordingUpload(_Payload):
    """RecordingUploadResponseSchema (schemas/workflow_recording.py)."""

    upload_url: str
    recording_id: str
    storage_key: str


class TriggeredCall(_Payload):
    status: str
    workflow_run_id: int
    workflow_run_name: str | None = None


# A streamed text-chat turn: the runtime sends a ": keep-alive" comment every 15 s while the
# model works (services/workflow/text_chat_stream.py), so a 45 s read timeout means "gone".
_CHAT_TIMEOUT = httpx.Timeout(15.0, connect=5.0, read=45.0)


class TextChatStream:
    """One streamed text-chat turn from the runtime, decoded from server-sent events.

    Verified in routes/workflow_text_chat.py (`_stream_pending_turn_response`): every event
    is one `data: <json>` line followed by a blank line, one of
    `{"type": "session"|"complete", "session": {...}}`,
    `{"type": "turn_event", "turn_id", "event": {"type", "created_at", "payload"}}` and
    `{"type": "error", "message"}`. The run id is also in the `X-Workflow-Run-Id` header,
    sent before the first byte so a lost stream can still be found.
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        raw = response.headers.get("X-Workflow-Run-Id", "")
        self.run_id: int | None = int(raw) if raw.isdigit() else None

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        """Each decoded event; a keep-alive comment comes through as `{"type": "ping"}`."""
        data: list[str] = []
        async for line in self._response.aiter_lines():
            if line.startswith(":"):
                yield {"type": "ping"}
            elif line.startswith("data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(" ") else value)
            elif not line.strip() and data:
                try:
                    event = json.loads("\n".join(data))
                except ValueError:
                    event = None
                data = []
                if isinstance(event, dict):
                    yield event


# ------------------------------------------------------------------ client


def _backoff(attempt: int) -> float:
    return float(min(2.0, 0.25 * 2**attempt) + random.uniform(0, 0.25))  # noqa: S311


def _message(detail: Any, fallback: str) -> str:
    if isinstance(detail, str) and detail.strip():
        return detail[:300]
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return str(detail["message"])[:300]
    return fallback


class VoiceRuntimeClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        bearer: str | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
        )

    async def __aenter__(self) -> VoiceRuntimeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: HttpMethod,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request with bounded retries; returns the raw response (no redirects)."""
        idempotent = method in _IDEMPOTENT
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = await self._http.request(
                    method, path, json=json, params=params, headers=headers
                )
            except httpx.ConnectError as exc:
                # Nothing reached the runtime, so any method is safe to retry.
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc
                if not idempotent:
                    break
            else:
                if resp.status_code >= 500 and idempotent and attempt < _MAX_ATTEMPTS - 1:
                    last_exc = None
                else:
                    return resp
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_backoff(attempt))
        log.warning(
            "voice_runtime_unreachable",
            extra={"method": method, "path": path, "error_type": type(last_exc).__name__},
        )
        raise RuntimeUnavailableError

    async def _call(
        self,
        method: HttpMethod,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        resp = await self._request(method, path, json=json, params=params, headers=headers)
        return self._parse(method, path, resp)

    @staticmethod
    def _parse(method: str, path: str, resp: httpx.Response) -> Any:
        if resp.is_success:
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        try:
            detail: Any = resp.json().get("detail")
        except ValueError:
            detail = None
        status = resp.status_code
        if status in (401, 403):
            log.error("voice_runtime_auth_rejected", extra={"path": path, "status": status})
            raise RuntimeUnavailableError("The voice service rejected this platform's credentials.")
        if status == 429:
            raise RuntimeCapacityError(
                _message(detail, "Call capacity reached. Try again shortly.")
            )
        if status >= 500:
            log.warning(
                "voice_runtime_error", extra={"method": method, "path": path, "status": status}
            )
            raise RuntimeUnavailableError
        raise RuntimeRejectedError(
            _message(detail, "The voice service rejected the request."),
            runtime_status=status,
            details=None if isinstance(detail, str) else detail,
        )

    # ---------------------------------------------------------------- platform

    async def health(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", "/health")
        return result

    async def signup(self, email: str, password: str, name: str) -> str:
        body = await self._call(
            "POST", "/auth/signup", json={"email": email, "password": password, "name": name}
        )
        return str(body["token"])

    async def create_api_key(self, name: str) -> str:
        body = await self._call("POST", "/user/api-keys", json={"name": name})
        return str(body["api_key"])

    # ---------------------------------------------------------------- models

    async def catalog(self) -> dict[str, dict[str, Any]]:
        body = await self._call("GET", "/model-connections/catalog")
        services: dict[str, dict[str, Any]] = body["services"]
        return services

    async def list_connections(self) -> list[ProviderConnection]:
        body = await self._call("GET", "/model-connections/provider-connections")
        return [ProviderConnection.model_validate(c) for c in body]

    async def create_connection(
        self,
        *,
        name: str,
        provider: str,
        credentials: dict[str, Any],
        connection_settings: dict[str, Any],
    ) -> ProviderConnection:
        body = await self._call(
            "POST",
            "/model-connections/provider-connections",
            json={
                "name": name,
                "provider": provider,
                "credentials": credentials,
                "connection_settings": connection_settings,
            },
        )
        return ProviderConnection.model_validate(body)

    async def update_connection(
        self,
        uuid: str,
        *,
        name: str | None = None,
        credentials: dict[str, Any] | None = None,
        connection_settings: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        patch: dict[str, Any] = {}
        if name is not None:
            patch["name"] = name
        if credentials:
            patch["credentials"] = credentials
        if connection_settings is not None:
            patch["connection_settings"] = connection_settings
        body = await self._call(
            "PATCH", f"/model-connections/provider-connections/{uuid}", json=patch
        )
        return ProviderConnection.model_validate(body)

    async def delete_connection(self, uuid: str) -> None:
        await self._call("DELETE", f"/model-connections/provider-connections/{uuid}")

    async def list_model_configurations(self) -> list[ModelConfiguration]:
        body = await self._call("GET", "/model-connections/model-configurations")
        return [ModelConfiguration.model_validate(c) for c in body]

    async def get_model_configuration(self, uuid: str) -> ModelConfiguration:
        body = await self._call("GET", f"/model-connections/model-configurations/{uuid}")
        return ModelConfiguration.model_validate(body)

    async def create_model_configuration(
        self, name: str, configuration: dict[str, Any]
    ) -> ModelConfiguration:
        body = await self._call(
            "POST",
            "/model-connections/model-configurations",
            json={"name": name, "configuration": configuration},
        )
        return ModelConfiguration.model_validate(body)

    async def update_model_configuration(
        self, uuid: str, configuration: dict[str, Any]
    ) -> ModelConfiguration:
        body = await self._call(
            "PATCH",
            f"/model-connections/model-configurations/{uuid}",
            json={"configuration": configuration},
        )
        return ModelConfiguration.model_validate(body)

    async def delete_model_configuration(self, uuid: str) -> None:
        await self._call("DELETE", f"/model-connections/model-configurations/{uuid}")

    async def get_default_configuration(self) -> str | None:
        body = await self._call("GET", "/model-connections/default")
        value = body.get("model_configuration_uuid") if isinstance(body, dict) else None
        return str(value) if value else None

    async def set_default_configuration(self, uuid: str) -> None:
        await self._call(
            "PUT", "/model-connections/default", json={"model_configuration_uuid": uuid}
        )

    async def validate_default_configuration(self) -> list[dict[str, Any]]:
        """Live provider check. Returns problems; empty means everything works."""
        try:
            await self._call("GET", "/user/configurations/user/validate")
        except RuntimeRejectedError as exc:
            detail = exc.details
            if isinstance(detail, dict) and isinstance(detail.get("status"), list):
                return list(detail["status"])
            if isinstance(detail, list):
                return detail
            return [{"model": "configuration", "message": exc.message}]
        return []

    async def voices(
        self, provider: str, *, connection_uuid: str, params: dict[str, str]
    ) -> dict[str, Any]:
        body: dict[str, Any] = await self._call(
            "GET",
            f"/user/configurations/voices/{provider}",
            params={**params, "provider_connection_uuid": connection_uuid},
        )
        return body

    # ---------------------------------------------------------------- webhooks auth

    async def create_header_credential(self, *, name: str, header: str, value: str) -> str:
        body = await self._call(
            "POST",
            "/credentials/",
            json={
                "name": name,
                "description": "Authenticates call events sent to the platform",
                "credential_type": "custom_header",
                "credential_data": {"header_name": header, "header_value": value},
            },
        )
        return str(body["uuid"])

    # ---------------------------------------------------------------- workflows

    async def create_workflow(self, name: str, definition: dict[str, Any]) -> Workflow:
        body = await self._call(
            "POST",
            "/workflow/create/definition",
            json={"name": name, "workflow_definition": definition},
        )
        return Workflow.model_validate(body)

    async def update_workflow(
        self,
        workflow_id: str,
        *,
        name: str,
        definition: dict[str, Any],
        configurations: dict[str, Any],
        template_context_variables: dict[str, str] | None = None,
    ) -> Workflow:
        payload: dict[str, Any] = {
            "name": name,
            "workflow_definition": definition,
            "workflow_configurations": configurations,
        }
        if template_context_variables is not None:
            # UpdateWorkflowRequest.template_context_variables (routes/workflow.py).
            payload["template_context_variables"] = template_context_variables
        body = await self._call("PUT", f"/workflow/{workflow_id}", json=payload)
        return Workflow.model_validate(body)

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """The workflow's definition, configurations and template variables.

        GET /workflow/fetch/{id} returns the draft when one exists, else the published
        version; ATLAS publishes every draft it writes, so this is what calls use.
        """
        body: dict[str, Any] = await self._call("GET", f"/workflow/fetch/{workflow_id}")
        return body

    # ---------------------------------------------------------------- agent audio
    # Verified in routes/workflow.py (ambient noise) and routes/workflow_recording.py.

    async def ambient_upload_url(
        self, workflow_id: str, *, filename: str, mime_type: str, size: int
    ) -> StorageUpload:
        """Signed PUT URL for a custom background-noise file (at most 10 MB).

        The workflow's `ambient_noise_configuration` then points at it with `storage_key`
        and `storage_backend` (services/pipecat/audio_mixer.py `build_audio_out_mixer`).
        """
        body = await self._call(
            "POST",
            "/workflow/ambient-noise/upload-url",
            json={
                "workflow_id": int(workflow_id),
                "filename": filename,
                "mime_type": mime_type,
                "file_size": size,
            },
        )
        return StorageUpload.model_validate(body)

    async def recording_upload_url(
        self, *, filename: str, mime_type: str, size: int
    ) -> RecordingUpload:
        """Signed PUT URL for one recording (at most 5 MB), plus its short recording id."""
        body = await self._call(
            "POST",
            "/workflow-recordings/upload-url",
            json={"files": [{"filename": filename, "mime_type": mime_type, "file_size": size}]},
        )
        return RecordingUpload.model_validate(body["items"][0])

    async def create_recording(
        self, *, recording_id: str, storage_key: str, transcript: str, metadata: dict[str, Any]
    ) -> str:
        """Register an uploaded recording; returns the recording id messages refer to."""
        body = await self._call(
            "POST",
            "/workflow-recordings/",
            json={
                "recordings": [
                    {
                        "recording_id": recording_id,
                        "storage_key": storage_key,
                        "transcript": transcript,
                        "metadata": metadata,
                    }
                ]
            },
        )
        return str(body["recordings"][0]["recording_id"])

    async def publish_workflow(self, workflow_id: str, *, note: str | None) -> int:
        body = await self._call(
            "POST",
            f"/workflow/{workflow_id}/publish",
            json={"version_name": None, "change_description": note},
        )
        return int(body.get("version_number", 0))

    async def set_workflow_status(
        self, workflow_id: str, status: Literal["active", "archived"]
    ) -> None:
        await self._call("PUT", f"/workflow/{workflow_id}/status", json={"status": status})

    async def get_run(self, workflow_id: str, run_id: str) -> WorkflowRun:
        body = await self._call("GET", f"/workflow/{workflow_id}/runs/{run_id}")
        return WorkflowRun.model_validate(body)

    async def artifact_location(
        self, token: str, artifact: Literal["transcript", "recording"]
    ) -> str | None:
        """Where a run's transcript or recording can be read from, or None if absent.

        Verified contract: GET /public/download/workflow/{token}/{artifact} answers 302
        with the object's storage URL in Location, 404 when the token or artifact is
        unknown. The redirect is deliberately not followed with this client so the
        platform's runtime credentials never reach the storage host.
        """
        path = f"/public/download/workflow/{token}/{artifact}"
        resp = await self._request("GET", path)
        if resp.is_redirect:
            return resp.headers.get("location") or None
        if resp.status_code == 404 or resp.is_success:
            return None
        self._parse("GET", path, resp)  # raises the mapped error for 4xx/5xx
        return None

    # ---------------------------------------------------------------- outbound calls

    async def trigger_outbound_call(
        self,
        workflow_uuid: str,
        *,
        phone_number: str,
        context: dict[str, Any],
        telephony_configuration_id: int | None = None,
        from_phone_number_id: int | None = None,
    ) -> TriggeredCall:
        """Place a real phone call with the published version of a workflow.

        Verified contract: POST /public/agent/workflow/{uuid} with
        {phone_number, initial_context, telephony_configuration_id?, from_phone_number_id?}.
        Raises RuntimeCapacityError (429) when the runtime's concurrency limit is reached.
        """
        body: dict[str, Any] = {"phone_number": phone_number, "initial_context": context}
        if telephony_configuration_id is not None:
            body["telephony_configuration_id"] = telephony_configuration_id
        if from_phone_number_id is not None:
            body["from_phone_number_id"] = from_phone_number_id
        result = await self._call("POST", f"/public/agent/workflow/{workflow_uuid}", json=body)
        return TriggeredCall.model_validate(result)

    # ---------------------------------------------------------------- browser sessions

    async def ensure_embed_token(
        self, workflow_id: str, allowed_domains: list[str], *, replace: bool = False
    ) -> str:
        """The workflow's one embed token, allowing at least `allowed_domains`.

        The voice service keeps one token per workflow and checks the browser's Origin
        against its domains when the call audio connects. Browser test calls and the
        website widget share it, so by default the domains already allowed are kept
        (one caller never locks the other out); `replace=True` sets them exactly, used
        when the widget's allowed sites change.
        """
        domains = list(dict.fromkeys(allowed_domains))
        if not replace:
            current = await self._call("GET", f"/workflow/{workflow_id}/embed-token")
            existing = current.get("allowed_domains") if isinstance(current, dict) else None
            if isinstance(existing, list) and existing:
                merged = [str(d) for d in existing] + domains
                if set(merged) == {str(d) for d in existing}:
                    return str(current["token"])
                domains = list(dict.fromkeys(merged))
        body = await self._call(
            "POST",
            f"/workflow/{workflow_id}/embed-token",
            json={
                "allowed_domains": domains,
                "settings": {"widgetType": "voice", "autoStart": False},
                "usage_limit": None,
                "expires_in_days": None,
            },
        )
        return str(body["token"])

    async def start_embed_session(
        self, token: str, *, origin: str, context: dict[str, Any]
    ) -> EmbedSession:
        body = await self._call(
            "POST",
            "/public/embed/init",
            json={"token": token, "context_variables": context},
            headers={"Origin": origin},
        )
        return EmbedSession.model_validate(body)

    async def turn_servers(self, session_token: str) -> list[dict[str, Any]]:
        try:
            body = await self._call("GET", f"/public/embed/turn-credentials/{session_token}")
        except (RuntimeUnavailableError, RuntimeRejectedError):
            return []
        servers = body.get("ice_servers") if isinstance(body, dict) else None
        return list(servers) if isinstance(servers, list) else []

    # ---------------------------------------------------------------- typed test chats
    # The authenticated text-chat routes (routes/workflow_text_chat.py). A chat is a run in
    # mode "textchat" that runs the workflow's draft when one exists, else the published
    # version; ATLAS only writes the workflow when publishing, so that is the published
    # version. Creating it already runs the greeting turn, so both calls below stream.
    # Never retried: a resent message would run its actions twice.

    def open_text_chat(
        self, workflow_id: str, *, context: dict[str, Any], name: str
    ) -> contextlib.AbstractAsyncContextManager[TextChatStream]:
        """Start a chat; the stream carries the agent's greeting turn."""
        return self._text_chat_stream(
            f"/workflow/{workflow_id}/text-chat/sessions/stream",
            {
                "request_id": str(uuid.uuid4()),
                "name": name,
                "initial_context": context,
                "annotations": {"tester": {"source": "atlas_test_panel", "modality": "text"}},
            },
        )

    def send_text_chat(
        self, workflow_id: str, run_id: str, text: str
    ) -> contextlib.AbstractAsyncContextManager[TextChatStream]:
        """Send the tester's message; the stream carries the agent's reply turn."""
        return self._text_chat_stream(
            f"/workflow/{workflow_id}/text-chat/sessions/{run_id}/messages/stream",
            {"text": text},
        )

    async def end_text_chat(self, workflow_id: str, run_id: str) -> bool:
        """End a chat (idempotent). Completing the run sends the completion webhook."""
        body = await self._call(
            "POST", f"/workflow/{workflow_id}/text-chat/sessions/{run_id}/end", json={}
        )
        return bool(isinstance(body, dict) and body.get("is_completed"))

    @contextlib.asynccontextmanager
    async def _text_chat_stream(
        self, path: str, body: dict[str, Any]
    ) -> AsyncIterator[TextChatStream]:
        request = self._http.build_request("POST", path, json=body, timeout=_CHAT_TIMEOUT)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.TransportError as exc:
            log.warning(
                "voice_runtime_unreachable",
                extra={"method": "POST", "path": path, "error_type": type(exc).__name__},
            )
            raise RuntimeUnavailableError from exc
        try:
            if not response.is_success:
                await response.aread()
                self._parse("POST", path, response)  # raises the mapped error
            yield TextChatStream(response)
        finally:
            await response.aclose()


# ------------------------------------------------------------------ health (cached)

_health_cache: tuple[float, bool, str | None] | None = None
_HEALTH_TTL = 30.0


async def runtime_health(base_url: str, request_timeout: float = 10.0) -> tuple[bool, str | None]:
    """(reachable, version). Cached briefly: the runtime's health check is slow locally."""
    global _health_cache
    now = time.monotonic()
    if _health_cache and now - _health_cache[0] < _HEALTH_TTL:
        return _health_cache[1], _health_cache[2]
    try:
        async with VoiceRuntimeClient(base_url, timeout=request_timeout) as client:
            body = await client.health()
        result: tuple[bool, str | None] = (body.get("status") == "ok", body.get("version"))
    except AppError:
        result = (False, None)
    _health_cache = (now, *result)
    return result
