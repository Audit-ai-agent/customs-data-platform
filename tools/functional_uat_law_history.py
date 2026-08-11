"""Manage the explicit local 65-law Evidence-history functional profile.

This tool never starts a scheduler, polls automatically, or reuses a clone
cache.  ``build`` creates the 2026-08-05 baseline and ``refresh`` is an
operator-selected collection window.  The normal service remains loopback
only and defaults to Evidence port 18083; run the retained three-law profile
on an explicit different loopback port if both are needed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Mapping, cast

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.law_history import (  # noqa: E402
    LAW_HISTORY_BASELINE_AS_OF,
    LAW_HISTORY_PROFILE,
    FunctionalLawHistoryEvidenceBackend,
    LawHistoryError,
    _active_manifest_path,
    _law_history_mutation_lock,
    _pending_refresh_path,
    build_law_history,
    recover_law_history_baseline,
    recover_law_history_build_lock,
    recover_law_history_published_lock,
    recover_law_history_refresh_lock,
    rebind_law_history_frozen_inputs,
    refresh_law_history,
    validate_law_history_state,
    verify_law_history_store,
)
from src.hsk_ccct_history import (  # noqa: E402
    ActiveArchiveCatalog,
    HskCcctArchiveError,
    capture_active_archive_catalog,
)
from src.local_profiles import (  # noqa: E402
    apply_functional_uat_law_history_runtime_environment,
    ensure_functional_uat_law_history_profile,
    functional_uat_law_history_cuda_opt_in,
    read_service_token,
    shared_model_cache_directory,
)
from src.functional_uat import prefetch_models  # noqa: E402
from src.profile_evidence import (  # noqa: E402
    FUNCTIONAL_UAT_MODE,
    LITERAL_LOOPBACK_HOSTS,
    ProfileEvidenceRestartRequired,
    create_profile_evidence_application,
)


LawHistoryRetrievalStrategy = Literal["rrf_lexical", "cross_encoder_rerank"]


@dataclass(frozen=True, slots=True)
class _LawHistoryServiceBinding:
    """The exact active-manifest configuration captured by one server process."""

    manifest_sha256: str
    coverage_through: date
    retrieval_strategy: LawHistoryRetrievalStrategy
    archive_catalog: ActiveArchiveCatalog

    def build_backend(self) -> FunctionalLawHistoryEvidenceBackend:
        return FunctionalLawHistoryEvidenceBackend(
            coverage_from=LAW_HISTORY_BASELINE_AS_OF,
            coverage_through=self.coverage_through,
            use_rerank=self.retrieval_strategy == "cross_encoder_rerank",
            archive_catalog=self.archive_catalog,
        )

    def validate_runtime(self) -> None:
        """Refuse requests once the active manifest no longer matches startup."""

        try:
            # Startup already hash-verifies every large local XLSX/receipt.
            # Request-time validation must remain bounded: the running
            # backend reads PostgreSQL only, so compare the activation pointer
            # and canonical manifests rather than rereading complete raw
            # archive bytes for every Evidence request.
            current = _capture_law_history_service_binding(verify_archive_assets=False)
        except HskCcctArchiveError as exc:
            # The activation pointer or a bound manifest is no longer valid.
            # Expose a restart-only 503 without leaking a local path/error.
            raise ProfileEvidenceRestartRequired("LAW_HISTORY_SERVICE_RESTART_REQUIRED") from exc
        if current != self:
            # Do not put manifest values or paths in the HTTP response.  The
            # profile layer maps this stable class to a restart-only 503.
            raise ProfileEvidenceRestartRequired("LAW_HISTORY_SERVICE_RESTART_REQUIRED")


def _binding_from_manifest(
    manifest: Mapping[str, object],
    manifest_sha256: str,
    archive_catalog: ActiveArchiveCatalog,
) -> _LawHistoryServiceBinding:
    """Extract only the backend-affecting fields from an already verified manifest."""

    coverage_raw = manifest.get("coverage_through")
    strategy_raw = manifest.get("retrieval_strategy")
    try:
        coverage_through = date.fromisoformat(cast(str, coverage_raw))
    except (TypeError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
    if strategy_raw not in {"rrf_lexical", "cross_encoder_rerank"}:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    return _LawHistoryServiceBinding(
        manifest_sha256=manifest_sha256,
        coverage_through=coverage_through,
        retrieval_strategy=cast(LawHistoryRetrievalStrategy, strategy_raw),
        archive_catalog=archive_catalog,
    )


def _capture_law_history_service_binding(
    *, verify_archive_assets: bool = True
) -> _LawHistoryServiceBinding:
    """Read a validated active manifest once, without mutating any profile state."""

    manifest, manifest_sha256 = validate_law_history_state()
    return _binding_from_manifest(
        manifest,
        manifest_sha256,
        capture_active_archive_catalog(verify_assets=verify_archive_assets),
    )


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return parsed


@contextmanager
def _cuda_opt_in_scope(allow_cuda: bool):
    """Permit CUDA only for one explicit local operator command."""

    if not allow_cuda:
        yield
        return
    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
    except Exception:
        cuda_available = False
    if not cuda_available:
        raise LawHistoryError("LAW_HISTORY_CUDA_UNAVAILABLE")
    with functional_uat_law_history_cuda_opt_in():
        yield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create ignored local profile config and service token")
    prefetch = commands.add_parser(
        "prefetch-models", help="download the required dense model into ignored local cache"
    )
    prefetch.add_argument(
        "--include-reranker",
        action="store_true",
        help="also prepare the optional bounded cross-encoder reranker",
    )
    build = commands.add_parser("build", help="build the complete 65-law baseline")
    build.add_argument("--as-of", type=_date, default=LAW_HISTORY_BASELINE_AS_OF)
    build.add_argument("--allow-cuda", action="store_true", help="explicit local-only CUDA opt-in")
    refresh = commands.add_parser(
        "refresh", help="explicitly collect one official law-history range"
    )
    refresh.add_argument("--from", dest="from_date", required=True, type=_date)
    refresh.add_argument("--to", dest="to_date", required=True, type=_date)
    refresh.add_argument(
        "--allow-cuda", action="store_true", help="explicit local-only CUDA opt-in"
    )
    recover = commands.add_parser(
        "recover-refresh",
        help="release a verified crash-left refresh lock; then rerun the same refresh",
    )
    recover.add_argument("--from", dest="from_date", required=True, type=_date)
    recover.add_argument("--to", dest="to_date", required=True, type=_date)
    recover.add_argument("--confirm-profile", required=True)
    recover_build = commands.add_parser(
        "recover-build",
        help="release a verified crash-left baseline-build lock; then reset before rebuilding",
    )
    recover_build.add_argument("--confirm-profile", required=True)
    recover_baseline = commands.add_parser(
        "recover-baseline",
        help="publish one fully verified, already-loaded baseline receipt batch without collection",
    )
    recover_baseline.add_argument("--source-batch", required=True)
    recover_baseline.add_argument("--confirm-profile", required=True)
    rebind_frozen_inputs = commands.add_parser(
        "rebind-frozen-inputs",
        help="replace only a verified stale fixed-input receipt without collection",
    )
    rebind_frozen_inputs.add_argument("--confirm-profile", required=True)
    recover_published = commands.add_parser(
        "recover-published",
        help="release a verified crash-left lock after a fully published active manifest",
    )
    recover_published.add_argument("--confirm-profile", required=True)
    reset = commands.add_parser(
        "reset", help="remove only this profile's local stores and active marker"
    )
    reset.add_argument("--confirm-profile", required=True)
    compose = commands.add_parser("compose", help="operate isolated local profile stores")
    compose.add_argument("action", choices=("up", "down", "status"))
    serve = commands.add_parser("serve", help="serve Evidence V3 on loopback")
    serve.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, default="127.0.0.1")
    serve.add_argument("--port", type=_port, default=18083)
    serve.add_argument("--allow-cuda", action="store_true", help="explicit local-only CUDA opt-in")
    commands.add_parser(
        "verify-store", help="verify local sources, models, and temporal store counts"
    )
    return parser


def _compose(action: str, *, profile: Path | None = None) -> int:
    """Run compose only against the supplied or freshly resolved profile."""

    if profile is None:
        profile = ensure_functional_uat_law_history_profile()
    command = [
        "docker",
        "compose",
        "--project-name",
        "customs-functional-uat-law-history",
        "--env-file",
        str(profile / "config.env"),
        "-f",
        str(LAW_ROOT / "docker-compose.yml"),
    ]
    if action == "up":
        command.extend(("up", "--detach", "--wait"))
    elif action == "down":
        command.append("down")
    elif action == "reset":
        command.extend(("down", "--volumes"))
    else:
        command.append("ps")
    try:
        return int(subprocess.run(command, check=False).returncode)
    except OSError as exc:
        raise LawHistoryError("DOCKER_COMPOSE_UNAVAILABLE") from exc


def _serve(host: str, port: int) -> int:
    binding = _capture_law_history_service_binding()
    backend = binding.build_backend()

    def guidance_is_not_part_of_this_profile() -> frozenset[str]:
        # V2 general guidance stays on its independently verified 18094 peer.
        raise LawHistoryError("LAW_HISTORY_PROFILE_HAS_NO_GUIDANCE_SNAPSHOT")

    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=read_service_token(LAW_HISTORY_PROFILE),
        backend=backend,
        runtime_state_validator=binding.validate_runtime,
        guidance_state_validator=guidance_is_not_part_of_this_profile,
    )
    print(
        "Data law-history functional UAT ready "
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


def _reset(confirm_profile: str) -> int:
    if confirm_profile != LAW_HISTORY_PROFILE:
        raise LawHistoryError("LAW_HISTORY_RESET_CONFIRMATION_INVALID")
    profile = ensure_functional_uat_law_history_profile()
    # Hold the same writer lock used by build, refresh, and manifest updates
    # for the entire destructive operation.  In particular, prove that the
    # active marker is safe and that no staged refresh exists *before* asking
    # Docker to remove this profile's volumes.  Do not call the public marker
    # discard helper here: it obtains this lock itself and would deadlock.
    with _law_history_mutation_lock(profile):
        active_marker = _reset_active_marker_path(profile)
        if _compose("reset", profile=profile) != 0:
            return 2
        # Compose cannot create profile-state files, but re-check immediately
        # before unlinking so a local replacement cannot turn reset into an
        # arbitrary-file deletion.
        active_marker = _reset_active_marker_path(profile)
        removed = _discard_reset_active_marker(active_marker)
    state = "removed" if removed else "already absent"
    print(
        "law-history stores and active manifest reset; "
        f"active marker {state}; raw local sources and artifacts were preserved"
    )
    return 0


def _reset_active_marker_path(profile: Path) -> Path:
    """Prove the profile marker/journal state is safe while reset owns its lock."""

    pending_refresh = _pending_refresh_path(profile)
    if pending_refresh.exists() or pending_refresh.is_symlink():
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    active_marker = _active_manifest_path(profile)
    if active_marker.exists() and not active_marker.is_file():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_PATH_UNSAFE")
    return active_marker


def _discard_reset_active_marker(active_marker: Path) -> bool:
    """Delete one already-validated regular marker without taking another lock."""

    if not active_marker.exists() and not active_marker.is_symlink():
        return False
    if active_marker.is_symlink() or not active_marker.is_file():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_PATH_UNSAFE")
    try:
        active_marker.unlink()
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_RESET_FAILED") from exc
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            profile = ensure_functional_uat_law_history_profile()
            print(f"law-history profile initialized: {profile}")
            print(
                "Set LAW_API_OC only in the ignored config.env before an explicit build or refresh."
            )
            return 0
        if args.command == "prefetch-models":
            profile = apply_functional_uat_law_history_runtime_environment()
            prefetch_models(
                profile_directory=profile,
                cache_directory=shared_model_cache_directory(),
                include_reranker=bool(args.include_reranker),
            )
            print("law-history required model cache is ready")
            return 0
        if args.command == "build":
            with _cuda_opt_in_scope(bool(args.allow_cuda)):
                artifact = build_law_history(as_of=args.as_of)
            print(f"law-history baseline manifest written: {artifact}")
            return 0
        if args.command == "refresh":
            with _cuda_opt_in_scope(bool(args.allow_cuda)):
                artifact = refresh_law_history(from_date=args.from_date, to_date=args.to_date)
            print(f"law-history refresh manifest written: {artifact}")
            return 0
        if args.command == "recover-refresh":
            released = recover_law_history_refresh_lock(
                from_date=args.from_date,
                to_date=args.to_date,
                confirm_profile=args.confirm_profile,
            )
            state = "released" if released else "already absent"
            print(
                "law-history refresh lock "
                f"{state}; rerun the same explicit refresh range to resume local receipts"
            )
            return 0
        if args.command == "recover-build":
            released = recover_law_history_build_lock(confirm_profile=args.confirm_profile)
            state = "released" if released else "already absent"
            print(
                "law-history baseline-build lock "
                f"{state}; run the explicit reset before another baseline build"
            )
            return 0
        if args.command == "recover-baseline":
            recover_law_history_baseline(
                source_batch=args.source_batch,
                confirm_profile=args.confirm_profile,
            )
            print("law-history verified baseline receipt batch was published")
            return 0
        if args.command == "rebind-frozen-inputs":
            rebind_law_history_frozen_inputs(confirm_profile=args.confirm_profile)
            print("law-history fixed-input receipt rebound after full local verification")
            return 0
        if args.command == "recover-published":
            released = recover_law_history_published_lock(confirm_profile=args.confirm_profile)
            state = "released" if released else "already absent"
            print(f"law-history published-state lock {state}; verified active state was retained")
            return 0
        if args.command == "reset":
            return _reset(args.confirm_profile)
        if args.command == "compose":
            return _compose(args.action)
        if args.command == "serve":
            with _cuda_opt_in_scope(bool(args.allow_cuda)):
                return _serve(args.host, args.port)
        if args.command == "verify-store":
            verify_law_history_store()
            print("law-history local source/model/full temporal store state verified")
            return 0
    except LawHistoryError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
