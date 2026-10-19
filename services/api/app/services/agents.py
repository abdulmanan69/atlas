"""AI agents: configuration versions, publishing to the voice runtime, lifecycle, tests."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidStateTransitionError,
    NotFoundError,
    ValidationFailedError,
)
from app.integrations.voice_runtime.client import RuntimeRejectedError
from app.integrations.voice_runtime.workflow_builder import (
    CompiledWorkflow,
    WebhookTarget,
    build_configurations,
    compile_workflow,
    effective_flow,
    guided_to_flow,
    variable_values,
)
from app.models.voice import Agent, AgentDirection, AgentStatus, AgentVersion, SyncStatus
from app.schemas.voice import (
    AgentConfig,
    AgentCreate,
    AgentMetaUpdate,
    AgentTemplate,
    CaptureField,
    CompiledPrompt,
    FlowDefinition,
    FlowIssue,
    FlowNode,
    FlowPath,
    FlowPosition,
    FlowValidation,
    IceServer,
    TestSession,
)
from app.services import actions as action_service
from app.services import agent_settings, audit, llm_warmup
from app.services import calls as call_service
from app.services import knowledge as knowledge_service
from app.services import leads as lead_service
from app.services.flows import compose_prompt, validate_flow
from app.services.runtime_accounts import ensure_account, runtime_client

log = logging.getLogger(__name__)

TEMPLATES: list[AgentTemplate] = [
    AgentTemplate(
        id="lead_qualifier",
        name="Lead qualifier",
        description=(
            "Calls new leads, confirms interest, asks qualifying questions and books a follow-up."
        ),
        direction=AgentDirection.OUTBOUND,
        config=AgentConfig(
            company_name="Your company",
            persona_name="Sarah",
            greeting="Hi {{first_name | fallback:there}}, this is Sarah from Your company. "
            "You recently asked about our services. Is now a good time for two quick questions?",
            instructions=(
                "Your goal is to find out whether this lead is a good fit and, if so, agree on a "
                "next step with a specialist. Be warm, brief and respectful of their time. If "
                "now is not a good time, ask when to call back and end the call."
            ),
            qualification_questions=[
                "What are you looking to achieve?",
                "What timeline are you working with?",
                "Do you have a budget in mind?",
                "Who else is involved in the decision?",
            ],
            capture_fields=[
                CaptureField(
                    name="timeline", type="string", description="When they want to start or buy"
                ),
                CaptureField(
                    name="budget", type="string", description="Budget they mentioned, with currency"
                ),
                CaptureField(
                    name="callback_time",
                    type="string",
                    description="Preferred time for a callback, if any",
                ),
            ],
        ),
    ),
    AgentTemplate(
        id="appointment_setter",
        name="Appointment setter",
        description="Books a meeting or viewing at a time that suits the lead.",
        direction=AgentDirection.OUTBOUND,
        config=AgentConfig(
            company_name="Your company",
            persona_name="Daniel",
            greeting="Hello {{first_name | fallback:there}}, it's Daniel from Your company. "
            "I'm calling to set up the appointment you requested. Do you have a minute?",
            instructions=(
                "Your goal is to agree on a date and time for an appointment. Offer weekday slots "
                "between 10am and 5pm. Confirm the date, time and the best phone number to reach "
                "them. Do not promise anything you have not been told."
            ),
            capture_fields=[
                CaptureField(
                    name="appointment_time", type="string", description="Agreed date and time"
                ),
                CaptureField(
                    name="confirmed",
                    type="boolean",
                    description="Did they confirm the appointment?",
                ),
            ],
        ),
    ),
    AgentTemplate(
        id="reengagement",
        name="Re-engagement follow-up",
        description=(
            "Re-contacts leads that went quiet and finds out whether they are still interested."
        ),
        direction=AgentDirection.OUTBOUND,
        config=AgentConfig(
            company_name="Your company",
            persona_name="Alex",
            greeting="Hi {{first_name | fallback:there}}, Alex here from Your company. We spoke a "
            "while ago and I wanted to check in. Is this a good moment?",
            instructions=(
                "Find out whether the lead is still interested, what changed since you last spoke, "
                "and whether they would like a specialist to follow up. Accept a no gracefully."
            ),
            capture_fields=[
                CaptureField(
                    name="still_interested",
                    type="boolean",
                    description="Are they still interested?",
                ),
                CaptureField(
                    name="reason", type="string", description="Main reason for their answer"
                ),
            ],
        ),
    ),
]

_TPL_NS = uuid.UUID("a2f6b1d4-8c3e-4b5a-9e7d-1f0c2b3a4d5e")
_tid = {k: str(uuid.uuid5(_TPL_NS, k)) for k in ("start", "qualify", "booked", "not_now")}
_pid = {k: str(uuid.uuid5(_TPL_NS, f"path:{k}")) for k in ("talk", "busy", "book", "decline")}

QUALIFY_AND_BOOK_FLOW = FlowDefinition(
    nodes=[
        FlowNode(
            id=_tid["start"],
            kind="start",
            name="Open the call",
            position=FlowPosition(x=0, y=120),
            greeting="Hi {{first_name | fallback:there}}, this is Sarah from Your company. "
            "You asked about our services recently. Do you have two minutes?",
            prompt=(
                "Find out whether now is a good time. If it is, move on to qualifying. "
                "If not, ask when would suit them and end the call politely."
            ),
        ),
        FlowNode(
            id=_tid["qualify"],
            kind="step",
            name="Qualify",
            position=FlowPosition(x=700, y=0),
            prompt=(
                "Ask, one at a time: what they are looking to achieve, their timeline, and "
                "whether they have a budget in mind. Listen to each answer before moving on. "
                "When they are a fit and open to it, offer a viewing or a call with a "
                "specialist on a weekday between 10am and 5pm and agree a time."
            ),
            capture_fields=[
                CaptureField(name="timeline", description="When they want to start or buy"),
                CaptureField(name="budget", description="Budget they mentioned, with currency"),
            ],
        ),
        FlowNode(
            id=_tid["booked"],
            kind="end",
            name="Booked",
            position=FlowPosition(x=1400, y=0),
            prompt="Confirm the agreed date and time, thank them, and say goodbye.",
            capture_fields=[
                CaptureField(name="appointment_time", description="Agreed date and time")
            ],
        ),
        FlowNode(
            id=_tid["not_now"],
            kind="end",
            name="Not now",
            position=FlowPosition(x=1400, y=260),
            prompt="Thank them for their time, confirm any callback they asked for, and say "
            "goodbye.",
            capture_fields=[
                CaptureField(
                    name="callback_time", description="Preferred time for a callback, if any"
                )
            ],
        ),
    ],
    paths=[
        FlowPath(
            id=_pid["talk"],
            from_node=_tid["start"],
            to_node=_tid["qualify"],
            name="Ready to talk",
            when="They confirm they have a moment to talk",
        ),
        FlowPath(
            id=_pid["busy"],
            from_node=_tid["start"],
            to_node=_tid["not_now"],
            name="Bad time",
            when="They cannot talk now, are not interested, or ask not to be called",
        ),
        FlowPath(
            id=_pid["book"],
            from_node=_tid["qualify"],
            to_node=_tid["booked"],
            name="Booked",
            when="A date and time for the viewing or call has been agreed",
            say_while_moving="Great, let me confirm that.",
        ),
        FlowPath(
            id=_pid["decline"],
            from_node=_tid["qualify"],
            to_node=_tid["not_now"],
            name="Not a fit",
            when="They are not a fit, decline the next step, or want to end the call",
        ),
    ],
)

TEMPLATES.append(
    AgentTemplate(
        id="qualify_and_book",
        name="Qualify and book (flow)",
        description=(
            "A branching conversation: opens the call, qualifies the lead, then books a "
            "viewing or parks the lead for later. Edited on the flow canvas."
        ),
        direction=AgentDirection.OUTBOUND,
        config=AgentConfig(
            mode="flow",
            company_name="Your company",
            persona_name="Sarah",
            greeting="Hi {{first_name | fallback:there}}, this is Sarah from Your company.",
            instructions="Qualify the lead and book a viewing when they are a fit.",
            flow=QUALIFY_AND_BOOK_FLOW,
            global_rules=(
                "You are Sarah, calling on behalf of Your company.\n"
                "- Keep each reply to one or two sentences: this is a live phone call.\n"
                "- Never invent prices, availability or policies.\n"
                "- If asked whether you are a person or a machine, say you are an AI "
                "assistant.\n"
                "- If the person asks not to be contacted again, apologise, confirm it, and "
                "end the call."
            ),
            capture_fields=[],
        ),
    )
)

_TRANSITIONS: dict[str, dict[AgentStatus, set[AgentStatus]]] = {
    "pause": {AgentStatus.ACTIVE: {AgentStatus.PAUSED}},
    "resume": {AgentStatus.PAUSED: {AgentStatus.ACTIVE}},
    "archive": {
        AgentStatus.DRAFT: {AgentStatus.ARCHIVED},
        AgentStatus.ACTIVE: {AgentStatus.ARCHIVED},
        AgentStatus.PAUSED: {AgentStatus.ARCHIVED},
    },
    "unarchive": {AgentStatus.ARCHIVED: {AgentStatus.PAUSED}},
}
_TARGET = {
    "pause": AgentStatus.PAUSED,
    "resume": AgentStatus.ACTIVE,
    "archive": AgentStatus.ARCHIVED,
    "unarchive": AgentStatus.PAUSED,
}
TEST_SESSION_SECONDS = 3600
_PLACEHOLDER_COMPANY = "Your company"


async def list_agents(
    db: AsyncSession, ctx: TenantContext, *, include_archived: bool
) -> list[Agent]:
    stmt = select(Agent).where(Agent.organization_id == ctx.org_id)
    if not include_archived:
        stmt = stmt.where(Agent.status != AgentStatus.ARCHIVED)
    return list((await db.execute(stmt.order_by(Agent.updated_at.desc()))).scalars().all())


async def get_agent(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(Agent.id == agent_id, Agent.organization_id == ctx.org_id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    return agent


async def get_version(db: AsyncSession, agent: Agent, version: int) -> AgentVersion:
    row = (
        await db.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent.id, AgentVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Version {version} not found")
    return row


async def config_of(db: AsyncSession, agent: Agent, version: int | None = None) -> AgentConfig:
    row = await get_version(db, agent, version or agent.latest_version)
    return AgentConfig.model_validate(row.config)


def _personalise(config: AgentConfig, company: str) -> AgentConfig:
    """Replace the template's placeholder company name everywhere the agent speaks."""

    def fill(text: str | None) -> str | None:
        return text.replace(_PLACEHOLDER_COMPANY, company) if text else text

    update: dict[str, Any] = {
        "company_name": company,
        "greeting": fill(config.greeting),
        "global_rules": fill(config.global_rules),
    }
    if config.flow is not None:
        update["flow"] = config.flow.model_copy(
            update={
                "nodes": [
                    n.model_copy(update={"greeting": fill(n.greeting), "prompt": fill(n.prompt)})
                    for n in config.flow.nodes
                ]
            }
        )
    return config.model_copy(update=update)


async def create_agent(db: AsyncSession, ctx: TenantContext, data: AgentCreate) -> Agent:
    count = await db.scalar(
        select(func.count())
        .select_from(Agent)
        .where(Agent.organization_id == ctx.org_id, Agent.status != AgentStatus.ARCHIVED)
    )
    if int(count or 0) >= 200:
        raise ValidationFailedError("Agent limit reached for this organization")
    if data.config is not None:
        config = data.config
    else:
        template = next((t for t in TEMPLATES if t.id == (data.template or "lead_qualifier")), None)
        if template is None:
            raise ValidationFailedError("Unknown template")
        config = _personalise(template.config, ctx.organization.name[:120])
    agent = Agent(
        organization_id=ctx.org_id,
        name=data.name,
        description=data.description,
        direction=data.direction,
        status=AgentStatus.DRAFT,
        latest_version=1,
        created_by_user_id=ctx.user_id,
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentVersion(
            organization_id=ctx.org_id,
            agent_id=agent.id,
            version=1,
            config=config.model_dump(mode="json"),
            note="Created",
            created_by_user_id=ctx.user_id,
        )
    )
    await audit.record(
        db,
        action="agent.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={"name": data.name},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(agent)
    return agent


async def update_meta(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, data: AgentMetaUpdate
) -> Agent:
    agent = await get_agent(db, ctx, agent_id)
    changes = data.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(agent, k, v)
    if changes:
        await audit.record(
            db,
            action="agent.updated",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="agent",
            resource_id=agent.id,
            changes=changes,
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
    await db.flush()
    await db.refresh(agent)
    return agent


async def save_config(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, config: AgentConfig, note: str | None
) -> Agent:
    agent = await get_agent(db, ctx, agent_id)
    if agent.status == AgentStatus.ARCHIVED:
        raise InvalidStateTransitionError("Restore the agent before editing it")
    current = await config_of(db, agent)
    if current == config:
        return agent
    await agent_settings.check_audio_references(db, ctx.org_id, agent.id, config)
    agent.latest_version += 1
    db.add(
        AgentVersion(
            organization_id=ctx.org_id,
            agent_id=agent.id,
            version=agent.latest_version,
            config=config.model_dump(mode="json"),
            note=note,
            created_by_user_id=ctx.user_id,
        )
    )
    await audit.record(
        db,
        action="agent.version_saved",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={"version": agent.latest_version, "note": note},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(agent)
    return agent


async def list_versions(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID
) -> list[AgentVersion]:
    agent = await get_agent(db, ctx, agent_id)
    rows = await db.execute(
        select(AgentVersion)
        .where(AgentVersion.agent_id == agent.id)
        .order_by(AgentVersion.version.desc())
    )
    return list(rows.scalars().all())


async def restore_version(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, version: int
) -> Agent:
    agent = await get_agent(db, ctx, agent_id)
    config = await config_of(db, agent, version)
    return await save_config(db, ctx, agent.id, config, f"Restored from version {version}")


async def _attached(
    db: AsyncSession, ctx: TenantContext, flow: FlowDefinition
) -> tuple[dict[str, str], set[str]]:
    """(action id → function name, document ids) for everything the flow references.

    Actions and documents live in the voice service and are referenced by id only; ids
    that no longer exist are left out so validation can report them.
    """
    action_ids = {a for n in flow.nodes for a in n.action_ids}
    document_ids = {k for n in flow.nodes for k in n.knowledge_ids}
    names: dict[str, str] = {}
    documents: set[str] = set()
    if action_ids:
        actions = await action_service.list_actions(db, ctx)
        names = {a.id: a.function_name for a in actions if a.id in action_ids}
    if document_ids:
        docs = await knowledge_service.list_documents(db, ctx)
        documents = {d.id for d in docs if d.id in document_ids}
    return names, documents


async def validate_config(
    db: AsyncSession, ctx: TenantContext, config: AgentConfig
) -> tuple[FlowValidation, dict[str, str], set[str]]:
    """Local rules plus checks against attached actions and documents."""
    flow = effective_flow(config)
    names, documents = await _attached(db, ctx, flow)
    result = validate_flow(flow, names, global_rules=config.global_rules)
    for n in flow.nodes:
        if any(a not in names for a in n.action_ids):
            result.errors.append(
                FlowIssue(
                    target="node",
                    id=n.id,
                    field="action_ids",
                    message="An attached action no longer exists. Remove it or attach another.",
                )
            )
        if any(k not in documents for k in n.knowledge_ids):
            result.errors.append(
                FlowIssue(
                    target="node",
                    id=n.id,
                    field="knowledge_ids",
                    message="An attached document no longer exists. Remove it or attach another.",
                )
            )
    result.valid = not result.errors
    return result, names, documents


async def validate_agent(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID
) -> FlowValidation:
    """Validate the latest version without touching the voice service."""
    agent = await get_agent(db, ctx, agent_id)
    result, _, _ = await validate_config(db, ctx, await config_of(db, agent))
    return result


async def convert_to_flow(db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID) -> Agent:
    """Save a new version in flow mode, equivalent to the current guided script."""
    agent = await get_agent(db, ctx, agent_id)
    config = await config_of(db, agent)
    if config.mode == "flow":
        raise InvalidStateTransitionError("This agent already uses the flow builder")
    converted = config.model_copy(update={"mode": "flow", "flow": guided_to_flow(config)})
    return await save_config(db, ctx, agent.id, converted, "Switched to the flow builder")


async def compiled_prompt(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, node_id: str | None
) -> CompiledPrompt:
    agent = await get_agent(db, ctx, agent_id)
    preview = compose_prompt(await config_of(db, agent), node_id)
    if preview is None:
        raise NotFoundError("Node not found")
    return preview


_GUIDED_FIELDS: dict[tuple[str, str | None], tuple[str, str]] = {
    ("start", "greeting"): ("Script", "greeting"),
    ("start", "prompt"): ("Script", "instructions"),
    ("start", "name"): ("Script", "persona_name"),
    ("end", "prompt"): ("Wrap-up", "closing"),
    ("end", "capture_fields"): ("Outcomes & data", "capture_fields"),
    ("path", "when"): ("Script", "end_condition"),
}


def _publish_details(config: AgentConfig, issues: list[FlowIssue]) -> list[dict[str, Any]]:
    """Issues plus a human `section`, phrased for the editor the agent is authored in."""
    flow = effective_flow(config)
    nodes = {n.id: n for n in flow.nodes}
    paths = {p.id: p for p in flow.paths}
    out: list[dict[str, Any]] = []
    for issue in issues:
        row: dict[str, Any] = issue.model_dump()
        for optional in ("code", "estimated_tokens", "budget"):  # prompt-budget fields only
            if row.get(optional) is None:
                row.pop(optional, None)
        section, field = "Agent", issue.field or ""
        if issue.target == "node" and (node := nodes.get(issue.id)):
            section = {"start": "Start", "step": "Step", "end": "End"}[node.kind] + f": {node.name}"
            if config.mode == "guided":
                section, field = _GUIDED_FIELDS.get((node.kind, issue.field), (section, field))
        elif issue.target == "path" and (path := paths.get(issue.id)):
            section = f"Path: {path.name}"
            if config.mode == "guided":
                section, field = _GUIDED_FIELDS.get(("path", issue.field), (section, field))
        elif issue.field == "global_rules":
            section = "Global rules"
        elif issue.field == "call_reporting":
            section = "Call reporting"
        row["section"], row["field"] = section, field
        out.append(row)
    return out


def _runtime_issues(compiled: CompiledWorkflow, details: Any) -> list[FlowIssue]:
    errors = details.get("errors") if isinstance(details, dict) else details
    if not isinstance(errors, list):
        return []
    return [compiled.translate(e) for e in errors if isinstance(e, dict)]


async def publish(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, version: int | None, note: str | None
) -> Agent:
    settings = get_settings()
    agent = await get_agent(db, ctx, agent_id)
    if agent.status == AgentStatus.ARCHIVED:
        raise InvalidStateTransitionError("Archived agents cannot be published")
    target = version or agent.latest_version
    config = await config_of(db, agent, target)
    checked, action_names, documents = await validate_config(db, ctx, config)
    if not checked.valid:
        raise ValidationFailedError(
            "Fix the issues below before publishing.",
            details=_publish_details(config, checked.errors),
        )
    # Action and document ids are the voice service's own uuids (see those services).
    tool_uuids = {a: a for a in action_names}
    document_uuids = {d: d for d in documents}
    account = await ensure_account(db, ctx.org_id)

    async with runtime_client(db, ctx.org_id) as client:
        configured = account.managed_model_config_uuid or (
            settings.voice_allow_hosted_trial_models and await client.get_default_configuration()
        )
        if not configured:
            raise ValidationFailedError(
                "Set up AI providers before publishing: choose speech-to-text, a "
                "language model and a voice under Voice AI → AI providers."
            )
        connections = await client.list_connections()
        connection_ids = {c.uuid for c in connections}
        try:
            build_configurations(config, connection_ids=connection_ids)
        except ValueError as exc:
            raise ValidationFailedError(f"Model override: {exc}") from exc
        # Sound files and a separate voicemail-detection model are checked before anything
        # is written to the voice service.
        await agent_settings.check_audio_references(db, ctx.org_id, agent.id, config)
        classifier = await agent_settings.classifier_for_publish(db, agent, config, connections)
        webhook = (
            WebhookTarget(
                url=f"{settings.voice_runtime_callback_url.rstrip('/')}/api/v1/webhooks/voice-runtime",
                credential_uuid=account.runtime_credential_uuid,
            )
            if account.runtime_credential_uuid
            else None
        )
        compiled = compile_workflow(
            config, webhook=webhook, tool_uuids=tool_uuids, document_uuids=document_uuids
        )
        runtime_name = f"{agent.name[:80]} · {agent.id.hex[:8]}"
        try:
            if not agent.runtime_workflow_id:
                created = await client.create_workflow(runtime_name, compiled.definition)
                agent.runtime_workflow_id = str(created.id)
                agent.runtime_workflow_uuid = created.workflow_uuid
                # Keep the mapping even if handing over sound files fails below.
                await db.commit()
            # Files go to storage scoped to the workflow, so they follow its creation.
            assets = await agent_settings.publish_assets(db, client, agent, config, classifier)
            try:
                configurations = build_configurations(
                    config, connection_ids=connection_ids, assets=assets
                )
            except ValueError as exc:
                raise ValidationFailedError(str(exc)) from exc
            await client.update_workflow(
                agent.runtime_workflow_id,
                name=runtime_name,
                definition=compiled.definition,
                configurations=configurations,
                template_context_variables=variable_values(config),
            )
            await client.publish_workflow(
                agent.runtime_workflow_id, note=note or f"Version {target}"
            )
        except RuntimeRejectedError as exc:
            agent.sync_status = SyncStatus.ERROR
            agent.sync_error = exc.message[:500]
            # Persist the failure state; the error response would otherwise roll it back.
            await db.commit()
            issues = _runtime_issues(compiled, exc.details) or [
                FlowIssue(target="flow", id="", field=None, message=exc.message)
            ]
            raise ValidationFailedError(
                "The agent could not be published. Fix the issues below and try again.",
                details=_publish_details(config, issues),
            ) from exc

    agent.published_version = target
    if agent.status == AgentStatus.DRAFT:
        agent.status = AgentStatus.ACTIVE
    agent.sync_status = SyncStatus.SYNCED
    agent.sync_error = None
    agent.synced_at = datetime.now(UTC)
    await audit.record(
        db,
        action="agent.published",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={"version": target, "note": note},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(agent)
    return agent


async def transition(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, action: str
) -> Agent:
    agent = await get_agent(db, ctx, agent_id)
    allowed = _TRANSITIONS[action].get(agent.status, set())
    target = _TARGET[action]
    if target not in allowed:
        raise InvalidStateTransitionError(f"Cannot {action} an agent that is {agent.status.value}")
    if action == "resume" and agent.published_version is None:
        raise InvalidStateTransitionError("Publish the agent before activating it")
    if agent.runtime_workflow_id and action in ("archive", "unarchive"):
        async with runtime_client(db, ctx.org_id) as client:
            await client.set_workflow_status(
                agent.runtime_workflow_id, "archived" if action == "archive" else "active"
            )
    agent.status = target
    await audit.record(
        db,
        action=f"agent.{action}d" if action.endswith("e") else f"agent.{action}ed",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(agent)
    return agent


SAMPLE_VARIABLES = {
    "first_name": "Alex",
    "last_name": "Sample",
    "full_name": "Alex Sample",
    "company": "Example Co",
    "job_title": "Operations Manager",
    "email": "alex@example.com",
}


def lead_variables(lead: Any) -> dict[str, str]:
    full = " ".join(p for p in (lead.first_name, lead.last_name) if p)
    values = {
        "first_name": lead.first_name,
        "last_name": lead.last_name or "",
        "full_name": full,
        "company": lead.company or "",
        "job_title": lead.job_title or "",
        "email": lead.email or "",
    }
    for key, value in (lead.custom_fields or {}).items():
        if isinstance(value, (str, int, float, bool)) and key not in values:
            values[str(key)] = str(value)
    return values


async def test_session(
    db: AsyncSession, ctx: TenantContext, agent_id: uuid.UUID, lead_id: uuid.UUID | None
) -> TestSession:
    settings = get_settings()
    agent = await get_agent(db, ctx, agent_id)
    if agent.status == AgentStatus.ARCHIVED:
        raise InvalidStateTransitionError("Archived agents cannot be tested")
    if not agent.runtime_workflow_id or agent.published_version is None:
        raise InvalidStateTransitionError("Publish the agent before starting a test call")
    lead = await lead_service.get_lead(db, ctx, lead_id) if lead_id else None
    variables = lead_variables(lead) if lead is not None else dict(SAMPLE_VARIABLES)
    origin = settings.web_app_origin.rstrip("/")
    domain = urlparse(origin).netloc
    # Recorded under Calls (mode "test") with transcript and response times. The id
    # rides in the session context so the completion webhook finds the record; if the
    # session cannot start, the request rolls back and the record is never committed.
    call = await call_service.create_test_call(
        db, ctx, agent, lead_id=lead.id if lead is not None else None
    )
    context = {**variables, "atlas_call_id": str(call.id), "atlas_test": "true"}
    async with runtime_client(db, ctx.org_id) as client:
        token = await client.ensure_embed_token(agent.runtime_workflow_id, [domain])
        session = await client.start_embed_session(token, origin=origin, context=context)
        ice = await client.turn_servers(session.session_token, origin=origin)
    call_service.mark_test_call_started(db, call, session.workflow_run_id)
    # Self-hosted models only: read the prompt while the browser connects (background).
    llm_warmup.schedule_published_warmup(ctx.org_id, agent.id, agent.published_version, context)
    public = urlparse(settings.voice_runtime_public_url)
    scheme = "wss" if public.scheme == "https" else "ws"
    signaling = f"{scheme}://{public.netloc}{public.path.rstrip('/')}/api/v1/ws/public/signaling/{session.session_token}"
    await audit.record(
        db,
        action="agent.test_call_started",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="agent",
        resource_id=agent.id,
        changes={"lead_id": str(lead_id) if lead_id else None, "call_id": str(call.id)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    servers = [IceServer.model_validate(s) for s in ice] or [
        IceServer(urls="stun:stun.l.google.com:19302")
    ]
    return TestSession(
        session_token=session.session_token,
        signaling_url=signaling,
        ice_servers=servers,
        expires_in_seconds=TEST_SESSION_SECONDS,
        variables=variables,
        call_id=call.id,
    )


def has_unpublished_changes(agent: Agent) -> bool:
    return agent.published_version != agent.latest_version
