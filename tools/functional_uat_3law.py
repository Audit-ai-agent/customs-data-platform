"""Manage the real, local three-law functional UAT profile.

This command is intentionally opt-in.  It starts no service and downloads no
model unless its explicit subcommand is selected.  All generated state lives
under the workspace ``_local`` / ``_artifacts`` boundary.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.functional_uat import (  # noqa: E402
    FUNCTIONAL_UAT_AS_OF,
    FunctionalUatEvidenceBackend,
    FunctionalUatError,
    build_functional_uat,
    discard_active_functional_uat_manifest,
    prefetch_models,
    validate_functional_uat_state,
    validate_functional_uat_guidance_state,
    verify_functional_uat_store,
)
from src.local_profiles import (  # noqa: E402
    FUNCTIONAL_UAT_PROFILE,
    apply_functional_uat_runtime_environment,
    ensure_functional_uat_profile,
    read_service_token,
    shared_model_cache_directory,
)
from src.profile_evidence import (  # noqa: E402
    FUNCTIONAL_UAT_MODE,
    LITERAL_LOOPBACK_HOSTS,
    create_profile_evidence_application,
)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def _as_of(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("as-of must be YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create ignored local profile config and token")
    subparsers.add_parser("prefetch-models", help="download pinned models into _local cache")
    build = subparsers.add_parser("build", help="create the bounded real-data UAT stores")
    build.add_argument("--as-of", type=_as_of, default=FUNCTIONAL_UAT_AS_OF)

    compose = subparsers.add_parser("compose", help="operate profile-scoped local stores")
    compose.add_argument("action", choices=("up", "down", "status"))

    reset = subparsers.add_parser(
        "reset",
        help="discard only functional-UAT store volumes and the active build marker",
    )
    reset.add_argument(
        "--confirm-profile",
        choices=(FUNCTIONAL_UAT_PROFILE,),
        required=True,
        help="type the exact profile name to confirm this scoped reset",
    )

    serve = subparsers.add_parser("serve", help="run functional Evidence V3 on loopback")
    serve.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=18083)
    subparsers.add_parser(
        "verify-store",
        help="compare the complete PostgreSQL/Qdrant/Neo4j three-law store explicitly",
    )
    return parser


def _compose(action: str) -> int:
    profile = ensure_functional_uat_profile()
    config = profile / "config.env"
    command = [
        "docker",
        "compose",
        "--project-name",
        "customs-functional-uat-3law",
        "--env-file",
        str(config),
        "-f",
        str(LAW_ROOT / "docker-compose.yml"),
    ]
    if action == "up":
        command.extend(("up", "--detach", "--wait"))
    elif action == "down":
        command.append("down")
    elif action == "reset":
        command.extend(("down", "--volumes"))
    elif action == "status":
        command.append("ps")
    else:
        command.append("ps")
    try:
        completed = subprocess.run(command, check=False)
    except OSError as exc:
        raise FunctionalUatError("DOCKER_COMPOSE_UNAVAILABLE") from exc
    return int(completed.returncode)


def _reset() -> int:
    """Reset only this profile's stores after the explicit CLI confirmation."""

    if _compose("reset") != 0:
        return 2
    discard_active_functional_uat_manifest()
    print(
        "functional UAT stores and active manifest reset; "
        "raw local sources and UAT artifacts were preserved"
    )
    return 0


def _serve(host: str, port: int) -> int:
    validate_functional_uat_state()
    backend = FunctionalUatEvidenceBackend(review_as_of=FUNCTIONAL_UAT_AS_OF)
    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=read_service_token(FUNCTIONAL_UAT_PROFILE),
        backend=backend,
        runtime_state_validator=validate_functional_uat_state,
        guidance_state_validator=validate_functional_uat_guidance_state,
    )
    print(
        "Data functional UAT ready "
        f"at http://{host}:{port} (mode=functional-uat, human-review-only)"
    )
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        workers=1,
    )
    return 0


def _verify_store() -> int:
    """Run deep integrity checks only when a Data maintainer asks for them."""

    verify_functional_uat_store()
    print("functional UAT three-store integrity verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            profile = ensure_functional_uat_profile()
            print(f"functional UAT profile initialized: {profile}")
            print("Set LAW_API_OC only in the ignored config.env before build.")
            return 0
        if args.command == "prefetch-models":
            profile = apply_functional_uat_runtime_environment()
            prefetch_models(
                profile_directory=profile, cache_directory=shared_model_cache_directory()
            )
            print("functional UAT models are cached locally")
            return 0
        if args.command == "build":
            artifact = build_functional_uat(as_of=args.as_of)
            print(f"functional UAT manifest written: {artifact}")
            return 0
        if args.command == "compose":
            return _compose(args.action)
        if args.command == "reset":
            return _reset()
        if args.command == "serve":
            return _serve(args.host, args.port)
        if args.command == "verify-store":
            return _verify_store()
    except FunctionalUatError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
