"""Evaluate the preserved three-law V3 diagnostic over actual loopback HTTP.

This runner measures only the restricted 3/58 corpus.  It sends the existing
five Hybrid Gold queries through the real V3 endpoint and separately checks one
HSK 0303540000 Structured lookup.  It never changes a store, runtime identity,
release lock, or automatic-completion policy.

The Hybrid evaluator consumes a V1-compatible projection because V3
intentionally adds ``REQUIREMENT_POLICY_UNAVAILABLE`` and changes the result to
``partial`` for every real requirement in this diagnostic.  The projection
removes only that V3 policy overlay; it does not add, reorder, or alter retrieved
law evidence.  The original V3 policy outcome is reported separately.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from evals.hybrid.evaluator import (  # noqa: E402
    build_request,
    evaluate_hybrid_case,
    load_hybrid_gold,
    summarize_hybrid,
)
from src.evidence_v3 import RetrieveResponseV3  # noqa: E402
from src.runtime_release_v3 import EvidenceRuntimeReleaseIdentityV3  # noqa: E402
from tools import run_three_law_v2_diagnostic as v2  # noqa: E402
from tools.run_evidence_v2_conformance_server import load_bearer_token_file  # noqa: E402
from tools.run_three_law_v3_diagnostic import (  # noqa: E402
    DIAGNOSTIC_PROFILE,
    DIAGNOSTIC_READINESS_PATH,
    DIAGNOSTIC_SCOPE,
    EXPECTED_DIAGNOSTIC_ARTICLE_COUNT,
    EXPECTED_DIAGNOSTIC_LAW_COUNT,
    EXPECTED_PRODUCTION_LAW_COUNT,
)

MODE = "THREE_LAW_RESTRICTED_RAG_DIAGNOSTIC"
_RUNTIME_READINESS_PATH = "/internal/rag/v3/health/ready"
_RUNTIME_RETRIEVAL_PATH = "/internal/rag/v3/retrieve"
_POLICY_ERROR = "REQUIREMENT_POLICY_UNAVAILABLE"
_MAX_IDENTITY_BYTES = 64 * 1024
_MAX_HTTP_RESPONSE_BYTES = 8 * 1024 * 1024


class RestrictedEvaluationError(RuntimeError):
    """The bounded diagnostic evaluation cannot be trusted."""


def _literal_loopback_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise RestrictedEvaluationError("Evidence base URL is invalid") from None
    if (
        value != value.strip()
        or parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RestrictedEvaluationError("Evidence base URL must be a literal loopback origin")
    return value.rstrip("/")


def load_identity(path: Path) -> EvidenceRuntimeReleaseIdentityV3:
    try:
        raw = path.read_bytes()
    except OSError:
        raise RestrictedEvaluationError("Evidence V3 identity file is unavailable") from None
    if not raw or len(raw) > _MAX_IDENTITY_BYTES:
        raise RestrictedEvaluationError("Evidence V3 identity file size is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
        return EvidenceRuntimeReleaseIdentityV3.model_validate(payload)
    except (UnicodeError, ValueError, TypeError):
        raise RestrictedEvaluationError("Evidence V3 identity file is invalid") from None


def _response_json(response: requests.Response) -> dict[str, Any]:
    if len(response.content) > _MAX_HTTP_RESPONSE_BYTES:
        raise RestrictedEvaluationError("Evidence response exceeds the diagnostic byte limit")
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        raise RestrictedEvaluationError("Evidence response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise RestrictedEvaluationError("Evidence response JSON is not an object")
    return payload


def _assert_response_identity(
    response: requests.Response,
    identity: EvidenceRuntimeReleaseIdentityV3,
    *,
    request_id: str | None = None,
) -> None:
    for name, expected in identity.binding_headers().items():
        if response.headers.get(name) != expected:
            raise RestrictedEvaluationError("Evidence response identity header mismatch")
    if request_id is not None and response.headers.get("X-Request-ID") != request_id:
        raise RestrictedEvaluationError("Evidence response request identity mismatch")
    if not response.headers.get("X-Service-Request-ID"):
        raise RestrictedEvaluationError("Evidence service request identity is missing")


def project_v3_to_hybrid_v1(response: RetrieveResponseV3) -> dict[str, Any]:
    """Remove only V3 requirement-policy overlay for ranking evaluation."""

    payload = copy.deepcopy(response.model_dump(mode="json"))
    payload["schema_version"] = "1.0"
    has_non_policy_error = bool(payload["errors"])
    for result in payload["results"]:
        original_errors = list(result["errors"])
        result["errors"] = [
            error for error in original_errors if error.get("code") != _POLICY_ERROR
        ]
        has_non_policy_error = has_non_policy_error or bool(result["errors"])
        if (
            result["coverage"] == "partial"
            and original_errors
            and not result["errors"]
            and all(error.get("code") == _POLICY_ERROR for error in original_errors)
        ):
            result["coverage"] = "complete"
        for evidence in result["evidence"]:
            evidence.pop("requirement_verification", None)
    if payload["status"] == "partial" and not has_non_policy_error:
        payload["status"] = "ok"
    return payload


def _hybrid_request(case: dict[str, Any], review_as_of: date) -> dict[str, Any]:
    body = build_request(case, review_as_of.isoformat())
    identity = f"hybrid-eval-{case['case_id']}"
    body.update(
        {
            "schema_version": "3.0",
            "request_id": identity,
            "case_id": identity,
            "review_run_id": identity,
        }
    )
    # The preserved CCCT snapshot is known to cover this exact HSK.  Retrieval
    # only reaches the law-support arm after Structured coverage is proven.
    body["items"][0]["hs_code"] = "0303540000"
    return body


def _structured_request(review_as_of: date) -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "request_id": "structured-eval-0303540000",
        "case_id": "structured-eval-0303540000",
        "review_run_id": "structured-eval-0303540000",
        "review_as_of": review_as_of.isoformat(),
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 10,
        "items": [
            {
                "item_id": "structured-item-0303540000",
                "description": "냉동 고등어",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }
        ],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "required_import_documents",
    }


def _gold_scope_metadata(gold: list[dict[str, Any]]) -> dict[str, object]:
    loaded_law_ids = sorted(v2.DIAGNOSTIC_LAW_IDS)
    relevant_law_ids = sorted(
        {
            str(relevant["law_id"])
            for case in gold
            for relevant in case["relevant"]
        }
    )
    covered = sorted(set(loaded_law_ids) & set(relevant_law_ids))
    all_loaded_covered = set(loaded_law_ids).issubset(relevant_law_ids)
    return {
        "loaded_law_ids": loaded_law_ids,
        "hybrid_gold_relevant_law_ids": relevant_law_ids,
        "hybrid_gold_law_coverage": f"{len(covered)}/{len(loaded_law_ids)}",
        "all_three_laws_hybrid_gold_covered": all_loaded_covered,
    }


def _post(
    session: requests.Session,
    *,
    base_url: str,
    token: str,
    identity: EvidenceRuntimeReleaseIdentityV3,
    body: dict[str, Any],
    timeout_seconds: float,
) -> tuple[RetrieveResponseV3, requests.Response, float]:
    request_id = str(body["request_id"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        **identity.binding_headers(),
    }
    started = time.monotonic()
    try:
        response = session.post(
            f"{base_url}{_RUNTIME_RETRIEVAL_PATH}",
            json=body,
            headers=headers,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
    except requests.RequestException:
        raise RestrictedEvaluationError("Evidence retrieval request failed") from None
    latency_ms = (time.monotonic() - started) * 1_000
    if response.status_code != 200:
        raise RestrictedEvaluationError("Evidence retrieval did not return HTTP 200")
    _assert_response_identity(response, identity, request_id=request_id)
    try:
        result = RetrieveResponseV3.model_validate(_response_json(response))
    except ValueError:
        raise RestrictedEvaluationError("Evidence retrieval response violates V3") from None
    if result.request_id != request_id:
        raise RestrictedEvaluationError("Evidence response body request identity mismatch")
    return result, response, latency_ms


def _structured_report(result: RetrieveResponseV3) -> dict[str, Any]:
    if len(result.results) != 1:
        return {"passed": False, "reason": "result_count_mismatch"}
    item = result.results[0]
    requirements = [
        evidence for evidence in item.evidence if evidence.source_type == "customs_requirement"
    ]
    exact_source = bool(requirements) and all(
        evidence.matched_hs_code == "0303540000"
        and evidence.source_record_id
        and evidence.source_uri
        and evidence.version_hash
        and set(evidence.retrieval_methods) == {"exact_hs", "graph"}
        for evidence in requirements
    )
    policy_fail_closed = bool(requirements) and all(
        evidence.requirement_verification is not None
        and evidence.requirement_verification.verification_policy == "human_only"
        and evidence.requirement_verification.approved is False
        for evidence in requirements
    )
    policy_error_present = any(error.code == _POLICY_ERROR for error in item.errors)
    checks = {
        "request_status_partial_due_to_policy": result.status == "partial",
        "knowledge_snapshot_present": bool(result.knowledge_snapshot_id),
        "hs_resolution_exact": item.hs_resolution == "exact",
        "resolved_hsk10_exact": item.resolved_hsk10 == "0303540000",
        "requirements_found": item.requirements_found,
        "requirement_count_positive": bool(requirements),
        "source_metadata_complete": exact_source,
        "unapproved_policy_human_only": policy_fail_closed,
        "requirement_policy_error_present": policy_error_present,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "requirement_count": len(requirements),
        "required_document_codes": sorted(
            {
                evidence.required_document_code
                for evidence in requirements
                if evidence.required_document_code
            }
        ),
    }


@dataclass(frozen=True, slots=True)
class RestrictedEvaluationReport:
    payload: dict[str, object]

    def to_wire(self) -> dict[str, object]:
        return self.payload


def run_restricted_evaluation(
    *,
    evidence_base_url: str,
    service_token_file: Path,
    identity_file: Path,
    review_as_of: date,
    timeout_seconds: float,
    session: requests.Session | None = None,
) -> dict[str, object]:
    base_url = _literal_loopback_origin(evidence_base_url)
    token = load_bearer_token_file(service_token_file)
    identity = load_identity(identity_file)
    client = session or requests.Session()
    owns_session = session is None
    try:
        auth = {"Authorization": f"Bearer {token}"}
        try:
            standard_ready = client.get(
                f"{base_url}{_RUNTIME_READINESS_PATH}",
                headers=auth,
                timeout=timeout_seconds,
                allow_redirects=False,
            )
            diagnostic_ready = client.get(
                f"{base_url}{DIAGNOSTIC_READINESS_PATH}",
                headers=auth,
                timeout=timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RestrictedEvaluationError("Evidence readiness request failed") from None
        if standard_ready.status_code != 200 or diagnostic_ready.status_code != 200:
            raise RestrictedEvaluationError("Evidence diagnostic is not ready")
        standard_payload = _response_json(standard_ready)
        diagnostic_payload = _response_json(diagnostic_ready)
        expected_identity = identity.model_dump(mode="json")
        if standard_payload.get("evidence_runtime_release") != expected_identity:
            raise RestrictedEvaluationError("standard readiness identity mismatch")
        if diagnostic_payload.get("evidence_runtime_release") != expected_identity:
            raise RestrictedEvaluationError("diagnostic readiness identity mismatch")
        if (
            diagnostic_payload.get("diagnostic_profile") != DIAGNOSTIC_PROFILE
            or diagnostic_payload.get("scope") != DIAGNOSTIC_SCOPE
            or diagnostic_payload.get("observed_law_count") != EXPECTED_DIAGNOSTIC_LAW_COUNT
            or diagnostic_payload.get("expected_production_law_count")
            != EXPECTED_PRODUCTION_LAW_COUNT
            or diagnostic_payload.get("observed_article_count")
            != EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
            or diagnostic_payload.get("qdrant_point_count")
            != EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
            or diagnostic_payload.get("neo4j_article_count")
            != EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
            or diagnostic_payload.get("release_ready") is not False
            or diagnostic_payload.get("rag_quality_evaluated") is not False
            or diagnostic_payload.get("automatic_completion_eligible") is not False
        ):
            raise RestrictedEvaluationError("diagnostic readiness provenance mismatch")
        supported_as_of = identity.base_runtime_release.supported_review_as_of
        if review_as_of != supported_as_of:
            raise RestrictedEvaluationError(
                "review_as_of does not equal the runtime-supported date"
            )

        gold = load_hybrid_gold()
        gold_scope = _gold_scope_metadata(gold)
        hybrid_reports: list[dict[str, Any]] = []
        v3_policy_partial_cases = 0
        law_method_counts: Counter[str] = Counter()
        law_score_component_counts: Counter[str] = Counter()
        law_evidence_count = 0
        for case in gold:
            v3_result, _response, latency_ms = _post(
                client,
                base_url=base_url,
                token=token,
                identity=identity,
                body=_hybrid_request(case, review_as_of),
                timeout_seconds=timeout_seconds,
            )
            if v3_result.status == "partial" and all(
                not result.errors
                or all(error.code == _POLICY_ERROR for error in result.errors)
                for result in v3_result.results
            ):
                v3_policy_partial_cases += 1
            case_method_counts: Counter[str] = Counter()
            case_score_component_counts: Counter[str] = Counter()
            for item_result in v3_result.results:
                for evidence in item_result.evidence:
                    if evidence.source_type != "law_article":
                        continue
                    law_evidence_count += 1
                    case_method_counts.update(evidence.retrieval_methods)
                    case_score_component_counts.update(evidence.raw_scores)
            law_method_counts.update(case_method_counts)
            law_score_component_counts.update(case_score_component_counts)
            projected = project_v3_to_hybrid_v1(v3_result)
            report = evaluate_hybrid_case(case, projected)
            report["latency_ms"] = round(latency_ms, 3)
            report["law_retrieval_method_counts"] = dict(sorted(case_method_counts.items()))
            report["law_score_component_counts"] = dict(
                sorted(case_score_component_counts.items())
            )
            hybrid_reports.append(report)
        hybrid_summary = summarize_hybrid(hybrid_reports)
        ranking_all_passed = bool(hybrid_summary["all_passed"])
        required_methods = {"dense", "bm25", "graph"}
        required_score_components = {"rrf", "lexical_overlap"}
        observed_methods = set(law_method_counts)
        observed_score_components = set(law_score_component_counts)
        retrieval_path_coverage_passed = (
            required_methods.issubset(observed_methods)
            and required_score_components.issubset(observed_score_components)
            and law_evidence_count > 0
        )
        hybrid_summary["v3_policy_partial_cases"] = v3_policy_partial_cases
        hybrid_summary["projection"] = "REMOVE_V3_REQUIREMENT_POLICY_OVERLAY_ONLY"
        hybrid_summary["law_evidence_count"] = law_evidence_count
        hybrid_summary["law_retrieval_method_counts"] = dict(sorted(law_method_counts.items()))
        hybrid_summary["law_score_component_counts"] = dict(
            sorted(law_score_component_counts.items())
        )
        hybrid_summary["required_law_retrieval_methods"] = sorted(required_methods)
        hybrid_summary["required_law_score_components"] = sorted(required_score_components)
        hybrid_summary["ranking_all_passed"] = ranking_all_passed
        hybrid_summary["retrieval_path_coverage_passed"] = retrieval_path_coverage_passed
        hybrid_summary["all_passed"] = ranking_all_passed and retrieval_path_coverage_passed

        structured_result, _structured_response, structured_latency_ms = _post(
            client,
            base_url=base_url,
            token=token,
            identity=identity,
            body=_structured_request(review_as_of),
            timeout_seconds=timeout_seconds,
        )
        structured = _structured_report(structured_result)
        structured["latency_ms"] = round(structured_latency_ms, 3)

        all_passed = bool(hybrid_summary["all_passed"]) and bool(structured["passed"])
        return {
            "schema_version": "three-law-restricted-rag-diagnostic/v1",
            "mode": MODE,
            "diagnostic_profile": DIAGNOSTIC_PROFILE,
            "scope": DIAGNOSTIC_SCOPE,
            "production_claim": False,
            "production_release_ready": False,
            "automatic_completion_eligible": False,
            "diagnostic_rag_quality_evaluated": True,
            "production_rag_quality_evaluated": False,
            "review_as_of": review_as_of.isoformat(),
            "knowledge_snapshot_id": identity.base_runtime_release.knowledge_snapshot_id,
            "runtime_release_v3_sha256": identity.runtime_release_v3_sha256,
            "expected_production_law_count": EXPECTED_PRODUCTION_LAW_COUNT,
            "observed_law_count": EXPECTED_DIAGNOSTIC_LAW_COUNT,
            "observed_article_count": EXPECTED_DIAGNOSTIC_ARTICLE_COUNT,
            **gold_scope,
            "hybrid": hybrid_summary,
            "structured": structured,
            "all_passed": all_passed,
        }
    finally:
        if owns_session:
            client.close()


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from None
    if not 0 < parsed <= 120:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 120")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--diagnostic-only-three-law", action="store_true", required=True)
    parser.add_argument("--evidence-base-url", required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--review-as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=_positive_timeout, default=75.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.diagnostic_only_three_law:
            raise RestrictedEvaluationError("explicit diagnostic acknowledgement is required")
        report = run_restricted_evaluation(
            evidence_base_url=args.evidence_base_url,
            service_token_file=args.service_token_file,
            identity_file=args.identity_file,
            review_as_of=args.review_as_of,
            timeout_seconds=args.timeout_seconds,
        )
        v2.write_diagnostic_manifest(
            RestrictedEvaluationReport(report),
            args.output,
            forbidden_roots=(ROOT,),
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "three_law_restricted_rag_evaluation_failed",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 70
    print(
        json.dumps(
            {
                "event": "three_law_restricted_rag_evaluation_completed",
                "mode": MODE,
                "production_claim": False,
                "all_passed": report["all_passed"],
                "cases_total": cast(dict[str, Any], report["hybrid"])["cases_total"],
            },
            sort_keys=True,
        )
    )
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
