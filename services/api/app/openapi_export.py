"""Write the OpenAPI document without starting a server: `python -m app.openapi_export out.json`.

Used by the web app's `pnpm gen:api` so frontend types always match the code on disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import app


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {target} ({len(app.openapi()['paths'])} paths)")


if __name__ == "__main__":
    main()
