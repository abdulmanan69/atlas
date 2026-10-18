"""Version 1 API router.

Endpoint modules under `app/api/v1/endpoints/` are discovered automatically: any
module exposing `router` is mounted. Tag order in the docs follows ORDER first,
then any remaining modules alphabetically.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter

from app.api.v1 import endpoints

ORDER = [
    "health",
    "auth",
    "organizations",
    "leads",
    "tasks",
    "agents",
    "ai_settings",
    "campaigns",
    "calls",
    "analytics",
    "audit",
]

api_router = APIRouter(prefix="/api/v1")

_discovered = sorted(m.name for m in pkgutil.iter_modules(endpoints.__path__))
for _name in [*[n for n in ORDER if n in _discovered], *[n for n in _discovered if n not in ORDER]]:
    _module = importlib.import_module(f"{endpoints.__name__}.{_name}")
    if hasattr(_module, "router"):
        api_router.include_router(_module.router)
