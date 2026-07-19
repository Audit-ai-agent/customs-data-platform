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
_EXPECTED_LAWS = {str(item["law_id"]): str(item["name"]) for item in TARGET_LAWS}
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


class GenerationValidationError(ValueError):
    """A generation or its release evidence is not safe to use."""


class GenerationState(StrEnum):
    BUILDING = "building"
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
    release_eligible: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FreshnessEvidence:
        if set(value) != {
            "status",
            "observed_at",
            "valid_through",
            "limitation",
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
        if status == "bounded_snapshot" and (valid_through is None or limitation is None):
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
            release_eligible=release_eligible,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observed_at": self.observed_at,
            "valid_through": self.valid_through,
            "limitation": self.limitation,
            "release_eligible": self.release_eligible,
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
        if self.dataset_id == "ccct" and self.snapshot_id == CCCT_BASELINE_SNAPSHOT_ID:
            expected_files = (CCCT_BASELINE_SOURCE,)
            if tuple(item.as_dict() for item in self.source_files) != expected_files:
                raise GenerationValidationError("CCCT baseline source hash/size/revision mismatch")
            if dict(self.metrics) != CCCT_BASELINE_METRICS:
                raise GenerationValidationError("CCCT baseline derived counts mismatch")
        if self.dataset_id == "hsk" and self.snapshot_id == HSK_BASELINE_SNAPSHOT_ID:
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
class SourceManifest:
    generated_at: str
    laws: tuple[LawSource, ...]
    datasets: tuple[DatasetSource, ...]
    schema_version: str = "1.0"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SourceManifest:
        if set(value) != {"schema_version", "generated_at", "laws", "datasets"}:
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
            laws=laws,
            datasets=datasets,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "laws": [item.as_dict() for item in self.laws],
            "datasets": [item.as_dict() for item in self.datasets],
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())

    def require_release_eligible_sources(self) -> None:
        blocked = [item.dataset_id for item in self.datasets if not item.freshness.release_eligible]
        if blocked:
            raise GenerationValidationError(
                "source freshness is not release-eligible: " + ",".join(blocked)
            )


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


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    source_manifest: SourceManifest
    model_manifest: Mapping[str, Any]
    index_config: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_manifest", validate_model_manifest(self.model_manifest))
        normalized_config = _json_safe(self.index_config, "index_config")
        if not isinstance(normalized_config, dict) or not normalized_config:
            raise GenerationValidationError("index_config must be a non-empty JSON object")
        object.__setattr__(self, "index_config", normalized_config)

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
        return cls(
            generation_id=generation_id,
            store_name=store_name,
            article_count=len(normalized),
            article_id_set_sha256=canonical_sha256([item[0] for item in normalized]),
            article_content_set_sha256=canonical_sha256(
                [
                    {"article_id": article_id, "content_sha256": content_hash}
                    for article_id, content_hash in normalized
                ]
            ),
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
            "model_manifest_sha256": self.model_manifest_sha256,
            "store_locator": self.store_locator,
            "store_revision": self.store_revision,
            "attested_at": self.attested_at,
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


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
    """Validate the production RAG release report and return its canonical hash."""

    report = _json_safe(value, "evaluation_report")
    required = {
        "schema_version",
        "generation_id",
        "run_id",
        "generated_at",
        "production_data",
        "mock_count",
        "unexpected_skips",
        "gold",
        "structured",
        "hybrid",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise GenerationValidationError("evaluation report fields do not match schema")
    if report["schema_version"] != "1.0" or report["generation_id"] != generation_id:
        raise GenerationValidationError("evaluation report identity does not match generation")
    _require_non_empty(report["run_id"], "run_id", maximum=200)
    _parse_utc(report["generated_at"], "generated_at")
    if report["production_data"] is not True:
        raise GenerationValidationError("evaluation must use production data")
    if _require_int(report["mock_count"], "mock_count") != 0:
        raise GenerationValidationError("mock retrieval is forbidden in release evaluation")
    if _require_int(report["unexpected_skips"], "unexpected_skips") != 0:
        raise GenerationValidationError("release evaluation cannot contain skips")

    gold = report["gold"]
    gold_required = {
        "dataset_sha256",
        "human_reviewed",
        "reviewer_id",
        "reviewed_at",
        "law_coverage_count",
        "positive_cases",
        "negative_cases",
        "ambiguous_cases",
    }
    if not isinstance(gold, dict) or set(gold) != gold_required:
        raise GenerationValidationError("Gold review fields do not match schema")
    _require_sha256(gold["dataset_sha256"], "gold.dataset_sha256")
    if gold["human_reviewed"] is not True:
        raise GenerationValidationError("human-reviewed Gold is required to seal")
    _require_non_empty(gold["reviewer_id"], "gold.reviewer_id", maximum=200)
    _parse_utc(gold["reviewed_at"], "gold.reviewed_at")
    if _require_int(gold["law_coverage_count"], "gold.law_coverage_count") != len(_EXPECTED_LAWS):
        raise GenerationValidationError("Gold must cover all target laws")
    _require_int(gold["positive_cases"], "gold.positive_cases", minimum=len(_EXPECTED_LAWS))
    _require_int(gold["negative_cases"], "gold.negative_cases", minimum=1)
    _require_int(gold["ambiguous_cases"], "gold.ambiguous_cases", minimum=1)

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
    if not isinstance(structured, dict) or set(structured) != structured_required:
        raise GenerationValidationError("structured evaluation fields do not match schema")
    structured_total = _require_int(structured["cases_total"], "structured.cases_total", minimum=5)
    if _require_int(structured["cases_passed"], "structured.cases_passed") != structured_total:
        raise GenerationValidationError("every structured case must pass")
    for metric in (
        "case_pass_rate",
        "contract_valid_rate",
        "hs_resolution_accuracy",
        "requirements_found_accuracy",
        "required_evidence_recall",
        "evidence_metadata_completeness",
    ):
        if _require_number(structured[metric], f"structured.{metric}") != 1.0:
            raise GenerationValidationError(f"structured.{metric} must equal 1.0")
    if (
        _require_number(
            structured["no_requirement_false_positive_rate"],
            "structured.no_requirement_false_positive_rate",
        )
        != 0.0
    ):
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
    if not isinstance(hybrid, dict) or set(hybrid) != hybrid_required:
        raise GenerationValidationError("hybrid evaluation fields do not match schema")
    hybrid_total = _require_int(hybrid["cases_total"], "hybrid.cases_total", minimum=5)
    if _require_int(hybrid["cases_passed"], "hybrid.cases_passed") != hybrid_total:
        raise GenerationValidationError("every hybrid case must pass")
    for metric in (
        "contract_valid_rate",
        "hit_at_5",
        "recall_at_10",
        "citation_metadata_completeness",
    ):
        if _require_number(hybrid[metric], f"hybrid.{metric}") != 1.0:
            raise GenerationValidationError(f"hybrid.{metric} must equal 1.0")
    if _require_number(hybrid["ndcg_at_10"], "hybrid.ndcg_at_10") < 0.8:
        raise GenerationValidationError("hybrid.ndcg_at_10 must be at least 0.8")
    return report, canonical_sha256(report)


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
        "store_attestation_sha256": {item.store_name: item.sha256 for item in ordered},
    }
    return payload, canonical_sha256(payload)


__all__ = [
    "CCCT_BASELINE_METRICS",
    "CCCT_BASELINE_SNAPSHOT_ID",
    "CCCT_BASELINE_SOURCE",
    "DatasetSource",
    "FreshnessEvidence",
    "GenerationSpec",
    "GenerationState",
    "GenerationValidationError",
    "HSK_BASELINE_METRICS",
    "HSK_BASELINE_SNAPSHOT_ID",
    "HSK_BASELINE_SOURCES",
    "LawSource",
    "SourceFile",
    "SourceManifest",
    "StoreAttestation",
    "build_seal_attestation",
    "canonical_json_bytes",
    "canonical_sha256",
    "release_model_manifest",
    "validate_model_manifest",
    "validate_rag_evaluation_report",
    "validate_store_attestations",
]
