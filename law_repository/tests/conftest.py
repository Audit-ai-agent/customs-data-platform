from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Callable

import pytest

from src.generation import (
    CCCT_BASELINE_METRICS,
    CCCT_BASELINE_SNAPSHOT_ID,
    CCCT_BASELINE_SOURCE,
    GenerationSpec,
    HSK_BASELINE_METRICS,
    HSK_BASELINE_SNAPSHOT_ID,
    HSK_BASELINE_SOURCES,
    SourceManifest,
    VerifiedEvaluationBundle,
    canonical_json_bytes,
    canonical_sha256,
    evaluation_review_subject_sha256,
    load_verified_evaluation_bundle,
    release_model_manifest,
)
from src.ingest.laws import TARGET_LAWS


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture()
def generation_manifest_factory() -> Callable[..., dict[str, Any]]:
    def factory(*, release_eligible: bool = True) -> dict[str, Any]:
        freshness = {
            "status": "verified_current",
            "observed_at": "2026-07-19T00:00:00Z",
            "valid_through": "2026-12-31",
            "limitation": None,
            "approval_artifact_sha256": _sha("dataset-approval"),
            "release_eligible": release_eligible,
        }
        laws = [
            {
                "law_id": item["law_id"],
                "law_name": item["name"],
                "mst": f"{int(item['law_id']):06d}",
                "official_uri": f"https://www.law.go.kr/법령/{item['law_id']}",
                "content_sha256": _sha(f"law:{item['law_id']}"),
                "current_article_count": 1,
            }
            for item in sorted(TARGET_LAWS, key=lambda candidate: candidate["law_id"])
        ]
        datasets = [
            {
                "dataset_id": "ccct",
                "snapshot_id": CCCT_BASELINE_SNAPSHOT_ID,
                "official_uri": "https://www.customs.go.kr/service/ccct",
                "source_files": [dict(CCCT_BASELINE_SOURCE)],
                "metrics": dict(CCCT_BASELINE_METRICS),
                "freshness": copy.deepcopy(freshness),
            },
            {
                "dataset_id": "hsk",
                "snapshot_id": HSK_BASELINE_SNAPSHOT_ID,
                "official_uri": "https://www.customs.go.kr/service/hsk",
                "source_files": [
                    dict(item)
                    for item in sorted(
                        HSK_BASELINE_SOURCES, key=lambda candidate: candidate["name"]
                    )
                ],
                "metrics": dict(HSK_BASELINE_METRICS),
                "freshness": copy.deepcopy(freshness),
            },
        ]
        return {
            "schema_version": "1.0",
            "generated_at": "2026-07-19T00:00:00Z",
            "law_corpus": {
                "schema_version": "1.0",
                "official_manifest_sha256": _sha("official-manifest"),
                "source_root_sha256": _sha("official-source-root"),
                "target_law_manifest_sha256": _sha("target-law-manifest"),
                "collector_revision": "1" * 40,
                "collected_at": "2026-07-18T00:00:00Z",
                "collection_as_of": "2026-07-18",
                "approval_artifact_sha256": _sha("approval-artifact"),
                "approved_at": "2026-07-19T00:00:00Z",
                "valid_through": "2026-12-31",
            },
            "laws": laws,
            "datasets": datasets,
        }

    return factory


@pytest.fixture()
def generation_spec(
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> GenerationSpec:
    return GenerationSpec(
        source_manifest=SourceManifest.from_mapping(generation_manifest_factory()),
        model_manifest=release_model_manifest(),
        index_config={
            "schema_version": "1.0",
            "dense_vector_name": "dense",
            "dense_vector_size": 1024,
            "distance": "cosine",
            "sparse_vector_name": "bm25",
            "sparse_modifier": "idf",
            "qdrant_collection_mode": "generation-isolated",
            "neo4j_projection_mode": "generation-tagged",
        },
    )


@pytest.fixture()
def generation_evaluation_factory() -> Callable[[str], dict[str, Any]]:
    def factory(generation_id: str) -> dict[str, Any]:
        law_ids = sorted(item["law_id"] for item in TARGET_LAWS)
        cases: list[dict[str, Any]] = [
            {
                "case_id": f"hybrid-{law_id}",
                "suite": "hybrid",
                "law_ids": [law_id],
                "category": "positive",
                "passed": True,
                "contract_valid": True,
                "hit_at_5": 1.0,
                "recall_at_10": 1.0,
                "ndcg_at_10": 0.8,
                "citation_metadata_complete": True,
            }
            for law_id in law_ids
        ]
        structured_categories = (
            "ambiguous",
            "negative",
            "positive",
            "positive",
            "positive",
        )
        cases.extend(
            {
                "case_id": f"structured-{index:02d}",
                "suite": "structured",
                "law_ids": [law_ids[index]],
                "category": category,
                "passed": True,
                "contract_valid": True,
                "hs_resolution_correct": True,
                "requirements_found_correct": True,
                "required_evidence_recall": 1.0,
                "evidence_metadata_complete": True,
                "false_positive": False,
            }
            for index, category in enumerate(structured_categories)
        )
        cases.sort(key=lambda item: item["case_id"])
        coverage = {law_id: 0 for law_id in law_ids}
        categories = {"positive": 0, "negative": 0, "ambiguous": 0}
        for case in cases:
            categories[case["category"]] += 1
            for law_id in case["law_ids"]:
                coverage[law_id] += 1
        gold_sha256 = _sha("human-reviewed-production-gold")
        report = {
            "schema_version": "2.0",
            "generation_id": generation_id,
            "run_id": "RUN-PRODUCTION-RAG-001",
            "generated_at": "2026-07-19T01:00:00Z",
            "production_data": True,
            "mock_count": 0,
            "unexpected_skips": 0,
            "artifacts": {
                "runner_sha256": _sha("runner"),
                "gold_sha256": gold_sha256,
                "query_manifest_sha256": _sha("query-manifest"),
                "code_sha256": _sha("code"),
                "reviewer_approval_sha256": "0" * 64,
                "case_manifest_sha256": canonical_sha256(cases),
            },
            "gold": {
                "dataset_sha256": gold_sha256,
                "human_reviewed": True,
                "reviewer_id": "reviewer-001",
                "reviewed_at": "2026-07-19T00:30:00Z",
                "law_coverage": coverage,
                "positive_cases": categories["positive"],
                "negative_cases": categories["negative"],
                "ambiguous_cases": categories["ambiguous"],
            },
            "cases": cases,
            "structured": {
                "cases_total": 5,
                "cases_passed": 5,
                "case_pass_rate": 1.0,
                "contract_valid_rate": 1.0,
                "hs_resolution_accuracy": 1.0,
                "requirements_found_accuracy": 1.0,
                "required_evidence_recall": 1.0,
                "evidence_metadata_completeness": 1.0,
                "no_requirement_false_positive_rate": 0.0,
            },
            "hybrid": {
                "cases_total": 58,
                "cases_passed": 58,
                "contract_valid_rate": 1.0,
                "hit_at_5": 1.0,
                "recall_at_10": 1.0,
                "ndcg_at_10": 0.8,
                "citation_metadata_completeness": 1.0,
            },
        }
        approval = {
            "schema_version": "1.0",
            "purpose": "production-rag-generation-seal",
            "generation_id": generation_id,
            "evaluation_subject_sha256": evaluation_review_subject_sha256(report),
            "gold_sha256": report["artifacts"]["gold_sha256"],
            "case_manifest_sha256": report["artifacts"]["case_manifest_sha256"],
            "reviewer_id": report["gold"]["reviewer_id"],
            "approved_at": report["gold"]["reviewed_at"],
        }
        report["artifacts"]["reviewer_approval_sha256"] = hashlib.sha256(
            canonical_json_bytes(approval)
        ).hexdigest()
        return report

    return factory


@pytest.fixture()
def generation_evaluation_bundle_factory(
    tmp_path: Path,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> Callable[[str], VerifiedEvaluationBundle]:
    def factory(generation_id: str) -> VerifiedEvaluationBundle:
        report = generation_evaluation_factory(generation_id)
        approval = {
            "schema_version": "1.0",
            "purpose": "production-rag-generation-seal",
            "generation_id": generation_id,
            "evaluation_subject_sha256": evaluation_review_subject_sha256(report),
            "gold_sha256": report["artifacts"]["gold_sha256"],
            "case_manifest_sha256": report["artifacts"]["case_manifest_sha256"],
            "reviewer_id": report["gold"]["reviewer_id"],
            "approved_at": report["gold"]["reviewed_at"],
        }
        bundle = tmp_path / generation_id
        bundle.mkdir(exist_ok=True)
        files = {
            "report.json": canonical_json_bytes(report),
            "runner.artifact": b"runner",
            "gold.artifact": b"human-reviewed-production-gold",
            "queries.artifact": b"query-manifest",
            "code.artifact": b"code",
            "cases.json": canonical_json_bytes(report["cases"]),
            "approval.json": canonical_json_bytes(approval),
        }
        for name, data in files.items():
            (bundle / name).write_bytes(data)
        return load_verified_evaluation_bundle(bundle, generation_id=generation_id)

    return factory
