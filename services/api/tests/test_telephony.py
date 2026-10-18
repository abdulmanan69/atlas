"""Calling providers and phone numbers against a contract-faithful fake voice runtime."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime, Org, _json

TS = "2026-10-18T10:00:00+00:00"
CONFIGS = "/organizations/telephony-configs"
# Mirrors the runtime's per-provider request schemas (providers/<name>/config.py).
REQUIRED: dict[str, tuple[str, ...]] = {
    "twilio": ("account_sid", "auth_token"),
    "vonage": ("api_key", "api_secret", "application_id", "private_key"),
    "plivo": ("auth_id", "auth_token"),
    "telnyx": ("api_key",),
    "ari": ("ari_endpoint", "app_name", "app_password"),
}
# Fields the runtime masks on read (ProviderUIField.sensitive).
SENSITIVE: dict[str, frozenset[str]] = {
    "twilio": frozenset({"account_sid", "auth_token"}),
    "vonage": frozenset({"api_key", "api_secret", "private_key", "signature_secret"}),
    "plivo": frozenset({"auth_id", "auth_token"}),
    "telnyx": frozenset({"api_key"}),
    "ari": frozenset({"app_password"}),
}
VENDOR = "dograh"


def _mask(value: str) -> str:
    return "*" * (len(value) - 4) + value[-4:] if len(value) > 4 else "****"


class TelephonyRuntime(FakeRuntime):
    """Adds the telephony-config and phone-number routes of routes/organization.py."""

    def __init__(self) -> None:
        super().__init__()
        self.configs: dict[int, dict[str, Any]] = {}
        self.numbers: dict[int, dict[str, Any]] = {}
        # config id -> numbers the provider account owns; absent means "accept any".
        self.owned: dict[int, set[str]] = {}
        self.in_use: set[int] = set()
        self.sync_ok = True
        self.sync_message: str | None = None

    def _authed(
        self, org: Org, m: str, path: str, body: Any, request: httpx.Request
    ) -> httpx.Response:
        if path.startswith(CONFIGS):
            return self._telephony(org, m, path, body)
        return super()._authed(org, m, path, body, request)

    # ------------------------------------------------------------ shapes

    def _item(self, cfg: dict[str, Any]) -> dict[str, Any]:
        active = [
            n
            for n in self.numbers.values()
            if n["telephony_configuration_id"] == cfg["id"] and n["is_active"]
        ]
        return {
            "id": cfg["id"],
            "name": cfg["name"],
            "provider": cfg["provider"],
            "connectivity": "sip" if cfg["provider"] == "ari" else "api",
            "is_default_outbound": cfg["is_default_outbound"],
            "inactive": False,
            "inactive_since": None,
            "inactive_reason": cfg.get("inactive_reason"),
            "phone_number_count": len(active),
            "supports_outbound": True,
            "is_ready_for_outbound": True,
            "outbound_blocked_reason": cfg.get("outbound_blocked_reason"),
            "created_at": TS,
            "updated_at": TS,
        }

    def _detail(self, cfg: dict[str, Any]) -> dict[str, Any]:
        item = self._item(cfg)
        del item["phone_number_count"]
        creds = {
            k: (_mask(v) if k in SENSITIVE.get(cfg["provider"], frozenset()) and v else v)
            for k, v in cfg["credentials"].items()
        }
        return {
            **item,
            "credentials": creds,
            "sip_connectivity": None,
            "setup_checklist": None,
            "supports_trunks": False,
            "trunks": [],
            "supports_phone_number_sync": cfg["provider"] == "twilio",
        }

    def _number(self, org: Org, n: dict[str, Any], sync: dict[str, Any] | None = None) -> Any:
        wf = org.workflows.get(n["inbound_workflow_id"]) if n["inbound_workflow_id"] else None
        return {
            "id": n["id"],
            "telephony_configuration_id": n["telephony_configuration_id"],
            "address": n["address"],
            "address_normalized": n["address"],
            "address_type": "pstn",
            "country_code": None,
            "label": n["label"],
            "inbound_workflow_id": n["inbound_workflow_id"],
            "inbound_workflow_name": wf["name"] if wf else None,
            "telephony_trunk_id": None,
            "is_active": n["is_active"],
            "is_default_caller_id": n["is_default_caller_id"],
            "extra_metadata": {},
            "created_at": TS,
            "updated_at": TS,
            "provider_sync": sync,
        }

    # ------------------------------------------------------------ routes

    def _telephony(self, org: Org, m: str, path: str, body: Any) -> httpx.Response:
        if path == CONFIGS:
            if m == "GET":
                items = [self._item(c) for c in self.configs.values() if c["_org"] == org.id]
                return _json(200, {"configurations": items})
            config = body["config"]
            provider = config.get("provider")
            if provider not in REQUIRED:
                return _json(422, {"detail": [{"msg": "Input tag does not match any tags"}]})
            missing = [f for f in REQUIRED[provider] if not config.get(f)]
            if missing:
                return _json(422, {"detail": [{"loc": ["body", "config", f]} for f in missing]})
            if any(
                c["_org"] == org.id and c["name"] == body["name"] for c in self.configs.values()
            ):
                return _json(409, {"detail": "A telephony configuration with that name exists"})
            cid = next(self._ids)
            self.configs[cid] = {
                "_org": org.id,
                "id": cid,
                "name": body["name"],
                "provider": provider,
                "credentials": {
                    k: v for k, v in config.items() if k not in ("provider", "from_numbers")
                },
                "is_default_outbound": body.get("is_default_outbound", False),
            }
            return _json(200, self._detail(self.configs[cid]))

        mm = re.fullmatch(rf"{CONFIGS}/(\d+)(/.*)?", path)
        assert mm
        cfg = self.configs.get(int(mm[1]))
        if cfg is None or cfg["_org"] != org.id:
            return _json(404, {"detail": "Telephony configuration not found"})
        rest = mm[2] or ""
        if rest == "":
            return self._config(org, cfg, m, body)
        if rest == "/set-default-outbound":
            for other in self.configs.values():
                if other["_org"] == org.id:
                    other["is_default_outbound"] = other is cfg
            return _json(200, self._detail(cfg))
        if rest == "/phone-numbers":
            if m == "GET":
                rows = [
                    self._number(org, n)
                    for n in self.numbers.values()
                    if n["telephony_configuration_id"] == cfg["id"]
                ]
                return _json(200, {"phone_numbers": rows})
            return self._create_number(org, cfg, body)
        if rest == "/phone-numbers/sync":
            if cfg["provider"] != "twilio":
                return _json(400, {"detail": "This provider cannot list its numbers"})
            created = []
            for address in sorted(self.owned.get(cfg["id"], set())):
                if any(
                    n["address"] == address and n["telephony_configuration_id"] == cfg["id"]
                    for n in self.numbers.values()
                ):
                    continue
                created.append(self._number(org, self._store_number(cfg, address, None)))
            return _json(
                200, {"created": created, "updated": [], "skipped": [], "missing_at_provider": []}
            )
        mn = re.fullmatch(r"/phone-numbers/(\d+)", rest)
        assert mn
        number = self.numbers.get(int(mn[1]))
        if number is None or number["telephony_configuration_id"] != cfg["id"]:
            return _json(404, {"detail": "Phone number not found"})
        if m == "GET":
            return _json(200, self._number(org, number))
        if m == "PUT":
            if body.get("inbound_workflow_id") is not None:
                if body["inbound_workflow_id"] not in org.workflows:
                    return _json(404, {"detail": "Workflow not found"})
                number["inbound_workflow_id"] = body["inbound_workflow_id"]
            if body.get("clear_inbound_workflow"):
                number["inbound_workflow_id"] = None
            if body.get("label") is not None:
                number["label"] = body["label"]
            sync = {"ok": self.sync_ok, "message": self.sync_message}
            return _json(200, self._number(org, number, sync))
        if not self.sync_ok:
            return _json(502, {"detail": "Provider rejected the phone-number detach request"})
        del self.numbers[number["id"]]
        return _json(200, {"message": "Phone number deleted", "provider_sync": {"ok": True}})

    def _config(self, org: Org, cfg: dict[str, Any], m: str, body: Any) -> httpx.Response:
        if m == "GET":
            return _json(200, self._detail(cfg))
        if m == "DELETE":
            if cfg["id"] in self.in_use:
                return _json(409, {"detail": "Configuration is used by scheduled calls"})
            for nid in [
                n["id"]
                for n in self.numbers.values()
                if n["telephony_configuration_id"] == cfg["id"]
            ]:
                del self.numbers[nid]
            del self.configs[cfg["id"]]
            return _json(200, {"message": "Telephony configuration deleted"})
        if body.get("name") is not None:
            cfg["name"] = body["name"]
        if body.get("config") is not None:
            config = dict(body["config"])
            if config.get("provider") != cfg["provider"]:
                return _json(400, {"detail": "Provider cannot be changed"})
            missing = [f for f in REQUIRED[cfg["provider"]] if not config.get(f)]
            if missing:
                return _json(422, {"detail": [{"loc": ["body", "config", f]} for f in missing]})
            creds = {k: v for k, v in config.items() if k not in ("provider", "from_numbers")}
            # preserve_masked_fields: a re-submitted mask restores the stored value.
            for k in SENSITIVE[cfg["provider"]]:
                stored = cfg["credentials"].get(k)
                if stored and creds.get(k) == _mask(stored):
                    creds[k] = stored
            cfg["credentials"] = creds
        return _json(200, self._detail(cfg))

    def _store_number(self, cfg: dict[str, Any], address: str, label: str | None) -> dict[str, Any]:
        nid = next(self._ids)
        self.numbers[nid] = {
            "id": nid,
            "_org": cfg["_org"],
            "telephony_configuration_id": cfg["id"],
            "address": address,
            "label": label,
            "inbound_workflow_id": None,
            "is_active": True,
            "is_default_caller_id": False,
        }
        return self.numbers[nid]

    def _create_number(self, org: Org, cfg: dict[str, Any], body: Any) -> httpx.Response:
        address = body["address"]
        if not re.fullmatch(r"\+\d{8,15}", address):
            return _json(422, {"detail": [{"msg": "address shape"}]})
        if any(n["_org"] == org.id and n["address"] == address for n in self.numbers.values()):
            return _json(409, {"detail": "A phone number with this address already exists."})
        if cfg["id"] in self.owned and address not in self.owned[cfg["id"]]:
            return _json(400, {"detail": "Phone number is not owned by this provider account"})
        return _json(200, self._number(org, self._store_number(cfg, address, body.get("label"))))


@pytest.fixture
def runtime() -> Iterator[TelephonyRuntime]:
    fake = TelephonyRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        yield fake


TWILIO = {"account_sid": "AC0123456789abcdef", "auth_token": "tok-super-secret-value"}


async def _twilio(client: httpx.AsyncClient, acct: Account, name: str = "Main") -> dict[str, Any]:
    r = await client.post(
        "/api/v1/telephony/providers",
        headers=acct.headers,
        json={"name": name, "provider": "twilio", "credentials": TWILIO},
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def _number(
    client: httpx.AsyncClient, acct: Account, provider_id: str, phone: str = "+14155550100"
) -> dict[str, Any]:
    r = await client.post(
        f"/api/v1/telephony/providers/{provider_id}/numbers",
        headers=acct.headers,
        json={"phone_number": phone, "label": "Front desk"},
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def _published_agent(client: httpx.AsyncClient, acct: Account) -> dict[str, Any]:
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
            "stt": {"connection_id": ids["deepgram"]},
            "tts": {"connection_id": ids["elevenlabs"], "settings": {"voice": "v1"}},
        },
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/v1/agents",
        headers=acct.headers,
        json={"name": "Receptionist", "direction": "inbound", "template": "lead_qualifier"},
    )
    assert r.status_code == 201, r.text
    agent = r.json()
    r = await client.post(f"/api/v1/agents/{agent['id']}/publish", headers=acct.headers, json={})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def _audit(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# ------------------------------------------------------------------ providers


async def test_catalog_lists_providers_with_exact_fields(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    r = await client.get("/api/v1/telephony/providers/catalog", headers=owner.headers)
    assert r.status_code == 200
    cat = {p["id"]: p for p in r.json()}
    assert set(cat) == {"twilio", "vonage", "plivo", "telnyx", "ari"}
    twilio = {f["name"]: f for f in cat["twilio"]["fields"]}
    assert set(twilio) == {"account_sid", "auth_token"} and twilio["auth_token"]["secret"]
    vonage = {f["name"]: f for f in cat["vonage"]["fields"]}
    assert vonage["private_key"]["multiline"] and not vonage["signature_secret"]["required"]
    assert cat["ari"]["connectivity"] == "sip"
    assert VENDOR not in r.text.lower()


async def test_provider_credentials_are_stored_by_runtime_and_never_echoed(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    created = await _twilio(client, owner)
    assert created["configured_fields"] == ["account_sid", "auth_token"]
    assert created["provider_title"] == "Twilio" and created["number_count"] == 0
    text = str(created)
    assert TWILIO["auth_token"] not in text and TWILIO["account_sid"] not in text
    stored = next(iter(runtime.configs.values()))["credentials"]
    assert stored == TWILIO, "runtime got the real values"

    listed = (await client.get("/api/v1/telephony/providers", headers=owner.headers)).json()
    assert [p["id"] for p in listed] == [created["id"]]
    assert TWILIO["auth_token"] not in str(listed)
    detail = await client.get(f"/api/v1/telephony/providers/{created['id']}", headers=owner.headers)
    assert detail.status_code == 200 and TWILIO["auth_token"] not in detail.text

    logs = await _audit("telephony.provider_created")
    assert len(logs) == 1 and TWILIO["auth_token"] not in str(logs[0].changes)
    assert logs[0].changes["credential_fields"] == ["account_sid", "auth_token"]


async def test_provider_input_is_validated_before_reaching_the_runtime(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    cases = [
        {"name": "X", "provider": "signalwire", "credentials": TWILIO},
        {"name": "X", "provider": "twilio", "credentials": {"account_sid": "AC1"}},
        {"name": "X", "provider": "twilio", "credentials": {**TWILIO, "password": "p"}},
    ]
    for body in cases:
        r = await client.post("/api/v1/telephony/providers", headers=owner.headers, json=body)
        assert r.status_code == 422, r.text
    missing = await client.post("/api/v1/telephony/providers", headers=owner.headers, json=cases[1])
    assert "Auth Token" in missing.json()["error"]["message"]
    assert runtime.count("POST", f"^{CONFIGS}$") == 0

    await _twilio(client, owner, "Main")
    dup = await client.post(
        "/api/v1/telephony/providers",
        headers=owner.headers,
        json={"name": "Main", "provider": "twilio", "credentials": TWILIO},
    )
    assert dup.status_code == 409


async def test_rotating_one_credential_keeps_the_others(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    created = await _twilio(client, owner)
    r = await client.patch(
        f"/api/v1/telephony/providers/{created['id']}",
        headers=owner.headers,
        json={"name": "Main (rotated)", "credentials": {"auth_token": "tok-new-value-123"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Main (rotated)"
    assert "tok-new-value-123" not in r.text
    stored = runtime.configs[int(created["id"])]["credentials"]
    assert stored == {"account_sid": TWILIO["account_sid"], "auth_token": "tok-new-value-123"}
    sent = runtime.last("PUT", rf"^{CONFIGS}/\d+$")
    assert sent["config"]["account_sid"] != TWILIO["account_sid"], "untouched secret sent as mask"
    log = (await _audit("telephony.provider_updated"))[0]
    assert log.changes["rotated_credentials"] == ["auth_token"]
    assert "tok-new-value-123" not in str(log.changes)


async def test_delete_provider_and_in_use_conflict(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    created = await _twilio(client, owner)
    runtime.in_use.add(int(created["id"]))
    r = await client.delete(f"/api/v1/telephony/providers/{created['id']}", headers=owner.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    runtime.in_use.clear()
    r = await client.delete(f"/api/v1/telephony/providers/{created['id']}", headers=owner.headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/telephony/providers", headers=owner.headers)).json() == []
    assert len(await _audit("telephony.provider_deleted")) == 1


async def test_default_outbound_switches_between_providers(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    first = await _twilio(client, owner, "First")
    second = await _twilio(client, owner, "Second")
    r = await client.post(
        f"/api/v1/telephony/providers/{second['id']}/set-default-outbound", headers=owner.headers
    )
    assert r.status_code == 200 and r.json()["is_default_outbound"]
    first_now = (
        await client.get(f"/api/v1/telephony/providers/{first['id']}", headers=owner.headers)
    ).json()
    assert not first_now["is_default_outbound"]


# ------------------------------------------------------------------ numbers


async def test_numbers_and_inbound_agent_round_trip(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    provider = await _twilio(client, owner)
    number = await _number(client, owner, provider["id"], "(415) 555-0100")
    assert number["phone_number"] == "+14155550100" and number["display"] == "+1 415-555-0100"
    assert number["inbound_agent_id"] is None and number["provider_name"] == "Main"

    bad = await client.post(
        f"/api/v1/telephony/providers/{provider['id']}/numbers",
        headers=owner.headers,
        json={"phone_number": "12345"},
    )
    assert bad.status_code == 422

    draft = await client.post(
        "/api/v1/agents", headers=owner.headers, json={"name": "Draft", "direction": "inbound"}
    )
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=owner.headers,
        json={"agent_id": draft.json()["id"]},
    )
    assert r.status_code == 422 and "Publish" in r.json()["error"]["message"]

    agent = await _published_agent(client, owner)
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=owner.headers,
        json={"agent_id": agent["id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inbound_agent_id"] == agent["id"] and body["inbound_agent_name"] == "Receptionist"
    assert body["provider_sync"] == {"ok": True, "message": None}
    org = next(iter(runtime.orgs.values()))
    workflow_id = next(iter(org.workflows))
    assert runtime.numbers[int(number["id"])]["inbound_workflow_id"] == workflow_id
    assert runtime.last("PUT", r"/phone-numbers/\d+$") == {"inbound_workflow_id": workflow_id}

    listed = (await client.get("/api/v1/telephony/numbers", headers=owner.headers)).json()
    assert listed[0]["inbound_agent_id"] == agent["id"]
    by_provider = (
        await client.get(
            f"/api/v1/telephony/providers/{provider['id']}/numbers", headers=owner.headers
        )
    ).json()
    assert by_provider == [{**listed[0], "provider_sync": None}]
    assert (
        await client.get(f"/api/v1/telephony/providers/{provider['id']}", headers=owner.headers)
    ).json()["number_count"] == 1

    runtime.sync_ok, runtime.sync_message = False, "Twilio lookup failed: number not found"
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=owner.headers,
        json={"agent_id": None},
    )
    assert r.status_code == 200
    assert r.json()["inbound_agent_id"] is None
    assert r.json()["provider_sync"] == {
        "ok": False,
        "message": "Twilio lookup failed: number not found",
    }
    assert runtime.last("PUT", r"/phone-numbers/\d+$") == {"clear_inbound_workflow": True}
    logs = await _audit("telephony.inbound_agent_assigned")
    assert [log.changes["agent_id"] for log in logs] == [agent["id"], None]

    r = await client.delete(f"/api/v1/telephony/numbers/{number['id']}", headers=owner.headers)
    assert r.status_code == 422, "provider refused the detach (runtime 502) is reported, not 503"
    assert "calling provider refused" in r.json()["error"]["message"]
    runtime.sync_ok = True
    r = await client.delete(f"/api/v1/telephony/numbers/{number['id']}", headers=owner.headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/telephony/numbers", headers=owner.headers)).json() == []


async def test_number_must_be_owned_by_the_provider_account(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    provider = await _twilio(client, owner)
    runtime.owned[int(provider["id"])] = {"+14155550100"}
    r = await client.post(
        f"/api/v1/telephony/providers/{provider['id']}/numbers",
        headers=owner.headers,
        json={"phone_number": "+14155550199"},
    )
    assert r.status_code == 422 and "not owned" in r.json()["error"]["message"]
    await _number(client, owner, provider["id"], "+14155550100")
    dup = await client.post(
        f"/api/v1/telephony/providers/{provider['id']}/numbers",
        headers=owner.headers,
        json={"phone_number": "+1 415 555 0100"},
    )
    assert dup.status_code == 409


async def test_sync_imports_numbers_the_account_holds(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    provider = await _twilio(client, owner)
    runtime.owned[int(provider["id"])] = {"+14155550100", "+14155550101"}
    r = await client.post(
        f"/api/v1/telephony/providers/{provider['id']}/numbers/sync", headers=owner.headers
    )
    assert r.status_code == 200, r.text
    assert sorted(n["phone_number"] for n in r.json()["added"]) == ["+14155550100", "+14155550101"]
    assert r.json()["missing_at_provider"] == []


# ------------------------------------------------------------------ roles, tenancy, setup


async def test_role_gating(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    provider = await _twilio(client, owner)
    number = await _number(client, owner, provider["id"])
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")

    for acct in (manager, viewer):
        assert (
            await client.get("/api/v1/telephony/providers", headers=acct.headers)
        ).status_code == 200
        assert (
            await client.get("/api/v1/telephony/setup-instructions/twilio", headers=acct.headers)
        ).status_code == 200
        r = await client.post(
            "/api/v1/telephony/providers",
            headers=acct.headers,
            json={"name": "Nope", "provider": "twilio", "credentials": TWILIO},
        )
        assert r.status_code == 403
        r = await client.post(
            f"/api/v1/telephony/providers/{provider['id']}/numbers",
            headers=acct.headers,
            json={"phone_number": "+14155550199"},
        )
        assert r.status_code == 403
        assert (
            await client.delete(f"/api/v1/telephony/numbers/{number['id']}", headers=acct.headers)
        ).status_code == 403

    # Managers run agent operations: they decide which agent answers a number.
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=manager.headers,
        json={"agent_id": None},
    )
    assert r.status_code == 200
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=viewer.headers,
        json={"agent_id": None},
    )
    assert r.status_code == 403


async def test_tenant_isolation(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    provider = await _twilio(client, owner)
    number = await _number(client, owner, provider["id"])
    other = await signup(client, "other@example.com", "Other Org")
    assert (await client.get("/api/v1/telephony/providers", headers=other.headers)).json() == []
    assert (await client.get("/api/v1/telephony/numbers", headers=other.headers)).json() == []
    for r in (
        await client.get(f"/api/v1/telephony/providers/{provider['id']}", headers=other.headers),
        await client.delete(f"/api/v1/telephony/providers/{provider['id']}", headers=other.headers),
        await client.get(
            f"/api/v1/telephony/providers/{provider['id']}/numbers", headers=other.headers
        ),
        await client.delete(f"/api/v1/telephony/numbers/{number['id']}", headers=other.headers),
        await client.put(
            f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
            headers=other.headers,
            json={"agent_id": None},
        ),
        await client.get("/api/v1/telephony/providers/not-a-number", headers=owner.headers),
    ):
        assert r.status_code == 404, r.text
    # An agent from another organization can never be put on a number.
    agent = await _published_agent(client, other)
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=owner.headers,
        json={"agent_id": agent["id"]},
    )
    assert r.status_code == 404
    r = await client.put(
        f"/api/v1/telephony/numbers/{number['id']}/inbound-agent",
        headers=owner.headers,
        json={"agent_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404
    assert int(provider["id"]) in runtime.configs and int(number["id"]) in runtime.numbers


async def test_setup_instructions_use_the_verified_runtime_routes(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    r = await client.get("/api/v1/telephony/setup-instructions/twilio", headers=owner.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["inbound_webhook_url"] == "http://localhost:8000/api/v1/telephony/inbound/run"
    assert body["fallback_webhook_url"] == "http://localhost:8000/api/v1/telephony/inbound/fallback"
    assert body["automatic"] is True
    assert any(body["inbound_webhook_url"] in step for step in body["steps"])
    assert any("ATLAS_VOICE_RUNTIME_PUBLIC_URL" in note for note in body["notes"]), (
        "localhost warning"
    )
    assert VENDOR not in r.text.lower()

    vonage = (
        await client.get("/api/v1/telephony/setup-instructions/vonage", headers=owner.headers)
    ).json()
    assert vonage["event_webhook_url"] == "http://localhost:8000/api/v1/telephony/vonage/events"
    ari = (
        await client.get("/api/v1/telephony/setup-instructions/ari", headers=owner.headers)
    ).json()
    assert ari["automatic"] is False and ari["event_webhook_url"] is None
    assert (
        await client.get("/api/v1/telephony/setup-instructions/signalwire", headers=owner.headers)
    ).status_code == 404


async def test_runtime_outage_is_degraded_not_fatal(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    await _twilio(client, owner)
    runtime.down = True
    r = await client.get("/api/v1/telephony/numbers", headers=owner.headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "integration_unavailable"


async def test_vendor_managed_configuration_is_hidden_and_reasons_are_scrubbed(
    client: httpx.AsyncClient, owner: Account, runtime: TelephonyRuntime
) -> None:
    """The runtime bootstraps every organization with its own hosted SIP connectivity.

    It is not an ATLAS provider: never listed, unreachable by id, and any runtime text
    that names the vendor is scrubbed before it reaches a tenant.
    """
    twilio = await _twilio(client, owner)
    org = next(iter(runtime.orgs.values()))
    hidden_id = next(runtime._ids)
    runtime.configs[hidden_id] = {
        "_org": org.id,
        "id": hidden_id,
        "name": "Dograh Cloudonix SIP",
        "provider": "cloudonix",
        "credentials": {"domain_id": "abc"},
        "is_default_outbound": True,
    }
    runtime.configs[int(twilio["id"])]["outbound_blocked_reason"] = (
        "Without one Dograh has no route for calls."
    )

    listed = (await client.get("/api/v1/telephony/providers", headers=owner.headers)).json()
    assert [p["id"] for p in listed] == [twilio["id"]]
    assert (
        "dograh"
        not in (await client.get("/api/v1/telephony/providers", headers=owner.headers)).text.lower()
    )
    assert (
        listed[0]["outbound_blocked_reason"]
        == "Without one the voice service has no route for calls."
    )

    r = await client.get(f"/api/v1/telephony/providers/{hidden_id}", headers=owner.headers)
    assert r.status_code == 404
    r = await client.patch(
        f"/api/v1/telephony/providers/{hidden_id}", headers=owner.headers, json={"name": "x"}
    )
    assert r.status_code == 404
    r = await client.delete(f"/api/v1/telephony/providers/{hidden_id}", headers=owner.headers)
    assert r.status_code == 404
