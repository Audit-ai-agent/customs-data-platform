"""Canonical, secret-free file inventory for an Evidence provider context."""

from __future__ import annotations

# The context carries only the source and canonical contract bytes needed to
# verify one Data revision.  It deliberately excludes Docker, cloud, release,
# tests, datasets, local state, and repository metadata.
FIXED_PROVIDER_CONTEXT_FILES: dict[str, str] = {
    "pyproject.toml": "pyproject.toml",
    "uv.lock": "uv.lock",
    "contracts/evidence/v1/rag_retrieval_v1.schema.json": (
        "contracts/evidence/v1/rag_retrieval_v1.schema.json"
    ),
    "contracts/evidence/v1/readiness_v1.schema.json": (
        "contracts/evidence/v1/readiness_v1.schema.json"
    ),
    "contracts/evidence/v2/readiness_v2.schema.json": (
        "contracts/evidence/v2/readiness_v2.schema.json"
    ),
    "contracts/evidence/v3/rag_retrieval_v3.schema.json": (
        "contracts/evidence/v3/rag_retrieval_v3.schema.json"
    ),
    "contracts/evidence/v3/readiness_v3.schema.json": (
        "contracts/evidence/v3/readiness_v3.schema.json"
    ),
    "contracts/evidence/v3/requirement_policy_v1.schema.json": (
        "contracts/evidence/v3/requirement_policy_v1.schema.json"
    ),
    "contracts/evidence/v3/requirement-policy.lock.json": (
        "contracts/evidence/v3/requirement-policy.lock.json"
    ),
    "contracts/evidence/v3/trusted_reviewer_store_v1.schema.json": (
        "contracts/evidence/v3/trusted_reviewer_store_v1.schema.json"
    ),
    "contracts/evidence/v3/trusted_reviewer_lock_v1.schema.json": (
        "contracts/evidence/v3/trusted_reviewer_lock_v1.schema.json"
    ),
    "contracts/evidence/v3/trusted-reviewers.lock.json": (
        "contracts/evidence/v3/trusted-reviewers.lock.json"
    ),
}

GENERATED_PROVIDER_CONTEXT_FILES = frozenset(
    {
        "source-manifest.json",
        "provider-build.json",
        "provider-runtime-build.json",
        "provider-runtime-lock.json",
    }
)


__all__ = ["FIXED_PROVIDER_CONTEXT_FILES", "GENERATED_PROVIDER_CONTEXT_FILES"]
