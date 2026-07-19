from __future__ import annotations

import copy
import hashlib
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
            "valid_through": None,
            "limitation": None,
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
            "dense_vector_size": 1024,
            "distance": "cosine",
            "sparse_modifier": "idf",
            "qdrant_collection_mode": "generation-isolated",
            "neo4j_projection_mode": "generation-tagged",
        },
    )


@pytest.fixture()
def generation_evaluation_factory() -> Callable[[str], dict[str, Any]]:
    def factory(generation_id: str) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generation_id": generation_id,
            "run_id": "RUN-PRODUCTION-RAG-001",
            "generated_at": "2026-07-19T01:00:00Z",
            "production_data": True,
            "mock_count": 0,
            "unexpected_skips": 0,
            "gold": {
                "dataset_sha256": _sha("human-reviewed-production-gold"),
                "human_reviewed": True,
                "reviewer_id": "reviewer-001",
                "reviewed_at": "2026-07-19T00:30:00Z",
                "law_coverage_count": 58,
                "positive_cases": 58,
                "negative_cases": 5,
                "ambiguous_cases": 5,
            },
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

    return factory
