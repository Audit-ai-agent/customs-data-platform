"""Run a strictly test-only, loopback Evidence Runtime V3 conformance server.

This process proves real HTTP/auth/schema/17-header interoperability only.  It
never opens PostgreSQL, Qdrant, or Neo4j; never reads a production policy or
reviewer trust store; and never claims legal coverage, RAG quality, domain
approval, release readiness, or automatic-completion eligibility.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.deadline import RequestDeadline  # noqa: E402
from src.production import create_test_runtime_v3_application  # noqa: E402
from src.production_settings import ProductionSettings  # noqa: E402
from src.runtime_release import GenerationRequestLease, RuntimeReleaseError  # noqa: E402
from src.runtime_release_v3 import (  # noqa: E402
    EvidenceRuntimeReleaseIdentityV3,
    RuntimeV3LeaseLoader,
    load_runtime_v3_contract_hashes,
)

from tools.run_evidence_v2_conformance_server import (  # noqa: E402
    FixedTestOnlyRuntimeLeaseLoader,
    ReadyTestOnlyV2ConformanceBackend,
    TestOnlyConformanceConfigurationError,
    derive_test_only_runtime_lease,
    load_bearer_token_file,
    load_verified_provider_runtime_lock,
)
from tools.test_only_v3_policy import build_test_only_policy_fixture  # noqa: E402

_MAX_IDENTITY_BYTES = 64 * 1024
_LITERAL_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
_PROFILE_WIRE_CONFORMANCE = "wire-conformance"
_PROFILE_LOCAL_USER_UAT = "local-user-uat"
_PROFILES = (_PROFILE_WIRE_CONFORMANCE, _PROFILE_LOCAL_USER_UAT)
_LOCAL_USER_UAT_NO_REQUIREMENT_HSK10 = "7318151000"
_UAT_READINESS_PATH = "/internal/rag/v3/health/uat-ready"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _write_identity_json(
    identity: EvidenceRuntimeReleaseIdentityV3,
    *,
    output: Path,
    production_context: Path,
) -> None:
    destination = output.resolve()
    if _is_within(destination, production_context):
        raise TestOnlyConformanceConfigurationError(
            "identity output must be outside the immutable production context"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise TestOnlyConformanceConfigurationError("identity output must not be a symbolic link")
    serialized = (
        json.dumps(
            identity.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > _MAX_IDENTITY_BYTES:
        raise TestOnlyConformanceConfigurationError("identity output exceeds safe size")
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != serialized:
            raise TestOnlyConformanceConfigurationError(
                "identity output already exists with different bytes"
            )
        return
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    except OSError as exc:
        raise TestOnlyConformanceConfigurationError("identity output could not be written") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def create_test_only_v3_conformance_application(
    *,
    production_context: Path,
    service_token_file: Path,
    identity_output: Path,
    host: str,
    port: int,
    profile: str = _PROFILE_WIRE_CONFORMANCE,
) -> Any:
    """Create the explicit synthetic V3 fixture with no production selectors."""

    if host not in _LITERAL_LOOPBACK_HOSTS:
        raise TestOnlyConformanceConfigurationError("host must be loopback-only")
    if not 1 <= port <= 65535:
        raise TestOnlyConformanceConfigurationError("port must be from 1 to 65535")
    if profile not in _PROFILES:
        raise TestOnlyConformanceConfigurationError("diagnostic profile is not supported")
    context = production_context.resolve()
    provider_lock = load_verified_provider_runtime_lock(context)
    token = load_bearer_token_file(service_token_file)
    base_lease = derive_test_only_runtime_lease(
        provider_lock,
        diagnostic_profile=profile,
    )
    policy_fixture = build_test_only_policy_fixture(
        knowledge_source_manifest_sha256=(base_lease.identity.knowledge_source_manifest_sha256),
        knowledge_snapshot_id=base_lease.identity.knowledge_snapshot_id,
    )
    base_loader = FixedTestOnlyRuntimeLeaseLoader(base_lease)
    runtime_loader = RuntimeV3LeaseLoader(
        base_loader=base_loader,
        policy_loader=lambda: policy_fixture.verified,
        contract_hash_loader=lambda: load_runtime_v3_contract_hashes(context),
    )
    runtime_lease = runtime_loader.acquire()
    if len(runtime_lease.identity.binding_headers()) != 17:
        raise TestOnlyConformanceConfigurationError(
            "test-only V3 identity does not expose the expected 17 bindings"
        )
    _write_identity_json(
        runtime_lease.identity,
        output=identity_output,
        production_context=context,
    )
    settings = ProductionSettings(
        _env_file=None,
        service_token=SecretStr(token),
        service_token_file=None,
        transport_mode="loopback_dev",
        trusted_proxy_cidrs="",
        allow_insecure_loopback_dev=True,
        bind_host=host,
        port=port,
        max_concurrency=8,
        request_deadline_seconds=10,
        store_operation_timeout_seconds=1,
        store_termination_timeout_seconds=0.25,
        model_inference_timeout_seconds=1,
        model_termination_timeout_seconds=0.25,
        deadline_completion_reserve_seconds=0.25,
        shutdown_grace_seconds=2,
        enable_openapi=False,
        log_level="warning",
    )
    backend = ReadyTestOnlyV2ConformanceBackend(
        snapshot_id=base_lease.identity.knowledge_snapshot_id,
        supported_review_as_of=base_lease.identity.supported_review_as_of,
        no_requirement_hsk10=(
            _LOCAL_USER_UAT_NO_REQUIREMENT_HSK10 if profile == _PROFILE_LOCAL_USER_UAT else None
        ),
    )

    def backend_factory(
        bound_lease: GenerationRequestLease,
        _deadline: RequestDeadline,
    ) -> ReadyTestOnlyV2ConformanceBackend:
        if bound_lease != base_lease:
            raise RuntimeReleaseError("test-only V3 conformance lease mismatch")
        return backend

    application = create_test_runtime_v3_application(
        service_settings=settings,
        runtime_release_loader=runtime_loader,
        backend_factory=backend_factory,
    )
    application.state.test_only_conformance_identity_output = str(identity_output.resolve())
    application.state.rag_quality_evaluated = False
    application.state.domain_reviewed = False
    application.state.release_ready = False
    application.state.automatic_completion_eligible = False
    application.state.diagnostic_profile = profile
    application.state.no_requirement_positive_control_hsk10 = (
        _LOCAL_USER_UAT_NO_REQUIREMENT_HSK10 if profile == _PROFILE_LOCAL_USER_UAT else None
    )

    if profile == _PROFILE_LOCAL_USER_UAT:

        @application.get(_UAT_READINESS_PATH, include_in_schema=False)
        async def local_user_uat_readiness() -> dict[str, Any]:
            return {
                "status": "ready",
                "mode": "LOCAL_USER_UAT_DIAGNOSTIC",
                "diagnostic_profile": profile,
                "claude_dependency": "owned_by_agent",
                "data_source": "SYNTHETIC_V3_NO_RAG_QUALITY",
                "no_requirement_positive_control_hsk10": (_LOCAL_USER_UAT_NO_REQUIREMENT_HSK10),
                "release_ready": False,
                "rag_quality_evaluated": False,
                "automatic_completion_eligible": False,
                "evidence_runtime_release": runtime_lease.identity.model_dump(mode="json"),
            }

    return application


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--test-only-conformance",
        action="store_true",
        required=True,
        help="mandatory acknowledgement that this is not production, legal, or RAG QA",
    )
    parser.add_argument("--production-context", type=Path, required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--identity-output", type=Path, required=True)
    parser.add_argument("--host", choices=_LITERAL_LOOPBACK_HOSTS, required=True)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument(
        "--profile",
        choices=_PROFILES,
        default=_PROFILE_WIRE_CONFORMANCE,
        help="explicit non-release fixture profile",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = create_test_only_v3_conformance_application(
        production_context=args.production_context,
        service_token_file=args.service_token_file,
        identity_output=args.identity_output,
        host=args.host,
        port=args.port,
        profile=args.profile,
    )
    identity = application.state.runtime_v3_release_loader.acquire().identity
    print(
        json.dumps(
            {
                "automatic_completion_eligible": False,
                "domain_reviewed": False,
                "event": "test_only_evidence_v3_conformance_starting",
                "host": args.host,
                "port": args.port,
                "diagnostic_profile": args.profile,
                "rag_quality_evaluated": False,
                "release_ready": False,
                "runtime_release_v3_sha256": identity.runtime_release_v3_sha256,
            },
            sort_keys=True,
        )
    )

    import uvicorn

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
