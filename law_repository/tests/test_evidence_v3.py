from __future__ import annotations

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence_v3 import RetrieveRequestV3, retrieve_evidence_v3


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


def test_v3_projects_data_evidence_without_a_policy_or_approval_gate() -> None:
    response = retrieve_evidence_v3(_request(), TestOnlyConformanceEvidenceBackend())

    assert response.schema_version == "3.0"
    requirement = next(
        evidence
        for result in response.results
        for evidence in result.evidence
        if evidence.required_document_code
    )
    assert requirement.eligibility == "finding"
    assert requirement.version_hash
    assert "requirement_verification" not in requirement.model_dump(mode="json")


def test_v3_request_rejects_duplicate_item_ids() -> None:
    payload = _request().model_dump(mode="json")
    payload["items"].append(dict(payload["items"][0]))
    try:
        RetrieveRequestV3.model_validate(payload)
    except ValueError as exc:
        assert "items.item_id must be unique" in str(exc)
    else:
        raise AssertionError("duplicate item IDs must be rejected")


def test_v3_response_accepts_only_a_complete_temporal_context() -> None:
    from src.evidence_v3 import RetrieveResponseV3

    payload = {
        "schema_version": "3.0",
        "request_id": "req-v3",
        "status": "partial",
        "knowledge_snapshot_id": "snapshot-1",
        "temporal_context": {
            "law_as_of": "2026-08-06",
            "hsk_snapshot_as_of": "2026-08-05",
            "requirements_snapshot_as_of": "2026-08-05",
        },
        "results": [],
        "errors": [],
    }
    response = RetrieveResponseV3.model_validate(payload)
    assert response.temporal_context is not None
    assert response.temporal_context.law_as_of.isoformat() == "2026-08-06"

    payload["temporal_context"].pop("requirements_snapshot_as_of")
    try:
        RetrieveResponseV3.model_validate(payload)
    except ValueError:
        pass
    else:
        raise AssertionError("partial temporal context must be rejected")
