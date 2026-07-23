"""Run the preserved real three-law stores behind Evidence Runtime V3.

This is a loopback-only, read-only diagnostic bridge.  It reuses the exact
PostgreSQL/Qdrant/Neo4j observation and retrieval backend from the reviewed V2
three-law diagnostic, then binds that observed state to the V3 wire identity.

It is deliberately not a Production selector:

* exactly 3 of the canonical 58 laws and exactly 1,007 current article leaves
  must be present in every store;
* PostgreSQL transactions are forced read-only and no generation is promoted;
* RAG quality, legal coverage, domain approval, and automatic completion stay
  false;
* real CCCT requirement codes have no approved diagnostic policy and therefore
  resolve to ``human_only``/``partial`` in V3;
* the service may bind only to a literal loopback address.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Request
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.deadline import RequestDeadline  # noqa: E402
from src.model_runtime import DenseModelSupervisor  # noqa: E402
from src.neo4j_runtime import Neo4jQueryExecutor  # noqa: E402
from src.production import create_test_runtime_v3_application  # noqa: E402
from src.production_settings import ProductionSettings  # noqa: E402
from src.runtime_api_v3 import runtime_v3_readiness  # noqa: E402
from src.runtime_release import GenerationRequestLease, RuntimeReleaseError  # noqa: E402
from src.runtime_release_v3 import (  # noqa: E402
    EvidenceRuntimeReleaseIdentityV3,
    RuntimeV3LeaseLoader,
    load_runtime_v3_contract_hashes,
)
from tools import run_three_law_v2_diagnostic as v2  # noqa: E402
from tools.run_evidence_v2_conformance_server import (  # noqa: E402
    VerifiedProviderRuntimeLock,
    load_bearer_token_file,
    load_verified_provider_runtime_lock,
)
from tools.run_evidence_v3_conformance_server import (  # noqa: E402
    write_test_only_v3_identity_json,
)
from tools.test_only_v3_policy import build_test_only_policy_fixture  # noqa: E402

DIAGNOSTIC_PROFILE = "preserved-3law-v3-diagnostic"
DIAGNOSTIC_SCOPE = v2.DIAGNOSTIC_SCOPE
LITERAL_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
EXPECTED_PRODUCTION_LAW_COUNT = v2.EXPECTED_PRODUCTION_LAW_COUNT
EXPECTED_DIAGNOSTIC_LAW_COUNT = v2.EXPECTED_DIAGNOSTIC_LAW_COUNT
EXPECTED_DIAGNOSTIC_ARTICLE_COUNT = 1_007
DIAGNOSTIC_READINESS_PATH = "/internal/rag/v3/health/diagnostic-ready"
_RESERVED_POLICY_DOCUMENT_CODE = "TEST_ONLY_PRESERVED_3LAW_NEVER_MATCH"
_MANIFEST_SCHEMA_VERSION = "three-law-evidence-v3-diagnostic/v1"


class ThreeLawV3DiagnosticError(v2.ThreeLawDiagnosticError):
    """The V3 bridge cannot prove its narrow diagnostic boundary."""


def _validate_exact_observation(
    observation: v2.ThreeLawObservation,
    *,
    expected_loaded_law_count: int,
) -> None:
    v2._validate_observation(  # noqa: SLF001 - reviewed tool boundary is intentionally reused
        observation,
        expected_loaded_law_count=expected_loaded_law_count,
    )
    if (
        observation.article_count != EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
        or observation.qdrant_point_count != EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
        or observation.neo4j_article_count != EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
    ):
        raise ThreeLawV3DiagnosticError(
            "preserved V3 diagnostic requires exactly 1,007 cross-store articles"
        )


def attest_executing_v3_repository(
    provider_lock: VerifiedProviderRuntimeLock,
    *,
    repository: Path = ROOT,
    deadline: RequestDeadline | None = None,
) -> None:
    """Bind both the reviewed V2 backend and this V3 bridge to one clean SHA."""

    v2.attest_executing_repository(
        provider_lock,
        repository=repository,
        deadline=deadline,
    )
    relative_runner = Path(__file__).resolve().relative_to(repository.resolve()).as_posix()
    committed_runner = v2._git(  # noqa: SLF001 - same bounded Git attestor
        repository,
        "show",
        f"HEAD:{relative_runner}",
        deadline=deadline,
        text=False,
    )
    if committed_runner != Path(__file__).read_bytes():
        raise ThreeLawV3DiagnosticError(
            "executing V3 diagnostic bytes do not match the provider revision"
        )


@dataclass(frozen=True, slots=True)
class ThreeLawV3DiagnosticManifest:
    data_revision: str
    observation: v2.ThreeLawObservation
    identity: EvidenceRuntimeReleaseIdentityV3

    def to_wire(self) -> dict[str, object]:
        base = v2.ThreeLawDiagnosticManifest(
            data_revision=self.data_revision,
            observation=self.observation,
            identity=self.identity.base_runtime_release,
        ).to_wire()
        base.pop("schema_version")
        base.pop("evidence_runtime_release")
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "diagnostic_profile": DIAGNOSTIC_PROFILE,
            **base,
            "law_ids": list(self.observation.law_ids),
            "expected_diagnostic_article_count": EXPECTED_DIAGNOSTIC_ARTICLE_COUNT,
            "domain_reviewed": False,
            "automatic_completion_eligible": False,
            "requirement_policy_mode": "TEST_ONLY_ENVELOPE_REAL_CODES_HUMAN_ONLY",
            "retrieval_runtime": {
                "postgresql": "structured-ledger-read-only",
                "qdrant": ["bge-m3-dense", "bm25-sparse", "rrf", "lexical-rerank"],
                "neo4j": "one-hop-law-and-requirement-expansion",
            },
            "evidence_runtime_release_v3": self.identity.model_dump(mode="json"),
        }


def _validate_new_output_path(
    output: Path,
    *,
    context: Path,
    label: str,
) -> Path:
    if not output.is_absolute():
        raise ThreeLawV3DiagnosticError(f"{label} path must be absolute")
    if (
        output.exists()
        or output.is_symlink()
        or not output.parent.is_dir()
        or v2._has_symlink_component(output.parent)  # noqa: SLF001
    ):
        raise ThreeLawV3DiagnosticError(
            f"{label} must be a new file under an existing non-symlink directory"
        )
    destination = output.parent.resolve() / output.name
    if v2._is_within(destination, ROOT) or v2._is_within(  # noqa: SLF001
        destination,
        context,
    ):
        raise ThreeLawV3DiagnosticError(
            f"{label} must be outside source and provider context roots"
        )
    return destination


def create_three_law_v3_diagnostic_application(
    *,
    diagnostic_only_ack: bool,
    provider_context: Path,
    service_token_file: Path,
    identity_output: Path,
    diagnostic_manifest_output: Path,
    host: str,
    port: int,
    expected_loaded_law_count: int = EXPECTED_DIAGNOSTIC_LAW_COUNT,
    observation_loader: v2.ObservationLoader | None = None,
    provider_lock_loader: Callable[
        [Path],
        VerifiedProviderRuntimeLock,
    ] = load_verified_provider_runtime_lock,
    database_backend_factory: Callable[
        [v2.ThreeLawObservation, RequestDeadline],
        Any,
    ]
    | None = None,
    executing_repository_attestor: v2.ExecutingRepositoryAttestor | None = None,
    neo4j_query_executor: Neo4jQueryExecutor | None = None,
    model_supervisor: DenseModelSupervisor | None = None,
    install_readonly_boundary: bool = True,
) -> Any:
    """Create an actual-store V3 app without weakening any Production gate."""

    if not diagnostic_only_ack:
        raise ThreeLawV3DiagnosticError(
            "explicit preserved three-law V3 diagnostic acknowledgement is required"
        )
    if host not in LITERAL_LOOPBACK_HOSTS:
        raise ThreeLawV3DiagnosticError("host must be loopback-only")
    if not 1 <= port <= 65_535:
        raise ThreeLawV3DiagnosticError("port must be from 1 to 65535")
    if expected_loaded_law_count != EXPECTED_DIAGNOSTIC_LAW_COUNT:
        raise ThreeLawV3DiagnosticError("expected loaded law count must be exactly 3")

    context = provider_context.resolve()
    identity_destination = _validate_new_output_path(
        identity_output,
        context=context,
        label="diagnostic identity output",
    )
    manifest_destination = _validate_new_output_path(
        diagnostic_manifest_output,
        context=context,
        label="diagnostic manifest output",
    )
    if identity_destination == manifest_destination:
        raise ThreeLawV3DiagnosticError("identity and manifest outputs must be different files")

    provider_lock = provider_lock_loader(context)
    if (
        v2._REVISION_PATTERN.fullmatch(provider_lock.provider_revision) is None  # noqa: SLF001
        or provider_lock.provider_release != "evidence-api-v1.2.0"
    ):
        raise ThreeLawV3DiagnosticError("provider runtime lock is not a valid V3 base revision")
    selected_attestor = executing_repository_attestor or (
        lambda deadline=None: attest_executing_v3_repository(
            provider_lock,
            deadline=deadline,
        )
    )
    startup_deadline = v2._deadline()  # noqa: SLF001 - same request budget as V2
    selected_attestor(startup_deadline)
    token = load_bearer_token_file(service_token_file)

    readonly_boundary: v2._ReadOnlyPostgresBoundary | None = None  # noqa: SLF001
    created_neo4j_executor = False
    shared_neo4j_executor = neo4j_query_executor
    created_model_supervisor = False
    shared_model_supervisor = model_supervisor
    wrote_identity = False
    try:
        if install_readonly_boundary:
            readonly_boundary = v2._ReadOnlyPostgresBoundary()  # noqa: SLF001
        if observation_loader is None or database_backend_factory is None:
            if shared_neo4j_executor is None:
                shared_neo4j_executor = v2._new_neo4j_executor()  # noqa: SLF001
                created_neo4j_executor = True
        if observation_loader is None:
            if shared_neo4j_executor is None:
                raise ThreeLawV3DiagnosticError("bounded Neo4j observer is unavailable")

            def selected_observation_loader(
                deadline: RequestDeadline | None = None,
            ) -> v2.ThreeLawObservation:
                return v2.observe_three_law_data(
                    deadline,
                    neo4j_query_executor=shared_neo4j_executor,
                )

        else:
            selected_observation_loader = observation_loader

        selected_database_backend_factory: Callable[
            [v2.ThreeLawObservation, RequestDeadline],
            Any,
        ]
        if database_backend_factory is None:
            if shared_neo4j_executor is None:
                raise ThreeLawV3DiagnosticError("bounded Neo4j backend is unavailable")
            if shared_model_supervisor is None:
                shared_model_supervisor = DenseModelSupervisor(
                    startup_timeout_seconds=180,
                    termination_timeout_seconds=0.5,
                )
                created_model_supervisor = True

            def selected_database_backend_factory(
                observation: v2.ThreeLawObservation,
                request_deadline: RequestDeadline,
            ) -> v2.ThreeLawDiagnosticDatabaseBackend:
                if shared_neo4j_executor is None or shared_model_supervisor is None:
                    raise ThreeLawV3DiagnosticError(
                        "bounded diagnostic dependencies are unavailable"
                    )
                return v2.ThreeLawDiagnosticDatabaseBackend(
                    observation,
                    deadline=request_deadline,
                    neo4j_query_executor=shared_neo4j_executor,
                    dense_encoder=shared_model_supervisor.encode,
                )

        else:
            selected_database_backend_factory = database_backend_factory

        initial_observation = selected_observation_loader(startup_deadline)
        _validate_exact_observation(
            initial_observation,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        base_lease = v2.derive_three_law_runtime_lease(provider_lock, initial_observation)
        base_loader = v2._ObservedDiagnosticRuntimeLeaseLoader(  # noqa: SLF001
            base_lease,
            initial_observation=initial_observation,
            observation_loader=selected_observation_loader,
            executing_repository_attestor=selected_attestor,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        policy_fixture = build_test_only_policy_fixture(
            knowledge_source_manifest_sha256=(
                base_lease.identity.knowledge_source_manifest_sha256
            ),
            knowledge_snapshot_id=base_lease.identity.knowledge_snapshot_id,
            required_document_code=_RESERVED_POLICY_DOCUMENT_CODE,
        )
        runtime_loader = RuntimeV3LeaseLoader(
            base_loader=base_loader,
            policy_loader=lambda: policy_fixture.verified,
            contract_hash_loader=lambda: load_runtime_v3_contract_hashes(context),
        )
        runtime_lease = runtime_loader.acquire(startup_deadline)
        if len(runtime_lease.identity.binding_headers()) != 17:
            raise ThreeLawV3DiagnosticError(
                "preserved three-law V3 identity must expose exactly 17 bindings"
            )

        service_settings = ProductionSettings(
            _env_file=None,
            service_token=SecretStr(token),
            service_token_file=None,
            transport_mode="loopback_dev",
            trusted_proxy_cidrs="",
            allow_insecure_loopback_dev=True,
            bind_host=host,
            port=port,
            max_concurrency=1,
            request_deadline_seconds=75,
            store_operation_timeout_seconds=20,
            store_termination_timeout_seconds=0.5,
            model_inference_timeout_seconds=20,
            model_termination_timeout_seconds=0.5,
            deadline_completion_reserve_seconds=2,
            shutdown_grace_seconds=5,
            enable_openapi=False,
            log_level="warning",
        )

        def runtime_backend_factory(
            bound_lease: GenerationRequestLease,
            request_deadline: RequestDeadline,
        ) -> Any:
            if bound_lease != base_lease:
                raise RuntimeReleaseError("preserved three-law V3 lease mismatch")
            selected_attestor(request_deadline)
            current = selected_observation_loader(request_deadline)
            _validate_exact_observation(
                current,
                expected_loaded_law_count=expected_loaded_law_count,
            )
            if current != initial_observation:
                raise RuntimeReleaseError("preserved three-law Data observation changed")
            return selected_database_backend_factory(current, request_deadline)

        closeable_list: list[Any] = []
        if readonly_boundary is not None:
            closeable_list.append(readonly_boundary)
        if created_neo4j_executor and shared_neo4j_executor is not None:
            closeable_list.append(shared_neo4j_executor)
        application = create_test_runtime_v3_application(
            service_settings=service_settings,
            runtime_release_loader=runtime_loader,
            backend_factory=runtime_backend_factory,
            runtime_closeables=tuple(closeable_list),
            model_supervisor=(
                shared_model_supervisor if database_backend_factory is None else None
            ),
        )
        manifest = ThreeLawV3DiagnosticManifest(
            data_revision=provider_lock.provider_revision,
            observation=initial_observation,
            identity=runtime_lease.identity,
        )
        write_test_only_v3_identity_json(
            runtime_lease.identity,
            output=identity_destination,
            production_context=context,
        )
        wrote_identity = True
        v2.write_diagnostic_manifest(
            manifest,
            manifest_destination,
            forbidden_roots=(ROOT, context),
        )
    except Exception:
        if wrote_identity:
            identity_destination.unlink(missing_ok=True)
        if readonly_boundary is not None:
            readonly_boundary.close()
        if created_neo4j_executor and shared_neo4j_executor is not None:
            close_neo4j = getattr(shared_neo4j_executor, "close", None)
            if not callable(close_neo4j):
                raise ThreeLawV3DiagnosticError(
                    "owned Neo4j executor has no close operation"
                )
            close_neo4j()
        if created_model_supervisor and shared_model_supervisor is not None:
            shared_model_supervisor.close()
        raise

    application.state.test_only_runtime_conformance = False
    application.state.test_only_three_law_v3_diagnostic = True
    application.state.diagnostic_profile = DIAGNOSTIC_PROFILE
    application.state.three_law_v3_diagnostic_manifest = manifest.to_wire()
    application.state.rag_quality_evaluated = False
    application.state.domain_reviewed = False
    application.state.release_ready = False
    application.state.automatic_completion_eligible = False

    @application.get(DIAGNOSTIC_READINESS_PATH, include_in_schema=False)
    def diagnostic_readiness(request: Request) -> dict[str, object]:
        readiness = runtime_v3_readiness(request)
        return {
            "status": "ready",
            "mode": "PRESERVED_THREE_LAW_V3_DIAGNOSTIC",
            "diagnostic_profile": DIAGNOSTIC_PROFILE,
            "scope": DIAGNOSTIC_SCOPE,
            "data_source": "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY",
            "expected_production_law_count": EXPECTED_PRODUCTION_LAW_COUNT,
            "observed_law_count": initial_observation.total_law_count,
            "observed_article_count": initial_observation.article_count,
            "qdrant_point_count": initial_observation.qdrant_point_count,
            "neo4j_article_count": initial_observation.neo4j_article_count,
            "knowledge_snapshot_id": initial_observation.knowledge_snapshot_id,
            "release_ready": False,
            "rag_quality_evaluated": False,
            "domain_reviewed": False,
            "automatic_completion_eligible": False,
            "evidence_runtime_release": readiness.evidence_runtime_release.model_dump(
                mode="json"
            ),
        }

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
        "--diagnostic-only-three-law",
        action="store_true",
        required=True,
        help="mandatory acknowledgement that this is a non-release 3/58 diagnostic",
    )
    parser.add_argument("--provider-context", type=Path, required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--identity-output", type=Path, required=True)
    parser.add_argument("--diagnostic-manifest-output", type=Path, required=True)
    parser.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, required=True)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument(
        "--expected-loaded-law-count",
        type=int,
        choices=(EXPECTED_DIAGNOSTIC_LAW_COUNT,),
        default=EXPECTED_DIAGNOSTIC_LAW_COUNT,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        application = create_three_law_v3_diagnostic_application(
            diagnostic_only_ack=args.diagnostic_only_three_law,
            provider_context=args.provider_context,
            service_token_file=args.service_token_file,
            identity_output=args.identity_output,
            diagnostic_manifest_output=args.diagnostic_manifest_output,
            host=args.host,
            port=args.port,
            expected_loaded_law_count=args.expected_loaded_law_count,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "preserved_three_law_v3_configuration_failed",
                    "exception_type": type(exc).__name__,
                    "reason": "invalid_configuration",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78

    manifest = application.state.three_law_v3_diagnostic_manifest
    print(
        json.dumps(
            {
                "event": "preserved_three_law_v3_diagnostic_starting",
                "diagnostic_profile": DIAGNOSTIC_PROFILE,
                "scope": DIAGNOSTIC_SCOPE,
                "host": args.host,
                "port": args.port,
                "observed_law_count": manifest["observed_total_law_count"],
                "observed_article_count": manifest["observed_article_count"],
                "release_ready": False,
                "rag_quality_evaluated": False,
                "automatic_completion_eligible": False,
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
            date_header=False,
            workers=1,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "preserved_three_law_v3_runtime_failed",
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
