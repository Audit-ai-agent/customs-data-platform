"""Additive Evidence v3 contract with requirement-verification policy.

V1 remains the canonical retrieval implementation.  V3 projects the same
source-bound evidence and adds an approved policy describing what constitutes
verification of each supporting document.  Missing policy never becomes a
permissive default: it is explicitly ``human_only`` and therefore cannot make
an Agent run eligible for automatic completion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.evidence import (
    Evidence,
    EvidenceBackend,
    EvidenceError,
    RetrieveItem,
    RetrieveRequest,
    retrieve_evidence,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty128 = Annotated[str, Field(min_length=1, max_length=128)]
VerificationPolicy = Literal[
    "presence_only",
    "content_match_required",
    "external_verification_required",
    "human_only",
]

UNAPPROVED_HUMAN_ONLY_POLICY_SHA256 = hashlib.sha256(
    b"customs-evidence/unapproved-human-only/v1"
).hexdigest()


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrieveRequestV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    case_id: NonEmpty128
    review_run_id: NonEmpty128
    review_as_of: date
    jurisdiction: Literal["KR"]
    trade_type: Literal["import", "export"]
    top_k: int = Field(ge=1, le=100)
    items: list[RetrieveItem] = Field(min_length=1, max_length=100)
    uploaded_requirement_document_codes: list[NonEmpty128] = Field(
        default_factory=list,
        json_schema_extra={"default": [], "uniqueItems": True},
    )
    validation_issue_codes: list[NonEmpty128] = Field(
        default_factory=list,
        json_schema_extra={"default": [], "uniqueItems": True},
    )
    query_intent: str = Field(default="required_import_documents", min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_values(self) -> RetrieveRequestV3:
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("items.item_id must be unique")
        for name in (
            "uploaded_requirement_document_codes",
            "validation_issue_codes",
        ):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")
        return self

    def as_v1(self) -> RetrieveRequest:
        payload = self.model_dump(mode="json")
        payload["schema_version"] = "1.0"
        return RetrieveRequest.model_validate(payload)


class RequirementPolicyBinding(ContractModel):
    required_document_code: NonEmpty128
    verification_policy: VerificationPolicy
    policy_source_sha256: Sha256
    policy_release: str = Field(min_length=1, max_length=128)
    approved: bool

    @model_validator(mode="after")
    def unapproved_policy_is_human_only(self) -> RequirementPolicyBinding:
        if not self.approved and self.verification_policy != "human_only":
            raise ValueError("unapproved verification policy must be human_only")
        return self


class EvidenceV3(Evidence):
    requirement_verification: RequirementPolicyBinding | None


class ItemEvidenceResultV3(ContractModel):
    item_id: NonEmpty128
    hs_resolution: Literal["exact", "unique_descendant", "ambiguous", "invalid", "missing"]
    resolved_hsk10: Annotated[str, Field(pattern=r"^[0-9]{10}$")] | None
    candidate_hsk10s: list[Annotated[str, Field(pattern=r"^[0-9]{10}$")]] = Field(
        json_schema_extra={"uniqueItems": True}
    )
    coverage: Literal["complete", "partial", "unknown"]
    requirements_found: bool
    evidence: list[EvidenceV3]
    errors: list[EvidenceError]


class RetrieveResponseV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    status: Literal["ok", "partial", "failed"]
    knowledge_snapshot_id: str | None
    results: list[ItemEvidenceResultV3] = Field(max_length=100)
    errors: list[EvidenceError] = Field(default_factory=list)


class RequirementPolicyResolver(Protocol):
    def resolve(self, required_document_code: str) -> RequirementPolicyBinding: ...


@dataclass(frozen=True, slots=True)
class MappingRequirementPolicyResolver:
    """Closed in-memory resolver used by verified bundle loaders and tests."""

    policies: Mapping[str, RequirementPolicyBinding]

    def resolve(self, required_document_code: str) -> RequirementPolicyBinding:
        return self.policies.get(required_document_code) or RequirementPolicyBinding(
            required_document_code=required_document_code,
            verification_policy="human_only",
            policy_source_sha256=UNAPPROVED_HUMAN_ONLY_POLICY_SHA256,
            policy_release="unapproved-human-only/v1",
            approved=False,
        )


def retrieve_evidence_v3(
    request: RetrieveRequestV3,
    backend: EvidenceBackend,
    policy_resolver: RequirementPolicyResolver,
) -> RetrieveResponseV3:
    """Run frozen V1 retrieval and add fail-closed requirement policy."""

    response = retrieve_evidence(request.as_v1(), backend)
    results: list[ItemEvidenceResultV3] = []
    has_unapproved_policy = False
    for result in response.results:
        evidence_rows: list[EvidenceV3] = []
        item_has_unapproved_policy = False
        for evidence in result.evidence:
            payload = evidence.model_dump(mode="json")
            code = evidence.required_document_code
            binding = policy_resolver.resolve(code) if code else None
            if binding is not None and not binding.approved:
                item_has_unapproved_policy = True
                has_unapproved_policy = True
            payload["requirement_verification"] = (
                binding.model_dump(mode="json") if binding else None
            )
            evidence_rows.append(EvidenceV3.model_validate(payload))
        result_payload = result.model_dump(mode="json")
        result_payload["evidence"] = [row.model_dump(mode="json") for row in evidence_rows]
        if item_has_unapproved_policy:
            result_payload["coverage"] = "partial"
            result_payload["errors"] = [
                *result_payload["errors"],
                {
                    "code": "REQUIREMENT_POLICY_UNAVAILABLE",
                    "message": "Requirement verification policy is unavailable",
                    "retryable": False,
                },
            ]
        results.append(ItemEvidenceResultV3.model_validate(result_payload))
    status = response.status
    if has_unapproved_policy and status == "ok":
        status = "partial"
    return RetrieveResponseV3(
        schema_version="3.0",
        request_id=response.request_id,
        status=status,
        knowledge_snapshot_id=response.knowledge_snapshot_id,
        results=results,
        errors=response.errors,
    )


__all__ = [
    "EvidenceV3",
    "ItemEvidenceResultV3",
    "MappingRequirementPolicyResolver",
    "RequirementPolicyBinding",
    "RequirementPolicyResolver",
    "RetrieveRequestV3",
    "RetrieveResponseV3",
    "UNAPPROVED_HUMAN_ONLY_POLICY_SHA256",
    "VerificationPolicy",
    "retrieve_evidence_v3",
]
