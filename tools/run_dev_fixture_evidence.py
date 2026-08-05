"""Run the deterministic loopback Evidence V3 server for team development.

This is the default Data service for a fresh clone.  It uses no Docker,
official-law API, model cache, or external credential.  Its results are
synthetic and always human-review-only; it is never a legal/UAT quality claim.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.local_profiles import DEV_FIXTURE_PROFILE, ensure_service_token, read_service_token  # noqa: E402
from src.profile_evidence import (  # noqa: E402
    LITERAL_LOOPBACK_HOSTS,
    create_dev_fixture_application,
)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, default="127.0.0.1")
    parser.add_argument("--port", type=_port, default=18083)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Creating a token is harmless and makes first-run setup one command.  It
    # is intentionally not printed or copied into the repository.
    ensure_service_token(DEV_FIXTURE_PROFILE)
    app = create_dev_fixture_application(service_token=read_service_token(DEV_FIXTURE_PROFILE))
    print(
        "Data dev-fixture ready "
        f"at http://{args.host}:{args.port} (mode=local-development, human-review-only)"
    )
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        workers=1,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
