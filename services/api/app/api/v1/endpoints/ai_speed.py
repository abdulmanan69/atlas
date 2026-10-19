"""Response speed: fast-stack guide, provider presets, platform pipeline health (ADR 0006)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.deps import DB, AgentsManage, IntegrationsManage
from app.schemas.common import ERROR_RESPONSES, ErrorResponse
from app.schemas.latency import LatencyGuide, PipelineHealth
from app.schemas.voice import ModelSelection
from app.services import ai_speed as service

router = APIRouter(prefix="/ai-settings", tags=["AI providers"], responses=ERROR_RESPONSES)


@router.get(
    "/latency-guide",
    response_model=LatencyGuide,
    summary="Typical provider latencies (as published by each provider) and fast presets",
)
async def latency_guide(ctx: AgentsManage) -> LatencyGuide:
    return service.latency_guide()


@router.post(
    "/presets/{preset_id}/apply",
    response_model=ModelSelection,
    summary="Use a preset's providers for calls, with the organization's existing connections",
    responses={
        409: {
            "model": ErrorResponse,
            "description": "A required connection is missing; error.details.missing lists "
            "the provider ids to connect first",
        }
    },
)
async def apply_preset(preset_id: str, ctx: IntegrationsManage, db: DB) -> ModelSelection:
    return await service.apply_preset(db, ctx, preset_id)


@router.get(
    "/pipeline-health",
    response_model=PipelineHealth,
    summary="Platform-wide provider and response latency since the voice service started",
)
async def pipeline_health(ctx: IntegrationsManage) -> PipelineHealth:
    # Owners and admins only (integrations:manage): the figures cover every tenant.
    return await service.pipeline_health()
