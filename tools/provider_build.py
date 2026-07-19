"""Pure helpers shared by provider-build generators."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

PROVIDER_RELEASE = "evidence-api-v1.1.0"
RUNTIME_PROVIDER_RELEASE = "evidence-api-v1.2.0"
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_RUNTIME_BUILD_FIELDS = {
    "contract",
    "schema_version",
    "provider_release",
    "provider_revision",
    "schema_sha256",
    "source_manifest_sha256",
    "provider_build_sha256",
}


class ProviderBuildError(RuntimeError):
    """A provider build cannot be attested without weakening provenance."""


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def create_provider_build_document(
    *,
    provider_revision: str,
    retrieval_schema: bytes,
    source_manifest: bytes,
) -> dict[str, str]:
    if not _REVISION_PATTERN.fullmatch(provider_revision):
        raise ProviderBuildError("provider_revision must be a full lowercase Git SHA")
    payload = {
        "contract": "evidence-retrieval",
        "schema_version": "1.0",
        "provider_release": PROVIDER_RELEASE,
        "provider_revision": provider_revision,
        "schema_sha256": hashlib.sha256(retrieval_schema).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(source_manifest).hexdigest(),
    }
    payload["provider_build_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def create_runtime_provider_build_document(
    *,
    provider_revision: str,
    retrieval_schema: bytes,
    source_manifest: bytes,
) -> dict[str, str]:
    """Create the code-build half of a v2 runtime release.

    This deliberately contains no generation ID. Generation and evaluation
    evidence are sealed separately and combined only by ``RuntimeReleaseLoader``.
    """

    if not _REVISION_PATTERN.fullmatch(provider_revision):
        raise ProviderBuildError("provider_revision must be a full lowercase Git SHA")
    payload = {
        "contract": "evidence-retrieval",
        "schema_version": "1.0",
        "provider_release": RUNTIME_PROVIDER_RELEASE,
        "provider_revision": provider_revision,
        "schema_sha256": hashlib.sha256(retrieval_schema).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(source_manifest).hexdigest(),
    }
    payload["provider_build_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def create_runtime_provider_lock_document(
    *,
    runtime_provider_build: Mapping[str, Any],
    retrieval_schema: bytes,
    readiness_schema: bytes,
    source_manifest: bytes,
) -> dict[str, str]:
    """Derive the Agent-consumable provider lock from one attested context."""

    if (
        set(runtime_provider_build) != _RUNTIME_BUILD_FIELDS
        or runtime_provider_build.get("contract") != "evidence-retrieval"
        or runtime_provider_build.get("schema_version") != "1.0"
        or runtime_provider_build.get("provider_release") != RUNTIME_PROVIDER_RELEASE
    ):
        raise ProviderBuildError("runtime provider build fields are invalid")
    revision = runtime_provider_build.get("provider_revision")
    if not isinstance(revision, str) or _REVISION_PATTERN.fullmatch(revision) is None:
        raise ProviderBuildError("runtime provider revision is invalid")
    for name in (
        "schema_sha256",
        "source_manifest_sha256",
        "provider_build_sha256",
    ):
        value = runtime_provider_build.get(name)
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise ProviderBuildError("runtime provider build digest is invalid")

    expected_build = create_runtime_provider_build_document(
        provider_revision=revision,
        retrieval_schema=retrieval_schema,
        source_manifest=source_manifest,
    )
    if dict(runtime_provider_build) != expected_build:
        raise ProviderBuildError("runtime provider build does not match context bytes")
    return {
        "contract": "evidence-runtime-release-provider-lock",
        "schema_version": "1.0",
        "provider_release": RUNTIME_PROVIDER_RELEASE,
        "provider_revision": revision,
        "provider_build_sha256": expected_build["provider_build_sha256"],
        "retrieval_schema_sha256": hashlib.sha256(retrieval_schema).hexdigest(),
        "readiness_schema_sha256": hashlib.sha256(readiness_schema).hexdigest(),
        "provider_source_manifest_sha256": hashlib.sha256(source_manifest).hexdigest(),
    }


def serialized_document(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
