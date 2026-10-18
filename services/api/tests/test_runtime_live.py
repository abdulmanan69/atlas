"""Contract tests against the real voice runtime container.

Skipped unless ATLAS_TEST_RUNTIME_URL is set (e.g. http://127.0.0.1:8000 with
`make voice-up`). They prove the workflow definitions ATLAS generates pass the
runtime's own validation, which the in-memory fake cannot guarantee.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator

import pytest

from app.integrations.voice_runtime.client import VoiceRuntimeClient
from app.integrations.voice_runtime.workflow_builder import (
    WebhookTarget,
    build_configurations,
    build_definition,
)
from app.schemas.voice import AgentConfig, CallSettings, CaptureField, FlowDefinition
from app.services.agents import TEMPLATES
from app.services.flows import validate_flow
from tests.test_flow import branching, node, path

RUNTIME_URL = os.environ.get("ATLAS_TEST_RUNTIME_URL")
pytestmark = pytest.mark.skipif(not RUNTIME_URL, reason="ATLAS_TEST_RUNTIME_URL not set")


@pytest.fixture
async def live() -> AsyncIterator[VoiceRuntimeClient]:
    assert RUNTIME_URL
    async with VoiceRuntimeClient(RUNTIME_URL, timeout=30) as anon:
        token = await anon.signup(
            f"contract-{secrets.token_hex(4)}@tenants.atlas.internal",
            secrets.token_urlsafe(24),
            "Contract test",
        )
    async with VoiceRuntimeClient(RUNTIME_URL, bearer=token, timeout=30) as user:
        key = await user.create_api_key("contract-test")
    async with VoiceRuntimeClient(RUNTIME_URL, api_key=key, timeout=30) as client:
        yield client


async def _publish(live: VoiceRuntimeClient, name: str, cfg: AgentConfig) -> None:
    credential = await live.create_header_credential(
        name=f"contract-{secrets.token_hex(3)}",
        header="X-Atlas-Webhook-Secret",
        value=secrets.token_urlsafe(16),
    )
    definition = build_definition(
        cfg,
        webhook=WebhookTarget(
            url="http://host.docker.internal:8080/api/v1/webhooks/voice-runtime",
            credential_uuid=credential,
        ),
    )
    wf = await live.create_workflow(name, definition)
    await live.update_workflow(
        str(wf.id), name=wf.name, definition=definition, configurations=build_configurations(cfg)
    )
    # Full validation of the draft by the runtime itself; raises on any error.
    body = await live._call("POST", f"/workflow/{wf.id}/validate")
    assert body == {"is_valid": True, "errors": []}
    version = await live.publish_workflow(str(wf.id), note="contract test")
    assert version >= 2


@pytest.mark.parametrize("template", [t.id for t in TEMPLATES])
async def test_generated_workflow_passes_runtime_validation(
    live: VoiceRuntimeClient, template: str
) -> None:
    cfg = next(t for t in TEMPLATES if t.id == template).config
    await _publish(live, f"contract-{template}", cfg)


async def test_multi_step_flow_passes_runtime_validation(live: VoiceRuntimeClient) -> None:
    """Global rules, a cycle, two Ends with captures, transition speech and call settings."""
    flow, _, _ = branching()
    assert validate_flow(flow).valid
    cfg = AgentConfig(
        mode="flow",
        flow=flow,
        global_rules="Keep every reply to one or two sentences.",
        company_name="Contract Co",
        greeting="Hi",
        instructions="Ignored in flow mode; kept so switching modes is lossless.",
        capture_fields=[CaptureField(name="ignored", description="Guided-only field")],
        call_settings=CallSettings(
            max_call_seconds=420,
            idle_timeout_seconds=12,
            turn_start="min_words",
            turn_stop="turn_analyzer",
            key_terms=["Contract Co", "Hina"],
            voicemail={"action": "leave_message", "message": "Sorry we missed you."},  # type: ignore[arg-type]
            ambient={"enabled": True, "volume": 0.2},  # type: ignore[arg-type]
        ),
    )
    await _publish(live, f"contract-flow-{secrets.token_hex(3)}", cfg)


async def test_self_loop_and_step_shapes_are_accepted_by_runtime(
    live: VoiceRuntimeClient,
) -> None:
    """Shapes our validator treats more strictly than the runtime still publish."""
    start, step, end = node("start", "Open"), node("step", "Loop"), node("end", "Done")
    flow = FlowDefinition(
        nodes=[start, step, end],
        paths=[path(start, step, "Go"), path(step, step, "Again"), path(step, end, "Finish")],
    )
    cfg = AgentConfig(
        mode="flow", flow=flow, company_name="C", greeting="Hi", instructions="x" * 20
    )
    await _publish(live, f"contract-loop-{secrets.token_hex(3)}", cfg)


async def test_catalog_contains_supported_providers(live: VoiceRuntimeClient) -> None:
    from app.services.ai_settings import SUPPORTED

    catalog = await live.catalog()
    for kind, providers in SUPPORTED.items():
        missing = [p for p in providers if p not in catalog.get(kind, {})]
        assert not missing, f"{kind}: runtime no longer offers {missing}"
