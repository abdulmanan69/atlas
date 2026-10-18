"""Runtime tool and credential calls behind ATLAS Actions.

Only the runtime's `http_api` tool category is ever created or read through this
module. Payload shapes were verified against the runtime source (v1.49):

- `GET /tools/?category=` lists an organization's tools, archived excluded by default
  (`api/routes/tool.py:96-134`, `api/db/tool_client.py:88-97`).
- `POST /tools/` takes `{name, description, category, definition}`; `category` must match
  `definition.type` (`api/schemas/tool.py:712-768`, `api/routes/tool.py:137-160`).
- `GET /tools/{uuid}` includes archived tools and answers 404 outside the organization
  (`api/routes/tool.py:163-189`).
- `PUT /tools/{uuid}` replaces `definition` wholesale when given
  (`api/schemas/tool.py:771-779`, `api/routes/tool.py:354-407`).
- `DELETE /tools/{uuid}` archives (`api/routes/tool.py:410-434`).
- `POST /tools/{uuid}/test` runs the HTTP call with `{llm_params, preset_params}` and
  returns status, status_code, data, error, hint, duration_ms
  (`api/schemas/tool.py:810-836`, `api/routes/tool.py:206-272`).
- Credentials: `POST /credentials/` `{name, description, credential_type, credential_data}`
  with per-type required fields, 409 on a duplicate name; `GET /credentials/`;
  `DELETE /credentials/{uuid}`; secrets are never returned
  (`api/routes/credentials.py:19-25,51-96,140-180,265-291`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.integrations.voice_runtime.client import VoiceRuntimeClient

HTTP_API = "http_api"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RuntimeTool(_Payload):
    tool_uuid: str
    name: str
    description: str | None = None
    category: str
    status: str
    definition: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RuntimeCredential(_Payload):
    uuid: str
    name: str
    description: str | None = None
    credential_type: str


class RuntimeToolTest(_Payload):
    status: str
    status_code: int | None = None
    data: Any = None
    error: str | None = None
    hint: str | None = None
    request_method: str
    request_url: str
    duration_ms: int


# ------------------------------------------------------------------ tools


async def list_http_tools(client: VoiceRuntimeClient) -> list[RuntimeTool]:
    body = await client._call("GET", "/tools/", params={"category": HTTP_API})
    return [RuntimeTool.model_validate(t) for t in body]


async def get_tool(client: VoiceRuntimeClient, tool_uuid: str) -> RuntimeTool:
    body = await client._call("GET", f"/tools/{tool_uuid}")
    return RuntimeTool.model_validate(body)


async def create_http_tool(
    client: VoiceRuntimeClient, *, name: str, description: str, definition: dict[str, Any]
) -> RuntimeTool:
    body = await client._call(
        "POST",
        "/tools/",
        json={
            "name": name,
            "description": description,
            "category": HTTP_API,
            "definition": definition,
        },
    )
    return RuntimeTool.model_validate(body)


async def update_http_tool(
    client: VoiceRuntimeClient,
    tool_uuid: str,
    *,
    name: str,
    description: str,
    definition: dict[str, Any],
) -> RuntimeTool:
    body = await client._call(
        "PUT",
        f"/tools/{tool_uuid}",
        json={"name": name, "description": description, "definition": definition},
    )
    return RuntimeTool.model_validate(body)


async def archive_tool(client: VoiceRuntimeClient, tool_uuid: str) -> None:
    await client._call("DELETE", f"/tools/{tool_uuid}")


async def test_tool(
    client: VoiceRuntimeClient,
    tool_uuid: str,
    *,
    llm_params: dict[str, Any],
    preset_params: dict[str, Any],
) -> RuntimeToolTest:
    body = await client._call(
        "POST",
        f"/tools/{tool_uuid}/test",
        json={"llm_params": llm_params, "preset_params": preset_params},
    )
    return RuntimeToolTest.model_validate(body)


# ------------------------------------------------------------------ credentials


async def list_credentials(client: VoiceRuntimeClient) -> list[RuntimeCredential]:
    body = await client._call("GET", "/credentials/")
    return [RuntimeCredential.model_validate(c) for c in body]


async def create_credential(
    client: VoiceRuntimeClient,
    *,
    name: str,
    description: str,
    credential_type: str,
    credential_data: dict[str, str],
) -> RuntimeCredential:
    body = await client._call(
        "POST",
        "/credentials/",
        json={
            "name": name,
            "description": description,
            "credential_type": credential_type,
            "credential_data": credential_data,
        },
    )
    return RuntimeCredential.model_validate(body)


async def delete_credential(client: VoiceRuntimeClient, credential_uuid: str) -> None:
    await client._call("DELETE", f"/credentials/{credential_uuid}")
