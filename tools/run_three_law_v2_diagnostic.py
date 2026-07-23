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
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from psycopg.rows import dict_row
from pydantic import SecretStr
from pydantic_core import PydanticSerializationError, to_jsonable_python

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
from src.config import settings as data_store_settings  # noqa: E402
from src.db import postgres  # noqa: E402
from src.db.qdrant import (  # noqa: E402
    BM25_VECTOR,
    COLLECTION,
    DENSE_VECTOR,
    bounded_client as bounded_qdrant_client,
    client as make_qdrant_client,
)
from src.deadline import RequestDeadline  # noqa: E402
from src.evidence import (  # noqa: E402
    DatabaseEvidenceBackend,
    LawArticleMatch,
    RequirementLookup,
)
from src.ingest.laws import TARGET_LAWS  # noqa: E402
from src.model_runtime import DenseModelSupervisor  # noqa: E402
from src.neo4j_runtime import KillableNeo4jQueryExecutor, Neo4jQueryExecutor  # noqa: E402
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
DIAGNOSTIC_STARTUP_OBSERVATION_DEADLINE_SECONDS = 180.0
DIAGNOSTIC_SERVICE_REQUEST_DEADLINE_SECONDS = 180.0
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOMAIN = b"customs-data-platform:three-law-evidence-diagnostic:v1\0"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_QDRANT_POINTS = 1_000_000
_MAX_STATE_MANIFEST_BYTES = 256 * 1024 * 1024
_QDRANT_SCROLL_PAGE_SIZE = 64
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
    postgres_retrieval_state_sha256: str
    qdrant_retrieval_state_sha256: str
    neo4j_retrieval_state_sha256: str

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
            "postgres_retrieval_state_sha256": self.postgres_retrieval_state_sha256,
            "qdrant_retrieval_state_sha256": self.qdrant_retrieval_state_sha256,
            "neo4j_retrieval_state_sha256": self.neo4j_retrieval_state_sha256,
            "release_ready": False,
            "rag_quality_evaluated": False,
        }


class ObservationLoader(Protocol):
    def __call__(
        self,
        deadline: RequestDeadline | None = None,
    ) -> ThreeLawObservation: ...


class ExecutingRepositoryAttestor(Protocol):
    def __call__(
        self,
        deadline: RequestDeadline | None = None,
    ) -> None: ...


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
    """Create the bounded budget for startup/offline full-store attestation.

    Runtime HTTP requests receive their own deadline from the production
    boundary.  Startup has to read every vector and graph row before binding a
    diagnostic identity, so it uses the same 180-second outer bound without
    weakening the 20-second cap on any individual store operation.
    """

    return RequestDeadline.start(
        started_at=time.monotonic(),
        request_timeout_seconds=DIAGNOSTIC_STARTUP_OBSERVATION_DEADLINE_SECONDS,
        store_timeout_seconds=20,
        model_timeout_seconds=20,
        completion_reserve_seconds=2,
    )


def _git(
    repository: Path,
    *arguments: str,
    deadline: RequestDeadline | None = None,
    text: bool = True,
) -> str | bytes:
    """Run one bounded, non-shell Git read for runtime source attestation."""

    if deadline is not None:
        deadline.assert_work_allowed()
        timeout = deadline.store_operation_seconds()
    else:
        timeout = 10.0
    executable = shutil.which("git")
    if executable is None:
        raise ThreeLawDiagnosticError("Git is unavailable for Data source attestation")
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and argv
            [executable, "-C", str(repository), *arguments],
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ThreeLawDiagnosticError("executing Data revision could not be attested") from exc
    if result.returncode:
        raise ThreeLawDiagnosticError("executing Data revision could not be attested")
    if deadline is not None:
        deadline.assert_work_allowed()
    output = result.stdout
    return output.strip() if isinstance(output, str) else output


def attest_executing_repository(
    provider_lock: VerifiedProviderRuntimeLock,
    *,
    repository: Path = ROOT,
    deadline: RequestDeadline | None = None,
) -> None:
    """Require the exact clean committed tree named by the provider context.

    The provider context proves its allowlisted runtime bytes.  This additional
    check prevents a diagnostic process from advertising that build while
    executing a different checkout, dirty source, or uncommitted runner.
    """

    status = _git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        deadline=deadline,
    )
    if status:
        raise ThreeLawDiagnosticError("executing Data repository is not clean")
    revision = _git(repository, "rev-parse", "HEAD", deadline=deadline)
    if revision != provider_lock.provider_revision:
        raise ThreeLawDiagnosticError(
            "executing Data revision does not match the provider runtime build"
        )
    relative_runner = Path(__file__).resolve().relative_to(repository.resolve()).as_posix()
    committed_runner = _git(
        repository,
        "show",
        f"HEAD:{relative_runner}",
        deadline=deadline,
        text=False,
    )
    if committed_runner != Path(__file__).read_bytes():
        raise ThreeLawDiagnosticError(
            "executing diagnostic bytes do not match the committed provider revision"
        )


def _canonical_value(value: Any) -> Any:
    """Convert store-driver values to deterministic, lossless JSON values."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump(mode="json")
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        value = to_list()
    try:
        return to_jsonable_python(
            value,
            bytes_mode="base64",
            inf_nan_mode="constants",
            serialize_unknown=False,
        )
    except (PydanticSerializationError, TypeError, ValueError) as exc:
        raise ThreeLawDiagnosticError(
            f"store state contains an unsupported value type: {type(value).__name__}"
        ) from exc


def _state_sha256(label: bytes, value: Any) -> str:
    serialized = canonical_json_bytes(_canonical_value(value))
    if len(serialized) > _MAX_STATE_MANIFEST_BYTES:
        raise ThreeLawDiagnosticError("store state manifest exceeds the diagnostic limit")
    digest = hashlib.sha256()
    digest.update(_DOMAIN)
    digest.update(b"state\0")
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(len(serialized).to_bytes(8, "big"))
    digest.update(serialized)
    return digest.hexdigest()


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
        observation.postgres_retrieval_state_sha256,
        observation.qdrant_retrieval_state_sha256,
        observation.neo4j_retrieval_state_sha256,
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


def _observe_qdrant_state(
    qdrant: Any,
    exact_count: int,
    *,
    collection_state: Any,
    eligible_by_point: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[str, ...], str]:
    """Validate and hash every retrieval-affecting point with bounded memory.

    Qdrant returns each dense vector as a Python list of 1,024 float objects.
    Keeping all 1,007 point objects alive until ``json.dumps`` caused a large
    transient allocation on constrained UAT hosts.  This observer retains the
    exact legacy canonical JSON and digest, but it:

    * requests only a small page at a time;
    * validates each payload against PostgreSQL immediately;
    * keeps only compact canonical bytes plus the point ID; and
    * streams those sorted bytes into the framed state digest.

    The compact bytes are necessary because Qdrant's native ID ordering is not
    guaranteed to equal the legacy ``str(id)`` ordering.  They are bounded by
    the same 256 MiB state-manifest limit and never leave process memory.
    """

    if exact_count < 0 or exact_count > _MAX_QDRANT_POINTS:
        raise ThreeLawDiagnosticError("Qdrant point count is outside the diagnostic limit")
    points: list[tuple[str, bytes]] = []
    offset: Any = None
    seen_offsets: set[str] = set()
    serialized_points_size = 0
    for _ in range(exact_count + 1):
        records, next_offset = qdrant.scroll(
            collection_name=COLLECTION,
            limit=_QDRANT_SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for record in records:
            if record.id is None:
                raise ThreeLawDiagnosticError("Qdrant returned a point without an identity")
            point_id = str(record.id)
            point = {
                "id": point_id,
                "payload": _canonical_value(record.payload),
                "vector": _canonical_value(record.vector),
            }
            expected = eligible_by_point.get(point_id)
            payload = point["payload"]
            if (
                expected is None
                or not isinstance(payload, dict)
                or any(
                    payload.get(field) != expected[field]
                    for field in (
                        "article_pk",
                        "law_id",
                        "law_name",
                        "hierarchy",
                        "article_no",
                        "paragraph_no",
                        "item_no",
                        "enforcement_date",
                        "text",
                    )
                )
            ):
                raise ThreeLawDiagnosticError("Qdrant retrieval payload differs from PostgreSQL")
            point_bytes = canonical_json_bytes(point)
            serialized_points_size += len(point_bytes)
            if len(points) > 0:
                serialized_points_size += 1
            if serialized_points_size > _MAX_STATE_MANIFEST_BYTES:
                raise ThreeLawDiagnosticError("store state manifest exceeds the diagnostic limit")
            points.append((point_id, point_bytes))
        if len(points) > exact_count:
            raise ThreeLawDiagnosticError("Qdrant scroll exceeded its exact point count")
        if next_offset is None:
            if len(points) != exact_count:
                raise ThreeLawDiagnosticError("Qdrant scroll did not return its exact point count")
            break
        marker = str(next_offset)
        if marker in seen_offsets:
            raise ThreeLawDiagnosticError("Qdrant pagination repeated an offset")
        seen_offsets.add(marker)
        offset = next_offset
    else:
        raise ThreeLawDiagnosticError("Qdrant pagination exceeded its exact point count")

    points.sort(key=lambda item: item[0])
    point_ids = tuple(point_id for point_id, _point_bytes in points)
    if len(set(point_ids)) != len(point_ids):
        raise ThreeLawDiagnosticError("Qdrant returned duplicate point identities")

    collection_bytes = canonical_json_bytes(_canonical_value(collection_state))
    json_prefix = b'{"collection":' + collection_bytes + b',"points":['
    json_suffix = b"]}"
    serialized_length = len(json_prefix) + serialized_points_size + len(json_suffix)
    if serialized_length > _MAX_STATE_MANIFEST_BYTES:
        raise ThreeLawDiagnosticError("store state manifest exceeds the diagnostic limit")

    digest = hashlib.sha256()
    label = b"qdrant-retrieval"
    digest.update(_DOMAIN)
    digest.update(b"state\0")
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(serialized_length.to_bytes(8, "big"))
    digest.update(json_prefix)
    for index, (_point_id, point_bytes) in enumerate(points):
        if index:
            digest.update(b",")
        digest.update(point_bytes)
    digest.update(json_suffix)
    return point_ids, digest.hexdigest()


def _eligible_law_leaf_projection(
    eligible_rows: tuple[tuple[Any, ...], ...],
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    if not eligible_rows:
        raise ThreeLawDiagnosticError("PostgreSQL has no eligible current law leaves")
    if any(row[1] is None for row in eligible_rows):
        raise ThreeLawDiagnosticError("an eligible current law leaf has no Qdrant point ID")
    point_ids = tuple(str(row[1]) for row in eligible_rows)
    if len(set(point_ids)) != len(point_ids):
        raise ThreeLawDiagnosticError("eligible law leaves do not map to unique Qdrant points")
    return point_ids, {
        str(row[1]): {
            "article_pk": int(row[0]),
            "law_id": str(row[2]),
            "article_no": int(row[3]),
            "paragraph_no": row[4],
            "item_no": row[5],
            "text": str(row[6]),
            "law_name": str(row[7]),
            "hierarchy": str(row[8]),
            "enforcement_date": row[9].isoformat(),
        }
        for row in eligible_rows
    }


def _new_neo4j_executor() -> KillableNeo4jQueryExecutor:
    return KillableNeo4jQueryExecutor(
        uri=data_store_settings.neo4j_uri,
        user=data_store_settings.neo4j_user,
        password=data_store_settings.neo4j_password,
        termination_timeout_seconds=0.5,
        maximum_rows=1_000_000,
        maximum_response_bytes=64 * 1024 * 1024,
    )


def observe_three_law_data(
    deadline: RequestDeadline | None = None,
    *,
    connection_factory: Callable[
        [RequestDeadline | None],
        Any,
    ] = readonly_postgres_connection,
    qdrant_factory: Callable[..., Any] = make_qdrant_client,
    neo4j_query_executor: Neo4jQueryExecutor | None = None,
) -> ThreeLawObservation:
    """Attest every store value that the legacy diagnostic backend can read."""

    request_deadline = deadline or _deadline()
    request_deadline.assert_work_allowed()
    try:
        with connection_factory(request_deadline) as connection, connection.cursor() as cursor:
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
                SELECT a.id, a.qdrant_point_id::text, a.law_id, a.article_no,
                       a.paragraph_no, a.item_no, a.content, l.law_name,
                       l.hierarchy, a.valid_from
                FROM law_articles a JOIN laws l USING (law_id)
                WHERE a.is_current
                  AND NOT EXISTS (
                    SELECT 1 FROM law_articles child
                    WHERE child.parent_article_pk=a.id
                  )
                ORDER BY a.id
                """
            )
            eligible_rows = tuple(cursor.fetchall())
            cursor.execute(
                """
                SELECT id
                FROM law_articles
                WHERE is_current AND paragraph_no IS NULL
                ORDER BY id
                """
            )
            postgres_graph_ids = tuple(int(row[0]) for row in cursor.fetchall())
            cursor.execute(
                """
                SELECT to_jsonb(snapshot_row), supported_review_as_of
                FROM (
                  SELECT s.*, (s.generated_at AT TIME ZONE 'Asia/Seoul')::date
                    AS supported_review_as_of
                  FROM knowledge_snapshots s
                  WHERE s.source_type='ccct' AND s.is_current
                ) snapshot_row
                """
            )
            snapshot_rows = tuple(cursor.fetchall())
            if len(snapshot_rows) != 1:
                raise ThreeLawDiagnosticError("the current CCCT snapshot is not unique")
            snapshot_state, supported_review_as_of = snapshot_rows[0]
            snapshot_id = snapshot_state.get("snapshot_id")
            state_queries = (
                (
                    "laws",
                    """
                    SELECT to_jsonb(row_state) FROM (
                      SELECT * FROM laws
                      WHERE law_id = ANY(%s)
                      ORDER BY law_id
                    ) row_state
                    """,
                    (list(DIAGNOSTIC_LAW_IDS),),
                ),
                (
                    "law_versions",
                    """
                    SELECT to_jsonb(row_state) FROM (
                      SELECT * FROM law_versions
                      WHERE law_id = ANY(%s)
                      ORDER BY law_id, mst, id
                    ) row_state
                    """,
                    (list(DIAGNOSTIC_LAW_IDS),),
                ),
                (
                    "law_articles",
                    """
                    SELECT to_jsonb(row_state) FROM (
                      SELECT * FROM law_articles
                      WHERE law_id = ANY(%s)
                      ORDER BY law_id, article_no, paragraph_no, item_no, valid_from, id
                    ) row_state
                    """,
                    (list(DIAGNOSTIC_LAW_IDS),),
                ),
                (
                    "ccct_queries",
                    """
                    SELECT to_jsonb(row_state) FROM (
                      SELECT * FROM ccct_queries
                      WHERE snapshot_id=%s
                      ORDER BY hsk10, trade_type
                    ) row_state
                    """,
                    (snapshot_id,),
                ),
                (
                    "ccct_requirements",
                    """
                    SELECT to_jsonb(row_state) FROM (
                      SELECT * FROM ccct_requirements
                      WHERE snapshot_id=%s
                      ORDER BY requirement_id
                    ) row_state
                    """,
                    (snapshot_id,),
                ),
                (
                    "ccct_source_records",
                    """
                    SELECT to_jsonb(row_state) FROM (
                      SELECT * FROM ccct_source_records
                      WHERE snapshot_id=%s
                      ORDER BY source_record_id
                    ) row_state
                    """,
                    (snapshot_id,),
                ),
            )
            postgres_state: dict[str, Any] = {
                "knowledge_snapshot": _canonical_value(snapshot_state)
            }
            for label, query, parameters in state_queries:
                cursor.execute(query, parameters)
                postgres_state[label] = [_canonical_value(row[0]) for row in cursor.fetchall()]
        request_deadline.assert_work_allowed()
    except ThreeLawDiagnosticError:
        raise
    except Exception as exc:
        raise ThreeLawDiagnosticError("read-only PostgreSQL observation failed") from exc

    if total_row is None or not isinstance(total_row[0], int):
        raise ThreeLawDiagnosticError("PostgreSQL returned an invalid law count")
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ThreeLawDiagnosticError("the current CCCT snapshot is missing")
    if not isinstance(supported_review_as_of, date):
        raise ThreeLawDiagnosticError("knowledge snapshot effective date is invalid")
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

    postgres_qdrant_ids, eligible_by_point = _eligible_law_leaf_projection(eligible_rows)

    try:
        with bounded_qdrant_client(
            request_deadline,
            client_factory=qdrant_factory,
        ) as qdrant:
            qdrant_count = int(qdrant.count(COLLECTION, exact=True).count)
            qdrant_collection = _canonical_value(qdrant.get_collection(COLLECTION))
            qdrant_ids, qdrant_retrieval_state_sha256 = _observe_qdrant_state(
                qdrant,
                qdrant_count,
                collection_state=qdrant_collection,
                eligible_by_point=eligible_by_point,
            )
            if int(qdrant.count(COLLECTION, exact=True).count) != qdrant_count:
                raise ThreeLawDiagnosticError("Qdrant point count changed during observation")
    except Exception as exc:
        raise ThreeLawDiagnosticError("read-only Qdrant observation failed") from exc
    if (
        len(set(qdrant_ids)) != len(qdrant_ids)
        or set(qdrant_ids) != set(postgres_qdrant_ids)
        or qdrant_count != len(eligible_rows)
    ):
        raise ThreeLawDiagnosticError("Qdrant projection does not match every eligible law leaf")

    created_executor = neo4j_query_executor is None
    executor = neo4j_query_executor or _new_neo4j_executor()
    try:
        graph_state_rows = executor.run(
            """
            CALL () {
              MATCH (article:Article)
              RETURN 'node' AS kind,
                     {
                       labels: labels(article),
                       properties: properties(article)
                     } AS state
              UNION ALL
              MATCH (hsk:HSK)
              RETURN 'node' AS kind,
                     {
                       labels: ['HSK'],
                       properties: {code: hsk.code}
                     } AS state
              UNION ALL
              MATCH (requirement:Requirement {source_type: 'ccct'})
              RETURN 'node' AS kind,
                     {
                       labels: ['Requirement'],
                       properties: {
                         source_type: requirement.source_type,
                         snapshot_id: requirement.snapshot_id
                       }
                     } AS state
              UNION ALL
              MATCH (source:Article)-[relationship]->(target)
              RETURN 'relationship' AS kind,
                     {
                       source_article_pk: source.article_pk,
                       relationship_type: type(relationship),
                       relationship_properties: properties(relationship),
                       target_labels: labels(target),
                       target_properties: {
                         article_pk: target.article_pk,
                         law_id: target.law_id,
                         article_no: target.article_no,
                         title: target.title,
                         name: target.name
                       }
                     } AS state
            }
            RETURN kind, state
            """,
            {},
            request_deadline,
        )
    except Exception as exc:
        raise ThreeLawDiagnosticError("read-only Neo4j observation failed") from exc
    finally:
        if created_executor:
            close = getattr(executor, "close", None)
            if callable(close):
                close()
    node_rows = [row["state"] for row in graph_state_rows if row.get("kind") == "node"]
    relationship_rows = [
        row["state"] for row in graph_state_rows if row.get("kind") == "relationship"
    ]
    canonical_nodes = tuple(
        sorted(
            (_canonical_value(row) for row in node_rows),
            key=canonical_json_bytes,
        )
    )
    canonical_relationships = tuple(
        sorted(
            (_canonical_value(row) for row in relationship_rows),
            key=canonical_json_bytes,
        )
    )
    neo4j_ids: list[int] = []
    graph_snapshots: set[str] = set()
    for row in canonical_nodes:
        labels = row.get("labels", [])
        properties = row.get("properties", {})
        if "Article" in labels:
            article_pk = properties.get("article_pk")
            if article_pk is None:
                raise ThreeLawDiagnosticError("Neo4j Article is missing article_pk")
            neo4j_ids.append(int(article_pk))
        if "Requirement" in labels and properties.get("source_type") == "ccct":
            graph_snapshot = properties.get("snapshot_id")
            if isinstance(graph_snapshot, str) and graph_snapshot:
                graph_snapshots.add(graph_snapshot)
    if graph_snapshots != {snapshot_id}:
        raise ThreeLawDiagnosticError("Neo4j CCCT snapshot differs from PostgreSQL")
    if (
        len(neo4j_ids) != len(set(neo4j_ids))
        or set(neo4j_ids) != set(postgres_graph_ids)
        or len(neo4j_ids) != len(postgres_graph_ids)
    ):
        raise ThreeLawDiagnosticError("Neo4j Article projection does not match PostgreSQL")

    observation = ThreeLawObservation(
        law_ids=loaded_ids,
        total_law_count=total_row[0],
        article_count=len(eligible_rows),
        qdrant_point_count=qdrant_count,
        neo4j_article_count=len(neo4j_ids),
        knowledge_snapshot_id=snapshot_id,
        supported_review_as_of=supported_review_as_of,
        law_article_manifest_sha256=_state_sha256(b"current-law-articles", law_article_manifest),
        postgres_qdrant_ids_sha256=_identifier_manifest_sha256(postgres_qdrant_ids),
        qdrant_point_ids_sha256=_identifier_manifest_sha256(qdrant_ids),
        postgres_graph_ids_sha256=_identifier_manifest_sha256(postgres_graph_ids),
        neo4j_article_ids_sha256=_identifier_manifest_sha256(tuple(neo4j_ids)),
        postgres_retrieval_state_sha256=_state_sha256(b"postgres-retrieval", postgres_state),
        qdrant_retrieval_state_sha256=qdrant_retrieval_state_sha256,
        neo4j_retrieval_state_sha256=_state_sha256(
            b"neo4j-retrieval",
            {
                "nodes": canonical_nodes,
                "relationships": canonical_relationships,
            },
        ),
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
    """Deadline-bound real DB backend for the observed non-release stores."""

    def __init__(
        self,
        observation: ThreeLawObservation,
        *,
        deadline: RequestDeadline,
        neo4j_query_executor: Neo4jQueryExecutor,
        dense_encoder: Callable[[list[str], RequestDeadline], Any],
        connection_factory: Callable[
            [RequestDeadline | None],
            Any,
        ] = readonly_postgres_connection,
        qdrant_factory: Callable[..., Any] = make_qdrant_client,
    ) -> None:
        super().__init__()
        self._diagnostic_observation = observation
        self._deadline = deadline
        self._neo4j_query_executor = neo4j_query_executor
        self._dense_encoder = dense_encoder
        self._connection_factory = connection_factory
        self._qdrant_factory = qdrant_factory

    def _require_deadline(self) -> RequestDeadline:
        self._deadline.assert_work_allowed()
        return self._deadline

    def _connection(self) -> Any:
        return self._connection_factory(self._require_deadline())

    def _run_neo4j(
        self,
        query: str,
        parameters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        return self._neo4j_query_executor.run(
            query,
            parameters,
            self._require_deadline(),
        )

    def validate_ready(self) -> None:
        snapshot = self.current_snapshot_id()
        if snapshot != self._diagnostic_observation.knowledge_snapshot_id:
            raise ThreeLawDiagnosticError("knowledge snapshot changed")
        if self.snapshot_effective_date(snapshot) != (
            self._diagnostic_observation.supported_review_as_of
        ):
            raise ThreeLawDiagnosticError("knowledge snapshot effective date changed")

    def current_snapshot_id(self) -> str | None:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snapshot_id
                FROM knowledge_snapshots
                WHERE source_type='ccct' AND is_current
                """
            )
            rows = tuple(cursor.fetchall())
        if len(rows) != 1 or not isinstance(rows[0][0], str):
            return None
        snapshot_id = rows[0][0]
        graph_rows = self._run_neo4j(
            """
            MATCH (requirement:Requirement {source_type: 'ccct'})
            RETURN DISTINCT requirement.snapshot_id AS snapshot_id
            ORDER BY snapshot_id
            """,
            {},
        )
        graph_snapshots = {
            str(row["snapshot_id"])
            for row in graph_rows
            if isinstance(row.get("snapshot_id"), str) and row["snapshot_id"]
        }
        if graph_snapshots != {snapshot_id}:
            raise ThreeLawDiagnosticError("CCCT knowledge snapshot changed")
        return snapshot_id

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        with self._connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT (generated_at AT TIME ZONE 'Asia/Seoul')::date
                FROM knowledge_snapshots
                WHERE snapshot_id=%s AND source_type='ccct'
                """,
                (snapshot_id,),
            )
            rows = tuple(cursor.fetchall())
        if len(rows) != 1 or not isinstance(rows[0][0], date):
            raise ThreeLawDiagnosticError("CCCT snapshot effective date is unavailable")
        return rows[0][0]

    def resolve_hsk(self, code: str | None) -> Any:
        try:
            normalized = repository_module.normalize_hsk(code)
        except ValueError:
            return repository_module.HskResolution("invalid", None, None)
        if normalized is None:
            return repository_module.HskResolution("missing", None, None)
        if len(normalized) == 6:
            rows = self._run_neo4j(
                """
                MATCH (h:HSK)
                WHERE h.code STARTS WITH $prefix
                RETURN h.code AS code
                ORDER BY code
                """,
                {"prefix": normalized},
            )
        else:
            rows = self._run_neo4j(
                """
                MATCH (h:HSK {code: $code})
                RETURN h.code AS code
                ORDER BY code
                """,
                {"code": normalized},
            )
        leaves = [str(row["code"]) for row in rows if isinstance(row.get("code"), str)]
        return repository_module.resolve_hsk_candidates(normalized, leaves)

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ) -> RequirementLookup:
        with self._connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM ccct_queries
                  WHERE snapshot_id=%s AND hsk10=%s AND trade_type=%s
                ) AS covered
                """,
                (snapshot_id, hsk10, trade_type),
            )
            coverage_row = cursor.fetchone()
            if coverage_row is None or not bool(coverage_row["covered"]):
                return RequirementLookup(False, ())
            cursor.execute(
                """
                SELECT r.requirement_id, r.requirement_version_hash, r.hsk10,
                       r.trade_type, r.law_code, r.law_name,
                       r.agency_code, r.agency_name,
                       r.document_code, r.document_name,
                       r.valid_from, r.valid_to,
                       src.source_record_id, src.source_uri
                FROM ccct_requirements r
                JOIN LATERAL (
                  SELECT s.source_record_id, s.source_uri
                  FROM ccct_source_records s
                  WHERE s.snapshot_id=r.snapshot_id
                    AND s.requirement_id=r.requirement_id
                  ORDER BY s.source_record_id
                  LIMIT 1
                ) src ON true
                WHERE r.snapshot_id=%s AND r.hsk10=%s AND r.trade_type=%s
                  AND (r.valid_from IS NULL OR r.valid_from <= %s)
                  AND (r.valid_to IS NULL OR r.valid_to >= %s)
                ORDER BY r.law_code, r.document_code, r.agency_code, r.requirement_id
                """,
                (snapshot_id, hsk10, trade_type, as_of, as_of),
            )
            rows = tuple(dict(row) for row in cursor.fetchall())
        return RequirementLookup(True, rows)

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

    def _arm_hits(
        self,
        qdrant: Any,
        query: str,
        arm: str,
        recall: int,
        as_of: date,
    ) -> list[Any]:
        if arm == DENSE_VECTOR:
            vectors = self._dense_encoder([query], self._require_deadline())
            if len(vectors) != 1:
                raise ThreeLawDiagnosticError("dense model returned an invalid vector batch")
            points = qdrant.query_points(
                COLLECTION,
                query=list(vectors[0]),
                using=DENSE_VECTOR,
                limit=recall,
                timeout=self._require_deadline().store_operation_whole_seconds(),
            ).points
        elif arm == BM25_VECTOR:
            from qdrant_client import models

            sparse_indices, sparse_values = repository_module.bm25.encode_query(query)
            points = qdrant.query_points(
                COLLECTION,
                query=models.SparseVector(
                    indices=sparse_indices,
                    values=sparse_values,
                ),
                using=BM25_VECTOR,
                limit=recall,
                timeout=self._require_deadline().store_operation_whole_seconds(),
            ).points
        else:
            raise ThreeLawDiagnosticError("unsupported diagnostic retrieval arm")

        hits: list[Any] = []
        for point in points:
            payload = point.payload
            if not isinstance(payload, dict):
                raise ThreeLawDiagnosticError("Qdrant candidate payload is unavailable")
            article_pk = payload.get("article_pk")
            if not isinstance(article_pk, int):
                raise ThreeLawDiagnosticError("Qdrant candidate article identity is invalid")
            with self._connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, content
                    FROM law_articles
                    WHERE id=%s
                      AND valid_from <= %s
                      AND (valid_to IS NULL OR valid_to > %s)
                    """,
                    (article_pk, as_of, as_of),
                )
                rows = tuple(cursor.fetchall())
            if len(rows) > 1:
                raise ThreeLawDiagnosticError("law candidate temporal identity is ambiguous")
            if rows:
                hits.append(
                    repository_module.Hit(
                        article_pk=int(rows[0][0]),
                        score=float(point.score),
                        text=str(rows[0][1]),
                    )
                )
        return hits

    def search_law_articles(
        self,
        query: str,
        top_k: int,
        as_of: date,
    ) -> tuple[LawArticleMatch, ...]:
        self._require_deadline()
        recall = min(max(top_k * 2, 8), 40)
        with bounded_qdrant_client(
            self._require_deadline(),
            client_factory=self._qdrant_factory,
        ) as qdrant:
            dense_hits = self._arm_hits(qdrant, query, DENSE_VECTOR, recall, as_of)
            bm25_hits = self._arm_hits(qdrant, query, BM25_VECTOR, recall, as_of)

        rrf_k = 60
        candidates: dict[int, dict[str, Any]] = {}
        for method, hits in (("dense", dense_hits), ("bm25", bm25_hits)):
            for rank, hit in enumerate(hits, start=1):
                entry = candidates.setdefault(
                    hit.article_pk,
                    {"methods": set(), "raw": {}, "rrf": 0.0},
                )
                entry["methods"].add(method)
                entry["raw"][method] = float(hit.score)
                entry["rrf"] += 1.0 / (rrf_k + rank)

        seeds = sorted(
            candidates,
            key=lambda article_pk: candidates[article_pk]["rrf"],
            reverse=True,
        )[:3]
        for seed_pk in seeds:
            related_rows = self._run_neo4j(
                repository_module._EXPAND_CYPHER,
                {"pk": seed_pk},
            )
            for related in related_rows:
                related_pk = related.get("article_pk")
                if not isinstance(related_pk, int) or related_pk == seed_pk:
                    continue
                entry = candidates.setdefault(
                    related_pk,
                    {
                        "methods": set(),
                        "raw": {},
                        "rrf": candidates[seed_pk]["rrf"] * 0.65,
                    },
                )
                entry["methods"].add("graph")
                entry["raw"]["graph"] = max(
                    float(entry["raw"].get("graph", 0.0)),
                    float(candidates[seed_pk]["rrf"]),
                )

        if not candidates:
            return ()
        with self._connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT a.id AS article_pk, a.law_id, l.law_name, l.hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.title, a.content,
                       a.content_hash, a.version_mst, a.valid_from, a.valid_to
                FROM law_articles a JOIN laws l USING (law_id)
                WHERE a.id = ANY(%s)
                  AND a.valid_from <= %s
                  AND (a.valid_to IS NULL OR a.valid_to > %s)
                """,
                (list(candidates), as_of, as_of),
            )
            rows = {int(row["article_pk"]): dict(row) for row in cursor.fetchall()}

        query_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", query.lower()))
        theoretical_rrf_max = 2.0 / (rrf_k + 1)
        matches: list[LawArticleMatch] = []
        for article_pk, candidate in candidates.items():
            row = rows.get(article_pk)
            if row is None:
                continue
            text_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", row["content"].lower()))
            lexical = len(query_tokens & text_tokens) / len(query_tokens) if query_tokens else 0.0
            rrf_normalized = min(1.0, candidate["rrf"] / theoretical_rrf_max)
            score = min(1.0, 0.85 * rrf_normalized + 0.15 * lexical)
            raw_scores = {
                **candidate["raw"],
                "rrf": float(candidate["rrf"]),
                "lexical_overlap": float(lexical),
            }
            methods = tuple(
                method for method in ("dense", "bm25", "graph") if method in candidate["methods"]
            )
            matches.append(LawArticleMatch(row, methods, score, raw_scores))
        matches.sort(key=lambda match: (-match.score, match.row["article_pk"]))
        self._require_deadline()
        return tuple(matches[:top_k])


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
        executing_repository_attestor: ExecutingRepositoryAttestor,
        expected_loaded_law_count: int,
    ) -> None:
        self._delegate = FixedTestOnlyRuntimeLeaseLoader(lease)
        self._initial_observation = initial_observation
        self._observation_loader = observation_loader
        self._executing_repository_attestor = executing_repository_attestor
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
        self._executing_repository_attestor(deadline)
        current = self._observation_loader(deadline)
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
            "postgres_retrieval_state_sha256": (self.observation.postgres_retrieval_state_sha256),
            "qdrant_retrieval_state_sha256": (self.observation.qdrant_retrieval_state_sha256),
            "neo4j_retrieval_state_sha256": (self.observation.neo4j_retrieval_state_sha256),
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


class DiagnosticManifest(Protocol):
    """Location-free diagnostic documents accepted by the shared safe writer."""

    def to_wire(self) -> dict[str, object]: ...


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
    manifest: DiagnosticManifest,
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


def create_three_law_diagnostic_application(
    *,
    diagnostic_only_ack: bool,
    provider_context: Path,
    service_token_file: Path,
    diagnostic_manifest_output: Path,
    host: str,
    port: int,
    expected_loaded_law_count: int = EXPECTED_DIAGNOSTIC_LAW_COUNT,
    observation_loader: ObservationLoader | None = None,
    provider_lock_loader: Callable[
        [Path],
        VerifiedProviderRuntimeLock,
    ] = load_verified_provider_runtime_lock,
    database_backend_factory: Callable[
        [ThreeLawObservation, RequestDeadline],
        Any,
    ]
    | None = None,
    executing_repository_attestor: ExecutingRepositoryAttestor | None = None,
    neo4j_query_executor: Neo4jQueryExecutor | None = None,
    model_supervisor: DenseModelSupervisor | None = None,
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
    selected_attestor = executing_repository_attestor or (
        lambda deadline=None: attest_executing_repository(
            provider_lock,
            deadline=deadline,
        )
    )
    startup_deadline = _deadline()
    selected_attestor(startup_deadline)
    token = load_bearer_token_file(service_token_file)

    readonly_boundary: _ReadOnlyPostgresBoundary | None = None
    created_neo4j_executor = False
    shared_neo4j_executor = neo4j_query_executor
    created_model_supervisor = False
    shared_model_supervisor = model_supervisor
    try:
        if install_readonly_boundary:
            readonly_boundary = _ReadOnlyPostgresBoundary()
        if observation_loader is None or database_backend_factory is None:
            if shared_neo4j_executor is None:
                shared_neo4j_executor = _new_neo4j_executor()
                created_neo4j_executor = True
        if observation_loader is None:
            if shared_neo4j_executor is None:
                raise ThreeLawDiagnosticError("bounded Neo4j observer is unavailable")

            def selected_observation_loader(
                deadline: RequestDeadline | None = None,
            ) -> ThreeLawObservation:
                return observe_three_law_data(
                    deadline,
                    neo4j_query_executor=shared_neo4j_executor,
                )

        else:
            selected_observation_loader = observation_loader

        selected_database_backend_factory: Callable[
            [ThreeLawObservation, RequestDeadline],
            Any,
        ]
        if database_backend_factory is None:
            if shared_neo4j_executor is None:
                raise ThreeLawDiagnosticError("bounded Neo4j backend is unavailable")
            if shared_model_supervisor is None:
                shared_model_supervisor = DenseModelSupervisor(
                    startup_timeout_seconds=180,
                    termination_timeout_seconds=0.5,
                )
                created_model_supervisor = True

            def selected_database_backend_factory(
                observation: ThreeLawObservation,
                request_deadline: RequestDeadline,
            ) -> ThreeLawDiagnosticDatabaseBackend:
                if shared_neo4j_executor is None or shared_model_supervisor is None:
                    raise ThreeLawDiagnosticError("bounded diagnostic dependencies are unavailable")
                return ThreeLawDiagnosticDatabaseBackend(
                    observation,
                    deadline=request_deadline,
                    neo4j_query_executor=shared_neo4j_executor,
                    dense_encoder=shared_model_supervisor.encode,
                )

        else:
            selected_database_backend_factory = database_backend_factory

        initial_observation = selected_observation_loader(startup_deadline)
        _validate_observation(
            initial_observation,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        lease = derive_three_law_runtime_lease(provider_lock, initial_observation)

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
            request_deadline_seconds=DIAGNOSTIC_SERVICE_REQUEST_DEADLINE_SECONDS,
            store_operation_timeout_seconds=20,
            store_termination_timeout_seconds=0.5,
            model_inference_timeout_seconds=20,
            model_termination_timeout_seconds=0.5,
            deadline_completion_reserve_seconds=2,
            shutdown_grace_seconds=5,
            enable_openapi=False,
            log_level="warning",
        )
        loader = _ObservedDiagnosticRuntimeLeaseLoader(
            lease,
            initial_observation=initial_observation,
            observation_loader=selected_observation_loader,
            executing_repository_attestor=selected_attestor,
            expected_loaded_law_count=expected_loaded_law_count,
        )

        def runtime_backend_factory(
            bound_lease: GenerationRequestLease,
            request_deadline: RequestDeadline,
        ) -> Any:
            if bound_lease != lease:
                raise RuntimeReleaseError("three-law diagnostic lease mismatch")
            selected_attestor(request_deadline)
            current = selected_observation_loader(request_deadline)
            _validate_observation(
                current,
                expected_loaded_law_count=expected_loaded_law_count,
            )
            if current != initial_observation:
                raise RuntimeReleaseError("three-law Data observation changed")
            return selected_database_backend_factory(current, request_deadline)

        closeable_list: list[Any] = []
        if readonly_boundary is not None:
            closeable_list.append(readonly_boundary)
        if created_neo4j_executor and shared_neo4j_executor is not None:
            closeable_list.append(shared_neo4j_executor)
        application = create_test_runtime_v2_application(
            service_settings=service_settings,
            runtime_release_loader=loader,
            backend_factory=runtime_backend_factory,
            runtime_closeables=tuple(closeable_list),
            model_supervisor=(
                shared_model_supervisor if database_backend_factory is None else None
            ),
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
        if created_neo4j_executor and shared_neo4j_executor is not None:
            shared_neo4j_executor.close()
        if created_model_supervisor and shared_model_supervisor is not None:
            shared_model_supervisor.close()
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
