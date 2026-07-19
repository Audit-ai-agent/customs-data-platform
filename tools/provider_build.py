"""Pure helpers shared by provider-build generators."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

PROVIDER_RELEASE = "evidence-api-v1.1.0"
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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


def serialized_document(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
