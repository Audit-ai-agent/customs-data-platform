"""Run an authenticated, loopback-only three-law Evidence v2 diagnostic.

This runner is deliberately not a Production release selector.  It observes
the preserved PostgreSQL/Qdrant/Neo4j stores, requires the exact three core
customs laws and byte/identifier-consistent projections, and derives an
unmistakably domain-separated non-release runtime identity.  Retrieval still
uses the real :class:`src.evidence.DatabaseEvidenceBackend`.

The Product 58-law readiness, generation, evaluation, seal, and promotion
logic are neither called nor modified by this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from pydantic import SecretStr

# The preserved dense/BM25 diagnostic was stable on this Windows workstation
# only with bounded native thread pools.  Apply the same process-local settings
# before importing any model/store module.  Offline mode also prevents an
# unnoticed model download from turning a local observation into a mutable run.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src import evidence as evidence_module  # noqa: E402
from src import repository as repository_module  # noqa: E402
from src.db import postgres  # noqa: E402
from src.db.neo4j import driver as make_neo4j_driver  # noqa: E402
from src.db.qdrant import COLLECTION, client as make_qdrant_client  # noqa: E402
from src.deadline import RequestDeadline  # noqa: E402
from src.evidence import DatabaseEvidenceBackend  # noqa: E402
from src.ingest.laws import TARGET_LAWS  # noqa: E402
from src.production import create_test_runtime_v2_application  # noqa: E402
from src.production_settings import ProductionSettings  # noqa: E402
from src.runtime_release import (  # noqa: E402
    EvidenceRuntimeReleaseIdentity,
    GenerationRequestLease,
    RuntimeReleaseError,
    runtime_release_sha256,
)

from tools.provider_build import canonical_json_bytes  # noqa: E402
from tools.run_evidence_v2_conformance_server import (  # noqa: E402
    LOOPBACK_HOSTS,
    FixedTestOnlyRuntimeLeaseLoader,
    VerifiedProviderRuntimeLock,
    load_bearer_token_file,
    load_verified_provider_runtime_lock,
)

EXPECTED_PRODUCTION_LAW_COUNT = len(TARGET_LAWS)
EXPECTED_DIAGNOSTIC_LAW_COUNT = 3
DIAGNOSTIC_LAW_IDS = ("001556", "002421", "006392")
DIAGNOSTIC_SCOPE = "DIAGNOSTIC_ONLY_3_OF_58"
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN = b"customs-data-platform:three-law-evidence-diagnostic:v1\0"
_MAX_MANIFEST_BYTES = 64 * 1024
_ORIGINAL_POSTGRES_CONNECT = postgres.connect

if not set(DIAGNOSTIC_LAW_IDS).issubset({str(item["law_id"]) for item in TARGET_LAWS}):
    raise RuntimeError("three-law diagnostic IDs are not in the canonical 58-law target set")


class ThreeLawDiagnosticError(RuntimeError):
    """The diagnostic cannot prove its narrow, non-release safety boundary."""


@dataclass(frozen=True, slots=True)
class ThreeLawObservation:
    """One exact read-only observation of the preserved three-store state."""

    law_ids: tuple[str, ...]
    total_law_count: int
    article_count: int
    qdrant_point_count: int
    neo4j_article_count: int
    knowledge_snapshot_id: str
    supported_review_as_of: date
    law_article_manifest_sha256: str
    postgres_qdrant_ids_sha256: str
    qdrant_point_ids_sha256: str
    postgres_graph_ids_sha256: str
    neo4j_article_ids_sha256: str

    @property
    def target_law_count(self) -> int:
        return len(self.law_ids)

    def identity_subject(self) -> dict[str, object]:
        return {
            "scope": DIAGNOSTIC_SCOPE,
            "law_ids": list(self.law_ids),
            "total_law_count": self.total_law_count,
            "article_count": self.article_count,
            "qdrant_point_count": self.qdrant_point_count,
            "neo4j_article_count": self.neo4j_article_count,
            "knowledge_snapshot_id": self.knowledge_snapshot_id,
            "supported_review_as_of": self.supported_review_as_of.isoformat(),
            "law_article_manifest_sha256": self.law_article_manifest_sha256,
            "postgres_qdrant_ids_sha256": self.postgres_qdrant_ids_sha256,
            "qdrant_point_ids_sha256": self.qdrant_point_ids_sha256,
            "postgres_graph_ids_sha256": self.postgres_graph_ids_sha256,
            "neo4j_article_ids_sha256": self.neo4j_article_ids_sha256,
            "release_ready": False,
            "rag_quality_evaluated": False,
        }


class ObservationLoader(Protocol):
    def __call__(self) -> ThreeLawObservation: ...


@contextmanager
def readonly_postgres_connection(
    deadline: RequestDeadline | None = None,
) -> Iterator[Any]:
    """Open the normal bounded connection but force its transaction read-only."""

    with _ORIGINAL_POSTGRES_CONNECT(deadline=deadline) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SHOW transaction_read_only")
            row = cursor.fetchone()
        if row != ("on",):
            raise ThreeLawDiagnosticError("PostgreSQL did not enforce read-only mode")
        yield connection


class _ReadOnlyPostgresBoundary:
    """Process-local patch for legacy backend imports, restored on shutdown."""

    def __init__(self) -> None:
        self._closed = False
        self._evidence_connect = evidence_module.connect
        self._repository_connect = repository_module.connect
        evidence_module.connect = readonly_postgres_connection
        repository_module.connect = readonly_postgres_connection

    def close(self) -> None:
        if self._closed:
            return
        evidence_module.connect = self._evidence_connect
        repository_module.connect = self._repository_connect
        self._closed = True


def _deadline() -> RequestDeadline:
    return RequestDeadline.start(
        started_at=time.monotonic(),
        request_timeout_seconds=30,
        store_timeout_seconds=10,
        model_timeout_seconds=5,
        completion_reserve_seconds=0.5,
    )


def _validate_observation(
    observation: ThreeLawObservation,
    *,
    expected_loaded_law_count: int,
) -> None:
    if expected_loaded_law_count != EXPECTED_DIAGNOSTIC_LAW_COUNT:
        raise ThreeLawDiagnosticError("this runner is restricted to the exact three-law corpus")
    if observation.law_ids != tuple(sorted(DIAGNOSTIC_LAW_IDS)):
        raise ThreeLawDiagnosticError("observed law IDs are not the exact three core customs laws")
    if (
        observation.target_law_count != expected_loaded_law_count
        or observation.total_law_count != expected_loaded_law_count
    ):
        raise ThreeLawDiagnosticError("observed law count is outside the three-law boundary")
    if observation.article_count <= 0:
        raise ThreeLawDiagnosticError("the three-law corpus has no indexable current articles")
    if observation.qdrant_point_count != observation.article_count:
        raise ThreeLawDiagnosticError("Qdrant point count does not match PostgreSQL")
    if observation.neo4j_article_count != observation.article_count:
        raise ThreeLawDiagnosticError("Neo4j article count does not match PostgreSQL")
    if not observation.knowledge_snapshot_id.strip():
        raise ThreeLawDiagnosticError("the current CCCT snapshot is missing")
    digests = (
        observation.law_article_manifest_sha256,
        observation.postgres_qdrant_ids_sha256,
        observation.qdrant_point_ids_sha256,
        observation.postgres_graph_ids_sha256,
        observation.neo4j_article_ids_sha256,
    )
    if any(_SHA256_PATTERN.fullmatch(value) is None for value in digests):
        raise ThreeLawDiagnosticError("three-law observation digest is invalid")
    if observation.postgres_qdrant_ids_sha256 != observation.qdrant_point_ids_sha256:
        raise ThreeLawDiagnosticError("Qdrant point identity digest does not match PostgreSQL")
    if observation.postgres_graph_ids_sha256 != observation.neo4j_article_ids_sha256:
        raise ThreeLawDiagnosticError("Neo4j article identity digest does not match PostgreSQL")


def _identifier_manifest_sha256(values: tuple[object, ...]) -> str:
    """Hash a type-stable, sorted identifier set for cross-store comparison."""

    normalized = sorted(str(value) for value in values)
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def observe_three_law_data(
    *,
    backend: DatabaseEvidenceBackend | None = None,
    connection_factory: Callable[
        [RequestDeadline | None],
        Any,
    ] = readonly_postgres_connection,
    qdrant_factory: Callable[[], Any] = make_qdrant_client,
    neo4j_driver_factory: Callable[[], Any] = make_neo4j_driver,
) -> ThreeLawObservation:
    """Attest the exact preserved law projections without changing any store."""

    selected_backend = backend or DatabaseEvidenceBackend()
    deadline = _deadline()
    try:
        with connection_factory(deadline) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT l.law_id, a.id, a.content, a.content_hash, a.version_mst,
                       EXISTS (
                         SELECT 1 FROM law_versions v
                         WHERE v.law_id=a.law_id AND v.mst=a.version_mst
                       ) AS has_law_version
                FROM laws l
                LEFT JOIN law_articles a
                  ON a.law_id=l.law_id AND a.is_current
                WHERE l.law_id = ANY(%s)
                ORDER BY l.law_id, a.id
                """,
                (list(DIAGNOSTIC_LAW_IDS),),
            )
            law_rows = tuple(cursor.fetchall())
            cursor.execute("SELECT count(DISTINCT law_id) FROM laws")
            total_row = cursor.fetchone()
            cursor.execute(
                """
                SELECT a.qdrant_point_id::text
                FROM law_articles a
                WHERE a.is_current
                  AND a.qdrant_point_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM law_articles child
                    WHERE child.parent_article_pk=a.id
                  )
                ORDER BY a.qdrant_point_id::text
                """
            )
            postgres_qdrant_ids = tuple(str(row[0]) for row in cursor.fetchall())
            cursor.execute(
                """
                SELECT id
                FROM law_articles
                WHERE is_current AND paragraph_no IS NULL
                ORDER BY id
                """
            )
            postgres_graph_ids = tuple(int(row[0]) for row in cursor.fetchall())
    except ThreeLawDiagnosticError:
        raise
    except Exception as exc:
        raise ThreeLawDiagnosticError("read-only PostgreSQL observation failed") from exc

    if total_row is None or not isinstance(total_row[0], int):
        raise ThreeLawDiagnosticError("PostgreSQL returned an invalid law count")
    loaded_ids = tuple(sorted({str(row[0]) for row in law_rows}))
    article_backed_ids = {str(row[0]) for row in law_rows if row[1] is not None}
    invalid_articles = 0
    law_article_manifest: list[dict[str, object]] = []
    for _law_id, article_id, content, content_hash, version_mst, has_version in law_rows:
        if article_id is None:
            continue
        observed_hash = (
            hashlib.sha256(content.encode("utf-8")).hexdigest()
            if isinstance(content, str) and content.strip()
            else None
        )
        if (
            observed_hash is None
            or observed_hash != content_hash
            or not isinstance(version_mst, str)
            or not version_mst.strip()
            or not has_version
        ):
            invalid_articles += 1
            continue
        law_article_manifest.append(
            {
                "law_id": str(_law_id),
                "article_id": int(article_id),
                "content_sha256": str(content_hash),
                "version_mst": version_mst,
            }
        )
    if (
        loaded_ids != tuple(sorted(DIAGNOSTIC_LAW_IDS))
        or article_backed_ids != set(DIAGNOSTIC_LAW_IDS)
        or invalid_articles
    ):
        raise ThreeLawDiagnosticError("PostgreSQL three-law provenance is incomplete")

    postgres_qdrant_set = set(postgres_qdrant_ids)
    if len(postgres_qdrant_set) != len(postgres_qdrant_ids):
        raise ThreeLawDiagnosticError("PostgreSQL contains duplicate Qdrant point IDs")
    try:
        qdrant = qdrant_factory()
        try:
            qdrant_count = int(qdrant.count(COLLECTION, exact=True).count)
            qdrant_ids = tuple(
                DatabaseEvidenceBackend._scroll_qdrant_point_ids(
                    qdrant,
                    COLLECTION,
                    qdrant_count,
                )
            )
        finally:
            close = getattr(qdrant, "close", None)
            if callable(close):
                close()
    except Exception as exc:
        raise ThreeLawDiagnosticError("read-only Qdrant observation failed") from exc
    if (
        len(set(qdrant_ids)) != len(qdrant_ids)
        or set(qdrant_ids) != postgres_qdrant_set
        or qdrant_count != len(postgres_qdrant_ids)
    ):
        raise ThreeLawDiagnosticError("Qdrant projection does not match PostgreSQL")

    try:
        driver = neo4j_driver_factory()
        try:
            with driver.session() as session:
                graph_rows = tuple(
                    session.run(
                        "MATCH (a:Article) RETURN a.article_pk AS article_pk ORDER BY a.article_pk"
                    )
                )
        finally:
            driver.close()
        neo4j_ids = tuple(
            int(row["article_pk"]) for row in graph_rows if row["article_pk"] is not None
        )
    except Exception as exc:
        raise ThreeLawDiagnosticError("read-only Neo4j observation failed") from exc
    if (
        len(neo4j_ids) != len(set(neo4j_ids))
        or set(neo4j_ids) != set(postgres_graph_ids)
        or len(neo4j_ids) != len(postgres_graph_ids)
    ):
        raise ThreeLawDiagnosticError("Neo4j projection does not match PostgreSQL")

    try:
        snapshot_id = selected_backend.current_snapshot_id()
        if snapshot_id is None:
            raise ThreeLawDiagnosticError("the current CCCT snapshot is missing")
        supported_review_as_of = selected_backend.snapshot_effective_date(snapshot_id)
    except ThreeLawDiagnosticError:
        raise
    except Exception as exc:
        raise ThreeLawDiagnosticError("knowledge snapshot observation failed") from exc
    if not isinstance(supported_review_as_of, date):
        raise ThreeLawDiagnosticError("knowledge snapshot effective date is invalid")

    law_article_manifest_sha256 = hashlib.sha256(
        canonical_json_bytes(law_article_manifest)
    ).hexdigest()
    postgres_qdrant_ids_sha256 = _identifier_manifest_sha256(postgres_qdrant_ids)
    qdrant_point_ids_sha256 = _identifier_manifest_sha256(qdrant_ids)
    postgres_graph_ids_sha256 = _identifier_manifest_sha256(postgres_graph_ids)
    neo4j_article_ids_sha256 = _identifier_manifest_sha256(neo4j_ids)
    observation = ThreeLawObservation(
        law_ids=loaded_ids,
        total_law_count=total_row[0],
        article_count=len(postgres_qdrant_ids),
        qdrant_point_count=qdrant_count,
        neo4j_article_count=len(neo4j_ids),
        knowledge_snapshot_id=snapshot_id,
        supported_review_as_of=supported_review_as_of,
        law_article_manifest_sha256=law_article_manifest_sha256,
        postgres_qdrant_ids_sha256=postgres_qdrant_ids_sha256,
        qdrant_point_ids_sha256=qdrant_point_ids_sha256,
        postgres_graph_ids_sha256=postgres_graph_ids_sha256,
        neo4j_article_ids_sha256=neo4j_article_ids_sha256,
    )
    _validate_observation(
        observation,
        expected_loaded_law_count=EXPECTED_DIAGNOSTIC_LAW_COUNT,
    )
    return observation


def _domain_sha256(label: bytes, subject: bytes) -> str:
    return hashlib.sha256(_DOMAIN + label + b"\0" + subject).hexdigest()


def derive_three_law_runtime_lease(
    provider_lock: VerifiedProviderRuntimeLock,
    observation: ThreeLawObservation,
) -> GenerationRequestLease:
    """Bind provider bytes to the observed stores without creating a release."""

    subject = canonical_json_bytes(
        {
            "provider": provider_lock.as_dict(),
            "observation": observation.identity_subject(),
        }
    )
    payload: dict[str, object] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": provider_lock.provider_release,
        "provider_revision": provider_lock.provider_revision,
        "provider_build_sha256": provider_lock.provider_build_sha256,
        "retrieval_schema_sha256": provider_lock.retrieval_schema_sha256,
        "provider_source_manifest_sha256": provider_lock.provider_source_manifest_sha256,
        "knowledge_generation_id": _domain_sha256(b"diagnostic-generation", subject),
        "knowledge_generation_attestation_sha256": _domain_sha256(
            b"observed-three-store-counts",
            subject,
        ),
        "knowledge_source_manifest_sha256": _domain_sha256(
            b"diagnostic-source-not-approved",
            subject,
        ),
        "model_manifest_sha256": _domain_sha256(b"diagnostic-model-runtime", subject),
        "index_config_sha256": _domain_sha256(b"observed-index-only", subject),
        "evaluation_report_sha256": _domain_sha256(
            b"rag-quality-not-evaluated",
            subject,
        ),
        "seal_attestation_sha256": _domain_sha256(b"diagnostic-not-sealed", subject),
        "knowledge_snapshot_id": observation.knowledge_snapshot_id,
        "supported_review_as_of": observation.supported_review_as_of.isoformat(),
    }
    payload["runtime_release_sha256"] = runtime_release_sha256(payload)
    try:
        identity = EvidenceRuntimeReleaseIdentity.model_validate(payload)
    except Exception as exc:
        raise ThreeLawDiagnosticError("derived diagnostic runtime identity is invalid") from exc
    return GenerationRequestLease(
        identity=identity,
        generation_epoch=0,
        generation_changed_at=(
            f"{observation.supported_review_as_of.isoformat()}T00:00:00.000000Z"
        ),
    )


class ThreeLawDiagnosticDatabaseBackend(DatabaseEvidenceBackend):
    """Real DB backend with only the three-law diagnostic checks overridden."""

    def __init__(self, observation: ThreeLawObservation) -> None:
        super().__init__()
        self._diagnostic_observation = observation

    def validate_ready(self) -> None:
        snapshot = self.current_snapshot_id()
        if snapshot != self._diagnostic_observation.knowledge_snapshot_id:
            raise ThreeLawDiagnosticError("knowledge snapshot changed")
        if self.snapshot_effective_date(snapshot) != (
            self._diagnostic_observation.supported_review_as_of
        ):
            raise ThreeLawDiagnosticError("knowledge snapshot effective date changed")

    def _verify_target_law_coverage(self) -> dict[str, int]:
        count = self._diagnostic_observation.target_law_count
        return {"expected": count, "loaded": count, "article_backed": count}

    def _verify_law_index(self) -> dict[str, int]:
        observed = self._diagnostic_observation
        return {
            "expected": observed.article_count,
            "postgres_indexed": observed.article_count,
            "qdrant": observed.qdrant_point_count,
            "postgres_graph_articles": observed.article_count,
            "neo4j": observed.neo4j_article_count,
            "neo4j_missing": 0,
            "neo4j_orphaned": 0,
        }


class _ObservedDiagnosticRuntimeLeaseLoader:
    """Keep the synthetic lease bound to the live read-only store observation.

    The runtime-v2 router calls ``assert_unchanged`` after readiness checks and
    after retrieval.  Re-observing here preserves the same before/after
    discard semantics as Production even though this diagnostic has no active
    generation table.
    """

    def __init__(
        self,
        lease: GenerationRequestLease,
        *,
        initial_observation: ThreeLawObservation,
        observation_loader: ObservationLoader,
        expected_loaded_law_count: int,
    ) -> None:
        self._delegate = FixedTestOnlyRuntimeLeaseLoader(lease)
        self._initial_observation = initial_observation
        self._observation_loader = observation_loader
        self._expected_loaded_law_count = expected_loaded_law_count

    def acquire(
        self,
        deadline: RequestDeadline | None = None,
    ) -> GenerationRequestLease:
        return self._delegate.acquire(deadline)

    def assert_unchanged(
        self,
        lease: GenerationRequestLease,
        deadline: RequestDeadline | None = None,
    ) -> None:
        self._delegate.assert_unchanged(lease, deadline)
        current = self._observation_loader()
        _validate_observation(
            current,
            expected_loaded_law_count=self._expected_loaded_law_count,
        )
        if current != self._initial_observation:
            raise RuntimeReleaseError("three-law Data observation changed during request")


@dataclass(frozen=True, slots=True)
class ThreeLawDiagnosticManifest:
    data_revision: str
    observation: ThreeLawObservation
    identity: EvidenceRuntimeReleaseIdentity

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": "three-law-evidence-diagnostic/v1",
            "scope": DIAGNOSTIC_SCOPE,
            "release_ready": False,
            "rag_quality_evaluated": False,
            "expected_production_law_count": EXPECTED_PRODUCTION_LAW_COUNT,
            "expected_diagnostic_law_count": EXPECTED_DIAGNOSTIC_LAW_COUNT,
            "observed_target_law_count": self.observation.target_law_count,
            "observed_total_law_count": self.observation.total_law_count,
            "observed_article_count": self.observation.article_count,
            "qdrant_point_count": self.observation.qdrant_point_count,
            "neo4j_article_count": self.observation.neo4j_article_count,
            "knowledge_snapshot_id": self.observation.knowledge_snapshot_id,
            "supported_review_as_of": (self.observation.supported_review_as_of.isoformat()),
            "law_article_manifest_sha256": (self.observation.law_article_manifest_sha256),
            "postgres_qdrant_ids_sha256": (self.observation.postgres_qdrant_ids_sha256),
            "qdrant_point_ids_sha256": self.observation.qdrant_point_ids_sha256,
            "postgres_graph_ids_sha256": (self.observation.postgres_graph_ids_sha256),
            "neo4j_article_ids_sha256": (self.observation.neo4j_article_ids_sha256),
            "runtime_stability": {
                "omp_num_threads": os.environ["OMP_NUM_THREADS"],
                "mkl_num_threads": os.environ["MKL_NUM_THREADS"],
                "tokenizers_parallelism": os.environ["TOKENIZERS_PARALLELISM"],
                "hf_hub_offline": os.environ["HF_HUB_OFFLINE"],
            },
            "data_revision": self.data_revision,
            "database_access": "read_only",
            "evidence_runtime_release": self.identity.model_dump(mode="json"),
        }


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


def write_diagnostic_manifest(
    manifest: ThreeLawDiagnosticManifest,
    output: Path,
    *,
    forbidden_roots: tuple[Path, ...],
) -> None:
    """Exclusively create a location-free, credential-free public manifest."""

    if not output.is_absolute():
        raise ThreeLawDiagnosticError("diagnostic manifest path must be absolute")
    if output.is_symlink() or not output.parent.is_dir() or _has_symlink_component(output.parent):
        raise ThreeLawDiagnosticError(
            "diagnostic manifest parent must be an existing non-symlink directory"
        )
    destination = output.parent.resolve() / output.name
    if any(_is_within(destination, root) for root in forbidden_roots):
        raise ThreeLawDiagnosticError(
            "diagnostic manifest must be outside source and provider context roots"
        )
    if destination.exists() or destination.is_symlink():
        raise ThreeLawDiagnosticError("diagnostic manifest must be a new regular file")
    payload = manifest.to_wire()
    if "evidence_base_url" in payload:
        raise ThreeLawDiagnosticError("diagnostic manifest must not contain a service location")
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > _MAX_MANIFEST_BYTES:
        raise ThreeLawDiagnosticError("diagnostic manifest exceeds its size limit")

    descriptor: int | None = None
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        created = True
        opened = os.fstat(descriptor)
        linked = os.stat(destination, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
            raise OSError("diagnostic manifest identity changed")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(destination, 0o600)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise ThreeLawDiagnosticError("diagnostic manifest could not be created") from exc


def _default_backend_factory(
    observation: ThreeLawObservation,
) -> ThreeLawDiagnosticDatabaseBackend:
    return ThreeLawDiagnosticDatabaseBackend(observation)


def create_three_law_diagnostic_application(
    *,
    diagnostic_only_ack: bool,
    provider_context: Path,
    service_token_file: Path,
    diagnostic_manifest_output: Path,
    host: str,
    port: int,
    expected_loaded_law_count: int = EXPECTED_DIAGNOSTIC_LAW_COUNT,
    observation_loader: ObservationLoader = observe_three_law_data,
    provider_lock_loader: Callable[
        [Path],
        VerifiedProviderRuntimeLock,
    ] = load_verified_provider_runtime_lock,
    database_backend_factory: Callable[
        [ThreeLawObservation],
        Any,
    ] = _default_backend_factory,
    install_readonly_boundary: bool = True,
):
    """Create the explicit non-release app after one exact store observation."""

    if not diagnostic_only_ack:
        raise ThreeLawDiagnosticError("explicit three-law diagnostic acknowledgement is required")
    if host not in LOOPBACK_HOSTS:
        raise ThreeLawDiagnosticError("host must be loopback-only")
    if not 1 <= port <= 65_535:
        raise ThreeLawDiagnosticError("port must be from 1 to 65535")
    if expected_loaded_law_count != EXPECTED_DIAGNOSTIC_LAW_COUNT:
        raise ThreeLawDiagnosticError("expected loaded law count must be exactly 3")

    context = provider_context.resolve()
    provider_lock = provider_lock_loader(context)
    if (
        not _REVISION_PATTERN.fullmatch(provider_lock.provider_revision)
        or provider_lock.provider_release != "evidence-api-v1.2.0"
    ):
        raise ThreeLawDiagnosticError("provider runtime lock is not a valid v2 Data revision")
    token = load_bearer_token_file(service_token_file)

    readonly_boundary: _ReadOnlyPostgresBoundary | None = None
    try:
        if install_readonly_boundary:
            readonly_boundary = _ReadOnlyPostgresBoundary()
        initial_observation = observation_loader()
        _validate_observation(
            initial_observation,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        lease = derive_three_law_runtime_lease(provider_lock, initial_observation)

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
            request_deadline_seconds=30,
            store_operation_timeout_seconds=10,
            store_termination_timeout_seconds=0.5,
            model_inference_timeout_seconds=10,
            model_termination_timeout_seconds=0.5,
            deadline_completion_reserve_seconds=0.5,
            shutdown_grace_seconds=5,
            enable_openapi=False,
            log_level="warning",
        )
        loader = _ObservedDiagnosticRuntimeLeaseLoader(
            lease,
            initial_observation=initial_observation,
            observation_loader=observation_loader,
            expected_loaded_law_count=expected_loaded_law_count,
        )

        def runtime_backend_factory(
            bound_lease: GenerationRequestLease,
            _request_deadline: RequestDeadline,
        ) -> Any:
            if bound_lease != lease:
                raise RuntimeReleaseError("three-law diagnostic lease mismatch")
            current = observation_loader()
            _validate_observation(
                current,
                expected_loaded_law_count=expected_loaded_law_count,
            )
            if current != initial_observation:
                raise RuntimeReleaseError("three-law Data observation changed")
            return database_backend_factory(current)

        closeables: tuple[Any, ...] = (readonly_boundary,) if readonly_boundary is not None else ()
        application = create_test_runtime_v2_application(
            service_settings=settings,
            runtime_release_loader=loader,
            backend_factory=runtime_backend_factory,
            runtime_closeables=closeables,
        )
        manifest = ThreeLawDiagnosticManifest(
            data_revision=provider_lock.provider_revision,
            observation=initial_observation,
            identity=lease.identity,
        )
        write_diagnostic_manifest(
            manifest,
            diagnostic_manifest_output,
            forbidden_roots=(ROOT, context),
        )
    except Exception:
        if readonly_boundary is not None:
            readonly_boundary.close()
        raise

    application.state.test_only_runtime_conformance = False
    application.state.test_only_three_law_evidence_diagnostic = True
    application.state.three_law_diagnostic_manifest = manifest.to_wire()
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
        "--diagnostic-only-three-law",
        action="store_true",
        required=True,
    )
    parser.add_argument(
        "--provider-context",
        "--production-context",
        dest="provider_context",
        type=Path,
        required=True,
    )
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--diagnostic-manifest-output", type=Path, required=True)
    parser.add_argument("--host", choices=LOOPBACK_HOSTS, required=True)
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
        application = create_three_law_diagnostic_application(
            diagnostic_only_ack=args.diagnostic_only_three_law,
            provider_context=args.provider_context,
            service_token_file=args.service_token_file,
            diagnostic_manifest_output=args.diagnostic_manifest_output,
            host=args.host,
            port=args.port,
            expected_loaded_law_count=args.expected_loaded_law_count,
        )
    except Exception as exc:
        safe_reason = (
            str(exc) if isinstance(exc, ThreeLawDiagnosticError) else "invalid_configuration"
        )
        print(
            json.dumps(
                {
                    "event": "three_law_evidence_diagnostic_configuration_failed",
                    "exception_type": type(exc).__name__,
                    "reason": safe_reason,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78

    manifest = application.state.three_law_diagnostic_manifest
    print(
        json.dumps(
            {
                "event": "three_law_evidence_v2_diagnostic_starting",
                "scope": DIAGNOSTIC_SCOPE,
                "host": args.host,
                "port": args.port,
                "observed_target_law_count": manifest["observed_target_law_count"],
                "observed_article_count": manifest["observed_article_count"],
                "release_ready": False,
                "rag_quality_evaluated": False,
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
                    "event": "three_law_evidence_diagnostic_runtime_failed",
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
