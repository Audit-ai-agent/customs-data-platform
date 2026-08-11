"""Build and serve a bounded local Korean trade guidance snapshot.

Normal build/serve commands never collect from the internet, start Docker, or
adopt the legacy three-law Data profile. Operators stage only registry-approved
official source bytes below the ignored local profile, then use ``build`` to
verify their hashes and derive a small guidance-only projection. The separately
named V2 detector performs a bounded allowlisted GET only after its explicit
``--confirm-network`` acknowledgement; it creates a non-active local candidate
and never approves, builds, or activates it.
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

from src.functional_uat_kr_trade import (  # noqa: E402
    FunctionalUatKrTradeError,
    build_kr_trade_guidance_snapshot,
    inspect_kr_trade_snapshot,
    query_kr_trade_guidance_v3,
    register_kr_trade_source_receipts,
    require_kr_trade_retrieval_snapshot,
)
from src.functional_uat_kr_trade_v2 import (  # noqa: E402
    approve_kr_trade_v2_source_changes,
    build_kr_trade_v2_guidance_snapshot,
    detect_kr_trade_v2_source_changes,
    inspect_kr_trade_v2_snapshot,
    list_kr_trade_v2_snapshot_history,
    query_kr_trade_v2_guidance_v3,
    require_kr_trade_v2_retrieval_snapshot,
)
from src.local_profiles import (  # noqa: E402
    FUNCTIONAL_UAT_KR_TRADE_PROFILE,
    ensure_functional_uat_kr_trade_profile,
    read_service_token,
)
from src.profile_evidence import (  # noqa: E402
    FUNCTIONAL_UAT_MODE,
    LITERAL_LOOPBACK_HOSTS,
    create_guidance_snapshot_application,
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
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="create isolated ignored local profile config and token")
    subparsers.add_parser("status", help="show only safe snapshot readiness metadata")
    register = subparsers.add_parser(
        "register-source-receipts",
        help="hash-check manually staged allowlisted source receipts without collecting or indexing",
    )
    register.add_argument(
        "--source-manifest",
        required=True,
        help="relative path below data/guidance-sources inside the ignored profile",
    )
    build = subparsers.add_parser(
        "build",
        help="hash-check staged allowlisted sources and create an immutable local query snapshot",
    )
    build.add_argument(
        "--source-manifest",
        required=True,
        help="relative v1 build manifest below data/guidance-sources in the ignored profile",
    )
    build_v2 = subparsers.add_parser(
        "build-v2",
        help="hash-check page-level V2 sources and create an immutable V2 guidance projection",
    )
    build_v2.add_argument(
        "--source-manifest",
        required=True,
        help="relative V2 build manifest below data/guidance-sources in the ignored profile",
    )
    subparsers.add_parser("verify", help="revalidate active source hashes and local projection")
    status_v2 = subparsers.add_parser("status-v2", help="show safe V2 snapshot readiness metadata")
    status_v2.add_argument(
        "--snapshot-id",
        help="inspect one immutable local V2 snapshot instead of the active pointer",
    )
    subparsers.add_parser(
        "list-v2-snapshot-history",
        help="list safe metadata for immutable local V2 snapshots without source paths",
    )
    subparsers.add_parser(
        "verify-v2", help="revalidate active V2 page-level source hashes and projection"
    )
    detect_v2 = subparsers.add_parser(
        "detect-v2-source-changes",
        help="explicitly GET registry allowlisted sources and stage a non-active local candidate",
    )
    detect_v2.add_argument(
        "--confirm-network",
        action="store_true",
        help="required acknowledgement before any official-source GET request",
    )
    approve_v2 = subparsers.add_parser(
        "approve-v2-source-changes",
        help="build and atomically activate an operator-approved local source candidate",
    )
    approve_v2.add_argument("--candidate-id", required=True)
    approve_v2.add_argument("--candidate-sha256", required=True)
    approve_v2.add_argument("--approval-id", required=True)
    serve = subparsers.add_parser(
        "serve-guidance",
        help="serve only /guidance/query from a verified snapshot; it is not an Evidence store",
    )
    serve.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=18093)
    serve_v2 = subparsers.add_parser(
        "serve-guidance-v2",
        help="serve only /guidance/query from the verified V2 page-level snapshot",
    )
    serve_v2.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, default="127.0.0.1")
    serve_v2.add_argument("--port", type=_port, default=18094)
    return parser


def _serve_guidance(host: str, port: int) -> int:
    require_kr_trade_retrieval_snapshot()
    app = create_guidance_snapshot_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=read_service_token(FUNCTIONAL_UAT_KR_TRADE_PROFILE),
        runtime_state_validator=require_kr_trade_retrieval_snapshot,
        guidance_query_handler=query_kr_trade_guidance_v3,
    )
    print(
        "functional-uat-kr-trade guidance snapshot ready "
        f"at http://{host}:{port} (mode=functional-uat, guidance-only, human-review-only)"
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


def _serve_guidance_v2(host: str, port: int) -> int:
    require_kr_trade_v2_retrieval_snapshot()
    app = create_guidance_snapshot_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=read_service_token(FUNCTIONAL_UAT_KR_TRADE_PROFILE),
        runtime_state_validator=require_kr_trade_v2_retrieval_snapshot,
        guidance_query_handler=query_kr_trade_v2_guidance_v3,
    )
    print(
        "functional-uat-kr-trade V2 guidance snapshot ready "
        f"at http://{host}:{port} (mode=functional-uat, guidance-only, human-review-only)"
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            ensure_functional_uat_kr_trade_profile()
            print(
                "functional-uat-kr-trade profile initialized; "
                "official source build and retrieval projection are still required"
            )
            return 0
        if args.command == "register-source-receipts":
            status = register_kr_trade_source_receipts(
                staged_manifest_relative=args.source_manifest,
            )
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} "
                f"status={status.status} sources={status.source_count}; "
                "retrieval projection is still required"
            )
            return 0
        if args.command == "build":
            status = build_kr_trade_guidance_snapshot(
                staged_manifest_relative=args.source_manifest,
            )
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} status={status.status} "
                f"sources={status.source_count} records={status.record_count} "
                f"coverage={status.coverage_count}; guidance-only snapshot verified"
            )
            return 0
        if args.command == "build-v2":
            v2_status = build_kr_trade_v2_guidance_snapshot(
                staged_manifest_relative=args.source_manifest,
            )
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} registry=2.0 status={v2_status.status} "
                f"sources={v2_status.source_count} records={v2_status.record_count} "
                f"covered_bundles={v2_status.covered_bundle_count}/{v2_status.coverage_count}; "
                "V2 guidance-only snapshot verified"
            )
            return 0
        if args.command == "detect-v2-source-changes":
            detection = detect_kr_trade_v2_source_changes(
                confirm_network=args.confirm_network,
            )
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} registry=2.0 "
                f"status={detection.status} sources={detection.source_count} "
                f"changed={detection.changed_source_count} unavailable={detection.unavailable_source_count} "
                f"active_snapshot={detection.active_snapshot_id or 'none'} "
                f"candidate={detection.candidate_id or 'none'} "
                f"candidate_sha256={detection.candidate_sha256 or 'none'}"
            )
            return 0
        if args.command == "approve-v2-source-changes":
            approved_status = approve_kr_trade_v2_source_changes(
                candidate_id=args.candidate_id,
                candidate_sha256=args.candidate_sha256,
                approval_id=args.approval_id,
            )
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} registry=2.0 status={approved_status.status} "
                f"snapshot={approved_status.snapshot_id} sources={approved_status.source_count} "
                "V2 candidate approval activated atomically"
            )
            return 0
        if args.command == "verify":
            status = inspect_kr_trade_snapshot()
            if status.status != "ready":
                raise FunctionalUatKrTradeError("KR_TRADE_RETRIEVAL_PROJECTION_REQUIRED")
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} status=ready "
                f"sources={status.source_count} records={status.record_count} "
                f"coverage={status.coverage_count}; local source hashes verified"
            )
            return 0
        if args.command == "serve-guidance":
            return _serve_guidance(args.host, args.port)
        if args.command == "verify-v2":
            v2_status = inspect_kr_trade_v2_snapshot()
            if v2_status.status != "ready":
                raise FunctionalUatKrTradeError("KR_TRADE_V2_RETRIEVAL_PROJECTION_REQUIRED")
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} registry=2.0 status=ready "
                f"sources={v2_status.source_count} records={v2_status.record_count} "
                f"covered_bundles={v2_status.covered_bundle_count}/{v2_status.coverage_count}; "
                "local source hashes verified"
            )
            return 0
        if args.command == "serve-guidance-v2":
            return _serve_guidance_v2(args.host, args.port)
        if args.command == "status-v2":
            v2_status = inspect_kr_trade_v2_snapshot(snapshot_id=args.snapshot_id)
            print(
                f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} registry=2.0 "
                f"status={v2_status.status} sources={v2_status.source_count} records={v2_status.record_count} "
                f"covered_bundles={v2_status.covered_bundle_count}/{v2_status.coverage_count} "
                f"snapshot={v2_status.snapshot_id or 'none'} registry={v2_status.registry_sha256[:12]}"
            )
            return 0
        if args.command == "list-v2-snapshot-history":
            history = list_kr_trade_v2_snapshot_history()
            for entry in history:
                snapshot = entry.snapshot
                print(
                    f"snapshot={snapshot.snapshot_id} active={str(entry.is_active).lower()} "
                    f"as_of={snapshot.as_of} sources={snapshot.source_count} "
                    f"records={snapshot.record_count} "
                    f"covered_bundles={snapshot.covered_bundle_count}/{snapshot.coverage_count}"
                )
            if not history:
                print("snapshot_history=empty")
            return 0
        status = inspect_kr_trade_snapshot()
        print(
            f"profile={FUNCTIONAL_UAT_KR_TRADE_PROFILE} "
            f"status={status.status} sources={status.source_count} records={status.record_count} "
            f"registry={status.registry_sha256[:12]}"
        )
        return 0
    except FunctionalUatKrTradeError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
