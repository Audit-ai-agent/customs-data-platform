"""Single source of truth for the Evidence production image context inventory.

The build tool and the independent release verifier must agree on this mapping.
Keeping it in the runtime source tree means the mapping itself is included in,
and attested by, every production context.
"""

from __future__ import annotations

FIXED_PRODUCTION_CONTEXT_FILES: dict[str, str] = {
    ".dockerignore": ".dockerignore",
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
    "contracts/evidence/v3/release_provenance_v1.schema.json": (
        "contracts/evidence/v3/release_provenance_v1.schema.json"
    ),
    "law_repository/deploy/production/Dockerfile": "Dockerfile",
    "law_repository/deploy/production/README.md": "README.md",
}

GENERATED_PRODUCTION_CONTEXT_FILES = frozenset(
    {
        "source-manifest.json",
        "provider-build.json",
        "provider-runtime-build.json",
        "provider-runtime-lock.json",
    }
)


__all__ = ["FIXED_PRODUCTION_CONTEXT_FILES", "GENERATED_PRODUCTION_CONTEXT_FILES"]
