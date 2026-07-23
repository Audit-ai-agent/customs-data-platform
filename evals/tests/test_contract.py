from __future__ import annotations

from copy import deepcopy

import pytest

from evals.contract import (
    ContractViolation,
    load_schema,
    stable_evidence_id,
    validate_message,
    validate_request,
    validate_response,
)
from evals.structured.evaluator import index_responses, load_records, validate_gold_case


def _fixtures():
    gold = load_records()
    responses = index_responses(load_records("evals/structured/fixtures/offline_responses.jsonl"))
    return gold, responses


def test_schema_exposes_the_frozen_v1_wire_fields():
    schema = load_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(schema["$defs"]["RetrieveResponse"]["properties"]) == {
        "schema_version",
        "request_id",
        "status",
        "knowledge_snapshot_id",
        "results",
        "errors",
    }
    result_required = set(schema["$defs"]["RetrieveResult"]["required"])
    assert result_required == {
        "item_id",
        "hs_resolution",
        "resolved_hsk10",
        "candidate_hsk10s",
        "coverage",
        "requirements_found",
        "evidence",
        "errors",
    }
    evidence_required = set(schema["$defs"]["Evidence"]["required"])
    assert evidence_required == {
        "evidence_id",
        "source_record_id",
        "source_type",
        "item_id",
        "title",
        "text",
        "law_id",
        "law_name",
        "article_no",
        "source_uri",
        "valid_from",
        "valid_to",
        "version_hash",
        "matched_hs_code",
        "trade_type",
        "required_document_code",
        "required_document_name",
        "agencies",
        "retrieval_methods",
        "eligibility",
        "score",
        "raw_scores",
    }


def test_every_gold_request_and_offline_response_satisfies_contract():
    gold, responses = _fixtures()
    assert len(gold) == len(responses) == 5
    for case in gold:
        validate_gold_case(case)
        assert validate_message(case["request"]) == "request"
        response = responses[case["case_id"]]
        validate_response(response)
        assert validate_message(response) == "response"
        assert response["request_id"] == case["request"]["request_id"]


def test_request_boundary_limits_are_frozen():
    request = deepcopy(_fixtures()[0][0]["request"])
    request["top_k"] = 100
    validate_request(request)

    request["top_k"] = 101
    with pytest.raises(ContractViolation):
        validate_request(request)

    request = deepcopy(_fixtures()[0][0]["request"])
    request["items"][0]["origin_country"] = "NOR"
    with pytest.raises(ContractViolation):
        validate_request(request)


def test_stable_evidence_id_is_source_version_bound_and_deterministic():
    source_id = "ccct:0303540000:import:89:005:20200406"
    version_hash = "3ef8222c7c7dd7cd04fde1733b4e6352020e6d002182e084513cc5b8bf979d8c"
    expected = "ev_28c68c16fc6df5a983d86e1d3addc4297240280bf41635aedaae1e65013af3a2"
    assert stable_evidence_id(source_id, version_hash) == expected
    assert stable_evidence_id(source_id, version_hash) == expected
    assert stable_evidence_id(source_id + ":changed", version_hash) != expected


def test_ambiguous_hs6_cannot_be_silently_zero_padded():
    _, responses = _fixtures()
    response = deepcopy(responses["gold_hs6_731815_ambiguous"])
    result = response["results"][0]
    result["resolved_hsk10"] = "7318150000"
    with pytest.raises(ContractViolation, match="ambiguous requires null"):
        validate_response(response)


def test_requirements_found_cannot_be_true_without_finding_evidence():
    _, responses = _fixtures()
    response = deepcopy(responses["gold_7318151000_no_requirements"])
    response["results"][0]["requirements_found"] = True
    with pytest.raises(ContractViolation, match="finding-eligible"):
        validate_response(response)


def test_evidence_id_must_match_source_record_and_version():
    _, responses = _fixtures()
    response = deepcopy(responses["gold_frozen_mackerel_exact"])
    response["results"][0]["evidence"][0]["evidence_id"] = "ev_" + "0" * 64
    with pytest.raises(ContractViolation, match="SHA-256"):
        validate_response(response)


def test_error_and_agency_wire_shapes_are_strict():
    _, responses = _fixtures()
    response = deepcopy(responses["gold_frozen_mackerel_exact"])
    response["status"] = "partial"
    response["results"][0]["coverage"] = "partial"
    response["results"][0]["errors"] = [
        {
            "code": "QDRANT_TIMEOUT",
            "message": "vector index timed out",
            "retryable": True,
            "component": "qdrant",
        }
    ]
    with pytest.raises(ContractViolation, match="additional property"):
        validate_response(response)

    response = deepcopy(responses["gold_frozen_mackerel_exact"])
    response["results"][0]["evidence"][0]["agencies"] = [
        {"agency_code": "005", "agency_name": "식품의약품안전처"}
    ]
    with pytest.raises(ContractViolation, match="expected string"):
        validate_response(response)


def test_request_item_rejects_derived_hs_fields_from_the_wire():
    gold, _ = _fixtures()
    request = deepcopy(gold[0]["request"])
    request["items"][0]["hs6"] = "731815"
    with pytest.raises(ContractViolation, match="additional property"):
        validate_request(request)


def test_supporting_evidence_allows_null_hs_score_and_empty_raw_scores():
    _, responses = _fixtures()
    response = deepcopy(responses["gold_7318151000_no_requirements"])
    source_record_id = "law:sample:article:1"
    version_hash = "0" * 64
    response["results"][0]["evidence"] = [
        {
            "evidence_id": stable_evidence_id(source_record_id, version_hash),
            "source_record_id": source_record_id,
            "source_type": "law_article",
            "item_id": "item_machine_screw_1",
            "title": "지원 법령 조문",
            "text": "품목 요건 판단의 참고 자료로만 사용하는 법령 조문이다.",
            "law_id": "sample-law",
            "law_name": "샘플 법령",
            "article_no": "제1조",
            "source_uri": "https://example.test/laws/sample/1",
            "valid_from": "2026-01-01",
            "valid_to": None,
            "version_hash": version_hash,
            "matched_hs_code": None,
            "trade_type": "import",
            "required_document_code": None,
            "required_document_name": None,
            "agencies": [],
            "retrieval_methods": ["dense"],
            "eligibility": "supporting",
            "score": None,
            "raw_scores": {},
        }
    ]
    validate_response(response)
