"""AI server monitor: checks every registered AI server every two minutes (ADR 0007).

Each check is one short request to the server's /health; a few servers are checked at a
time. A change of status (for example ok to down) is recorded in the audit log.
"""

from __future__ import annotations

import logging
from typing import Any

from arq import cron

from app.services import ai_servers

log = logging.getLogger(__name__)


async def check_ai_servers(ctx: dict[str, Any]) -> dict[str, int]:
    counts = await ai_servers.check_all()
    if counts["changed"]:
        log.info("ai_servers_checked", extra=counts)
    return counts


FUNCTIONS = [check_ai_servers]
# Odd minutes, so it does not start together with the call reconciliation (even minutes).
CRON_JOBS = [
    cron(
        check_ai_servers,
        minute=set(range(1, 60, 2)),
        second=40,
        run_at_startup=False,
        timeout=90,
    )
]
