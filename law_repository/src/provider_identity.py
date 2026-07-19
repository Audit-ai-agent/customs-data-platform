"""Evidence API provider build identity and readiness wire models.

The retrieval JSON body remains Evidence schema v1.0.  Provider release/build
identity is bound out-of-band through readiness and HTTP headers so an Agent
cannot accidentally combine one readiness check with another deployed build.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROVIDER_RELEASE = "evidence-api-v1.1.0"
EVIDENCE_CONTRACT = "evidence-retrieval"
EVIDENCE_SCHEMA_VERSION = "1.0"

HEADER_SCHEMA_SHA256 = "X-Evidence-Schema-SHA256"
HEADER_PROVIDER_RELEASE = "X-Evidence-Provider-Release"
HEADER_PROVIDER_REVISION = "X-Evidence-Provider-Revision"
HEADER_PROVIDER_BUILD_SHA256 = "X-Evidence-Provider-Build-SHA256"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_FIELDS = {
    "contract",
    "schema_version",
    "provider_release",
    "provider_revision",
    "schema_sha256",
    "source_manifest_sha256",
    "provider_build_sha256",
}

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class ProviderIdentityError(RuntimeError):
    """Provider build attestation is missing, malformed, or does not match bytes."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderBuildIdentity(_StrictModel):
    contract: Literal["evidence-retrieval"]
    schema_version: Literal["1.0"]
    provider_release: Literal["evidence-api-v1.1.0"]
    provider_revision: GitRevision
    schema_sha256: Sha256
    source_manifest_sha256: Sha256
    provider_build_sha256: Sha256

    @model_validator(mode="after")
    def build_digest_matches_payload(self):
        expected = provider_build_sha256(self.model_dump(exclude={"provider_build_sha256"}))
        if self.provider_build_sha256 != expected:
            raise ValueError("provider_build_sha256 does not match identity payload")
        return self

    def binding_headers(self) -> dict[str, str]:
        return {
            HEADER_SCHEMA_SHA256: self.schema_sha256,
            HEADER_PROVIDER_RELEASE: self.provider_release,
            HEADER_PROVIDER_REVISION: self.provider_revision,
            HEADER_PROVIDER_BUILD_SHA256: self.provider_build_sha256,
        }


class LawCorpusReadiness(_StrictModel):
    expected: int = Field(ge=1)
    loaded: int = Field(ge=0)
    article_backed: int = Field(ge=0)

    @model_validator(mode="after")
    def complete_target_corpus(self):
        if self.loaded != self.expected or self.article_backed != self.expected:
            raise ValueError("target law corpus is incomplete")
        return self


class LawIndexReadiness(_StrictModel):
    expected: int = Field(ge=1)
    postgres_indexed: int = Field(ge=0)
    qdrant: int = Field(ge=0)
    postgres_graph_articles: int = Field(ge=1)
    neo4j: int = Field(ge=0)
    neo4j_missing: int = Field(ge=0)
    neo4j_orphaned: int = Field(ge=0)

    @model_validator(mode="after")
    def complete_projections(self):
        if self.postgres_indexed != self.expected or self.qdrant != self.expected:
            raise ValueError("law retrieval index is incomplete")
        if (
            self.neo4j != self.postgres_graph_articles
            or self.neo4j_missing != 0
            or self.neo4j_orphaned != 0
        ):
            raise ValueError("law graph projection is incomplete")
        return self


class ReadinessResponse(_StrictModel):
    status: Literal["ready"]
    knowledge_snapshot_id: Annotated[str, Field(min_length=1)]
    supported_review_as_of: date
    evidence_contract: ProviderBuildIdentity
    law_corpus: LawCorpusReadiness
    law_index: LawIndexReadiness


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def provider_build_sha256(payload_without_build_sha256: Mapping[str, object]) -> str:
    """Hash the canonical identity payload, excluding its self-referential digest."""
    return hashlib.sha256(_canonical_json_bytes(payload_without_build_sha256)).hexdigest()


def create_provider_build_identity(
    *,
    provider_revision: str,
    schema_sha256: str,
    source_manifest_sha256: str,
) -> ProviderBuildIdentity:
    payload: dict[str, object] = {
        "contract": EVIDENCE_CONTRACT,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "provider_release": PROVIDER_RELEASE,
        "provider_revision": provider_revision,
        "schema_sha256": schema_sha256,
        "source_manifest_sha256": source_manifest_sha256,
    }
    payload["provider_build_sha256"] = provider_build_sha256(payload)
    return ProviderBuildIdentity.model_validate(payload)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderIdentityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json_object(path: Path, *, label: str) -> tuple[dict[str, object], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProviderIdentityError(f"{label} is unavailable") from exc
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ProviderIdentityError(f"non-standard JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderIdentityError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProviderIdentityError(f"{label} must be a JSON object")
    return value, raw


def _safe_manifest_path(bundle_root: Path, relative_path: object) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ProviderIdentityError("source manifest contains an invalid path")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ProviderIdentityError("source manifest paths must be relative")
    resolved = (bundle_root / candidate).resolve()
    try:
        resolved.relative_to(bundle_root.resolve())
    except ValueError as exc:
        raise ProviderIdentityError("source manifest path escapes the bundle root") from exc
    return resolved


def _verify_source_manifest(
    *,
    manifest: dict[str, object],
    manifest_sha256: str,
    identity: ProviderBuildIdentity,
    bundle_root: Path,
) -> None:
    if set(manifest) != {
        "schema_version",
        "source_repository",
        "source_commit",
        "files",
    }:
        raise ProviderIdentityError("source manifest fields are not exact")
    if (
        manifest.get("schema_version") != "1.0"
        or manifest.get("source_repository") != "customs-data-platform"
    ):
        raise ProviderIdentityError("source manifest ownership/version is invalid")
    if manifest_sha256 != identity.source_manifest_sha256:
        raise ProviderIdentityError("source manifest digest does not match provider identity")
    if manifest.get("source_commit") != identity.provider_revision:
        raise ProviderIdentityError("source manifest revision does not match provider identity")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ProviderIdentityError("source manifest has no files")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise ProviderIdentityError("source manifest file entry is malformed")
        relative_path = entry["path"]
        expected_sha = entry["sha256"]
        expected_bytes = entry["bytes"]
        if not isinstance(relative_path, str) or relative_path in seen:
            raise ProviderIdentityError("source manifest paths must be unique strings")
        seen.add(relative_path)
        if not isinstance(expected_sha, str) or not _SHA256_PATTERN.fullmatch(expected_sha):
            raise ProviderIdentityError("source manifest contains an invalid SHA-256")
        if not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool) or expected_bytes < 0:
            raise ProviderIdentityError("source manifest contains an invalid byte count")
        path = _safe_manifest_path(bundle_root, relative_path)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ProviderIdentityError("a source manifest file is unavailable") from exc
        if len(payload) != expected_bytes or hashlib.sha256(payload).hexdigest() != expected_sha:
            raise ProviderIdentityError("a source manifest file does not match its attestation")


def load_provider_build_identity(
    *,
    bundle_root: Path | None = None,
) -> ProviderBuildIdentity:
    """Load and verify the exact deployed build, failing closed on every mismatch."""
    root = bundle_root
    if root is None:
        configured = os.environ.get("EVIDENCE_PROVIDER_BUNDLE_ROOT")
        root = Path(configured) if configured else Path(__file__).resolve().parents[2]
    root = root.resolve()

    identity_payload, _ = _read_json_object(
        root / "provider-build.json",
        label="provider build identity",
    )
    if set(identity_payload) != _IDENTITY_FIELDS:
        raise ProviderIdentityError("provider build identity fields are not exact")
    try:
        identity = ProviderBuildIdentity.model_validate(identity_payload)
    except Exception as exc:
        raise ProviderIdentityError("provider build identity is invalid") from exc

    retrieval_schema = root / "contracts" / "evidence" / "v1" / "rag_retrieval_v1.schema.json"
    try:
        schema_sha = hashlib.sha256(retrieval_schema.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProviderIdentityError("Evidence retrieval schema is unavailable") from exc
    if schema_sha != identity.schema_sha256:
        raise ProviderIdentityError("Evidence retrieval schema does not match provider identity")

    manifest, manifest_raw = _read_json_object(
        root / "source-manifest.json",
        label="source manifest",
    )
    _verify_source_manifest(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        identity=identity,
        bundle_root=root,
    )
    return identity


def binding_header_mismatches(
    headers: Mapping[str, str],
    identity: ProviderBuildIdentity,
) -> tuple[str, ...]:
    """Return exact build-binding headers that are absent or do not match."""
    normalized = {key.lower(): value for key, value in headers.items()}
    return tuple(
        name
        for name, expected in identity.binding_headers().items()
        if normalized.get(name.lower()) != expected
    )
