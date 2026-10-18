"""Voice agents: create, edit (versioned), publish, lifecycle, browser test calls."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.v1.deps import DB, AgentsManage, AgentsRead
from app.models.voice import Agent
from app.schemas.common import ERROR_RESPONSES
from app.schemas.voice import (
    AgentConfigSave,
    AgentCreate,
    AgentDetail,
    AgentMetaUpdate,
    AgentOut,
    AgentTemplate,
    AgentVersionOut,
    CompiledPrompt,
    FlowValidation,
    PublishRequest,
    TestSession,
    TestSessionRequest,
)
from app.services import agents as service

router = APIRouter(prefix="/agents", tags=["Agents"], responses=ERROR_RESPONSES)


async def _detail(db: DB, agent: Agent) -> AgentDetail:
    config = await service.config_of(db, agent)
    return AgentDetail(
        **AgentOut.model_validate(agent).model_dump(),
        config=config,
        has_unpublished_changes=service.has_unpublished_changes(agent),
    )


@router.get(
    "/templates", response_model=list[AgentTemplate], summary="Starting points for new agents"
)
async def templates(_: AgentsRead) -> list[AgentTemplate]:
    return service.TEMPLATES


@router.get("", response_model=list[AgentOut])
async def list_agents(ctx: AgentsRead, db: DB, include_archived: bool = False) -> list[AgentOut]:
    return [
        AgentOut.model_validate(a)
        for a in await service.list_agents(db, ctx, include_archived=include_archived)
    ]


@router.post("", response_model=AgentDetail, status_code=status.HTTP_201_CREATED)
async def create_agent(data: AgentCreate, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.create_agent(db, ctx, data))


@router.get("/{agent_id}", response_model=AgentDetail)
async def get_agent(agent_id: uuid.UUID, ctx: AgentsRead, db: DB) -> AgentDetail:
    return await _detail(db, await service.get_agent(db, ctx, agent_id))


@router.patch("/{agent_id}", response_model=AgentDetail, summary="Rename or describe the agent")
async def update_agent(
    agent_id: uuid.UUID, data: AgentMetaUpdate, ctx: AgentsManage, db: DB
) -> AgentDetail:
    return await _detail(db, await service.update_meta(db, ctx, agent_id, data))


@router.put(
    "/{agent_id}/config",
    response_model=AgentDetail,
    summary="Save configuration as a new version (does not affect live calls until published)",
)
async def save_config(
    agent_id: uuid.UUID, data: AgentConfigSave, ctx: AgentsManage, db: DB
) -> AgentDetail:
    return await _detail(db, await service.save_config(db, ctx, agent_id, data.config, data.note))


@router.get("/{agent_id}/versions", response_model=list[AgentVersionOut])
async def versions(agent_id: uuid.UUID, ctx: AgentsRead, db: DB) -> list[AgentVersionOut]:
    agent = await service.get_agent(db, ctx, agent_id)
    return [
        AgentVersionOut(
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            created_by_user_id=v.created_by_user_id,
            is_published=v.version == agent.published_version,
        )
        for v in await service.list_versions(db, ctx, agent_id)
    ]


@router.post(
    "/{agent_id}/versions/{version}/restore",
    response_model=AgentDetail,
    summary="Copy an earlier version into a new latest version",
)
async def restore(agent_id: uuid.UUID, version: int, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.restore_version(db, ctx, agent_id, version))


@router.post(
    "/{agent_id}/validate",
    response_model=FlowValidation,
    summary="Check the latest version for problems that would block publishing",
    description=(
        "Runs the same checks publishing runs, without touching the voice service. Errors "
        "block publishing; warnings do not. Each issue names the node or path (`target`, "
        "`id`) and the field to fix. Guided agents are checked as the Start → End flow "
        "they compile to."
    ),
)
async def validate(agent_id: uuid.UUID, ctx: AgentsRead, db: DB) -> FlowValidation:
    return await service.validate_agent(db, ctx, agent_id)


@router.post(
    "/{agent_id}/convert-to-flow",
    response_model=AgentDetail,
    summary="Switch a guided agent to the flow builder",
    description=(
        "Saves a new version in flow mode that says and does exactly what the guided "
        "script did: a Start with the greeting and instructions, one End with the closing "
        "and captured details, and a single path taken when the end condition holds. "
        "One-way: there is no conversion back. 409 when the agent already uses flows."
    ),
    responses={409: {"description": "The agent already uses the flow builder"}},
)
async def convert_to_flow(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.convert_to_flow(db, ctx, agent_id))


@router.get(
    "/{agent_id}/compiled-prompt",
    response_model=CompiledPrompt,
    summary="Preview what the model is told at a node",
    description=(
        "The system prompt for one node of the latest version, composed the way it is "
        "sent on calls: global rules first (when the node uses them), then the node's "
        "prompt. Defaults to the Start. Template variables are left as written."
    ),
)
async def compiled_prompt(
    agent_id: uuid.UUID,
    ctx: AgentsRead,
    db: DB,
    node_id: str | None = Query(default=None, description="Defaults to the Start"),
) -> CompiledPrompt:
    return await service.compiled_prompt(db, ctx, agent_id, node_id)


@router.post(
    "/{agent_id}/publish",
    response_model=AgentDetail,
    summary="Make a version live (validated by ATLAS, then by the voice service)",
)
async def publish(
    agent_id: uuid.UUID, data: PublishRequest, ctx: AgentsManage, db: DB
) -> AgentDetail:
    return await _detail(db, await service.publish(db, ctx, agent_id, data.version, data.note))


@router.post("/{agent_id}/pause", response_model=AgentDetail)
async def pause(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.transition(db, ctx, agent_id, "pause"))


@router.post("/{agent_id}/resume", response_model=AgentDetail)
async def resume(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.transition(db, ctx, agent_id, "resume"))


@router.post("/{agent_id}/archive", response_model=AgentDetail)
async def archive(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.transition(db, ctx, agent_id, "archive"))


@router.post("/{agent_id}/unarchive", response_model=AgentDetail)
async def unarchive(agent_id: uuid.UUID, ctx: AgentsManage, db: DB) -> AgentDetail:
    return await _detail(db, await service.transition(db, ctx, agent_id, "unarchive"))


@router.post(
    "/{agent_id}/test-session",
    response_model=TestSession,
    summary="Start a browser test call with the published version",
)
async def test_session(
    agent_id: uuid.UUID, data: TestSessionRequest, ctx: AgentsManage, db: DB
) -> TestSession:
    return await service.test_session(db, ctx, agent_id, data.lead_id)
