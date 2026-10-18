"""Scrub the runtime vendor's name from text that is passed through to tenants.

The runtime occasionally embeds its own product name in free-text fields (provider
sync messages, inactive reasons, bootstrap resource names). ATLAS never shows that
name, so every pass-through string goes through `scrub_text`.
"""

from __future__ import annotations

import re

# The vendor's product name, spelled once here so it never appears elsewhere in ATLAS.
_VENDOR = re.compile(r"\bdograh\b", re.IGNORECASE)
REPLACEMENT = "the voice service"


def scrub_text(value: str | None) -> str | None:
    if value is None:
        return None
    scrubbed = _VENDOR.sub(REPLACEMENT, value)
    # Sentence starts read better capitalised.
    return re.sub(
        r"(^|[.!?]\s+)the voice service",
        lambda m: m.group(1) + "The voice service",
        scrubbed,
    )


def mentions_vendor(value: str | None) -> bool:
    return bool(value) and _VENDOR.search(value or "") is not None
