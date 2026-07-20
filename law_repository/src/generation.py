"""Immutable knowledge-generation contracts and release-gate validation.

This module is deliberately independent from the Evidence HTTP application.
Ingestion and release tooling can build and validate a generation first; the
Evidence runtime can consume the active pointer in a later integration change.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from src.embed.model_identity import release_model_inventory
from src.ingest.laws import TARGET_LAWS

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LAW_ID = re.compile(r"^[0-9]{6}$")
_MST = re.compile(r"^[0-9]{1,20}$")
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_LOCATOR = re.compile(r"^[A-Za-z0-9._:/-]{1,512}$")
_LAW_HOSTS = frozenset({"law.go.kr", "www.law.go.kr"})
_CUSTOMS_HOSTS = frozenset(
    {
        "customs.go.kr",
        "www.customs.go.kr",
        "unipass.customs.go.kr",
    }
)
_STORE_NAMES = frozenset({"postgresql", "qdrant", "neo4j"})
_DATASET_IDS = frozenset({"hsk", "ccct"})
_DATASET_MEMBER_KINDS = frozenset(
    {
        "ccct_query",
        "ccct_requirement",
        "ccct_source_record",
        "ccct_document",
        "hsk_leaf",
    }
)
_EXPECTED_LAWS = {str(item["law_id"]): str(item["name"]) for item in TARGET_LAWS}
_INDEX_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "dense_vector_name",
        "dense_vector_size",
        "distance",
        "sparse_vector_name",
        "sparse_modifier",
        "qdrant_collection_mode",
        "neo4j_projection_mode",
    }
)
CCCT_BASELINE_SNAPSHOT_ID = "ccct_212f4c459765d5a79f5a42e2eef77bcbe43bd7adb5c4d526a9ecc3588622e107"
CCCT_BASELINE_SOURCE = {
    "name": "ccct_progress.jsonl",
    "sha256": "3125d5f83290136528e520a37fbbafc45f7f4ea57dbc7824e2203708a971e5c1",
    "byte_size": 5_234_494,
    "source_revision": "git:4ef50b1",
}
CCCT_BASELINE_METRICS = {
    "query_count": 22_654,
    "unique_hsk_count": 11_327,
    "trade_variant_count": 2,
    "source_requirement_count": 14_726,
    "document_count": 63,
}
HSK_BASELINE_SNAPSHOT_ID = "hsk-2026-01-01"
HSK_BASELINE_SOURCES = (
    {
        "name": "관세청_HS부호 단위별 품목명_20260101.xlsx",
        "sha256": "9a5a942b5e44a817bf3fb9ef6145fa23ef110a9692e4f3e0b84eb33672409ba2",
        "byte_size": 800_868,
        "source_revision": "source-date:2026-01-01",
    },
    {
        "name": "관세청_HS부호_20260101.xlsx",
        "sha256": "020661f75ac044be0eec049013b074be77ca8fb6d13f8283666f1f3e594054e5",
        "byte_size": 1_419_595,
        "source_revision": "source-date:2026-01-01",
    },
)
HSK_BASELINE_METRICS = {
    "hierarchy_node_count": 17_072,
    "unique_hierarchy_node_count": 17_072,
    "leaf_node_count": 11_327,
    "leaf_attribute_count": 11_327,
    "missing_leaf_attribute_count": 0,
}
_BASELINE_MEMBER_COUNTS = {
    "ccct_query": CCCT_BASELINE_METRICS["query_count"],
    "ccct_requirement": CCCT_BASELINE_METRICS["source_requirement_count"],
    "ccct_source_record": CCCT_BASELINE_METRICS["source_requirement_count"],
    "ccct_document": CCCT_BASELINE_METRICS["document_count"],
    "hsk_leaf": HSK_BASELINE_METRICS["leaf_node_count"],
}
_EVALUATION_BUNDLE_MARKER = object()
_EVALUATION_BUNDLE_MAX_FILE_BYTES = 64 * 1024 * 1024


class GenerationValidationError(ValueError):
    """A generation or its release evidence is not safe to use."""


class GenerationState(StrEnum):
    BUILDING = "building"
    ABORTED = "aborted"
    VALIDATED = "validated"
    SEALED = "sealed"
    ACTIVE = "active"
    RETIRED = "retired"


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise GenerationValidationError(f"{field} must be a lowercase SHA-256")
    return value


def _require_non_empty(value: object, field: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise GenerationValidationError(f"{field} must be a bounded non-empty string")
    return value


def _require_int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GenerationValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _require_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenerationValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GenerationValidationError(f"{field} must be finite")
    return result


def _parse_utc(value: object, field: str) -> str:
    raw = _require_non_empty(value, field, maximum=40)
    if not raw.endswith("Z"):
        raise GenerationValidationError(f"{field} must use canonical UTC Z notation")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise GenerationValidationError(f"{field} is not a valid timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise GenerationValidationError(f"{field} must be UTC")
    canonical = parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if raw != canonical:
        raise GenerationValidationError(f"{field} must use second-precision canonical UTC")
    return raw


def _parse_date(value: object, field: str) -> str:
    raw = _require_non_empty(value, field, maximum=10)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise GenerationValidationError(f"{field} is not a valid ISO date") from exc
    if parsed.isoformat() != raw:
        raise GenerationValidationError(f"{field} must use canonical ISO date notation")
    return raw


def _official_uri(value: object, field: str, allowed_hosts: frozenset[str]) -> str:
    raw = _require_non_empty(value, field, maximum=1024)
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or parsed.query
        or parsed.fragment
    ):
        raise GenerationValidationError(f"{field} must be a query-free official HTTPS source URI")
    return raw


def _json_safe(value: Any, field: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GenerationValidationError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise GenerationValidationError(f"{field} contains an invalid object key")
            normalized[key] = _json_safe(item, f"{field}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, f"{field}[]") for item in value]
    raise GenerationValidationError(f"{field} contains a non-JSON value")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON evidence with one deterministic byte representation."""

    normalized = _json_safe(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class LawSource:
    law_id: str
    law_name: str
    mst: str
    official_uri: str
    content_sha256: str
    current_article_count: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LawSource:
        allowed = {
            "law_id",
            "law_name",
            "mst",
            "official_uri",
            "content_sha256",
            "current_article_count",
        }
        if set(value) != allowed:
            raise GenerationValidationError("law source fields do not match schema")
        law_id = _require_non_empty(value["law_id"], "law_id", maximum=6)
        if _LAW_ID.fullmatch(law_id) is None:
            raise GenerationValidationError("law_id must contain six digits")
        expected_name = _EXPECTED_LAWS.get(law_id)
        law_name = _require_non_empty(value["law_name"], "law_name", maximum=300)
        if expected_name is None or law_name != expected_name:
            raise GenerationValidationError(f"law source is not the approved target: {law_id}")
        mst = _require_non_empty(value["mst"], "mst", maximum=20)
        if _MST.fullmatch(mst) is None:
            raise GenerationValidationError("mst must be a decimal law revision identifier")
        return cls(
            law_id=law_id,
            law_name=law_name,
            mst=mst,
            official_uri=_official_uri(value["official_uri"], "official_uri", _LAW_HOSTS),
            content_sha256=_require_sha256(value["content_sha256"], "content_sha256"),
            current_article_count=_require_int(
                value["current_article_count"], "current_article_count", minimum=1
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "law_id": self.law_id,
            "law_name": self.law_name,
            "mst": self.mst,
            "official_uri": self.official_uri,
            "content_sha256": self.content_sha256,
            "current_article_count": self.current_article_count,
        }


@dataclass(frozen=True, slots=True)
class SourceFile:
    name: str
    sha256: str
    byte_size: int
    source_revision: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SourceFile:
        if set(value) != {
            "name",
            "sha256",
            "byte_size",
            "source_revision",
        }:
            raise GenerationValidationError("source file fields do not match schema")
        name = _require_non_empty(value["name"], "source_file.name", maximum=300)
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise GenerationValidationError("source_file.name must be a basename")
        return cls(
            name=name,
            sha256=_require_sha256(value["sha256"], "source_file.sha256"),
            byte_size=_require_int(value["byte_size"], "source_file.byte_size", minimum=1),
            source_revision=_require_non_empty(
                value["source_revision"],
                "source_file.source_revision",
                maximum=200,
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "source_revision": self.source_revision,
        }


@dataclass(frozen=True, slots=True)
class FreshnessEvidence:
    status: str
    observed_at: str
    valid_through: str | None
    limitation: str | None
    approval_artifact_sha256: str
    release_eligible: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FreshnessEvidence:
        if set(value) != {
            "status",
            "observed_at",
            "valid_through",
            "limitation",
            "approval_artifact_sha256",
            "release_eligible",
        }:
            raise GenerationValidationError("freshness fields do not match schema")
        status = _require_non_empty(value["status"], "freshness.status", maximum=40)
        if status not in {"verified_current", "bounded_snapshot"}:
            raise GenerationValidationError("freshness.status is unsupported")
        valid_through = value["valid_through"]
        if valid_through is not None:
            valid_through = _parse_date(valid_through, "freshness.valid_through")
        limitation = value["limitation"]
        if limitation is not None:
            limitation = _require_non_empty(
                limitation,
                "freshness.limitation",
                maximum=1000,
            )
        if valid_through is None:
            raise GenerationValidationError("freshness requires an explicit valid_through date")
        if status == "bounded_snapshot" and limitation is None:
            raise GenerationValidationError(
                "bounded_snapshot freshness requires valid_through and limitation"
            )
        release_eligible = value["release_eligible"]
        if not isinstance(release_eligible, bool):
            raise GenerationValidationError("freshness.release_eligible must be boolean")
        return cls(
            status=status,
            observed_at=_parse_utc(value["observed_at"], "freshness.observed_at"),
            valid_through=valid_through,
            limitation=limitation,
            approval_artifact_sha256=_require_sha256(
                value["approval_artifact_sha256"],
                "freshness.approval_artifact_sha256",
            ),
            release_eligible=release_eligible,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "valid_through": self.valid_through,
            "limitation": self.limitation,
            "approval_artifact_sha256": self.approval_artifact_sha256,
            "release_eligible": self.release_eligible,
        }


@dataclass(frozen=True, slots=True)
class LawCorpusBinding:
    """Auditable binding to a locally verified official 58-law source root.

    A hash by itself is not an approval.  The build path must independently
    load and verify both the source root and the approval artifact whose hashes
    are carried here before any generation row is created.
    """

    official_manifest_sha256: str
    source_root_sha256: str
    target_law_manifest_sha256: str
    collector_revision: str
    collected_at: str
    collection_as_of: str
    approval_artifact_sha256: str
    approved_at: str
    valid_through: str
    schema_version: str = "1.0"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> LawCorpusBinding:
        required = {
            "schema_version",
            "official_manifest_sha256",
            "source_root_sha256",
            "target_law_manifest_sha256",
            "collector_revision",
            "collected_at",
            "collection_as_of",
            "approval_artifact_sha256",
            "approved_at",
            "valid_through",
        }
        if set(value) != required or value["schema_version"] != "1.0":
            raise GenerationValidationError("law corpus binding fields do not match schema")
        collector_revision = _require_non_empty(
            value["collector_revision"],
            "law_corpus.collector_revision",
            maximum=40,
        )
        if _FULL_COMMIT.fullmatch(collector_revision) is None:
            raise GenerationValidationError("law corpus collector revision must be a full commit")
        collected_at = _parse_utc(value["collected_at"], "law_corpus.collected_at")
        approved_at = _parse_utc(value["approved_at"], "law_corpus.approved_at")
        collection_as_of = _parse_date(
            value["collection_as_of"],
            "law_corpus.collection_as_of",
        )
        valid_through = _parse_date(value["valid_through"], "law_corpus.valid_through")
        if approved_at < collected_at:
            raise GenerationValidationError("law corpus approval predates collection")
        if valid_through < collection_as_of:
            raise GenerationValidationError("law corpus validity predates collection")
        return cls(
            official_manifest_sha256=_require_sha256(
                value["official_manifest_sha256"],
                "law_corpus.official_manifest_sha256",
            ),
            source_root_sha256=_require_sha256(
                value["source_root_sha256"],
                "law_corpus.source_root_sha256",
            ),
            target_law_manifest_sha256=_require_sha256(
                value["target_law_manifest_sha256"],
                "law_corpus.target_law_manifest_sha256",
            ),
            collector_revision=collector_revision,
            collected_at=collected_at,
            collection_as_of=collection_as_of,
            approval_artifact_sha256=_require_sha256(
                value["approval_artifact_sha256"],
                "law_corpus.approval_artifact_sha256",
            ),
            approved_at=approved_at,
            valid_through=valid_through,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "official_manifest_sha256": self.official_manifest_sha256,
            "source_root_sha256": self.source_root_sha256,
            "target_law_manifest_sha256": self.target_law_manifest_sha256,
            "collector_revision": self.collector_revision,
            "collected_at": self.collected_at,
            "collection_as_of": self.collection_as_of,
            "approval_artifact_sha256": self.approval_artifact_sha256,
            "approved_at": self.approved_at,
            "valid_through": self.valid_through,
        }


@dataclass(frozen=True, slots=True)
class DatasetSource:
    dataset_id: str
    snapshot_id: str
    official_uri: str
    source_files: tuple[SourceFile, ...]
    metrics: Mapping[str, int]
    freshness: FreshnessEvidence

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DatasetSource:
        if set(value) != {
            "dataset_id",
            "snapshot_id",
            "official_uri",
            "source_files",
            "metrics",
            "freshness",
        }:
            raise GenerationValidationError("dataset source fields do not match schema")
        dataset_id = _require_non_empty(value["dataset_id"], "dataset_id", maximum=20)
        if dataset_id not in _DATASET_IDS:
            raise GenerationValidationError(f"unsupported dataset_id: {dataset_id}")
        raw_files = value["source_files"]
        if not isinstance(raw_files, list):
            raise GenerationValidationError("dataset source_files must be an array")
        source_files = tuple(SourceFile.from_mapping(item) for item in raw_files)
        names = [item.name for item in source_files]
        if not source_files or names != sorted(names) or len(names) != len(set(names)):
            raise GenerationValidationError("source_files must be unique and sorted by name")
        raw_metrics = value["metrics"]
        if not isinstance(raw_metrics, Mapping) or not raw_metrics:
            raise GenerationValidationError("dataset metrics must be a non-empty object")
        metrics: dict[str, int] = {}
        for key, metric in raw_metrics.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise GenerationValidationError("dataset metric name is invalid")
            metrics[key] = _require_int(metric, f"metrics.{key}")
        item = cls(
            dataset_id=dataset_id,
            snapshot_id=_require_non_empty(value["snapshot_id"], "snapshot_id", maximum=200),
            official_uri=_official_uri(
                value["official_uri"],
                "official_uri",
                _CUSTOMS_HOSTS,
            ),
            source_files=source_files,
            metrics=metrics,
            freshness=FreshnessEvidence.from_mapping(value["freshness"]),
        )
        item._validate_pinned_baseline()
        return item

    def _validate_pinned_baseline(self) -> None:
        if self.dataset_id == "ccct":
            if self.snapshot_id != CCCT_BASELINE_SNAPSHOT_ID:
                raise GenerationValidationError("CCCT snapshot is not the pinned release baseline")
            expected_files = (CCCT_BASELINE_SOURCE,)
            if tuple(item.as_dict() for item in self.source_files) != expected_files:
                raise GenerationValidationError("CCCT baseline source hash/size/revision mismatch")
            if dict(self.metrics) != CCCT_BASELINE_METRICS:
                raise GenerationValidationError("CCCT baseline derived counts mismatch")
        if self.dataset_id == "hsk":
            if self.snapshot_id != HSK_BASELINE_SNAPSHOT_ID:
                raise GenerationValidationError("HSK snapshot is not the pinned release baseline")
            expected_files = tuple(sorted(HSK_BASELINE_SOURCES, key=lambda item: item["name"]))
            if tuple(item.as_dict() for item in self.source_files) != expected_files:
                raise GenerationValidationError("HSK baseline source hashes/sizes mismatch")
            if dict(self.metrics) != HSK_BASELINE_METRICS:
                raise GenerationValidationError("HSK baseline parser counts mismatch")

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "official_uri": self.official_uri,
            "source_files": [item.as_dict() for item in self.source_files],
            "metrics": dict(self.metrics),
            "freshness": self.freshness.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class DatasetMember:
    """One immutable generation-scoped membership row."""

    dataset_id: str
    snapshot_id: str
    member_kind: str
    member_key: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.dataset_id not in _DATASET_IDS:
            raise GenerationValidationError("dataset member dataset_id is invalid")
        if self.member_kind not in _DATASET_MEMBER_KINDS:
            raise GenerationValidationError("dataset member kind is invalid")
        if self.member_kind.startswith("ccct_") != (self.dataset_id == "ccct"):
            raise GenerationValidationError("dataset member kind does not match dataset")
        _require_non_empty(self.snapshot_id, "dataset_member.snapshot_id", maximum=200)
        _require_non_empty(self.member_key, "dataset_member.member_key", maximum=500)
        _require_sha256(self.payload_sha256, "dataset_member.payload_sha256")

    def as_dict(self) -> dict[str, str]:
        return {
            "dataset_id": self.dataset_id,
            "snapshot_id": self.snapshot_id,
            "member_kind": self.member_kind,
            "member_key": self.member_key,
            "payload_sha256": self.payload_sha256,
        }


def validate_dataset_members(
    manifest: SourceManifest,
    members: Iterable[DatasetMember],
) -> tuple[DatasetMember, ...]:
    """Validate exact pinned CCCT/HSK membership before PostgreSQL insertion."""

    normalized = tuple(
        sorted(
            members,
            key=lambda item: (
                item.dataset_id,
                item.member_kind,
                item.member_key,
            ),
        )
    )
    keys = [(item.dataset_id, item.member_kind, item.member_key) for item in normalized]
    if len(keys) != len(set(keys)):
        raise GenerationValidationError("dataset membership contains duplicate keys")
    snapshot_by_dataset = {item.dataset_id: item.snapshot_id for item in manifest.datasets}
    if any(item.snapshot_id != snapshot_by_dataset.get(item.dataset_id) for item in normalized):
        raise GenerationValidationError("dataset membership snapshot does not match manifest")
    counts = {kind: 0 for kind in _BASELINE_MEMBER_COUNTS}
    for item in normalized:
        counts[item.member_kind] += 1
    if counts != _BASELINE_MEMBER_COUNTS:
        raise GenerationValidationError(
            f"dataset membership counts do not match pinned baselines: {counts}"
        )
    return normalized


@dataclass(frozen=True, slots=True)
class SourceManifest:
    generated_at: str
    law_corpus: LawCorpusBinding
    laws: tuple[LawSource, ...]
    datasets: tuple[DatasetSource, ...]
    schema_version: str = "1.0"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SourceManifest:
        if set(value) != {
            "schema_version",
            "generated_at",
            "law_corpus",
            "laws",
            "datasets",
        }:
            raise GenerationValidationError("source manifest fields do not match schema")
        if value["schema_version"] != "1.0":
            raise GenerationValidationError("unsupported source manifest schema")
        raw_laws = value["laws"]
        raw_datasets = value["datasets"]
        if not isinstance(raw_laws, list) or not isinstance(raw_datasets, list):
            raise GenerationValidationError("source manifest arrays are required")
        laws = tuple(LawSource.from_mapping(item) for item in raw_laws)
        datasets = tuple(DatasetSource.from_mapping(item) for item in raw_datasets)
        law_ids = [item.law_id for item in laws]
        dataset_ids = [item.dataset_id for item in datasets]
        if len(laws) != len(_EXPECTED_LAWS) or set(law_ids) != set(_EXPECTED_LAWS):
            raise GenerationValidationError(
                f"source manifest must contain all {len(_EXPECTED_LAWS)} target laws"
            )
        if law_ids != sorted(law_ids) or len(law_ids) != len(set(law_ids)):
            raise GenerationValidationError("law sources must be unique and sorted by law_id")
        if set(dataset_ids) != _DATASET_IDS or dataset_ids != sorted(dataset_ids):
            raise GenerationValidationError("datasets must contain sorted ccct and hsk entries")
        return cls(
            generated_at=_parse_utc(value["generated_at"], "generated_at"),
            law_corpus=LawCorpusBinding.from_mapping(value["law_corpus"]),
            laws=laws,
            datasets=datasets,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "law_corpus": self.law_corpus.as_dict(),
            "laws": [item.as_dict() for item in self.laws],
            "datasets": [item.as_dict() for item in self.datasets],
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def require_release_eligible_sources(self, *, as_of: str | None = None) -> None:
        blocked = [item.dataset_id for item in self.datasets if not item.freshness.release_eligible]
        if blocked:
            raise GenerationValidationError(
                "source freshness is not release-eligible: " + ",".join(blocked)
            )
        reference_date = _parse_date(
            as_of or self.generated_at[:10],
            "release_reference_date",
        )
        if self.law_corpus.valid_through < reference_date:
            raise GenerationValidationError("official law corpus approval has expired")
        expired = [
            item.dataset_id
            for item in self.datasets
            if item.freshness.valid_through is not None
            and item.freshness.valid_through < reference_date
        ]
        if expired:
            raise GenerationValidationError("dataset freshness has expired: " + ",".join(expired))


def release_model_manifest() -> dict[str, Any]:
    """Return the pinned model inventory plus immutable sparse-index identity."""

    sparse_config = {
        "algorithm": "qdrant-bm25-idf",
        "purpose": "sparse-retrieval",
        "revision": "qdrant-native-bm25-idf-v1",
    }
    return {
        "schema_version": "1.0",
        "models": release_model_inventory(),
        "sparse": sparse_config,
    }


def validate_model_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_safe(value, "model_manifest")
    if not isinstance(normalized, dict) or set(normalized) != {
        "schema_version",
        "models",
        "sparse",
    }:
        raise GenerationValidationError("model manifest fields do not match schema")
    if normalized["schema_version"] != "1.0":
        raise GenerationValidationError("unsupported model manifest schema")
    models = normalized["models"]
    if not isinstance(models, dict) or set(models) != {"dense", "reranker"}:
        raise GenerationValidationError("dense and reranker model identities are required")
    for key in ("dense", "reranker"):
        identity = models[key]
        if not isinstance(identity, dict) or set(identity) != {
            "model_id",
            "purpose",
            "revision",
        }:
            raise GenerationValidationError(f"{key} model identity is invalid")
        _require_non_empty(identity["model_id"], f"{key}.model_id")
        _require_non_empty(identity["purpose"], f"{key}.purpose")
        revision = _require_non_empty(identity["revision"], f"{key}.revision", maximum=40)
        if _FULL_COMMIT.fullmatch(revision) is None:
            raise GenerationValidationError(f"{key} model revision must be a full commit")
    sparse = normalized["sparse"]
    if not isinstance(sparse, dict) or set(sparse) != {"algorithm", "purpose", "revision"}:
        raise GenerationValidationError("sparse model identity is invalid")
    for key in sparse:
        _require_non_empty(sparse[key], f"sparse.{key}")
    return normalized


def validate_index_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the only index layout supported by release schema 1.0.

    Arbitrary JSON used to be accepted and was then hashed into a generation
    identity even though neither Qdrant nor Neo4j proved that it was applied.
    The strict schema makes every accepted field enforceable by store adapters.
    """

    normalized = _json_safe(value, "index_config")
    if not isinstance(normalized, dict) or set(normalized) != _INDEX_CONFIG_FIELDS:
        raise GenerationValidationError("index_config fields do not match schema 1.0")
    expected_literals = {
        "schema_version": "1.0",
        "dense_vector_name": "dense",
        "distance": "cosine",
        "sparse_vector_name": "bm25",
        "sparse_modifier": "idf",
        "qdrant_collection_mode": "generation-isolated",
        "neo4j_projection_mode": "generation-tagged",
    }
    for field, expected in expected_literals.items():
        if normalized[field] != expected:
            raise GenerationValidationError(f"index_config.{field} is unsupported")
    dense_size = _require_int(
        normalized["dense_vector_size"],
        "index_config.dense_vector_size",
        minimum=1,
    )
    if dense_size > 16_384:
        raise GenerationValidationError("index_config.dense_vector_size is too large")
    normalized["dense_vector_size"] = dense_size
    return normalized


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    source_manifest: SourceManifest
    model_manifest: Mapping[str, Any]
    index_config: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_manifest", validate_model_manifest(self.model_manifest))
        object.__setattr__(self, "index_config", validate_index_config(self.index_config))

    @property
    def source_manifest_sha256(self) -> str:
        return self.source_manifest.sha256

    @property
    def model_manifest_sha256(self) -> str:
        return canonical_sha256(self.model_manifest)

    @property
    def index_config_sha256(self) -> str:
        return canonical_sha256(self.index_config)

    @property
    def generation_id(self) -> str:
        return canonical_sha256(
            {
                "schema_version": "1.0",
                "source_manifest_sha256": self.source_manifest_sha256,
                "model_manifest_sha256": self.model_manifest_sha256,
                "index_config_sha256": self.index_config_sha256,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "source_manifest": self.source_manifest.as_dict(),
            "source_manifest_sha256": self.source_manifest_sha256,
            "model_manifest": self.model_manifest,
            "model_manifest_sha256": self.model_manifest_sha256,
            "index_config": self.index_config,
            "index_config_sha256": self.index_config_sha256,
        }


@dataclass(frozen=True, slots=True)
class StoreAttestation:
    generation_id: str
    store_name: str
    article_count: int
    article_id_set_sha256: str
    article_content_set_sha256: str
    projection_sha256: str
    model_manifest_sha256: str
    store_locator: str
    store_revision: str
    attested_at: str

    def __post_init__(self) -> None:
        _require_sha256(self.generation_id, "generation_id")
        if self.store_name not in _STORE_NAMES:
            raise GenerationValidationError(f"unsupported store_name: {self.store_name}")
        _require_int(self.article_count, "article_count", minimum=1)
        _require_sha256(self.article_id_set_sha256, "article_id_set_sha256")
        _require_sha256(self.article_content_set_sha256, "article_content_set_sha256")
        _require_sha256(self.projection_sha256, "projection_sha256")
        _require_sha256(self.model_manifest_sha256, "model_manifest_sha256")
        if (
            not isinstance(self.store_locator, str)
            or _SAFE_LOCATOR.fullmatch(self.store_locator) is None
            or "://" in self.store_locator
        ):
            raise GenerationValidationError("store_locator must not contain a connection URI")
        _require_non_empty(self.store_revision, "store_revision", maximum=256)
        _parse_utc(self.attested_at, "attested_at")

    @classmethod
    def from_article_records(
        cls,
        *,
        generation_id: str,
        store_name: str,
        records: Iterable[tuple[int | str, str]],
        model_manifest_sha256: str,
        store_locator: str,
        store_revision: str,
        attested_at: str,
        projection: Any | None = None,
    ) -> StoreAttestation:
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for article_id, content_sha256 in records:
            if (
                isinstance(article_id, bool)
                or not isinstance(article_id, (int, str))
                or (isinstance(article_id, int) and article_id <= 0)
                or (isinstance(article_id, str) and not article_id.strip())
            ):
                raise GenerationValidationError("article IDs must be positive or non-empty")
            article_key = str(article_id)
            if not article_key or article_key in seen:
                raise GenerationValidationError("article IDs must be unique and non-empty")
            seen.add(article_key)
            normalized.append(
                (article_key, _require_sha256(content_sha256, "article content hash"))
            )
        normalized.sort()
        if not normalized:
            raise GenerationValidationError("store attestation cannot be empty")
        canonical_membership = [
            {"article_id": article_id, "content_sha256": content_hash}
            for article_id, content_hash in normalized
        ]
        projection_value = (
            {"schema_version": "1.0", "article_membership": canonical_membership}
            if projection is None
            else projection
        )
        return cls(
            generation_id=generation_id,
            store_name=store_name,
            article_count=len(normalized),
            article_id_set_sha256=canonical_sha256([item[0] for item in normalized]),
            article_content_set_sha256=canonical_sha256(canonical_membership),
            projection_sha256=canonical_sha256(projection_value),
            model_manifest_sha256=model_manifest_sha256,
            store_locator=store_locator,
            store_revision=store_revision,
            attested_at=attested_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generation_id": self.generation_id,
            "store_name": self.store_name,
            "article_count": self.article_count,
            "article_id_set_sha256": self.article_id_set_sha256,
            "article_content_set_sha256": self.article_content_set_sha256,
            "projection_sha256": self.projection_sha256,
            "model_manifest_sha256": self.model_manifest_sha256,
            "store_locator": self.store_locator,
            "store_revision": self.store_revision,
            "attested_at": self.attested_at,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    @property
    def sealed_projection_sha256(self) -> str:
        """Stable store identity used by the generation seal.

        ``attested_at`` proves when an observation happened and therefore
        changes on every mandatory pre-promotion re-attestation.  The sealed
        projection excludes that observation time while binding every
        immutable projection and store-configuration field.
        """

        return canonical_sha256(
            {
                "schema_version": "1.0",
                "generation_id": self.generation_id,
                "store_name": self.store_name,
                "article_count": self.article_count,
                "article_id_set_sha256": self.article_id_set_sha256,
                "article_content_set_sha256": self.article_content_set_sha256,
                "projection_sha256": self.projection_sha256,
                "model_manifest_sha256": self.model_manifest_sha256,
                "store_locator": self.store_locator,
                "store_revision": self.store_revision,
            }
        )


def validate_store_attestations(
    generation_id: str,
    model_manifest_sha256: str,
    attestations: Sequence[StoreAttestation],
) -> tuple[StoreAttestation, ...]:
    by_store = {item.store_name: item for item in attestations}
    if len(by_store) != len(attestations) or set(by_store) != _STORE_NAMES:
        raise GenerationValidationError("exactly one attestation per required store is required")
    ordered = tuple(by_store[name] for name in sorted(by_store))
    for item in ordered:
        if item.generation_id != generation_id:
            raise GenerationValidationError("store generation IDs do not match")
        if item.model_manifest_sha256 != model_manifest_sha256:
            raise GenerationValidationError("store model manifests do not match generation")
    baseline = ordered[0]
    for item in ordered[1:]:
        if (
            item.article_count != baseline.article_count
            or item.article_id_set_sha256 != baseline.article_id_set_sha256
            or item.article_content_set_sha256 != baseline.article_content_set_sha256
        ):
            raise GenerationValidationError("cross-store article attestations do not match")
    return ordered


def validate_rag_evaluation_report(
    value: Mapping[str, Any],
    *,
    generation_id: str,
) -> tuple[dict[str, Any], str]:
    """Validate per-case Production RAG evidence and recompute every aggregate.

    Release schema 2.0 intentionally rejects a metrics-only report.  An
    immutable case manifest binds the runner, Gold, queries, code, exact case
    IDs and reviewer approval.  Aggregate values are accepted only when they
    are the exact result of the sealed case records.
    """

    report = _json_safe(value, "evaluation_report")
    required = {
        "schema_version",
        "generation_id",
        "run_id",
        "generated_at",
        "production_data",
        "mock_count",
        "unexpected_skips",
        "artifacts",
        "gold",
        "cases",
        "structured",
        "hybrid",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise GenerationValidationError("evaluation report fields do not match schema")
    if report["schema_version"] != "2.0" or report["generation_id"] != generation_id:
        raise GenerationValidationError("evaluation report identity does not match generation")
    _require_non_empty(report["run_id"], "run_id", maximum=200)
    _parse_utc(report["generated_at"], "generated_at")
    if report["production_data"] is not True:
        raise GenerationValidationError("evaluation must use production data")
    if _require_int(report["mock_count"], "mock_count") != 0:
        raise GenerationValidationError("mock retrieval is forbidden in release evaluation")
    if _require_int(report["unexpected_skips"], "unexpected_skips") != 0:
        raise GenerationValidationError("release evaluation cannot contain skips")

    artifacts = report["artifacts"]
    artifact_fields = {
        "runner_sha256",
        "gold_sha256",
        "query_manifest_sha256",
        "code_sha256",
        "reviewer_approval_sha256",
        "case_manifest_sha256",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != artifact_fields:
        raise GenerationValidationError("evaluation artifact fields do not match schema")
    for key in sorted(artifact_fields):
        _require_sha256(artifacts[key], f"artifacts.{key}")

    gold = report["gold"]
    gold_required = {
        "dataset_sha256",
        "human_reviewed",
        "reviewer_id",
        "reviewed_at",
        "law_coverage",
        "positive_cases",
        "negative_cases",
        "ambiguous_cases",
    }
    if not isinstance(gold, dict) or set(gold) != gold_required:
        raise GenerationValidationError("Gold review fields do not match schema")
    dataset_hash = _require_sha256(gold["dataset_sha256"], "gold.dataset_sha256")
    if dataset_hash != artifacts["gold_sha256"]:
        raise GenerationValidationError("Gold dataset and artifact hashes do not match")
    if gold["human_reviewed"] is not True:
        raise GenerationValidationError("human-reviewed Gold is required to seal")
    _require_non_empty(gold["reviewer_id"], "gold.reviewer_id", maximum=200)
    _parse_utc(gold["reviewed_at"], "gold.reviewed_at")
    law_coverage = gold["law_coverage"]
    if not isinstance(law_coverage, dict) or set(law_coverage) != set(_EXPECTED_LAWS):
        raise GenerationValidationError("Gold must cover all target laws")
    normalized_coverage = {
        law_id: _require_int(count, f"gold.law_coverage.{law_id}", minimum=1)
        for law_id, count in law_coverage.items()
    }

    raw_cases = report["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise GenerationValidationError("evaluation cases must be a non-empty array")
    case_ids: list[str] = []
    observed_coverage = {law_id: 0 for law_id in _EXPECTED_LAWS}
    categories = {"positive": 0, "negative": 0, "ambiguous": 0}
    structured_cases: list[dict[str, Any]] = []
    hybrid_cases: list[dict[str, Any]] = []
    common_fields = {"case_id", "suite", "law_ids", "category", "passed"}
    structured_fields = common_fields | {
        "contract_valid",
        "hs_resolution_correct",
        "requirements_found_correct",
        "required_evidence_recall",
        "evidence_metadata_complete",
        "false_positive",
    }
    hybrid_fields = common_fields | {
        "contract_valid",
        "hit_at_5",
        "recall_at_10",
        "ndcg_at_10",
        "citation_metadata_complete",
    }
    for index, case in enumerate(raw_cases):
        if not isinstance(case, dict):
            raise GenerationValidationError("evaluation case must be an object")
        suite = case.get("suite")
        expected_fields = structured_fields if suite == "structured" else hybrid_fields
        if suite not in {"structured", "hybrid"} or set(case) != expected_fields:
            raise GenerationValidationError("evaluation case fields do not match suite schema")
        case_id = _require_non_empty(case["case_id"], f"cases[{index}].case_id", maximum=200)
        case_ids.append(case_id)
        law_ids = case["law_ids"]
        if (
            not isinstance(law_ids, list)
            or not law_ids
            or law_ids != sorted(law_ids)
            or len(law_ids) != len(set(law_ids))
            or any(law_id not in _EXPECTED_LAWS for law_id in law_ids)
        ):
            raise GenerationValidationError("case law_ids must be unique sorted target laws")
        for law_id in law_ids:
            observed_coverage[law_id] += 1
        category = case["category"]
        if category not in categories:
            raise GenerationValidationError("case category is unsupported")
        categories[category] += 1
        for field in ("passed", "contract_valid"):
            if not isinstance(case[field], bool):
                raise GenerationValidationError(f"cases[{index}].{field} must be boolean")
        if suite == "structured":
            for field in (
                "hs_resolution_correct",
                "requirements_found_correct",
                "evidence_metadata_complete",
                "false_positive",
            ):
                if not isinstance(case[field], bool):
                    raise GenerationValidationError(f"cases[{index}].{field} must be boolean")
            recall = _require_number(
                case["required_evidence_recall"],
                f"cases[{index}].required_evidence_recall",
            )
            if not 0.0 <= recall <= 1.0:
                raise GenerationValidationError("structured recall is outside [0,1]")
            structured_cases.append(case)
        else:
            for field in ("hit_at_5", "recall_at_10", "ndcg_at_10"):
                metric = _require_number(case[field], f"cases[{index}].{field}")
                if not 0.0 <= metric <= 1.0:
                    raise GenerationValidationError(f"hybrid {field} is outside [0,1]")
            if not isinstance(case["citation_metadata_complete"], bool):
                raise GenerationValidationError(
                    f"cases[{index}].citation_metadata_complete must be boolean"
                )
            hybrid_cases.append(case)

    if case_ids != sorted(case_ids) or len(case_ids) != len(set(case_ids)):
        raise GenerationValidationError("evaluation case IDs must be unique and sorted")
    if observed_coverage != normalized_coverage:
        raise GenerationValidationError("Gold law coverage does not match case records")
    if canonical_sha256(raw_cases) != artifacts["case_manifest_sha256"]:
        raise GenerationValidationError("case manifest hash does not match case records")
    for category, observed in categories.items():
        if _require_int(gold[f"{category}_cases"], f"gold.{category}_cases") != observed:
            raise GenerationValidationError("Gold category counts do not match case records")
    if categories["positive"] < len(_EXPECTED_LAWS):
        raise GenerationValidationError("Gold requires at least one positive case per target law")
    if categories["negative"] < 1 or categories["ambiguous"] < 1:
        raise GenerationValidationError("Gold requires negative and ambiguous cases")

    def _mean(items: Sequence[dict[str, Any]], field: str) -> float:
        return math.fsum(float(item[field]) for item in items) / len(items)

    structured = report["structured"]
    structured_required = {
        "cases_total",
        "cases_passed",
        "case_pass_rate",
        "contract_valid_rate",
        "hs_resolution_accuracy",
        "requirements_found_accuracy",
        "required_evidence_recall",
        "evidence_metadata_completeness",
        "no_requirement_false_positive_rate",
    }
    if (
        not isinstance(structured, dict)
        or set(structured) != structured_required
        or len(structured_cases) < 5
    ):
        raise GenerationValidationError("structured evaluation fields do not match schema")
    recomputed_structured = {
        "cases_total": len(structured_cases),
        "cases_passed": sum(bool(item["passed"]) for item in structured_cases),
        "case_pass_rate": _mean(structured_cases, "passed"),
        "contract_valid_rate": _mean(structured_cases, "contract_valid"),
        "hs_resolution_accuracy": _mean(structured_cases, "hs_resolution_correct"),
        "requirements_found_accuracy": _mean(
            structured_cases,
            "requirements_found_correct",
        ),
        "required_evidence_recall": _mean(
            structured_cases,
            "required_evidence_recall",
        ),
        "evidence_metadata_completeness": _mean(
            structured_cases,
            "evidence_metadata_complete",
        ),
        "no_requirement_false_positive_rate": _mean(
            structured_cases,
            "false_positive",
        ),
    }
    for field, expected in recomputed_structured.items():
        if _require_number(structured[field], f"structured.{field}") != expected:
            raise GenerationValidationError(f"structured.{field} does not match case records")
    for metric in (
        "case_pass_rate",
        "contract_valid_rate",
        "hs_resolution_accuracy",
        "requirements_found_accuracy",
        "required_evidence_recall",
        "evidence_metadata_completeness",
    ):
        if recomputed_structured[metric] != 1.0:
            raise GenerationValidationError(f"structured.{metric} must equal 1.0")
    if recomputed_structured["no_requirement_false_positive_rate"] != 0.0:
        raise GenerationValidationError("structured false-positive rate must equal zero")

    hybrid = report["hybrid"]
    hybrid_required = {
        "cases_total",
        "cases_passed",
        "contract_valid_rate",
        "hit_at_5",
        "recall_at_10",
        "ndcg_at_10",
        "citation_metadata_completeness",
    }
    if (
        not isinstance(hybrid, dict)
        or set(hybrid) != hybrid_required
        or len(hybrid_cases) < len(_EXPECTED_LAWS)
    ):
        raise GenerationValidationError("hybrid evaluation fields do not match schema")
    recomputed_hybrid = {
        "cases_total": len(hybrid_cases),
        "cases_passed": sum(bool(item["passed"]) for item in hybrid_cases),
        "contract_valid_rate": _mean(hybrid_cases, "contract_valid"),
        "hit_at_5": _mean(hybrid_cases, "hit_at_5"),
        "recall_at_10": _mean(hybrid_cases, "recall_at_10"),
        "ndcg_at_10": _mean(hybrid_cases, "ndcg_at_10"),
        "citation_metadata_completeness": _mean(
            hybrid_cases,
            "citation_metadata_complete",
        ),
    }
    for field, expected in recomputed_hybrid.items():
        if _require_number(hybrid[field], f"hybrid.{field}") != expected:
            raise GenerationValidationError(f"hybrid.{field} does not match case records")
    for metric in (
        "contract_valid_rate",
        "hit_at_5",
        "recall_at_10",
        "citation_metadata_completeness",
    ):
        if recomputed_hybrid[metric] != 1.0:
            raise GenerationValidationError(f"hybrid.{metric} must equal 1.0")
    if recomputed_hybrid["ndcg_at_10"] < 0.8:
        raise GenerationValidationError("hybrid.ndcg_at_10 must be at least 0.8")
    return report, canonical_sha256(report)


def evaluation_review_subject_sha256(report: Mapping[str, Any]) -> str:
    """Hash the complete evaluation subject without its non-circular approval hash."""

    normalized = _json_safe(report, "evaluation_report")
    if not isinstance(normalized, dict):
        raise GenerationValidationError("evaluation report must be an object")
    try:
        artifacts = dict(normalized["artifacts"])
        artifacts.pop("reviewer_approval_sha256")
    except (KeyError, TypeError):
        raise GenerationValidationError("evaluation artifact manifest is invalid") from None
    normalized = dict(normalized)
    normalized["artifacts"] = artifacts
    return canonical_sha256(normalized)


def _strict_json_document(data: bytes, field: str) -> Any:
    if not data or len(data) > _EVALUATION_BUNDLE_MAX_FILE_BYTES:
        raise GenerationValidationError(f"{field} size is invalid")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError):
        raise GenerationValidationError(f"{field} is not strict JSON") from None


def _bundle_file(root: Path, filename: str) -> bytes:
    candidate = root / filename
    if candidate.is_symlink():
        raise GenerationValidationError("evaluation bundle path is invalid")
    try:
        path = candidate.resolve(strict=True)
    except OSError:
        raise GenerationValidationError("evaluation bundle file is unavailable") from None
    if path.parent != root or path.is_symlink() or not path.is_file():
        raise GenerationValidationError("evaluation bundle path is invalid")
    try:
        size = path.stat().st_size
        if size <= 0 or size > _EVALUATION_BUNDLE_MAX_FILE_BYTES:
            raise GenerationValidationError("evaluation bundle file size is invalid")
        data = path.read_bytes()
    except OSError:
        raise GenerationValidationError("evaluation bundle file is unavailable") from None
    if len(data) != size:
        raise GenerationValidationError("evaluation bundle file changed during read")
    return data


@dataclass(frozen=True, slots=True)
class VerifiedEvaluationBundle:
    """Opaque proof that evaluation artifacts were read and cross-verified."""

    report: Mapping[str, Any]
    report_sha256: str
    approval_sha256: str
    _marker: object

    def __post_init__(self) -> None:
        if self._marker is not _EVALUATION_BUNDLE_MARKER:
            raise GenerationValidationError(
                "VerifiedEvaluationBundle must be created by the bundle loader"
            )
        _require_sha256(self.report_sha256, "evaluation_report_sha256")
        _require_sha256(self.approval_sha256, "evaluation_approval_sha256")


def load_verified_evaluation_bundle(
    root: Path,
    *,
    generation_id: str,
) -> VerifiedEvaluationBundle:
    """Load conventional immutable artifact files and verify all bindings.

    Required files are ``report.json``, ``runner.artifact``, ``gold.artifact``,
    ``queries.artifact``, ``code.artifact``, ``cases.json`` and
    ``approval.json``.  Only hashes and bounded strict JSON are exposed; raw
    artifact bytes never enter the generation row.
    """

    if root.is_symlink():
        raise GenerationValidationError("evaluation bundle root is invalid")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise GenerationValidationError("evaluation bundle root is unavailable") from None
    if not resolved_root.is_dir():
        raise GenerationValidationError("evaluation bundle root is invalid")
    expected_filenames = {
        "report.json",
        "runner.artifact",
        "gold.artifact",
        "queries.artifact",
        "code.artifact",
        "cases.json",
        "approval.json",
    }
    try:
        observed_filenames = {item.name for item in resolved_root.iterdir()}
    except OSError:
        raise GenerationValidationError("evaluation bundle root is unavailable") from None
    if observed_filenames != expected_filenames:
        raise GenerationValidationError("evaluation bundle file inventory does not match schema")
    report_bytes = _bundle_file(resolved_root, "report.json")
    runner_bytes = _bundle_file(resolved_root, "runner.artifact")
    gold_bytes = _bundle_file(resolved_root, "gold.artifact")
    query_bytes = _bundle_file(resolved_root, "queries.artifact")
    code_bytes = _bundle_file(resolved_root, "code.artifact")
    cases_bytes = _bundle_file(resolved_root, "cases.json")
    approval_bytes = _bundle_file(resolved_root, "approval.json")

    report_value = _strict_json_document(report_bytes, "report.json")
    if not isinstance(report_value, Mapping):
        raise GenerationValidationError("report.json must contain an object")
    report, report_sha256 = validate_rag_evaluation_report(
        report_value,
        generation_id=generation_id,
    )
    cases = _strict_json_document(cases_bytes, "cases.json")
    approval = _strict_json_document(approval_bytes, "approval.json")
    if cases != report["cases"]:
        raise GenerationValidationError("case artifact does not match evaluation report")
    artifacts = report["artifacts"]
    observed_hashes = {
        "runner_sha256": hashlib.sha256(runner_bytes).hexdigest(),
        "gold_sha256": hashlib.sha256(gold_bytes).hexdigest(),
        "query_manifest_sha256": hashlib.sha256(query_bytes).hexdigest(),
        "code_sha256": hashlib.sha256(code_bytes).hexdigest(),
        "case_manifest_sha256": canonical_sha256(cases),
        "reviewer_approval_sha256": hashlib.sha256(approval_bytes).hexdigest(),
    }
    if artifacts != observed_hashes:
        raise GenerationValidationError("evaluation artifact bytes do not match manifest")
    approval_fields = {
        "schema_version",
        "purpose",
        "generation_id",
        "evaluation_subject_sha256",
        "gold_sha256",
        "case_manifest_sha256",
        "reviewer_id",
        "approved_at",
    }
    if not isinstance(approval, dict) or set(approval) != approval_fields:
        raise GenerationValidationError("evaluation approval fields do not match schema")
    expected_approval = {
        "schema_version": "1.0",
        "purpose": "production-rag-generation-seal",
        "generation_id": generation_id,
        "evaluation_subject_sha256": evaluation_review_subject_sha256(report),
        "gold_sha256": artifacts["gold_sha256"],
        "case_manifest_sha256": artifacts["case_manifest_sha256"],
        "reviewer_id": report["gold"]["reviewer_id"],
        "approved_at": report["gold"]["reviewed_at"],
    }
    if approval != expected_approval:
        raise GenerationValidationError("evaluation approval does not bind exact artifacts")
    return VerifiedEvaluationBundle(
        report=report,
        report_sha256=report_sha256,
        approval_sha256=observed_hashes["reviewer_approval_sha256"],
        _marker=_EVALUATION_BUNDLE_MARKER,
    )


def build_seal_attestation(
    spec: GenerationSpec,
    attestations: Sequence[StoreAttestation],
    evaluation_report_sha256: str,
) -> tuple[dict[str, Any], str]:
    ordered = validate_store_attestations(
        spec.generation_id,
        spec.model_manifest_sha256,
        attestations,
    )
    payload = {
        "schema_version": "1.0",
        "generation_id": spec.generation_id,
        "source_manifest_sha256": spec.source_manifest_sha256,
        "model_manifest_sha256": spec.model_manifest_sha256,
        "index_config_sha256": spec.index_config_sha256,
        "evaluation_report_sha256": _require_sha256(
            evaluation_report_sha256,
            "evaluation_report_sha256",
        ),
        "store_projection_sha256": {
            item.store_name: item.sealed_projection_sha256 for item in ordered
        },
    }
    return payload, canonical_sha256(payload)


__all__ = [
    "CCCT_BASELINE_METRICS",
    "CCCT_BASELINE_SNAPSHOT_ID",
    "CCCT_BASELINE_SOURCE",
    "DatasetSource",
    "DatasetMember",
    "FreshnessEvidence",
    "GenerationSpec",
    "GenerationState",
    "GenerationValidationError",
    "HSK_BASELINE_METRICS",
    "HSK_BASELINE_SNAPSHOT_ID",
    "HSK_BASELINE_SOURCES",
    "LawCorpusBinding",
    "LawSource",
    "SourceFile",
    "SourceManifest",
    "StoreAttestation",
    "VerifiedEvaluationBundle",
    "build_seal_attestation",
    "canonical_json_bytes",
    "canonical_sha256",
    "evaluation_review_subject_sha256",
    "load_verified_evaluation_bundle",
    "release_model_manifest",
    "validate_model_manifest",
    "validate_index_config",
    "validate_dataset_members",
    "validate_rag_evaluation_report",
    "validate_store_attestations",
]
