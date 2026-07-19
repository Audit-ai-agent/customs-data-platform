"""Explicit test-only backend for Evidence HTTP conformance tests.

This module is never selected by an environment variable and is not used by
the production ``app``.  It proves request/response/header behavior without
pretending that the required 58-law production corpus has been loaded.
"""
from __future__ import annotations

from datetime import date

from src import repository
from src.evidence import LawArticleMatch, RequirementLookup


class TestOnlyConformanceEvidenceBackend:
    """Small deterministic backend available only through the test factory."""

    __test__ = False

    def __init__(self, *, effective_date: date = date(2026, 7, 10)) -> None:
        self.effective_date = effective_date
        self.retrieve_calls = 0

    def readiness(self) -> dict:
        return {
            "status": "ready",
            "knowledge_snapshot_id": "test-only-conformance-snapshot",
            "supported_review_as_of": self.effective_date,
            "law_corpus": {"expected": 1, "loaded": 1, "article_backed": 1},
            "law_index": {
                "expected": 1,
                "postgres_indexed": 1,
                "qdrant": 1,
                "postgres_graph_articles": 1,
                "neo4j": 1,
                "neo4j_missing": 0,
                "neo4j_orphaned": 0,
            },
        }

    def current_snapshot_id(self) -> str:
        self.retrieve_calls += 1
        return "test-only-conformance-snapshot"

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        assert snapshot_id == "test-only-conformance-snapshot"
        return self.effective_date

    def resolve_hsk(self, code: str | None) -> repository.HskResolution:
        if code and len(code) == 10 and code.isdigit():
            return repository.HskResolution("exact", code, code, (code,))
        return repository.HskResolution(
            "missing" if not code else "invalid",
            code,
            None,
            (),
        )

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ) -> RequirementLookup:
        assert snapshot_id == "test-only-conformance-snapshot"
        if (
            hsk10 != "0303540000"
            or trade_type != "import"
            or as_of != self.effective_date
        ):
            return RequirementLookup(False, ())
        return RequirementLookup(True, ({
            "requirement_id": "test_only_ccct_req_0303540000",
            "requirement_version_hash": "a" * 64,
            "hsk10": "0303540000",
            "trade_type": "import",
            "law_code": "89",
            "law_name": "수입식품안전관리 특별법",
            "agency_code": "005",
            "agency_name": "식품의약품안전처",
            "document_code": "CCCT_DOC_IMPORT_CONFIRMATION",
            "document_name": "수입식품등 수입신고확인증",
            "valid_from": date(2020, 4, 6),
            "valid_to": None,
            "source_record_id": "test_only_ccct_src_0303540000",
            "source_uri": (
                "https://apis.data.go.kr/example"
                "?hsSgn=0303540000&imexTpcd=2"
            ),
        },))

    def search_law_articles(
        self,
        query: str,
        top_k: int,
        as_of: date,
    ) -> tuple[LawArticleMatch, ...]:
        return ()
