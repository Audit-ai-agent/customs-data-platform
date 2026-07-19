"""Serve a fail-closed v2 readiness diagnostic for preserved team Data.

This tool is deliberately narrower than the production Evidence service. It
opens only PostgreSQL, forces every transaction read-only, and proves the
earliest production release blocker (incomplete law corpus or absent
generation tables) through the real runtime-v2 HTTP boundary. It never
migrates, seeds, promotes a generation, starts a model, or synthesizes a
release identity.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.db import postgres  # noqa: E402
from src.deadline import RequestDeadline  # noqa: E402
from src.generation_runtime import ActiveGenerationLoader  # noqa: E402
from src.ingest.laws import TARGET_LAWS  # noqa: E402
from src.production import create_test_runtime_v2_application  # noqa: E402
from src.production_settings import ProductionSettings  # noqa: E402
from src.provider_identity import load_runtime_provider_build_identity  # noqa: E402
from src.runtime_release import (  # noqa: E402
    GenerationRequestLease,
    PostgresGenerationMetadataLoader,
    RuntimeReleaseError,
    RuntimeReleaseLoader,
)

from tools.run_evidence_v2_conformance_server import (  # noqa: E402
    LOOPBACK_HOSTS,
    load_bearer_token_file,
    load_verified_provider_runtime_lock,
)

TARGET_LAW_IDS = tuple(sorted(str(item["law_id"]) for item in TARGET_LAWS))
EXPECTED_LAW_COUNT = len(TARGET_LAW_IDS)
GENERATION_TABLES = (
    "knowledge_generation_active_pointer",
    "knowledge_generation_articles",
    "knowledge_generation_dataset_members",
    "knowledge_generation_datasets",
    "knowledge_generation_events",
    "knowledge_generation_laws",
    "knowledge_generation_store_attestations",
    "knowledge_generations",
)


class PreservedDataDiagnosticError(RuntimeError):
    """The diagnostic cannot prove a safe, incomplete, read-only state."""


@dataclass(frozen=True, slots=True)
class PreservedDataAudit:
    provider_revision: str
    target_law_count: int
    total_law_count: int
    article_count: int
    generation_tables_present: tuple[str, ...]

    @property
    def generation_tables_missing(self) -> tuple[str, ...]:
        return tuple(
            name for name in GENERATION_TABLES if name not in self.generation_tables_present
        )

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "preserved-data-readonly-diagnostic/v1",
            "test_only": True,
            "release_ready": False,
            "rag_quality_evaluated": False,
            "access_mode": "postgres_transaction_read_only",
            "database_write_attempted": False,
            "expected_law_count": EXPECTED_LAW_COUNT,
            "target_law_count": self.target_law_count,
            "total_law_count": self.total_law_count,
            "article_count": self.article_count,
            "generation_tables_expected": list(GENERATION_TABLES),
            "generation_tables_present": list(self.generation_tables_present),
            "generation_tables_missing": list(self.generation_tables_missing),
            "provider_revision": self.provider_revision,
            "status": "blocked_incomplete_data",
        }


ConnectionFactory = Callable[[RequestDeadline | None], Any]


@contextmanager
def readonly_postgres_connection(
    deadline: RequestDeadline | None = None,
) -> Iterator[Any]:
    """Use the hardened PostgreSQL boundary with a read-only transaction."""

    with postgres.connect(deadline=deadline) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SHOW transaction_read_only")
            row = cursor.fetchone()
        if row != ("on",):
            raise PreservedDataDiagnosticError("PostgreSQL did not enforce read-only mode")
        yield connection


def audit_preserved_postgres(
    *,
    provider_revision: str,
    connection_factory: ConnectionFactory = readonly_postgres_connection,
) -> PreservedDataAudit:
    """Read only the minimum corpus and generation metadata needed to block release."""

    deadline = RequestDeadline.start(
        started_at=time.monotonic(),
        request_timeout_seconds=10,
        store_timeout_seconds=5,
        model_timeout_seconds=1,
        completion_reserve_seconds=0.5,
    )
    try:
        with connection_factory(deadline) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema='public'
                      AND table_name = ANY(%s)
                    ORDER BY table_name
                    """,
                    (list(GENERATION_TABLES),),
                )
                present = tuple(str(row[0]) for row in cursor.fetchall())
                cursor.execute(
                    """
                    SELECT count(DISTINCT law_id)
                    FROM laws
                    WHERE law_id = ANY(%s)
                    """,
                    (list(TARGET_LAW_IDS),),
                )
                target_law_row = cursor.fetchone()
                cursor.execute("SELECT count(DISTINCT law_id) FROM laws")
                total_law_row = cursor.fetchone()
                cursor.execute("SELECT count(*) FROM law_articles")
                article_row = cursor.fetchone()
    except PreservedDataDiagnosticError:
        raise
    except Exception as exc:
        raise PreservedDataDiagnosticError("read-only PostgreSQL audit failed") from exc

    if (
        target_law_row is None
        or total_law_row is None
        or article_row is None
        or not isinstance(target_law_row[0], int)
        or not isinstance(total_law_row[0], int)
        or not isinstance(article_row[0], int)
    ):
        raise PreservedDataDiagnosticError("read-only PostgreSQL audit returned invalid counts")
    audit = PreservedDataAudit(
        provider_revision=provider_revision,
        target_law_count=target_law_row[0],
        total_law_count=total_law_row[0],
        article_count=article_row[0],
        generation_tables_present=present,
    )
    if audit.target_law_count >= EXPECTED_LAW_COUNT and not audit.generation_tables_missing:
        raise PreservedDataDiagnosticError(
            "preserved Data is no longer an incomplete diagnostic target"
        )
    return audit


class _FailClosedDiagnosticRuntimeLoader:
    """Run the real release loader but never expose a ready lease from this tool."""

    def __init__(self, delegate: RuntimeReleaseLoader) -> None:
        self._delegate = delegate

    def acquire(
        self,
        deadline: RequestDeadline | None = None,
    ) -> GenerationRequestLease:
        try:
            self._delegate.acquire(deadline)
        except Exception as exc:
            raise RuntimeReleaseError("preserved runtime is not release ready") from exc
        raise RuntimeReleaseError("diagnostic runner never exposes a ready release")

    def assert_unchanged(
        self,
        lease: GenerationRequestLease,
        deadline: RequestDeadline | None = None,
    ) -> None:
        del lease, deadline
        raise RuntimeReleaseError("diagnostic runner never exposes a ready release")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def write_audit_json(
    audit: PreservedDataAudit,
    output: Path,
    *,
    forbidden_roots: tuple[Path, ...] = (ROOT,),
) -> None:
    """Exclusively create a non-secret audit outside every source checkout."""

    if not output.is_absolute():
        raise PreservedDataDiagnosticError("audit output path must be absolute")
    if output.is_symlink() or not output.parent.is_dir() or _has_symlink_component(output.parent):
        raise PreservedDataDiagnosticError(
            "audit output parent must be an existing non-symlink directory"
        )
    destination = output.parent.resolve() / output.name
    if any(_is_within(destination, root) for root in forbidden_roots):
        raise PreservedDataDiagnosticError(
            "audit output must be outside source and production context roots"
        )
    if destination.exists() or destination.is_symlink():
        raise PreservedDataDiagnosticError("audit output must be a new regular file")
    payload = (
        json.dumps(
            audit.to_wire(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            destination,
            flags,
            0o600,
        )
        created = True
        opened = os.fstat(descriptor)
        linked = os.stat(destination, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
            raise OSError("audit output identity changed")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(destination, 0o600)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise PreservedDataDiagnosticError("audit output could not be created") from exc


def create_preserved_data_diagnostic_application(
    *,
    production_context: Path,
    service_token_file: Path,
    audit_output: Path,
    host: str,
    port: int,
    connection_factory: ConnectionFactory = readonly_postgres_connection,
    provider_lock_loader: Callable[[Path], Any] = load_verified_provider_runtime_lock,
    provider_identity_loader: Callable[[Path], Any] = (
        lambda root: load_runtime_provider_build_identity(bundle_root=root)
    ),
):
    if host not in LOOPBACK_HOSTS:
        raise PreservedDataDiagnosticError("host must be loopback-only")
    if not 1 <= port <= 65_535:
        raise PreservedDataDiagnosticError("port must be from 1 to 65535")
    context = production_context.resolve()
    verified_lock = provider_lock_loader(context)
    token = load_bearer_token_file(service_token_file)
    audit = audit_preserved_postgres(
        provider_revision=str(verified_lock.provider_revision),
        connection_factory=connection_factory,
    )
    write_audit_json(
        audit,
        audit_output,
        forbidden_roots=(ROOT, context),
    )

    active_loader = ActiveGenerationLoader(
        connection_factory=connection_factory,
        external_stores=(),
    )
    delegate = RuntimeReleaseLoader(
        provider_loader=lambda: provider_identity_loader(context),
        active_generation_loader=active_loader,
        metadata_loader=PostgresGenerationMetadataLoader(
            connection_factory=connection_factory,
        ),
    )
    runtime_loader = _FailClosedDiagnosticRuntimeLoader(delegate)
    settings = ProductionSettings(
        _env_file=None,
        service_token=SecretStr(token),
        service_token_file=None,
        transport_mode="loopback_dev",
        trusted_proxy_cidrs="",
        allow_insecure_loopback_dev=True,
        bind_host=host,
        port=port,
        max_concurrency=2,
        request_deadline_seconds=10,
        store_operation_timeout_seconds=5,
        store_termination_timeout_seconds=0.25,
        model_inference_timeout_seconds=1,
        model_termination_timeout_seconds=0.25,
        deadline_completion_reserve_seconds=0.5,
        shutdown_grace_seconds=2,
        enable_openapi=False,
        log_level="warning",
    )

    def forbidden_backend(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeReleaseError("diagnostic backend must never be constructed")

    application = create_test_runtime_v2_application(
        service_settings=settings,
        runtime_release_loader=runtime_loader,
        backend_factory=forbidden_backend,
    )
    application.state.test_only_runtime_conformance = False
    application.state.test_only_preserved_readonly_diagnostic = True
    application.state.preserved_data_audit = audit.to_wire()
    application.state.rag_quality_evaluated = False
    application.state.release_ready = False
    return application


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--test-only-preserved-readonly-diagnostic",
        action="store_true",
        required=True,
    )
    parser.add_argument("--production-context", type=Path, required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--host", choices=LOOPBACK_HOSTS, required=True)
    parser.add_argument("--port", type=_port, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        application = create_preserved_data_diagnostic_application(
            production_context=args.production_context,
            service_token_file=args.service_token_file,
            audit_output=args.audit_output,
            host=args.host,
            port=args.port,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "preserved_data_diagnostic_configuration_failed",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78
    audit = application.state.preserved_data_audit
    print(
        json.dumps(
            {
                "event": "test_only_preserved_data_v2_diagnostic_starting",
                "host": args.host,
                "port": args.port,
                "target_law_count": audit["target_law_count"],
                "generation_tables_present": len(audit["generation_tables_present"]),
                "release_ready": False,
            },
            sort_keys=True,
        )
    )

    import uvicorn

    try:
        uvicorn.run(
            application,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "preserved_data_diagnostic_runtime_failed",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
