"""Development seed: `python -m app.seed`. Creates clearly-labelled synthetic demo data.

Idempotent and incremental: creates the demo organization and leads on first run, and
adds the voice demo (an agent, a finished dry-run campaign and simulated calls) when it
is missing. Refuses to run in production. Nothing here touches the voice runtime.
"""

from __future__ import annotations

import asyncio
import os
import random
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import dispose_engine, get_sessionmaker
from app.integrations.voice_runtime.simulated import simulate
from app.models.calls import Call, CallDirection, CallMode, CallState
from app.models.campaigns import (
    DEFAULT_CALLING_HOURS,
    DEFAULT_RETRY_POLICY,
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignMode,
    CampaignStatus,
)
from app.models.crm import Activity, ActivityType, ActorType, Lead, PipelineStage
from app.models.identity import Membership, Organization, User
from app.models.voice import Agent, AgentDirection, AgentStatus, AgentVersion, SyncStatus
from app.schemas.identity import OrganizationCreate
from app.services.latency import simulated as simulated_latency
from app.services.organizations import create_organization

DEMO_EMAIL = "demo@example.com"
DEMO_ORG_NAME = "Demo Realty (demo data)"
FIRST = [
    "Ayesha",
    "Bilal",
    "Chen",
    "Daniel",
    "Elena",
    "Farah",
    "Gabriel",
    "Hina",
    "Imran",
    "Julia",
    "Kamran",
    "Lina",
    "Marcus",
    "Noor",
    "Omar",
    "Priya",
    "Quinn",
    "Rashid",
    "Sara",
    "Tariq",
]
LAST = ["Khan", "Ahmed", "Wei", "Smith", "Rossi", "Malik", "Silva", "Raza", "Qureshi", "Novak"]
COMPANIES = [
    "Northwind Traders",
    "Contoso Ltd",
    "Fabrikam",
    "Tailspin Toys",
    "Adventure Works",
    None,
    None,
]
SOURCES = ["web_form", "csv_import", "referral", "inbound_call", "event"]


async def _seed_org(
    db: AsyncSession, rng: random.Random, password: str
) -> tuple[Organization, User]:
    user = User(email=DEMO_EMAIL, full_name="Demo Owner", password_hash=hash_password(password))
    db.add(user)
    await db.flush()
    org = await create_organization(
        db,
        user,
        OrganizationCreate(name=DEMO_ORG_NAME, default_country="GB", timezone="Europe/London"),
    )
    stages = list(
        (
            await db.execute(
                select(PipelineStage)
                .where(PipelineStage.organization_id == org.id)
                .order_by(PipelineStage.position)
            )
        ).scalars()
    )
    weights = [30, 25, 15, 8, 8, 10, 4][: len(stages)]
    now = datetime.now(UTC)
    for i in range(40):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        created = now - timedelta(days=rng.randint(0, 60), hours=rng.randint(0, 23))
        stage = rng.choices(stages, weights=weights)[0]
        lead = Lead(
            organization_id=org.id,
            first_name=first,
            last_name=last,
            phone_e164=f"+44207946{i:04d}",
            phone_raw=f"020 7946 {i:04d}",
            email=f"{first.lower()}.{last.lower()}{i}@example.com",
            company=rng.choice(COMPANIES),
            source=rng.choice(SOURCES),
            tags=["demo", *(["vip"] if i % 7 == 0 else [])],
            custom_fields={},
            stage_id=stage.id,
            owner_user_id=user.id if i % 3 else None,
            created_by_user_id=user.id,
            next_follow_up_at=now + timedelta(days=rng.randint(-3, 10)) if i % 4 == 0 else None,
        )
        lead.created_at = created
        db.add(lead)
        await db.flush()
        db.add(
            Activity(
                organization_id=org.id,
                lead_id=lead.id,
                type=ActivityType.SYSTEM,
                actor_type=ActorType.SYSTEM,
                summary="Lead created (demo data)",
                data={"demo": True},
                occurred_at=created,
            )
        )
    return org, user


async def _seed_voice(db: AsyncSession, org: Organization, user: User, rng: random.Random) -> int:
    """An agent, a finished dry-run campaign and simulated calls. Returns calls created."""
    from app.services.agents import TEMPLATES  # local import: avoids a cycle at module load

    template = next(t for t in TEMPLATES if t.id == "lead_qualifier")
    config = template.config.model_copy(update={"company_name": org.name})
    config = config.model_copy(
        update={"greeting": config.greeting.replace("Your company", org.name)}
    )
    outcomes = [o.code for o in config.outcomes]
    now = datetime.now(UTC)

    agent = Agent(
        organization_id=org.id,
        name="Viewing qualifier (demo)",
        description="Demo agent. Not connected to the voice service; calls are simulated.",
        direction=AgentDirection.OUTBOUND,
        status=AgentStatus.ACTIVE,
        latest_version=1,
        published_version=1,
        sync_status=SyncStatus.NOT_SYNCED,
        created_by_user_id=user.id,
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentVersion(
            organization_id=org.id,
            agent_id=agent.id,
            version=1,
            config=config.model_dump(mode="json"),
            note="Created (demo data)",
            created_by_user_id=user.id,
        )
    )

    leads = list(
        (
            await db.execute(
                select(Lead)
                .where(Lead.organization_id == org.id, Lead.deleted_at.is_(None))
                .order_by(Lead.created_at)
            )
        ).scalars()
    )
    callable_leads = [lead for lead in leads if lead.phone_e164 and not lead.do_not_call][:24]
    started = now - timedelta(days=12)
    campaign = Campaign(
        organization_id=org.id,
        name="October viewings (demo dry run)",
        objective="Re-contact autumn enquiries and book viewings.",
        agent_id=agent.id,
        mode=CampaignMode.DRY_RUN,
        status=CampaignStatus.COMPLETED,
        lead_filter={"tag": "demo"},
        timezone=org.timezone,
        calling_hours=dict(DEFAULT_CALLING_HOURS),
        max_concurrency=3,
        calls_per_minute=6,
        retry_policy=dict(DEFAULT_RETRY_POLICY),
        created_by_user_id=user.id,
        started_by_user_id=user.id,
        started_at=started,
        finished_at=started + timedelta(hours=6),
    )
    db.add(campaign)
    await db.flush()

    created_calls = 0
    answered = 0
    for index, lead in enumerate(callable_leads):
        # Campaign calls over one afternoon, plus ad-hoc calls spread over the last month.
        in_campaign = index < 16
        call_time = (
            started + timedelta(minutes=12 * index)
            if in_campaign
            else now - timedelta(days=rng.randint(1, 29), hours=rng.randint(9, 17))
        )
        result = simulate(
            seed=f"demo-{lead.id}", first_name=lead.first_name, available_outcomes=outcomes
        )
        call = Call(
            organization_id=org.id,
            lead_id=lead.id,
            agent_id=agent.id,
            agent_version=1,
            campaign_id=campaign.id if in_campaign else None,
            direction=CallDirection.OUTBOUND,
            mode=CallMode.SIMULATED,
            state=CallState.COMPLETED if result.answered else CallState.NO_ANSWER,
            to_phone_e164=lead.phone_e164,
            started_at=call_time,
            ended_at=call_time + timedelta(seconds=result.duration_seconds),
            duration_seconds=result.duration_seconds if result.answered else None,
            outcome=result.outcome,
            captured=result.captured if result.answered else {},
            opted_out=result.opted_out,
            transcript=result.transcript or None,
            initiated_by_user_id=user.id,
            result_synced_at=call_time + timedelta(seconds=result.duration_seconds + 5),
            # Synthetic, labelled source "simulated": never counted in real averages.
            latency=(
                simulated_latency(
                    f"demo-{lead.id}",
                    duration_seconds=result.duration_seconds,
                    started_at=call_time,
                )
                if result.answered
                else None
            ),
        )
        call.created_at = call_time
        db.add(call)
        await db.flush()
        created_calls += 1
        if result.answered:
            answered += 1
            lead.last_contacted_at = call.ended_at
        if result.opted_out:
            lead.do_not_call = True
        db.add(
            Activity(
                organization_id=org.id,
                lead_id=lead.id,
                type=ActivityType.CALL,
                actor_type=ActorType.AI_AGENT,
                summary=(
                    f"[Simulated] Call completed · {result.outcome}"
                    if result.answered
                    else "[Simulated] Call not answered"
                ),
                data={
                    "call_id": str(call.id),
                    "mode": CallMode.SIMULATED.value,
                    "outcome": result.outcome,
                    "duration_seconds": call.duration_seconds,
                    "captured": call.captured,
                    "opted_out": result.opted_out,
                    "demo": True,
                },
                occurred_at=call.ended_at or call_time,
            )
        )
        if in_campaign:
            db.add(
                CampaignLead(
                    organization_id=org.id,
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    status=CampaignLeadStatus.COMPLETED
                    if result.answered
                    else CampaignLeadStatus.EXHAUSTED,
                    attempts=1 if result.answered else 2,
                    last_call_id=call.id,
                    last_outcome=result.outcome,
                )
            )
    campaign.stats = {
        "attempted": min(16, len(callable_leads)),
        "answered": answered,
        "demo": True,
    }
    return created_calls


async def seed() -> None:
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to seed a production environment")
    rng = random.Random(42)  # noqa: S311 - deterministic synthetic data, not security
    password = os.environ.get("ATLAS_DEMO_PASSWORD") or secrets.token_urlsafe(12)
    created_org = False
    calls_created = 0

    async with get_sessionmaker()() as db:
        user = (
            await db.execute(select(User).where(func.lower(User.email) == DEMO_EMAIL))
        ).scalar_one_or_none()
        if user is None:
            org, user = await _seed_org(db, rng, password)
            created_org = True
        else:
            org_row = (
                await db.execute(
                    select(Organization)
                    .join(Membership, Membership.organization_id == Organization.id)
                    .where(Membership.user_id == user.id, Organization.name == DEMO_ORG_NAME)
                )
            ).scalar_one_or_none()
            if org_row is None:
                print(f"Demo user exists but the organization '{DEMO_ORG_NAME}' was not found.")
                return
            org = org_row
        has_calls = await db.scalar(select(Call.id).where(Call.organization_id == org.id))
        if not has_calls:
            calls_created = await _seed_voice(db, org, user, rng)
        await db.commit()

    if created_org:
        print(f"Seeded synthetic demo organization '{DEMO_ORG_NAME}' with 40 leads.")
        print(f"  Sign in: {DEMO_EMAIL}")
        if "ATLAS_DEMO_PASSWORD" not in os.environ:
            print(f"  Password (generated, shown once): {password}")
    if calls_created:
        print(
            "Added the voice demo: 1 agent, 1 finished dry-run campaign, "
            f"{calls_created} simulated calls."
        )
    if not created_org and not calls_created:
        print("Demo data already present. Nothing to do.")


async def _main() -> None:
    try:
        await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
