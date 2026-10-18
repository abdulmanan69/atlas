"""Outbound webhook delivery: outbox fan-out, signed HTTP delivery with retries, cleanup.

Every 15 seconds unprocessed `outbox_events` are fanned out into one
`webhook_deliveries` row per subscribed active endpoint, and due deliveries are sent.
A delivery is retried with exponential backoff until `webhook_max_attempts`, then
marked `dead` (a customer can retry it from the API). An endpoint that fails
`webhook_auto_disable_after_failures` times in a row is disabled and audited.

All jobs are idempotent: rows are claimed with `FOR UPDATE SKIP LOCKED` and leased for
a minute, so a crashed worker's batch is simply picked up again.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from arq import cron
from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import get_secret_box
from app.db.session import get_sessionmaker
from app.models.developer import DeliveryStatus, OutboxEvent, WebhookDelivery, WebhookEndpoint
from app.schemas.developer import WILDCARD_EVENT
from app.services import audit
from app.services.outbound_webhooks import (
    DeliveryResult,
    backoff_seconds,
    build_envelope,
    http_client,
    send_event,
)

log = logging.getLogger(__name__)

FAN_OUT_BATCH = 500
DELIVERY_BATCH = 100
DELIVERY_CONCURRENCY = 10
LEASE_SECONDS = 60
OUTBOX_RETENTION = timedelta(days=7)
DELIVERY_RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class _Job:
    delivery_id: uuid.UUID
    endpoint_id: uuid.UUID
    organization_id: uuid.UUID
    url: str
    secret: str
    event_type: str
    event_id: uuid.UUID
    payload: dict[str, Any]


# ------------------------------------------------------------------- fan-out


async def fan_out(sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime) -> int:
    """Turn unprocessed outbox events into deliveries. Returns deliveries created."""
    created = 0
    async with sessionmaker() as db:
        events = list(
            (
                await db.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.processed_at.is_(None))
                    .order_by(OutboxEvent.id)
                    .limit(FAN_OUT_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        if not events:
            return 0
        endpoints = (
            await db.execute(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.organization_id.in_({e.organization_id for e in events}),
                    WebhookEndpoint.is_active.is_(True),
                )
            )
        ).scalars()
        by_org: dict[uuid.UUID, list[WebhookEndpoint]] = defaultdict(list)
        for endpoint in endpoints:
            by_org[endpoint.organization_id].append(endpoint)
        for event in events:
            for endpoint in by_org.get(event.organization_id, ()):
                if (
                    WILDCARD_EVENT not in endpoint.events
                    and event.event_type not in endpoint.events
                ):
                    continue
                db.add(
                    WebhookDelivery(
                        organization_id=event.organization_id,
                        endpoint_id=endpoint.id,
                        event_type=event.event_type,
                        event_id=event.event_id,
                        payload=build_envelope(
                            event_id=event.event_id,
                            event_type=event.event_type,
                            organization_id=event.organization_id,
                            created_at=event.created_at,
                            data=event.data,
                        ),
                        status=DeliveryStatus.PENDING,
                        next_attempt_at=now,
                    )
                )
                created += 1
            event.processed_at = now
        await db.commit()
    if created:
        log.info("webhook_fan_out", extra={"events": len(events), "deliveries": created})
    return created


# ------------------------------------------------------------------ delivery


async def _claim(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> list[_Job]:
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(WebhookDelivery, WebhookEndpoint)
                .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
                .where(
                    WebhookDelivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.FAILED]),
                    WebhookDelivery.next_attempt_at <= now,
                    WebhookEndpoint.is_active.is_(True),
                )
                .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
                .limit(DELIVERY_BATCH)
                .with_for_update(skip_locked=True, of=WebhookDelivery)
            )
        ).all()
        box = get_secret_box()
        jobs: list[_Job] = []
        for delivery, endpoint in rows:
            # Lease: if this worker dies mid-batch the row becomes due again shortly.
            delivery.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
            jobs.append(
                _Job(
                    delivery_id=delivery.id,
                    endpoint_id=endpoint.id,
                    organization_id=delivery.organization_id,
                    url=endpoint.url,
                    secret=box.decrypt(endpoint.secret_encrypted),
                    event_type=delivery.event_type,
                    event_id=delivery.event_id,
                    payload=delivery.payload,
                )
            )
        await db.commit()
    return jobs


async def _apply_result(db: AsyncSession, job: _Job, result: DeliveryResult, now: datetime) -> None:
    settings = get_settings()
    delivery = await db.get(WebhookDelivery, job.delivery_id)
    if delivery is None:  # endpoint deleted while we were sending
        return
    delivery.attempts += 1
    delivery.last_status_code = result.status_code
    delivery.last_error = result.error
    if result.ok:
        delivery.status = DeliveryStatus.DELIVERED
        delivery.delivered_at = now
        delivery.next_attempt_at = None
        await db.execute(
            update(WebhookEndpoint)
            .where(WebhookEndpoint.id == job.endpoint_id)
            .values(failure_count=0)
            .execution_options(synchronize_session=False)
        )
        return

    if delivery.attempts >= settings.webhook_max_attempts:
        delivery.status = DeliveryStatus.DEAD
        delivery.next_attempt_at = None
    else:
        delivery.status = DeliveryStatus.FAILED
        delivery.next_attempt_at = now + timedelta(seconds=backoff_seconds(delivery.attempts))
    failures = await db.scalar(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.id == job.endpoint_id)
        .values(failure_count=WebhookEndpoint.failure_count + 1)
        .returning(WebhookEndpoint.failure_count)
        .execution_options(synchronize_session=False)
    )
    if failures is None or failures < settings.webhook_auto_disable_after_failures:
        return
    endpoint = await db.get(WebhookEndpoint, job.endpoint_id)
    if endpoint is None or not endpoint.is_active:
        return
    endpoint.is_active = False
    endpoint.disabled_at = now
    endpoint.disabled_reason = (
        f"Disabled automatically after {failures} consecutive failed deliveries"
    )
    await audit.record(
        db,
        action="webhook.auto_disabled",
        organization_id=job.organization_id,
        actor_user_id=None,
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        changes={"failure_count": failures, "last_error": result.error, "url": endpoint.url},
    )
    log.warning(
        "webhook_endpoint_auto_disabled",
        extra={"endpoint_id": str(endpoint.id), "failures": failures},
    )


async def deliver_due(sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime) -> int:
    """Send every due delivery once and record the outcome. Returns deliveries attempted."""
    jobs = await _claim(sessionmaker, now)
    if not jobs:
        return 0
    semaphore = asyncio.Semaphore(DELIVERY_CONCURRENCY)

    async def run(job: _Job) -> DeliveryResult:
        async with semaphore, http_client() as client:
            return await send_event(
                client,
                url=job.url,
                secret=job.secret,
                event_type=job.event_type,
                event_id=job.event_id,
                payload=job.payload,
                delivery_id=job.delivery_id,
            )

    results = await asyncio.gather(*(run(job) for job in jobs))
    async with sessionmaker() as db:
        for job, result in zip(jobs, results, strict=True):
            await _apply_result(db, job, result, now)
        await db.commit()
    delivered = sum(1 for r in results if r.ok)
    log.info("webhook_deliveries", extra={"attempted": len(jobs), "delivered": delivered})
    return len(jobs)


async def purge(sessionmaker: async_sessionmaker[AsyncSession], *, now: datetime) -> int:
    """Drop processed outbox rows and finished deliveries past their retention."""
    async with sessionmaker() as db:
        events = cast(
            "CursorResult[Any]",
            await db.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.processed_at.is_not(None),
                    OutboxEvent.processed_at < now - OUTBOX_RETENTION,
                )
            ),
        )
        deliveries = cast(
            "CursorResult[Any]",
            await db.execute(
                delete(WebhookDelivery).where(
                    WebhookDelivery.status.in_([DeliveryStatus.DELIVERED, DeliveryStatus.DEAD]),
                    WebhookDelivery.created_at < now - DELIVERY_RETENTION,
                )
            ),
        )
        await db.commit()
    return int(events.rowcount or 0) + int(deliveries.rowcount or 0)


# ------------------------------------------------------------------ ARQ jobs


async def fan_out_outbox(ctx: dict[Any, Any]) -> int:
    return await fan_out(get_sessionmaker(), now=datetime.now(UTC))


async def deliver_pending(ctx: dict[Any, Any]) -> int:
    return await deliver_due(get_sessionmaker(), now=datetime.now(UTC))


async def purge_webhook_history(ctx: dict[Any, Any]) -> int:
    return await purge(get_sessionmaker(), now=datetime.now(UTC))


FUNCTIONS = [fan_out_outbox, deliver_pending, purge_webhook_history]
CRON_JOBS = [
    cron(fan_out_outbox, second={0, 15, 30, 45}, run_at_startup=False, timeout=120),
    cron(deliver_pending, second={5, 20, 35, 50}, run_at_startup=False, timeout=240),
    cron(purge_webhook_history, hour=3, minute=17, run_at_startup=False, timeout=600),
]
