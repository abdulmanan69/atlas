"""Wire-level names of the voice runtime's webhook delivery (integration layer only).

Verified against the runtime's delivery task (tasks/webhook_delivery.py, `_build_headers`):
every delivery carries a stable delivery id, the run id and the attempt number as HTTP
headers. The header names contain the vendor's product name; they are read here and
nowhere else, and never surface in responses, logs, API descriptions or documentation.
"""

from __future__ import annotations

# Stable per-delivery identifier, identical across the runtime's retries (dedupe key).
DELIVERY_ID_HEADER = "X-Dograh-Delivery-Id"
# Runtime run id of the call the delivery is about (fallback when the body lacks it).
RUN_ID_HEADER = "X-Dograh-Workflow-Run-Id"
# 1-based attempt counter (informational).
ATTEMPT_HEADER = "X-Dograh-Delivery-Attempt"

__all__ = ["ATTEMPT_HEADER", "DELIVERY_ID_HEADER", "RUN_ID_HEADER"]
