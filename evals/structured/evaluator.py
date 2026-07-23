"""Gold-set evaluation for Evidence Service responses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from evals.contract import ContractViolation, validate_request, validate_response


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD_PATH = ROOT / "evals" / "structured" / "fixtures" / "gold_cases.json"


def load_records(path: Path | str = DEFAULT_GOLD_PATH) -> list[dict[str, Any]]:
    """Load a JSON array or newline-delimited JSON records."""

    path = Path(path)
    with path.open(encoding="utf-8") as handle:
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in handle if line.strip()]
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"fixture must contain a JSON array: {path}")
    return payload


def index_responses(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        case_id = record["case_id"]
        if case_id in indexed:
            raise ValueError(f"duplicate response case_id: {case_id}")
        indexed[case_id] = record["response"]
    return indexed


def validate_gold_case(case: dict[str, Any]) -> None:
    required = {"case_id", "description", "request", "expected"}
    missing = required - case.keys()
    if missing:
        raise ValueError(f"gold case missing fields {sorted(missing)}")
    if case["case_id"] != case["request"]["case_id"]:
        raise ValueError("case_id must equal request.case_id")
    validate_request(case["request"])


def evaluate_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one live or offline response against one gold case."""

    validate_gold_case(case)
    checks: list[dict[str, Any]] = []
    try:
        validate_response(response)
        _check(checks, "contract_valid", True, "response satisfies v1 schema and invariants")
    except ContractViolation as exc:
        _check(checks, "contract_valid", False, "; ".join(exc.errors))
        return _case_report(case, response, checks, expected_rows=0, matched_rows=0)

    _check(
        checks,
        "request_id_correlated",
        response["request_id"] == case["request"]["request_id"],
        f"expected {case['request']['request_id']!r}, got {response['request_id']!r}",
    )

    expected = case["expected"]
    _check(
        checks,
        "status",
        response["status"] in expected["allowed_statuses"],
        f"allowed {expected['allowed_statuses']!r}, got {response['status']!r}",
    )
    result = next(
        (entry for entry in response["results"] if entry["item_id"] == expected["item_id"]),
        None,
    )
    _check(
        checks,
        "item_present",
        result is not None,
        (
            f"result {expected['item_id']!r} is present"
            if result is not None
            else f"missing result {expected['item_id']!r}"
        ),
    )
    if result is None:
        return _case_report(case, response, checks, expected_rows=0, matched_rows=0)

    scalar_fields = ("hs_resolution", "resolved_hsk10", "coverage", "requirements_found")
    for field in scalar_fields:
        _check(
            checks,
            field,
            result[field] == expected[field],
            f"expected {expected[field]!r}, got {result[field]!r}",
        )

    expected_candidates = set(expected.get("candidate_hsk10s", []))
    actual_candidates = set(result["candidate_hsk10s"])
    missing_candidates = sorted(expected_candidates - actual_candidates)
    _check(
        checks,
        "candidate_hsk10s",
        not missing_candidates,
        (
            "all required candidates are present"
            if not missing_candidates
            else f"missing candidates {missing_candidates!r}"
        ),
    )

    required_evidence = expected.get("required_evidence", [])
    available = list(enumerate(result["evidence"]))
    matched_indexes: set[int] = set()
    matched_rows = 0
    for row_index, evidence_expectation in enumerate(required_evidence):
        matches = _matching_evidence_indexes(
            available,
            evidence_expectation,
            matched_indexes,
        )
        passed = bool(matches)
        if matches:
            matched_indexes.update(matches)
            matched_rows += 1
        _check(
            checks,
            f"evidence_row_{row_index + 1}",
            passed,
            (
                f"distinct evidence matched {evidence_expectation!r}"
                if passed
                else f"no distinct evidence matched {evidence_expectation!r}"
            ),
        )

    minimum = expected.get("minimum_evidence_records", len(required_evidence))
    _check(
        checks,
        "minimum_evidence_records",
        len(result["evidence"]) >= minimum,
        f"expected >= {minimum}, got {len(result['evidence'])}",
    )
    if not expected.get("allow_additional_evidence", True):
        _check(
            checks,
            "no_additional_evidence",
            len(result["evidence"]) == len(required_evidence),
            f"expected exactly {len(required_evidence)}, got {len(result['evidence'])}",
        )

    return _case_report(
        case,
        response,
        checks,
        expected_rows=len(required_evidence),
        matched_rows=matched_rows,
    )


def summarize(case_reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    reports = list(case_reports)
    total = len(reports)
    passed = sum(report["passed"] for report in reports)
    contract_valid = sum(report["metrics"]["contract_valid"] for report in reports)
    hs_correct = sum(report["metrics"]["hs_resolution_correct"] for report in reports)
    requirement_correct = sum(report["metrics"]["requirements_found_correct"] for report in reports)
    expected_rows = sum(report["metrics"]["expected_evidence_rows"] for report in reports)
    matched_rows = sum(report["metrics"]["matched_evidence_rows"] for report in reports)
    evidence_records = sum(report["metrics"]["evidence_records"] for report in reports)
    complete_evidence = sum(report["metrics"]["complete_evidence_records"] for report in reports)
    negative_cases = sum(report["metrics"]["negative_case"] for report in reports)
    false_positive_cases = sum(report["metrics"]["negative_false_positive"] for report in reports)

    return {
        "cases_total": total,
        "cases_passed": passed,
        "case_pass_rate": _ratio(passed, total),
        "contract_valid_rate": _ratio(contract_valid, total),
        "hs_resolution_accuracy": _ratio(hs_correct, total),
        "requirements_found_accuracy": _ratio(requirement_correct, total),
        "required_evidence_recall": _ratio(matched_rows, expected_rows),
        "evidence_metadata_completeness": _ratio(complete_evidence, evidence_records),
        "no_requirement_false_positive_rate": _ratio(false_positive_cases, negative_cases),
        "all_passed": passed == total and total > 0,
        "cases": reports,
    }


def _case_report(
    case: dict[str, Any],
    response: dict[str, Any],
    checks: list[dict[str, Any]],
    expected_rows: int,
    matched_rows: int,
) -> dict[str, Any]:
    expected = case["expected"]
    result = next(
        (
            entry
            for entry in response.get("results", [])
            if entry.get("item_id") == expected["item_id"]
        ),
        None,
    )
    evidence = result.get("evidence", []) if result else []
    # `requirements_found=false` with partial/unknown coverage does not prove
    # that requirements do not exist.  Only complete coverage is a legitimate
    # no-requirement (negative) case for the false-positive metric.
    negative_case = not expected["requirements_found"] and expected.get("coverage") == "complete"
    negative_false_positive = bool(result and negative_case and result.get("requirements_found"))
    check_map = {check["name"]: check["passed"] for check in checks}
    return {
        "case_id": case["case_id"],
        "description": case["description"],
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "metrics": {
            "contract_valid": int(check_map.get("contract_valid", False)),
            "hs_resolution_correct": int(check_map.get("hs_resolution", False)),
            "requirements_found_correct": int(check_map.get("requirements_found", False)),
            "expected_evidence_rows": expected_rows,
            "matched_evidence_rows": matched_rows,
            "evidence_records": len(evidence),
            "complete_evidence_records": sum(_metadata_complete(item) for item in evidence),
            "negative_case": int(negative_case),
            "negative_false_positive": int(negative_false_positive),
        },
    }


def _check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def _evidence_matches(evidence: dict[str, Any], expected: dict[str, Any]) -> bool:
    for field, expected_value in expected.items():
        if field == "agencies":
            if not set(expected_value).issubset(set(evidence.get("agencies", []))):
                return False
        elif evidence.get(field) != expected_value:
            return False
    return True


def _matching_evidence_indexes(
    available: list[tuple[int, dict[str, Any]]],
    expected: dict[str, Any],
    used_indexes: set[int],
) -> set[int]:
    """Match one expected row, or an agency union split across lossless rows.

    CCCT stores one canonical Requirement per agency. A gold expectation may
    therefore assert that the union of otherwise-identical evidence rows
    contains all expected agencies without requiring the service to collapse
    them back into one lossy record.
    """

    expected_agencies = set(expected.get("agencies", []))
    if len(expected_agencies) <= 1:
        for index, evidence in available:
            if index not in used_indexes and _evidence_matches(evidence, expected):
                return {index}
        return set()

    without_agencies = {key: value for key, value in expected.items() if key != "agencies"}
    candidates = [
        (index, evidence)
        for index, evidence in available
        if index not in used_indexes and _evidence_matches(evidence, without_agencies)
    ]
    actual_agencies = {
        agency for _, evidence in candidates for agency in evidence.get("agencies", [])
    }
    if not expected_agencies.issubset(actual_agencies):
        return set()
    return {
        index
        for index, evidence in candidates
        if expected_agencies.intersection(evidence.get("agencies", []))
    }


def _metadata_complete(evidence: dict[str, Any]) -> int:
    common = (
        evidence.get("evidence_id"),
        evidence.get("source_record_id"),
        evidence.get("source_uri"),
        evidence.get("version_hash"),
        evidence.get("retrieval_methods"),
    )
    if not all(common):
        return 0
    if evidence.get("source_type") == "customs_requirement":
        requirement = (
            evidence.get("law_id"),
            evidence.get("law_name"),
            evidence.get("valid_from"),
            evidence.get("matched_hs_code"),
            evidence.get("required_document_code"),
            evidence.get("required_document_name"),
            evidence.get("agencies"),
        )
        return int(all(requirement))
    return 1


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)
