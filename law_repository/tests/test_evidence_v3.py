from __future__ import annotations

from datetime import date

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence_v3 import (
    MappingRequirementPolicyResolver,
    RequirementPolicyBinding,
    RetrieveRequestV3,
    UNAPPROVED_HUMAN_ONLY_POLICY_SHA256,
    retrieve_evidence_v3,
)


def _request() -> RetrieveRequestV3:
    return RetrieveRequestV3.model_validate(
        {
            "schema_version": "3.0",
            "request_id": "req-v3",
            "case_id": "case-v3",
            "review_run_id": "run-v3",
            "review_as_of": "2026-07-10",
            "jurisdiction": "KR",
            "trade_type": "import",
            "top_k": 5,
            "items": [
                {
                    "item_id": "item-1",
                    "description": "Frozen mackerel",
                    "hs_code": "0303540000",
                    "origin_country": "NO",
                    "destination_country": "KR",
                }
            ],
            "uploaded_requirement_document_codes": [],
            "validation_issue_codes": [],
        }
    )


def test_v3_unmapped_requirement_is_explicit_human_only() -> None:
    response = retrieve_evidence_v3(
        _request(),
        TestOnlyConformanceEvidenceBackend(),
        MappingRequirementPolicyResolver({}),
    )

    assert response.schema_version == "3.0"
    requirement = next(
        evidence
        for result in response.results
        for evidence in result.evidence
        if evidence.required_document_code
    )
    assert requirement.requirement_verification is not None
    assert requirement.requirement_verification.verification_policy == "human_only"
    assert requirement.requirement_verification.approved is False
    assert (
        requirement.requirement_verification.policy_source_sha256
        == UNAPPROVED_HUMAN_ONLY_POLICY_SHA256
    )
    assert response.status == "partial"
    assert response.results[0].coverage == "partial"
    assert response.results[0].errors[0].code == "REQUIREMENT_POLICY_UNAVAILABLE"


def test_v3_approved_policy_is_projected_without_changing_v1_evidence() -> None:
    policy = RequirementPolicyBinding(
        required_document_code="CCCT_DOC_IMPORT_CONFIRMATION",
        verification_policy="content_match_required",
        policy_source_sha256="1" * 64,
        policy_release="requirement-policy-test-v1",
        approved=True,
    )
    response = retrieve_evidence_v3(
        _request(),
        TestOnlyConformanceEvidenceBackend(),
        MappingRequirementPolicyResolver({policy.required_document_code: policy}),
    )
    requirement = next(
        evidence
        for result in response.results
        for evidence in result.evidence
        if evidence.required_document_code == policy.required_document_code
    )

    assert requirement.requirement_verification == policy
    assert requirement.eligibility == "finding"
    assert requirement.version_hash


def test_v3_request_rejects_duplicate_item_ids() -> None:
    payload = _request().model_dump(mode="json")
    payload["items"].append(dict(payload["items"][0]))
    try:
        RetrieveRequestV3.model_validate(payload)
    except ValueError as exc:
        assert "items.item_id must be unique" in str(exc)
    else:
        raise AssertionError("duplicate item IDs must be rejected")
