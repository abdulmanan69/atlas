"""In-memory stand-in for the voice runtime's HTTP API, faithful to its verified contracts.

Like the real runtime it rejects unknown fields, checks provider keys on create,
scopes everything to the organization behind the X-API-Key, and enforces the
Origin check on embed sessions. Tests mount it with respx.
"""

from __future__ import annotations

import itertools
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE = "http://runtime.test"
VALID_KEY = "sk-valid"


@dataclass
class Org:
    id: int
    connections: dict[str, dict[str, Any]] = field(default_factory=dict)
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    default: str | None = None
    credentials: dict[str, dict[str, Any]] = field(default_factory=dict)
    workflows: dict[int, dict[str, Any]] = field(default_factory=dict)
    validate_problems: list[dict[str, str]] = field(default_factory=list)


CATALOG: dict[str, dict[str, Any]] = {
    "llm": {
        "openai": {
            "title": "OpenAI",
            "credential_fields": ["api_key"],
            "connection_fields": ["base_url"],
            "credential_required": True,
            "connection_required": False,
            "settings_schema": {
                "properties": {
                    "model": {"type": "string", "examples": ["gpt-4.1-mini"]},
                    "temperature": {"type": "number"},
                }
            },
        },
        "vendorhosted": {
            "title": "Vendor",
            "credential_fields": ["api_key"],
            "connection_fields": [],
            "credential_required": True,
            "connection_required": False,
            "settings_schema": {},
        },
        "speaches": {
            "title": "Speaches",
            "credential_fields": ["api_key"],
            "connection_fields": ["base_url"],
            "credential_required": False,
            "connection_required": True,
            "settings_schema": {"properties": {"model": {"type": "string"}}},
        },
    },
    "stt": {
        "deepgram": {
            "title": "Deepgram",
            "credential_fields": ["api_key"],
            "connection_fields": [],
            "credential_required": True,
            "connection_required": False,
            "settings_schema": {"properties": {"model": {"type": "string"}}},
        }
    },
    "tts": {
        "elevenlabs": {
            "title": "ElevenLabs",
            "credential_fields": ["api_key"],
            "connection_fields": [],
            "credential_required": True,
            "connection_required": False,
            "settings_schema": {
                "properties": {"voice": {"type": "string", "voice_catalog": "dropdown"}}
            },
        }
    },
    "embeddings": {
        "openai": {
            "title": "OpenAI",
            "credential_fields": ["api_key"],
            "connection_fields": [],
            "credential_required": True,
            "connection_required": False,
            "settings_schema": {},
        }
    },
    "realtime": {},
}


def _json(status: int, body: Any = None) -> httpx.Response:
    if status == 204:
        return httpx.Response(204)
    return httpx.Response(status, json=body)


def _forbid_extra(body: dict[str, Any], allowed: set[str]) -> httpx.Response | None:
    extra = set(body) - allowed
    if extra:
        return _json(422, {"detail": [{"msg": f"Extra inputs are not permitted: {sorted(extra)}"}]})
    return None


class FakeRuntime:
    def __init__(self) -> None:
        self.orgs: dict[int, Org] = {}
        self.tokens: dict[str, int] = {}
        self.keys: dict[str, int] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.embed_tokens: dict[str, tuple[int, int, list[str]]] = {}
        self.down = False
        self._ids = itertools.count(1)

    # ------------------------------------------------------------ helpers

    def org_for_key(self, key: str) -> Org:
        return self.orgs[self.keys[key]]

    def last(self, method: str, pattern: str) -> Any:
        for m, path, body in reversed(self.calls):
            if m == method and re.search(pattern, path):
                return body
        raise AssertionError(f"no {method} {pattern}")

    def count(self, method: str, pattern: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and re.search(pattern, p))

    # ------------------------------------------------------------ dispatch

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("runtime down")
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        m = request.method

        if path == "/health":
            return _json(200, {"status": "ok", "version": "1.49.0"})
        if m == "POST" and path == "/auth/signup":
            org = Org(id=next(self._ids))
            boot = str(uuid.uuid4())
            org.connections[boot] = {
                "uuid": boot,
                "name": "Vendor",
                "provider": "vendorhosted",
                "connection_settings": {},
                "configured_credentials": ["api_key"],
                "revision": 1,
                "is_active": True,
            }
            cfg = str(uuid.uuid4())
            org.configs[cfg] = {
                "uuid": cfg,
                "name": "Default",
                "revision": 1,
                "is_active": True,
                "configuration": {"llm": {"provider_connection_uuid": boot, "settings": {}}},
            }
            org.default = cfg
            self.orgs[org.id] = org
            token = f"tok-{org.id}"
            self.tokens[token] = org.id
            return _json(200, {"token": token, "user": {"id": org.id, "email": body["email"]}})
        if m == "POST" and path == "/user/api-keys":
            org_id = self.tokens[request.headers["authorization"].removeprefix("Bearer ")]
            key = f"key-{org_id}-{uuid.uuid4().hex[:6]}"
            self.keys[key] = org_id
            return _json(200, {"api_key": key, "id": 1, "name": body["name"]})
        if path.startswith("/public/embed/"):
            return self._public(m, path, body, request)

        key = request.headers.get("x-api-key")
        if key not in self.keys:
            return _json(401, {"detail": "Invalid API key"})
        org = self.org_for_key(key)
        return self._authed(org, m, path, body, request)

    def _public(self, m: str, path: str, body: Any, request: httpx.Request) -> httpx.Response:
        if m == "POST" and path == "/public/embed/init":
            entry = self.embed_tokens.get(body["token"])
            origin = request.headers.get("origin", "")
            host = re.sub(r"^https?://", "", origin)
            if entry is None or (entry[2] and host not in entry[2]):
                return _json(403, {"detail": "Origin not allowed"})
            return _json(
                200,
                {
                    "session_token": f"emb_session_{uuid.uuid4().hex[:8]}",
                    "workflow_run_id": next(self._ids),
                    "config": {},
                },
            )
        if path.startswith("/public/embed/turn-credentials/"):
            return _json(503, {"detail": "TURN is not enabled"})
        return _json(404, {"detail": "Not Found"})

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        mc = "/model-connections"
        if m == "GET" and path == f"{mc}/catalog":
            return _json(200, {"services": CATALOG})
        if path.rstrip("/") == f"{mc}/provider-connections":
            if m == "GET":
                return _json(200, [c for c in org.connections.values() if c["is_active"]])
            if err := _forbid_extra(
                body, {"name", "provider", "credentials", "connection_settings"}
            ):
                return err
            if (
                body["provider"] in ("openai", "deepgram", "elevenlabs")
                and body["credentials"].get("api_key") != VALID_KEY
            ):
                return _json(422, {"detail": "Provider rejected the API key: invalid credentials"})
            cid = str(uuid.uuid4())
            org.connections[cid] = {
                "uuid": cid,
                "name": body["name"],
                "provider": body["provider"],
                "connection_settings": body["connection_settings"],
                "configured_credentials": sorted(body["credentials"]),
                "revision": 1,
                "is_active": True,
            }
            return _json(201, org.connections[cid])
        if mm := re.fullmatch(rf"{mc}/provider-connections/([\w-]+)", path):
            conn = org.connections.get(mm[1])
            if conn is None:
                return _json(404, {"detail": "Provider connection not found"})
            if m == "DELETE":
                conn["is_active"] = False
                return _json(204)
            if m == "PATCH":
                if err := _forbid_extra(
                    body, {"name", "credentials", "connection_settings", "revision"}
                ):
                    return err
                if "name" in body:
                    conn["name"] = body["name"]
                conn["revision"] += 1
                return _json(200, conn)
        if path.rstrip("/") == f"{mc}/model-configurations":
            if m == "GET":
                return _json(200, [c for c in org.configs.values() if c["is_active"]])
            cid = str(uuid.uuid4())
            org.configs[cid] = {
                "uuid": cid,
                "name": body["name"],
                "configuration": body["configuration"],
                "revision": 1,
                "is_active": True,
            }
            return _json(201, org.configs[cid])
        if mm := re.fullmatch(rf"{mc}/model-configurations/([\w-]+)", path):
            cfg = org.configs.get(mm[1])
            if cfg is None or not cfg["is_active"]:
                return _json(404, {"detail": "Model configuration not found"})
            if m == "GET":
                return _json(200, cfg)
            if m == "PATCH":
                cfg["configuration"] = body["configuration"]
                cfg["revision"] += 1
                return _json(200, cfg)
            if m == "DELETE":
                # Real runtime rule, found by the live contract test.
                if org.default == cfg["uuid"]:
                    return _json(
                        400, {"detail": "Select another organization default before archiving"}
                    )
                cfg["is_active"] = False
                return _json(204)
        if path == f"{mc}/default":
            if m == "GET":
                return _json(200, {"model_configuration_uuid": org.default})
            org.default = body["model_configuration_uuid"]
            return _json(200, {"model_configuration_uuid": org.default})
        if m == "GET" and path == "/user/configurations/user/validate":
            if org.validate_problems:
                return _json(422, {"detail": {"status": org.validate_problems}})
            return _json(200, {"status": []})
        if m == "GET" and path.startswith("/user/configurations/voices/"):
            return _json(
                200, {"voices": [{"voice_id": "v1", "name": "Rachel", "gender": "female"}]}
            )
        if m == "POST" and path == "/credentials/":
            cid = str(uuid.uuid4())
            org.credentials[cid] = body
            return _json(
                200, {"uuid": cid, "name": body["name"], "credential_type": body["credential_type"]}
            )
        return self._workflows(org, m, path, body)

    def _workflows(self, org: Org, m: str, path: str, body: Any) -> httpx.Response:
        if m == "POST" and path == "/workflow/create/definition":
            wid = next(self._ids)
            org.workflows[wid] = {
                "id": wid,
                "name": body["name"],
                "status": "active",
                "workflow_uuid": str(uuid.uuid4()),
                "draft": body["workflow_definition"],
                "configurations": {},
                "published": 1,
            }
            return _json(
                200,
                {
                    k: v
                    for k, v in org.workflows[wid].items()
                    if k in ("id", "name", "status", "workflow_uuid")
                },
            )
        if not (mm := re.fullmatch(r"/workflow/(\d+)(/.*)?", path)):
            return _json(404, {"detail": "Not Found"})
        wf = org.workflows.get(int(mm[1]))
        if wf is None:
            return _json(404, {"detail": "Workflow not found"})
        rest = mm[2] or ""
        if m == "PUT" and rest == "":
            wf.update(
                name=body["name"],
                draft=body["workflow_definition"],
                configurations=body["workflow_configurations"],
            )
            return _json(200, {k: wf[k] for k in ("id", "name", "status", "workflow_uuid")})
        if m == "POST" and rest == "/publish":
            start = next((n for n in wf["draft"]["nodes"] if n["type"] == "startCall"), None)
            if start is None or not start["data"].get("prompt", "").strip():
                return _json(
                    422,
                    {
                        "detail": {
                            "is_valid": False,
                            "errors": [
                                {
                                    "kind": "node",
                                    "id": "start",
                                    "field": "prompt",
                                    "message": "Prompt is required",
                                }
                            ],
                        }
                    },
                )
            wf["published"] += 1
            return _json(
                200, {"id": wf["id"], "version_number": wf["published"], "status": "published"}
            )
        if m == "PUT" and rest == "/status":
            wf["status"] = body["status"]
            return _json(200, {"id": wf["id"], "status": wf["status"]})
        if m == "POST" and rest == "/embed-token":
            token = f"embed-{uuid.uuid4().hex[:8]}"
            self.embed_tokens[token] = (org.id, wf["id"], body.get("allowed_domains") or [])
            return _json(200, {"token": token, "allowed_domains": body.get("allowed_domains")})
        return _json(404, {"detail": "Not Found"})
