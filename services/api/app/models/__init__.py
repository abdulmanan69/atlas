"""Import every model module so Alembic autogenerate and relationship resolution see them.

Modules are discovered automatically: adding `app/models/<domain>.py` is enough.
"""

from __future__ import annotations

import importlib
import pkgutil

__all__: list[str] = []

for _module in sorted(m.name for m in pkgutil.iter_modules(__path__)):
    _mod = importlib.import_module(f"{__name__}.{_module}")
    for _name in getattr(_mod, "__all__", ()):
        globals()[_name] = getattr(_mod, _name)
        __all__.append(_name)
