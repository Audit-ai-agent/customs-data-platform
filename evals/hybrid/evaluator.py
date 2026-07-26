"""Small deterministic quality gate for live hybrid law retrieval."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from evals.contract import ContractViolation, validate_response


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HYBRID_GOLD = ROOT / "evals" / "hybrid" / "fixtures" / "hybrid_law_gold.json"


def load_hybrid_gold(path: Path | str = DEFAULT_HYBRID_GOLD) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as handle:
        records = json.load(handle)
    if not isinstance(records, list) or not records:
        raise ValueError("hybrid gold must be a non-empty JSON array")
    for record in records:
        if not {"case_id", "description", "query", "relevant"}.issubset(record):
            raise ValueError("hybrid gold record is missing required fields")
        if not record["relevant"]:
            raise ValueError("every hybrid gold case needs at least one relevant article")
    return records


def build_request(case: dict[str, Any], review_as_of: str) -> dict[str, Any]:
    case_id = case["case_id"]
    return {
        "schema_version": "1.0",
        "request_id": f"hybrid_eval:{case_id}",
        "case_id": f"hybrid_eval:{case_id}",
        "review_run_id": f"hybrid_eval:{case_id}",
        "review_as_of": review_as_of,
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 10,
        "items": [
            {
                "item_id": "law_query_item",
                "description": case["query"],
                "hs_code": "7318151000",
                "origin_country": "CN",
                "destination_country": "KR",
            }
        ],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "law_support",
    }


def evaluate_hybrid_case(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    contract_errors: list[str] = []
    try:
        validate_response(response)
    except ContractViolation as exc:
        contract_errors = exc.errors

    result = next(
        (item for item in response.get("results", []) if item.get("item_id") == "law_query_item"),
        None,
    )
    evidence = result.get("evidence", []) if result else []
    ranking = [
        (item.get("law_id"), item.get("article_no"))
        for item in evidence
        if item.get("source_type") == "law_article"
    ]
    relevant = {
        (item["law_id"], item["article_no"]): int(item.get("gain", 1)) for item in case["relevant"]
    }
    top5 = set(ranking[:5])
    top10 = set(ranking[:10])
    hit_at_5 = int(bool(top5 & relevant.keys()))
    recall_at_10 = len(top10 & relevant.keys()) / len(relevant)
    dcg = _dcg([relevant.get(key, 0) for key in ranking[:10]])
    ideal = _dcg(sorted(relevant.values(), reverse=True)[:10])
    ndcg_at_10 = dcg / ideal if ideal else 0.0
    citation_complete = int(
        bool(ranking)
        and all(
            item.get("evidence_id")
            and item.get("source_record_id")
            and item.get("source_uri")
            and item.get("version_hash")
            and item.get("law_id")
            and item.get("article_no")
            and item.get("retrieval_methods")
            for item in evidence
            if item.get("source_type") == "law_article"
        )
    )
    passed = (
        not contract_errors
        and response.get("status") == "ok"
        and result is not None
        and result.get("coverage") == "complete"
        and hit_at_5 == 1
        and recall_at_10 == 1.0
        and citation_complete == 1
    )
    return {
        "case_id": case["case_id"],
        "description": case["description"],
        "passed": passed,
        "contract_errors": contract_errors,
        "ranking": [f"{law_id}:{article_no}" for law_id, article_no in ranking[:10]],
        "relevant": [f"{law_id}:{article_no}" for law_id, article_no in relevant],
        "metrics": {
            "contract_valid": int(not contract_errors),
            "hit_at_5": hit_at_5,
            "recall_at_10": round(recall_at_10, 6),
            "ndcg_at_10": round(ndcg_at_10, 6),
            "citation_metadata_complete": citation_complete,
        },
    }


def summarize_hybrid(reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    reports = list(reports)
    count = len(reports)
    if not count:
        raise ValueError("at least one hybrid report is required")

    def mean(metric: str) -> float:
        return round(sum(item["metrics"][metric] for item in reports) / count, 6)

    metrics = {
        "contract_valid_rate": mean("contract_valid"),
        "hit_at_5": mean("hit_at_5"),
        "recall_at_10": mean("recall_at_10"),
        "ndcg_at_10": mean("ndcg_at_10"),
        "citation_metadata_completeness": mean("citation_metadata_complete"),
    }
    thresholds = {
        "contract_valid_rate": 1.0,
        "hit_at_5": 1.0,
        "recall_at_10": 1.0,
        "ndcg_at_10": 0.8,
        "citation_metadata_completeness": 1.0,
    }
    threshold_passed = all(metrics[key] >= value for key, value in thresholds.items())
    summary = {
        "mode": "hybrid-evaluate",
        "cases_total": count,
        "cases_passed": sum(bool(item["passed"]) for item in reports),
        **metrics,
        "thresholds": thresholds,
        "all_passed": all(item["passed"] for item in reports) and threshold_passed,
        "cases": reports,
    }
    latencies = sorted(float(item["latency_ms"]) for item in reports if "latency_ms" in item)
    if latencies:
        summary["latency_ms"] = {
            "min": round(latencies[0], 3),
            "p50": round(_percentile(latencies, 0.50), 3),
            "p95": round(_percentile(latencies, 0.95), 3),
            "max": round(latencies[-1], 3),
        }
    return summary


def _dcg(gains: Iterable[int]) -> float:
    return sum((2**gain - 1) / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))


def _percentile(values: list[float], quantile: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
