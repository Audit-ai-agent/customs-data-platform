"""Run a strictly test-only, loopback Evidence Runtime v2 conformance server.

This runner proves HTTP/authentication/schema/header interoperability only.
It never opens a Data store, never selects a production fixture through an
environment variable, and does not claim RAG quality or release readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src import repository  # noqa: E402
from src.deadline import RequestDeadline  # noqa: E402
from src.evidence import LawArticleMatch, RequirementLookup  # noqa: E402
from src.production import create_test_runtime_v2_application  # noqa: E402
from src.production_settings import ProductionSettings  # noqa: E402
from src.runtime_release import (  # noqa: E402
    EvidenceRuntimeReleaseIdentity,
    GenerationRequestLease,
    RuntimeReleaseError,
    runtime_release_sha256,
)

from tools.provider_build import (  # noqa: E402
    RUNTIME_PROVIDER_RELEASE,
    canonical_json_bytes,
    create_runtime_provider_build_document,
)

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
SUPPORTED_REVIEW_AS_OF = date(2026, 7, 10)
_LOCK_FIELDS = frozenset(
    {
        "contract",
        "schema_version",
        "provider_release",
        "provider_revision",
        "provider_build_sha256",
        "retrieval_schema_sha256",
        "readiness_schema_sha256",
        "provider_source_manifest_sha256",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,512}$")
_MAX_LOCK_BYTES = 16_384
_MAX_SCHEMA_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_TOKEN_FILE_BYTES = 1_024
_DERIVATION_DOMAIN = b"customs-data-platform:test-only-v2-conformance:v1\0"


class TestOnlyConformanceConfigurationError(RuntimeError):
    """The explicit conformance fixture is malformed or unsafe to start."""


@dataclass(frozen=True, slots=True)
class VerifiedProviderRuntimeLock:
    """The exact eight-field lock proven against one production context."""

    contract: str
    schema_version: str
    provider_release: str
    provider_revision: str
    provider_build_sha256: str
    retrieval_schema_sha256: str
    readiness_schema_sha256: str
    provider_source_manifest_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "contract": self.contract,
            "schema_version": self.schema_version,
            "provider_release": self.provider_release,
            "provider_revision": self.provider_revision,
            "provider_build_sha256": self.provider_build_sha256,
            "retrieval_schema_sha256": self.retrieval_schema_sha256,
            "readiness_schema_sha256": self.readiness_schema_sha256,
            "provider_source_manifest_sha256": self.provider_source_manifest_sha256,
        }


class FixedTestOnlyRuntimeLeaseLoader:
    """Immutable in-memory lease loader; no database or production selector."""

    def __init__(self, lease: GenerationRequestLease) -> None:
        self._lease = lease

    def acquire(
        self,
        deadline: RequestDeadline | None = None,
    ) -> GenerationRequestLease:
        if deadline is not None:
            deadline.assert_work_allowed()
        return self._lease

    def assert_unchanged(
        self,
        lease: GenerationRequestLease,
        deadline: RequestDeadline | None = None,
    ) -> None:
        if deadline is not None:
            deadline.assert_work_allowed()
        if lease != self._lease or lease.token != self._lease.token:
            raise RuntimeReleaseError("test-only conformance lease changed")


class ReadyTestOnlyV2ConformanceBackend:
    """One deterministic row for wire conformance, never for RAG evaluation."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        supported_review_as_of: date,
        no_requirement_hsk10: str | None = None,
    ) -> None:
        if no_requirement_hsk10 is not None and (
            len(no_requirement_hsk10) != 10 or not no_requirement_hsk10.isdigit()
        ):
            raise ValueError("no-requirement HSK must be exactly ten digits")
        self.snapshot_id = snapshot_id
        self.supported_review_as_of = supported_review_as_of
        self.no_requirement_hsk10 = no_requirement_hsk10

    def validate_ready(self) -> None:
        if not self.snapshot_id.startswith("test-only-conformance.no-rag-quality."):
            raise RuntimeError("test-only snapshot marker is absent")

    def current_snapshot_id(self) -> str:
        return self.snapshot_id

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        if snapshot_id != self.snapshot_id:
            raise ValueError("test-only conformance snapshot mismatch")
        return self.supported_review_as_of

    def resolve_hsk(self, code: str | None) -> repository.HskResolution:
        if code and len(code) == 10 and code.isdigit():
            return repository.HskResolution("exact", code, code, (code,))
        return repository.HskResolution(
            "missing" if not code else "invalid",
            code,
            None,
            (),
        )

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ) -> RequirementLookup:
        if snapshot_id != self.snapshot_id:
            raise ValueError("test-only conformance snapshot mismatch")
        if (
            self.no_requirement_hsk10 is not None
            and hsk10 == self.no_requirement_hsk10
            and trade_type == "import"
            and as_of == self.supported_review_as_of
        ):
            # A covered query with an empty requirement set is different from
            # an uncovered query.  LOCAL UAT uses this explicit positive
            # control to exercise a genuinely clean business path without
            # pretending that a synthetic issuer is production-trusted.
            return RequirementLookup(True, ())
        if hsk10 != "0303540000" or trade_type != "import" or as_of != self.supported_review_as_of:
            return RequirementLookup(False, ())
        return RequirementLookup(
            True,
            (
                {
                    "requirement_id": "test_only_v2_conformance_req_0303540000",
                    "requirement_version_hash": _domain_sha256(
                        b"requirement-version",
                        self.snapshot_id.encode("ascii"),
                    ),
                    "hsk10": hsk10,
                    "trade_type": trade_type,
                    "law_code": "89",
                    "law_name": "수입식품안전관리 특별법",
                    "agency_code": "005",
                    "agency_name": "식품의약품안전처",
                    "document_code": "TEST_ONLY_IMPORT_CONFIRMATION",
                    "document_name": "테스트 전용 수입신고확인증",
                    "valid_from": date(2020, 4, 6),
                    "valid_to": None,
                    "source_record_id": "test_only_v2_conformance_src_0303540000",
                    "source_uri": (
                        "https://example.invalid/test-only-conformance"
                        "?hsk10=0303540000&trade_type=import"
                    ),
                },
            ),
        )

    def search_law_articles(
        self,
        query: str,
        top_k: int,
        as_of: date,
    ) -> tuple[LawArticleMatch, ...]:
        if (
            self.no_requirement_hsk10 is not None
            and f"HSK {self.no_requirement_hsk10}" in query
            and top_k >= 1
            and as_of == self.supported_review_as_of
        ):
            # LOCAL USER UAT must distinguish "covered and no requirement"
            # from "no citable Evidence".  This immutable synthetic HSK
            # support is deliberately non-finding and cannot prove legal or
            # Production coverage; it only binds the positive-control result
            # to one citable, source-versioned diagnostic record.
            return (
                LawArticleMatch(
                    row={
                        "article_pk": 7318151000,
                        "law_id": "test-only-local-uat",
                        "law_name": "LOCAL UAT 합성 HSK 근거",
                        "hierarchy": "진단",
                        "article_no": 100,
                        "paragraph_no": None,
                        "item_no": None,
                        "title": "무요건 Positive Control",
                        "content": (
                            "이 합성 기록은 LOCAL USER UAT의 계약 경로만 검증하며 "
                            "실제 법령 또는 무요건 법률 판단이 아니다."
                        ),
                        "content_hash": _domain_sha256(
                            b"local-user-uat-law-support",
                            self.snapshot_id.encode("ascii"),
                        ),
                        "version_mst": "local-user-uat-v1",
                        "valid_from": self.supported_review_as_of,
                        "valid_to": None,
                    },
                    retrieval_methods=("bm25",),
                    score=1.0,
                    raw_scores={"bm25": 1.0},
                ),
            )
        return ()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TestOnlyConformanceConfigurationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> object:
    raise TestOnlyConformanceConfigurationError(f"non-standard JSON constant: {value}")


def _read_regular_file(path: Path, *, label: str, maximum_bytes: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise TestOnlyConformanceConfigurationError(
                f"{label} must be a regular, non-symlink file"
            )
        size = path.stat().st_size
        if size < 1 or size > maximum_bytes:
            raise TestOnlyConformanceConfigurationError(f"{label} has an invalid size")
        return path.read_bytes()
    except TestOnlyConformanceConfigurationError:
        raise
    except OSError as exc:
        raise TestOnlyConformanceConfigurationError(f"{label} is unavailable") from exc


def _strict_json_object(
    raw: bytes,
    *,
    label: str,
) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonstandard_constant,
        )
    except TestOnlyConformanceConfigurationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestOnlyConformanceConfigurationError(f"{label} must be strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TestOnlyConformanceConfigurationError(f"{label} must be a JSON object")
    return value


def _context_file(
    context_root: Path,
    relative_path: str,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    root = context_root.resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise TestOnlyConformanceConfigurationError(
            f"{label} escapes the production context"
        ) from exc
    return _read_regular_file(candidate, label=label, maximum_bytes=maximum_bytes)


def load_verified_provider_runtime_lock(
    production_context: Path,
) -> VerifiedProviderRuntimeLock:
    """Verify the lock, context schemas, source manifest, and provider build."""

    root = production_context.resolve()
    if not root.is_dir():
        raise TestOnlyConformanceConfigurationError(
            "production context must be an existing directory"
        )
    lock_raw = _context_file(
        root,
        "provider-runtime-lock.json",
        label="provider runtime lock",
        maximum_bytes=_MAX_LOCK_BYTES,
    )
    lock_payload = _strict_json_object(lock_raw, label="provider runtime lock")
    if set(lock_payload) != _LOCK_FIELDS:
        raise TestOnlyConformanceConfigurationError(
            "provider runtime lock fields are not the exact canonical eight"
        )
    if (
        lock_payload.get("contract") != "evidence-runtime-release-provider-lock"
        or lock_payload.get("schema_version") != "1.0"
        or lock_payload.get("provider_release") != RUNTIME_PROVIDER_RELEASE
    ):
        raise TestOnlyConformanceConfigurationError(
            "provider runtime lock contract, schema, or release is invalid"
        )
    revision = lock_payload.get("provider_revision")
    if not isinstance(revision, str) or _REVISION_PATTERN.fullmatch(revision) is None:
        raise TestOnlyConformanceConfigurationError("provider runtime lock revision is invalid")
    for field_name in _LOCK_FIELDS - {
        "contract",
        "schema_version",
        "provider_release",
        "provider_revision",
    }:
        digest = lock_payload.get(field_name)
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise TestOnlyConformanceConfigurationError(
                f"provider runtime lock {field_name} is invalid"
            )

    retrieval_schema = _context_file(
        root,
        "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        label="retrieval schema",
        maximum_bytes=_MAX_SCHEMA_BYTES,
    )
    readiness_schema = _context_file(
        root,
        "contracts/evidence/v2/readiness_v2.schema.json",
        label="readiness schema",
        maximum_bytes=_MAX_SCHEMA_BYTES,
    )
    source_manifest_raw = _context_file(
        root,
        "source-manifest.json",
        label="source manifest",
        maximum_bytes=_MAX_SOURCE_MANIFEST_BYTES,
    )
    observed = {
        "retrieval_schema_sha256": hashlib.sha256(retrieval_schema).hexdigest(),
        "readiness_schema_sha256": hashlib.sha256(readiness_schema).hexdigest(),
        "provider_source_manifest_sha256": hashlib.sha256(source_manifest_raw).hexdigest(),
    }
    for field_name, digest in observed.items():
        if lock_payload[field_name] != digest:
            raise TestOnlyConformanceConfigurationError(
                f"provider runtime lock {field_name} does not match context bytes"
            )

    source_manifest = _strict_json_object(source_manifest_raw, label="source manifest")
    if source_manifest.get("source_commit") != revision:
        raise TestOnlyConformanceConfigurationError(
            "source manifest revision does not match provider runtime lock"
        )
    expected_build = create_runtime_provider_build_document(
        provider_revision=revision,
        retrieval_schema=retrieval_schema,
        source_manifest=source_manifest_raw,
    )
    if lock_payload["provider_build_sha256"] != expected_build["provider_build_sha256"]:
        raise TestOnlyConformanceConfigurationError(
            "provider build digest does not match production context"
        )
    return VerifiedProviderRuntimeLock(**{name: str(lock_payload[name]) for name in _LOCK_FIELDS})


def _domain_sha256(label: bytes, payload: bytes) -> str:
    return hashlib.sha256(_DERIVATION_DOMAIN + label + b"\0" + payload).hexdigest()


def derive_test_only_runtime_lease(
    provider_lock: VerifiedProviderRuntimeLock,
    *,
    diagnostic_profile: str = "wire-conformance",
) -> GenerationRequestLease:
    """Derive a stable, unmistakably non-release Evidence runtime identity."""

    if (
        not diagnostic_profile
        or len(diagnostic_profile) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in diagnostic_profile
        )
    ):
        raise TestOnlyConformanceConfigurationError("diagnostic profile identifier is invalid")
    lock_bytes = (
        canonical_json_bytes(provider_lock.as_dict()) + b"\0" + diagnostic_profile.encode("ascii")
    )
    snapshot_suffix = _domain_sha256(b"snapshot", lock_bytes)[:24]
    payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": provider_lock.provider_release,
        "provider_revision": provider_lock.provider_revision,
        "provider_build_sha256": provider_lock.provider_build_sha256,
        "retrieval_schema_sha256": provider_lock.retrieval_schema_sha256,
        "provider_source_manifest_sha256": provider_lock.provider_source_manifest_sha256,
        "knowledge_generation_id": _domain_sha256(b"generation", lock_bytes),
        "knowledge_generation_attestation_sha256": _domain_sha256(
            b"generation-attestation",
            lock_bytes,
        ),
        "knowledge_source_manifest_sha256": _domain_sha256(
            b"test-only-knowledge-source",
            lock_bytes,
        ),
        "model_manifest_sha256": _domain_sha256(b"no-production-model", lock_bytes),
        "index_config_sha256": _domain_sha256(b"no-production-index", lock_bytes),
        "evaluation_report_sha256": _domain_sha256(
            b"not-a-rag-quality-evaluation",
            lock_bytes,
        ),
        "seal_attestation_sha256": _domain_sha256(b"test-only-seal", lock_bytes),
        "knowledge_snapshot_id": (f"test-only-conformance.no-rag-quality.{snapshot_suffix}"),
        "supported_review_as_of": SUPPORTED_REVIEW_AS_OF.isoformat(),
    }
    payload["runtime_release_sha256"] = runtime_release_sha256(payload)
    identity = EvidenceRuntimeReleaseIdentity.model_validate(payload)
    return GenerationRequestLease(
        identity=identity,
        generation_epoch=1,
        generation_changed_at="2026-07-10T00:00:00.000000Z",
    )


def load_bearer_token_file(path: Path) -> str:
    """Read one strict bearer token without consulting argv values or env."""

    if not path.is_absolute():
        raise TestOnlyConformanceConfigurationError("service token file path must be absolute")
    raw = _read_regular_file(
        path,
        label="service token file",
        maximum_bytes=_MAX_TOKEN_FILE_BYTES,
    )
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TestOnlyConformanceConfigurationError(
            "service token file must contain ASCII"
        ) from exc
    if text.endswith("\r\n"):
        token = text[:-2]
    elif text.endswith("\n"):
        token = text[:-1]
    else:
        token = text
    if "\r" in token or "\n" in token or not _TOKEN_PATTERN.fullmatch(token):
        raise TestOnlyConformanceConfigurationError(
            "service token file must contain one 32-512 character bearer-safe token"
        )
    return token


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def write_identity_json(
    identity: EvidenceRuntimeReleaseIdentity,
    *,
    output: Path,
    production_context: Path,
) -> None:
    """Atomically write only the public runtime identity, never credentials."""

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
    if destination.exists():
        existing = _read_regular_file(
            destination,
            label="identity output",
            maximum_bytes=_MAX_LOCK_BYTES,
        )
        if existing == serialized:
            return
        raise TestOnlyConformanceConfigurationError(
            "identity output already exists with different bytes"
        )
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


def create_test_only_v2_conformance_application(
    *,
    production_context: Path,
    service_token_file: Path,
    identity_output: Path,
    host: str,
    port: int,
):
    """Build the explicit fixture application and write its public identity."""

    if host not in LOOPBACK_HOSTS:
        raise TestOnlyConformanceConfigurationError("host must be loopback-only")
    if not 1 <= port <= 65535:
        raise TestOnlyConformanceConfigurationError("port must be from 1 to 65535")
    provider_lock = load_verified_provider_runtime_lock(production_context)
    token = load_bearer_token_file(service_token_file)
    lease = derive_test_only_runtime_lease(provider_lock)
    write_identity_json(
        lease.identity,
        output=identity_output,
        production_context=production_context,
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
    loader = FixedTestOnlyRuntimeLeaseLoader(lease)
    backend = ReadyTestOnlyV2ConformanceBackend(
        snapshot_id=lease.identity.knowledge_snapshot_id,
        supported_review_as_of=lease.identity.supported_review_as_of,
    )

    def backend_factory(
        bound_lease: GenerationRequestLease,
        _deadline: RequestDeadline,
    ) -> ReadyTestOnlyV2ConformanceBackend:
        if bound_lease != lease:
            raise RuntimeReleaseError("test-only conformance lease mismatch")
        return backend

    application = create_test_runtime_v2_application(
        service_settings=settings,
        runtime_release_loader=loader,
        backend_factory=backend_factory,
    )
    application.state.test_only_conformance_identity_output = str(identity_output.resolve())
    application.state.rag_quality_evaluated = False
    application.state.release_ready = False
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
        help="mandatory acknowledgement that this fixture is not production or RAG QA",
    )
    parser.add_argument("--production-context", type=Path, required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--identity-output", type=Path, required=True)
    parser.add_argument("--host", choices=LOOPBACK_HOSTS, required=True)
    parser.add_argument("--port", type=_port, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    application = create_test_only_v2_conformance_application(
        production_context=args.production_context,
        service_token_file=args.service_token_file,
        identity_output=args.identity_output,
        host=args.host,
        port=args.port,
    )
    identity = application.state.runtime_release_loader.acquire().identity
    print(
        json.dumps(
            {
                "event": "test_only_evidence_v2_conformance_starting",
                "host": args.host,
                "port": args.port,
                "rag_quality_evaluated": False,
                "release_ready": False,
                "runtime_release_sha256": identity.runtime_release_sha256,
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
