from __future__ import annotations

from copy import deepcopy

from evals.structured.evaluator import evaluate_case, index_responses, load_records, summarize


def _gold_and_responses():
    gold = load_records()
    responses = index_responses(
        load_records("evals/structured/fixtures/offline_responses.jsonl")
    )
    return gold, responses


def test_offline_gold_evaluation_is_fully_green():
    gold, responses = _gold_and_responses()
    reports = [evaluate_case(case, responses[case["case_id"]]) for case in gold]
    summary = summarize(reports)
    assert summary["all_passed"] is True
    assert summary["cases_total"] == summary["cases_passed"] == 5
    assert summary["contract_valid_rate"] == 1.0
    assert summary["hs_resolution_accuracy"] == 1.0
    assert summary["requirements_found_accuracy"] == 1.0
    assert summary["required_evidence_recall"] == 1.0
    assert summary["evidence_metadata_completeness"] == 1.0
    assert summary["no_requirement_false_positive_rate"] == 0.0


def test_two_chemical_law_document_rows_are_independently_required():
    gold, responses = _gold_and_responses()
    case = next(
        item for item in gold if item["case_id"] == "gold_3808929000_distinct_document_rows"
    )
    response = deepcopy(responses[case["case_id"]])
    evidence = response["results"][0]["evidence"]
    response["results"][0]["evidence"] = [
        row for row in evidence if row["required_document_name"] != "화학물질 수입허가(신고)서"
    ]
    report = evaluate_case(case, response)
    assert report["passed"] is False
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert "evidence_row_2" in failed
    assert "minimum_evidence_records" in failed


def test_multi_agency_gold_rejects_single_agency_collapse():
    gold, responses = _gold_and_responses()
    case = next(
        item for item in gold if item["case_id"] == "gold_0101299000_multi_agency"
    )
    response = deepcopy(responses[case["case_id"]])
    cites = next(
        row
        for row in response["results"][0]["evidence"]
        if row["law_id"] == "71"
    )
    cites["agencies"] = ["유역(지방)환경청"]
    report = evaluate_case(case, response)
    assert report["passed"] is False
    assert any(
        check["name"] == "evidence_row_2" and not check["passed"]
        for check in report["checks"]
    )


def test_no_requirement_false_positive_metric_is_explicit():
    gold, responses = _gold_and_responses()
    case = next(
        item for item in gold if item["case_id"] == "gold_7318151000_no_requirements"
    )
    response = deepcopy(responses["gold_frozen_mackerel_exact"])
    response["request_id"] = case["request"]["request_id"]
    result = response["results"][0]
    result["item_id"] = case["expected"]["item_id"]
    result["resolved_hsk10"] = "7318151000"
    result["candidate_hsk10s"] = ["7318151000"]
    for evidence in result["evidence"]:
        evidence["item_id"] = result["item_id"]
    report = evaluate_case(case, response)
    assert report["passed"] is False
    assert report["metrics"]["negative_case"] == 1
    assert report["metrics"]["negative_false_positive"] == 1
