"""Evidence runtime v3 identity binding retrieval to approved policy."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.deadline import RequestDeadline
from src.requirement_policy_release import (
    DEFAULT_POLICY_RELEASE_PATH,
    HEADER_COVERAGE_MATRIX_SHA256,
    HEADER_REQUIREMENT_POLICY_RELEASE,
    HEADER_REQUIREMENT_POLICY_SHA256,
    HEADER_REVIEWER_TRUST_STORE_SHA256,
    HEADER_RUNTIME_V3_SHA256,
    VerifiedRequirementPolicyRelease,
    load_verified_requirement_policy_release,
)
from src.runtime_release import (
    EvidenceRuntimeReleaseIdentity,
    GenerationRequestLease,
    RuntimeLeaseLoader,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DEFAULT_PROVIDER_BUNDLE_ROOT = Path("/app")
V3_RETRIEVAL_SCHEMA_PATH = Path("contracts/evidence/v3/rag_retrieval_v3.schema.json")
V3_READINESS_SCHEMA_PATH = Path("contracts/evidence/v3/readiness_v3.schema.json")
REQUIREMENT_POLICY_SCHEMA_PATH = Path("contracts/evidence/v3/requirement_policy_v1.schema.json")

HEADER_V3_RETRIEVAL_SCHEMA_SHA256 = "X-Evidence-V3-Retrieval-Schema-SHA256"
HEADER_V3_READINESS_SCHEMA_SHA256 = "X-Evidence-V3-Readiness-Schema-SHA256"
HEADER_REQUIREMENT_POLICY_SCHEMA_SHA256 = "X-Evidence-Requirement-Policy-Schema-SHA256"
HEADER_REQUIREMENT_POLICY_APPROVAL_SHA256 = "X-Evidence-Requirement-Policy-Approval-SHA256"


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvidenceRuntimeReleaseIdentityV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: Literal["evidence-runtime-release"]
    schema_version: Literal["3.0"]
    retrieval_schema_version: Literal["3.0"]
    base_runtime_release: EvidenceRuntimeReleaseIdentity
    retrieval_schema_sha256: Sha256
    readiness_schema_sha256: Sha256
    requirement_policy_schema_sha256: Sha256
    requirement_policy_release: str = Field(min_length=1, max_length=128)
    requirement_policy_manifest_sha256: Sha256
    coverage_matrix_sha256: Sha256
    requirement_policy_approval_sha256: Sha256
    reviewer_trust_store_sha256: Sha256
    runtime_release_v3_sha256: Sha256

    @model_validator(mode="after")
    def digest_matches(self) -> EvidenceRuntimeReleaseIdentityV3:
        payload = self.model_dump(mode="json", exclude={"runtime_release_v3_sha256"})
        if self.runtime_release_v3_sha256 != _canonical_sha256(payload):
            raise ValueError("runtime_release_v3_sha256 does not match identity")
        return self

    def binding_headers(self) -> dict[str, str]:
        return {
            **self.base_runtime_release.binding_headers(),
            HEADER_V3_RETRIEVAL_SCHEMA_SHA256: self.retrieval_schema_sha256,
            HEADER_V3_READINESS_SCHEMA_SHA256: self.readiness_schema_sha256,
            HEADER_REQUIREMENT_POLICY_SCHEMA_SHA256: self.requirement_policy_schema_sha256,
            HEADER_REQUIREMENT_POLICY_RELEASE: self.requirement_policy_release,
            HEADER_REQUIREMENT_POLICY_SHA256: self.requirement_policy_manifest_sha256,
            HEADER_COVERAGE_MATRIX_SHA256: self.coverage_matrix_sha256,
            HEADER_REQUIREMENT_POLICY_APPROVAL_SHA256: (self.requirement_policy_approval_sha256),
            HEADER_REVIEWER_TRUST_STORE_SHA256: self.reviewer_trust_store_sha256,
            HEADER_RUNTIME_V3_SHA256: self.runtime_release_v3_sha256,
        }


class EvidenceRuntimeReadinessV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"]
    evidence_runtime_release: EvidenceRuntimeReleaseIdentityV3


class RuntimeV3ContractHashes(BaseModel):
    """Digests of the exact provider-owned V3 contract bytes in the image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_schema_sha256: Sha256
    readiness_schema_sha256: Sha256
    requirement_policy_schema_sha256: Sha256


def load_runtime_v3_contract_hashes(
    bundle_root: Path | None = None,
) -> RuntimeV3ContractHashes:
    configured_root = os.environ.get("EVIDENCE_PROVIDER_BUNDLE_ROOT")
    root = bundle_root
    if root is None:
        root = Path(configured_root) if configured_root else DEFAULT_PROVIDER_BUNDLE_ROOT
    root = root.resolve()

    def digest(relative_path: Path) -> str:
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - constants cannot escape
            raise ValueError("Evidence v3 schema path escapes provider bundle") from exc
        if not path.is_file():
            raise ValueError("Evidence v3 schema is unavailable")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    return RuntimeV3ContractHashes(
        retrieval_schema_sha256=digest(V3_RETRIEVAL_SCHEMA_PATH),
        readiness_schema_sha256=digest(V3_READINESS_SCHEMA_PATH),
        requirement_policy_schema_sha256=digest(REQUIREMENT_POLICY_SCHEMA_PATH),
    )


@dataclass(frozen=True, slots=True)
class RuntimeV3Lease:
    base_lease: GenerationRequestLease
    identity: EvidenceRuntimeReleaseIdentityV3
    policy_release: VerifiedRequirementPolicyRelease

    @property
    def token(self) -> tuple[object, ...]:
        return (
            *self.base_lease.token,
            self.identity.requirement_policy_manifest_sha256,
            self.identity.coverage_matrix_sha256,
            self.identity.requirement_policy_approval_sha256,
            self.identity.reviewer_trust_store_sha256,
            self.identity.runtime_release_v3_sha256,
        )


class RuntimeV3LeaseLoaderProtocol(Protocol):
    def acquire(self, deadline: RequestDeadline | None = None) -> RuntimeV3Lease: ...

    def assert_unchanged(
        self,
        lease: RuntimeV3Lease,
        deadline: RequestDeadline | None = None,
    ) -> None: ...


class RuntimeV3LeaseLoader:
    def __init__(
        self,
        *,
        base_loader: RuntimeLeaseLoader,
        policy_loader: Callable[[], VerifiedRequirementPolicyRelease] = lambda: (
            load_verified_requirement_policy_release(DEFAULT_POLICY_RELEASE_PATH)
        ),
        contract_hash_loader: Callable[[], RuntimeV3ContractHashes] = (
            load_runtime_v3_contract_hashes
        ),
    ) -> None:
        self._base_loader = base_loader
        self._policy_loader = policy_loader
        self._contract_hash_loader = contract_hash_loader

    def acquire(self, deadline: RequestDeadline | None = None) -> RuntimeV3Lease:
        base = self._base_loader.acquire(deadline)
        policy = self._policy_loader()
        contract_hashes = self._contract_hash_loader()
        release = policy.release
        release.assert_current_approved()
        if (
            release.knowledge_source_manifest_sha256
            != base.identity.knowledge_source_manifest_sha256
            or release.knowledge_snapshot_id != base.identity.knowledge_snapshot_id
        ):
            raise ValueError("requirement verification policy does not match knowledge generation")
        if (
            release.policy_manifest_sha256 is None
            or release.coverage_matrix_sha256 is None
            or release.approval_sha256 is None
        ):
            raise ValueError("approved requirement verification policy is incomplete")
        payload: dict[str, object] = {
            "contract": "evidence-runtime-release",
            "schema_version": "3.0",
            "retrieval_schema_version": "3.0",
            "base_runtime_release": base.identity.model_dump(mode="json"),
            **contract_hashes.model_dump(mode="json"),
            "requirement_policy_release": release.policy_release,
            "requirement_policy_manifest_sha256": release.policy_manifest_sha256,
            "coverage_matrix_sha256": release.coverage_matrix_sha256,
            "requirement_policy_approval_sha256": policy.verified_approval_sha256,
            "reviewer_trust_store_sha256": policy.reviewer_trust_store_sha256,
        }
        payload["runtime_release_v3_sha256"] = _canonical_sha256(payload)
        identity = EvidenceRuntimeReleaseIdentityV3.model_validate(payload)
        return RuntimeV3Lease(base_lease=base, identity=identity, policy_release=policy)

    def assert_unchanged(
        self,
        lease: RuntimeV3Lease,
        deadline: RequestDeadline | None = None,
    ) -> None:
        self._base_loader.assert_unchanged(lease.base_lease, deadline)
        current = self.acquire(deadline)
        if current.token != lease.token or current.identity != lease.identity:
            raise ValueError("Evidence runtime v3 changed during request")


def runtime_v3_binding_header_mismatches(
    headers: Mapping[str, str],
    identity: EvidenceRuntimeReleaseIdentityV3,
) -> tuple[str, ...]:
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
    "DEFAULT_POLICY_RELEASE_PATH",
    "DEFAULT_PROVIDER_BUNDLE_ROOT",
    "EvidenceRuntimeReadinessV3",
    "EvidenceRuntimeReleaseIdentityV3",
    "HEADER_REQUIREMENT_POLICY_APPROVAL_SHA256",
    "HEADER_REQUIREMENT_POLICY_SCHEMA_SHA256",
    "HEADER_V3_READINESS_SCHEMA_SHA256",
    "HEADER_V3_RETRIEVAL_SCHEMA_SHA256",
    "REQUIREMENT_POLICY_SCHEMA_PATH",
    "RuntimeV3Lease",
    "RuntimeV3ContractHashes",
    "RuntimeV3LeaseLoader",
    "RuntimeV3LeaseLoaderProtocol",
    "V3_READINESS_SCHEMA_PATH",
    "V3_RETRIEVAL_SCHEMA_PATH",
    "load_runtime_v3_contract_hashes",
    "runtime_v3_binding_header_mismatches",
]
