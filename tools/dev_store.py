"""Operate the optional, isolated Data-store development profile.

``dev-fixture`` remains the team default.  Use this only when changing Data
ingestion or retrieval code and real PostgreSQL/Qdrant/Neo4j are useful.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.local_profiles import (  # noqa: E402
    DEV_STORE_PROFILE,
    LocalProfileError,
    apply_data_store_environment,
    ensure_dev_store_profile,
    load_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create ignored config with random local DB passwords")
    compose = subparsers.add_parser("compose", help="operate dev-store Docker services")
    compose.add_argument("action", choices=("up", "down", "status"))
    execute = subparsers.add_parser("exec", help="run one Data command with profile environment")
    execute.add_argument("command_line", nargs=argparse.REMAINDER)
    return parser


def _compose(action: str) -> int:
    profile = ensure_dev_store_profile()
    command = [
        "docker",
        "compose",
        "--project-name",
        "customs-dev-store",
        "--env-file",
        str(profile / "config.env"),
        "-f",
        str(LAW_ROOT / "docker-compose.yml"),
    ]
    if action == "up":
        command.extend(("up", "--detach", "--wait"))
    elif action == "down":
        command.append("down")
    else:
        command.append("ps")
    try:
        return int(subprocess.run(command, check=False).returncode)
    except OSError:
        print("BLOCKED: Docker Compose is unavailable", file=sys.stderr)
        return 2


def _exec(command_line: list[str]) -> int:
    if not command_line:
        print("BLOCKED: provide a command after --", file=sys.stderr)
        return 2
    try:
        profile = apply_data_store_environment(DEV_STORE_PROFILE)
        environment = os.environ.copy()
        environment.update(load_config(profile / "config.env"))
        return int(
            subprocess.run(command_line, cwd=LAW_ROOT, env=environment, check=False).returncode
        )
    except LocalProfileError:
        print("BLOCKED: initialize dev-store first", file=sys.stderr)
        return 2
    except OSError:
        print("BLOCKED: requested command is unavailable", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        print(f"dev-store profile initialized: {ensure_dev_store_profile()}")
        return 0
    if args.command == "compose":
        return _compose(args.action)
    return _exec(args.command_line)


if __name__ == "__main__":
    raise SystemExit(main())
