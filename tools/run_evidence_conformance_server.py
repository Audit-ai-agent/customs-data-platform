"""Run an explicit loopback-only Evidence API conformance server.

This command is test infrastructure, not a production backend selector.  It
requires an opt-in argv flag and a sealed provider bundle; no environment
variable can turn the production application into this fixture.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "law_repository"))

from src.provider_identity import load_provider_build_identity  # noqa: E402
from src.webapp import create_test_conformance_application  # noqa: E402

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test-only-conformance",
        action="store_true",
        required=True,
        help="mandatory acknowledgement that this is a non-production fixture",
    )
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--host", choices=LOOPBACK_HOSTS, required=True)
    parser.add_argument("--port", type=_port, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    identity = load_provider_build_identity(bundle_root=args.bundle_root)
    app = create_test_conformance_application(identity=identity)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
