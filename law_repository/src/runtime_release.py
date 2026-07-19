"""Generation-bound Evidence runtime release identity.

The provider code build and the sealed knowledge generation are independent
release inputs.  This module binds them into one request-visible identity and
adds an internal epoch lease so A -> B -> A promotion cannot pass as an
unchanged request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Annotated, Literal

from src.db.postgres import connect
from src.generation import GenerationValidationError, SourceManifest
from src.generation_runtime import (
    ActiveGenerationError,
    ActiveGenerationLoader,
)
from src.generation_stores import Neo4jGenerationStore, QdrantGenerationStore
from src.provider_identity import (
    HEADER_PROVIDER_BUILD_SHA256,
    HEADER_PROVIDER_RELEASE,
    HEADER_PROVIDER_REVISION,
    HEADER_SCHEMA_SHA256,
    RuntimeProviderBuildIdentity,
    load_runtime_provider_build_identity,
)

RUNTIME_CONTRACT = "evidence-runtime-release"
RUNTIME_SCHEMA_VERSION = "2.0"
RETRIEVAL_SCHEMA_VERSION = "1.0"

HEADER_KNOWLEDGE_GENERATION = "X-Evidence-Knowledge-Generation"
HEADER_GENERATION_ATTESTATION_SHA256 = "X-Evidence-Generation-Attestation-SHA256"
HEADER_EVALUATION_SHA256 = "X-Evidence-Evaluation-SHA256"
HEADER_RUNTIME_RELEASE_SHA256 = "X-Evidence-Runtime-Release-SHA256"

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
SnapshotId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$")]


class RuntimeReleaseError(RuntimeError):
    """The production runtime identity is absent, inconsistent, or changed."""


class EvidenceRuntimeReleaseIdentity(BaseModel):
    """Exact identity returned by readiness and bound to every retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["evidence-runtime-release"]
    schema_version: Literal["2.0"]
    retrieval_schema_version: Literal["1.0"]
    provider_release: Literal["evidence-api-v1.2.0"]
    provider_revision: GitRevision
    provider_build_sha256: Sha256
    retrieval_schema_sha256: Sha256
    provider_source_manifest_sha256: Sha256
    knowledge_generation_id: Sha256
    knowledge_generation_attestation_sha256: Sha256
    knowledge_source_manifest_sha256: Sha256
    model_manifest_sha256: Sha256
    index_config_sha256: Sha256
    evaluation_report_sha256: Sha256
    seal_attestation_sha256: Sha256
    knowledge_snapshot_id: SnapshotId
    supported_review_as_of: date
    runtime_release_sha256: Sha256

    @model_validator(mode="after")
    def runtime_digest_matches_payload(self):
        expected = runtime_release_sha256(
            self.model_dump(mode="json", exclude={"runtime_release_sha256"})
        )
        if self.runtime_release_sha256 != expected:
            raise ValueError("runtime_release_sha256 does not match identity payload")
        return self

    def binding_headers(self) -> dict[str, str]:
        return {
            HEADER_SCHEMA_SHA256: self.retrieval_schema_sha256,
            HEADER_PROVIDER_RELEASE: self.provider_release,
            HEADER_PROVIDER_REVISION: self.provider_revision,
            HEADER_PROVIDER_BUILD_SHA256: self.provider_build_sha256,
            HEADER_KNOWLEDGE_GENERATION: self.knowledge_generation_id,
            HEADER_GENERATION_ATTESTATION_SHA256: (
                self.knowledge_generation_attestation_sha256
            ),
            HEADER_EVALUATION_SHA256: self.evaluation_report_sha256,
            HEADER_RUNTIME_RELEASE_SHA256: self.runtime_release_sha256,
        }


class EvidenceRuntimeReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"]
    evidence_runtime_release: EvidenceRuntimeReleaseIdentity


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def runtime_release_sha256(payload_without_digest: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload_without_digest)).hexdigest()


@dataclass(frozen=True, slots=True)
class GenerationReleaseMetadata:
    generation_id: str
    source_manifest_sha256: str
    knowledge_snapshot_id: str
    supported_review_as_of: date
    source_manifest: SourceManifest


class GenerationMetadataLoader(Protocol):
    def load(self, generation_id: str) -> GenerationReleaseMetadata: ...


class PostgresGenerationMetadataLoader:
    """Read the CCCT/HSK generation metadata from the PostgreSQL authority."""

    def __init__(self, connection_factory: Callable[[], Any] = connect) -> None:
        self._connection_factory = connection_factory

    def load(self, generation_id: str) -> GenerationReleaseMetadata:
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT source_manifest, source_manifest_sha256
                FROM knowledge_generations
                WHERE generation_id=%s AND state='active'
                """,
                (generation_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeReleaseError("active generation metadata is unavailable")
            raw_manifest, stored_hash = row
            try:
                manifest = SourceManifest.from_mapping(raw_manifest)
                manifest.require_release_eligible_sources()
            except (TypeError, GenerationValidationError) as exc:
                raise RuntimeReleaseError("knowledge source manifest is invalid") from exc
            if manifest.sha256 != stored_hash:
                raise RuntimeReleaseError("knowledge source manifest hash mismatch")
            datasets = {item.dataset_id: item for item in manifest.datasets}
            ccct = datasets["ccct"]
            cur.execute(
                """
                SELECT (generated_at AT TIME ZONE 'Asia/Seoul')::date
                FROM knowledge_snapshots
                WHERE snapshot_id=%s AND source_type='ccct'
                """,
                (ccct.snapshot_id,),
            )
            snapshot_row = cur.fetchone()
        if snapshot_row is None or not isinstance(snapshot_row[0], date):
            raise RuntimeReleaseError("generation CCCT snapshot metadata is unavailable")
        supported = snapshot_row[0]
        if ccct.freshness.valid_through is not None:
            valid_through = date.fromisoformat(ccct.freshness.valid_through)
            if supported > valid_through:
                raise RuntimeReleaseError("CCCT snapshot exceeds attested source freshness")
        return GenerationReleaseMetadata(
            generation_id=generation_id,
            source_manifest_sha256=stored_hash,
            knowledge_snapshot_id=ccct.snapshot_id,
            supported_review_as_of=supported,
            source_manifest=manifest,
        )


@dataclass(frozen=True, slots=True)
class GenerationRequestLease:
    """Public identity plus non-wire epoch fields used to detect ABA."""

    identity: EvidenceRuntimeReleaseIdentity
    generation_epoch: int
    generation_changed_at: str

    @property
    def token(self) -> tuple[str, int, str, str, str]:
        return (
            self.identity.knowledge_generation_id,
            self.generation_epoch,
            self.generation_changed_at,
            self.identity.seal_attestation_sha256,
            self.identity.runtime_release_sha256,
        )


class RuntimeLeaseLoader(Protocol):
    def acquire(self) -> GenerationRequestLease: ...

    def assert_unchanged(self, lease: GenerationRequestLease) -> None: ...


def _production_active_loader() -> ActiveGenerationLoader:
    # External aliases/pointers are mandatory in production.  The revision
    # label is unused for read-only pointer checks but remains explicit.
    return ActiveGenerationLoader(
        external_stores=(
            QdrantGenerationStore(store_revision="runtime-v2-read"),
            Neo4jGenerationStore(store_revision="runtime-v2-read"),
        )
    )


class RuntimeReleaseLoader:
    """Combine a verified code build and one active sealed generation."""

    def __init__(
        self,
        *,
        provider_loader: Callable[[], RuntimeProviderBuildIdentity] = (
            load_runtime_provider_build_identity
        ),
        active_generation_loader: ActiveGenerationLoader | None = None,
        metadata_loader: GenerationMetadataLoader | None = None,
    ) -> None:
        self._provider_loader = provider_loader
        self._active_loader = active_generation_loader or _production_active_loader()
        self._metadata_loader = metadata_loader or PostgresGenerationMetadataLoader()

    def acquire(self) -> GenerationRequestLease:
        try:
            provider = self._provider_loader()
            active = self._active_loader.load()
            metadata = self._metadata_loader.load(active.generation_id)
        except (ActiveGenerationError, RuntimeReleaseError):
            raise
        except Exception as exc:
            raise RuntimeReleaseError("runtime release identity is unavailable") from exc
        if metadata.generation_id != active.generation_id:
            raise RuntimeReleaseError("knowledge generation metadata identity mismatch")
        if metadata.source_manifest_sha256 != active.source_manifest_sha256:
            raise RuntimeReleaseError("knowledge source manifest identity mismatch")

        payload: dict[str, object] = {
            "contract": RUNTIME_CONTRACT,
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "retrieval_schema_version": RETRIEVAL_SCHEMA_VERSION,
            "provider_release": provider.provider_release,
            "provider_revision": provider.provider_revision,
            "provider_build_sha256": provider.provider_build_sha256,
            "retrieval_schema_sha256": provider.schema_sha256,
            "provider_source_manifest_sha256": provider.source_manifest_sha256,
            "knowledge_generation_id": active.generation_id,
            "knowledge_generation_attestation_sha256": active.sha256,
            "knowledge_source_manifest_sha256": active.source_manifest_sha256,
            "model_manifest_sha256": active.model_manifest_sha256,
            "index_config_sha256": active.index_config_sha256,
            "evaluation_report_sha256": active.evaluation_report_sha256,
            "seal_attestation_sha256": active.seal_attestation_sha256,
            "knowledge_snapshot_id": metadata.knowledge_snapshot_id,
            "supported_review_as_of": metadata.supported_review_as_of.isoformat(),
        }
        payload["runtime_release_sha256"] = runtime_release_sha256(payload)
        try:
            identity = EvidenceRuntimeReleaseIdentity.model_validate(payload)
        except Exception as exc:
            raise RuntimeReleaseError("runtime release identity is invalid") from exc
        return GenerationRequestLease(
            identity=identity,
            generation_epoch=active.epoch,
            generation_changed_at=active.changed_at,
        )

    def assert_unchanged(self, lease: GenerationRequestLease) -> None:
        current = self.acquire()
        if current.token != lease.token or current.identity != lease.identity:
            raise RuntimeReleaseError("runtime release changed during request")


def runtime_binding_header_mismatches(
    headers: Mapping[str, str],
    identity: EvidenceRuntimeReleaseIdentity,
) -> tuple[str, ...]:
    """Return names whose cardinality/value do not exactly match the lease."""

    getlist = getattr(headers, "getlist", None)

    def values_for(name: str) -> tuple[str, ...]:
        if callable(getlist):
            return tuple(getlist(name))
        return tuple(value for key, value in headers.items() if key.lower() == name.lower())

    return tuple(
        name
        for name, expected in identity.binding_headers().items()
        if values_for(name) != (expected,)
    )


__all__ = [
    "EvidenceRuntimeReadiness",
    "EvidenceRuntimeReleaseIdentity",
    "GenerationReleaseMetadata",
    "GenerationRequestLease",
    "PostgresGenerationMetadataLoader",
    "RuntimeLeaseLoader",
    "RuntimeReleaseError",
    "RuntimeReleaseLoader",
    "runtime_binding_header_mismatches",
    "runtime_release_sha256",
]
