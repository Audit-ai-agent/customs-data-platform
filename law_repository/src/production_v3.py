"""Dedicated Evidence v3 production-candidate entrypoint."""

from __future__ import annotations

import json
import sys

from src.production import create_production_evidence_v3_application, run_production_server
from src.production_settings import ProductionSettings


def main() -> int:
    try:
        settings = ProductionSettings()
        app = create_production_evidence_v3_application(settings)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "evidence_runtime_v3_startup_failed",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78
    return run_production_server(settings, app)


if __name__ == "__main__":
    raise SystemExit(main())
