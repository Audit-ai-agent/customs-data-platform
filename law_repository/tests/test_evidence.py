"""Evidence Service v1 계약·오류 의미·근거 ID 검증."""
from __future__ import annotations

import hashlib
from datetime import date

import pytest
from pydantic import ValidationError

from src.evidence import (
    KnowledgeSnapshotMismatch,
    LawArticleMatch,
    RequirementLookup,
    RetrieveRequest,
    retrieve_evidence,
)
from src.repository import HskResolution


def _request(**overrides) -> RetrieveRequest:
    payload = {
        "schema_version": "1.0",
        "request_id": "req-1",
        "case_id": "case-1",
        "review_run_id": "run-1",
        "review_as_of": date(2026, 7, 10),
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 8,
        "items": [{
            "item_id": "invoice:1",
            "description": "Frozen mackerel",
            "hs_code": "0303540000",
            "origin_country": "NO",
            "destination_country": "KR",
        }],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "required_import_documents",
    }
    payload.update(overrides)
    return RetrieveRequest.model_validate(payload)


def _row():
    return {
        "requirement_id": "ccct_req_1",
        "requirement_version_hash": "a" * 64,
        "hsk10": "0303540000",
        "trade_type": "import",
        "law_code": "89",
        "law_name": "수입식품안전관리 특별법",
        "agency_code": "005",
        "agency_name": "식품의약품안전처",
        "document_code": "CCCT_DOC_ABC",
        "document_name": "수입식품등 수입신고확인증",
        "valid_from": date(2020, 4, 6),
        "valid_to": None,
        "source_record_id": "ccct_src_1",
        "source_uri": "https://apis.data.go.kr/example?hsSgn=0303540000&imexTpcd=2",
    }


class FakeBackend:
    snapshot = "ccct_snapshot_1"
    effective_date = date(2026, 7, 10)
    resolution = HskResolution("exact", "0303540000", "0303540000", ("0303540000",))
    lookup = RequirementLookup(True, (_row(),))

    def current_snapshot_id(self):
        return self.snapshot

    def snapshot_effective_date(self, snapshot_id):
        assert snapshot_id == self.snapshot
        return self.effective_date

    def resolve_hsk(self, code):
        return self.resolution

    def lookup_requirements(self, snapshot_id, hsk10, trade_type, as_of):
        assert snapshot_id == self.snapshot
        assert trade_type == "import"
        return self.lookup

    def search_law_articles(self, query, top_k, as_of):
        return ()


def test_exact_hsk_returns_complete_structured_evidence():
    response = retrieve_evidence(_request(), FakeBackend())
    assert response.status == "ok"
    assert response.knowledge_snapshot_id == "ccct_snapshot_1"
    result = response.results[0]
    assert result.hs_resolution == "exact"
    assert result.coverage == "complete"
    assert result.requirements_found is True
    evidence = result.evidence[0]
    assert evidence.item_id == "invoice:1"
    assert evidence.source_type == "customs_requirement"
    assert evidence.required_document_name == "수입식품등 수입신고확인증"
    assert evidence.agencies == ["식품의약품안전처"]
    assert evidence.eligibility == "finding"
    assert evidence.article_no is None  # CCCT만으로 조문을 조작하지 않음
    expected_id = "ev_" + hashlib.sha256(
        ("ccct_src_1\x1f" + "a" * 64).encode("utf-8")
    ).hexdigest()
    assert evidence.evidence_id == expected_id


def test_no_requirement_is_complete_only_when_query_was_covered():
    backend = FakeBackend()
    backend.lookup = RequirementLookup(True, ())
    response = retrieve_evidence(_request(), backend)
    result = response.results[0]
    assert response.status == "ok"
    assert result.coverage == "complete"
    assert result.requirements_found is False
    assert result.errors == []


def test_missing_coverage_is_partial_not_no_requirement():
    backend = FakeBackend()
    backend.lookup = RequirementLookup(False, ())
    response = retrieve_evidence(_request(), backend)
    assert response.status == "partial"
    assert response.results[0].coverage == "partial"
    assert response.results[0].errors[0].code == "CCCT_COVERAGE_MISSING"


def test_ambiguous_hs6_never_returns_evidence():
    backend = FakeBackend()
    backend.resolution = HskResolution(
        "ambiguous", "731815", None, ("7318151000", "7318159000")
    )
    response = retrieve_evidence(
        _request(items=[{
            "item_id": "invoice:1",
            "description": "Bolts",
            "hs_code": "731815",
            "origin_country": "CN",
            "destination_country": "KR",
        }]),
        backend,
    )
    result = response.results[0]
    assert response.status == "partial"
    assert response.errors[0].code == "PARTIAL_RESULTS"
    assert response.errors[0].retryable is False
    assert result.hs_resolution == "ambiguous"
    assert result.coverage == "unknown"
    assert result.evidence == []
    assert result.errors[0].code == "HS_CODE_AMBIGUOUS"


def test_store_failure_is_explicit_failed_response():
    class FailingBackend(FakeBackend):
        def current_snapshot_id(self):
            raise RuntimeError("postgres down")

    response = retrieve_evidence(_request(), FailingBackend())
    assert response.status == "failed"
    assert response.knowledge_snapshot_id is None
    assert response.errors[0].code == "KNOWLEDGE_STORE_UNAVAILABLE"
    assert response.errors[0].retryable is True
    assert response.results[0].coverage == "unknown"


def test_snapshot_mismatch_is_explicit_failed_response():
    class MismatchBackend(FakeBackend):
        def current_snapshot_id(self):
            raise KnowledgeSnapshotMismatch("postgres=ccct_a, neo4j=ccct_b")

    response = retrieve_evidence(_request(), MismatchBackend())
    assert response.status == "failed"
    assert response.errors[0].code == "KNOWLEDGE_SNAPSHOT_MISMATCH"


def test_review_date_outside_snapshot_date_fails_closed():
    response = retrieve_evidence(
        _request(review_as_of=date(2010, 1, 1)),
        FakeBackend(),
    )
    assert response.status == "failed"
    assert response.knowledge_snapshot_id == "ccct_snapshot_1"
    assert response.errors[0].code == "UNSUPPORTED_REVIEW_AS_OF"
    assert response.errors[0].retryable is False
    result = response.results[0]
    assert result.hs_resolution == "exact"
    assert result.resolved_hsk10 == "0303540000"
    assert result.coverage == "unknown"
    assert result.requirements_found is False
    assert result.evidence == []
    assert result.errors[0].code == "UNSUPPORTED_REVIEW_AS_OF"


def test_evidence_id_is_stable_across_requests_and_items():
    first = retrieve_evidence(_request(), FakeBackend()).results[0].evidence[0]
    second = retrieve_evidence(
        _request(
            request_id="req-2",
            items=[{
                "item_id": "invoice:99",
                "description": "Frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }],
        ),
        FakeBackend(),
    ).results[0].evidence[0]
    assert first.evidence_id == second.evidence_id
    assert second.item_id == "invoice:99"


def test_webapp_exposes_versioned_internal_route():
    from src.webapp import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/internal/rag/v1/retrieve"]
    assert "/internal/rag/v1/health/live" in paths
    assert "/internal/rag/v1/health/ready" in paths


def test_readiness_rejects_a_partially_loaded_target_law_corpus(monkeypatch):
    from src.evidence import DatabaseEvidenceBackend
    from src.ingest.laws import TARGET_LAWS

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return None

        def fetchall(self):
            return [(TARGET_LAWS[0]["law_id"], 1)]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("src.evidence.connect", lambda: FakeConnection())
    with pytest.raises(
        RuntimeError,
        match=(
            rf"expected={len(TARGET_LAWS)}, loaded=1, article_backed=1, "
            rf"missing={len(TARGET_LAWS) - 1}, without_current_articles=0"
        ),
    ):
        DatabaseEvidenceBackend()._verify_target_law_coverage()


def test_readiness_rejects_target_law_with_no_verified_current_article(monkeypatch):
    from src.evidence import DatabaseEvidenceBackend
    from src.ingest.laws import TARGET_LAWS

    assert len(TARGET_LAWS) == 58

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, query, *_args):
            assert "btrim(a.content) <> ''" in query
            assert "a.content_hash ~ '^[0-9a-f]{64}$'" in query
            assert "FROM law_versions v" in query

        def fetchall(self):
            return [
                (law["law_id"], 0 if index == 0 else 1)
                for index, law in enumerate(TARGET_LAWS)
            ]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("src.evidence.connect", lambda: FakeConnection())
    with pytest.raises(
        RuntimeError,
        match=(
            r"expected=58, loaded=58, article_backed=57, "
            r"missing=0, without_current_articles=1"
        ),
    ):
        DatabaseEvidenceBackend()._verify_target_law_coverage()


def test_law_graph_readiness_detects_equal_count_id_replacement(monkeypatch):
    from types import SimpleNamespace

    from src.evidence import DatabaseEvidenceBackend

    class FakeCursor:
        def __init__(self):
            self.query_index = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            self.query_index += 1

        def fetchone(self):
            assert self.query_index == 1
            return (2, 2, 2)

        def fetchall(self):
            assert self.query_index == 2
            return [(101,), (102,)]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return FakeCursor()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def run(self, query):
            assert "a.article_pk AS article_pk" in query
            return [{"article_pk": 101}, {"article_pk": 103}]

    class FakeDriver:
        def session(self):
            return FakeSession()

        def close(self):
            return None

    monkeypatch.setattr("src.evidence.connect", lambda: FakeConnection())
    monkeypatch.setattr(
        "src.db.qdrant.client",
        lambda: SimpleNamespace(
            count=lambda *_args, **_kwargs: SimpleNamespace(count=2)
        ),
    )
    monkeypatch.setattr("src.evidence.make_driver", lambda: FakeDriver())

    with pytest.raises(
        RuntimeError,
        match=(
            r"postgres_graph_articles=2, neo4j=2, "
            r"missing=1, orphaned=1"
        ),
    ):
        DatabaseEvidenceBackend()._verify_law_index()


def test_request_limits_match_central_wire_contract():
    request = _request(top_k=100)
    assert request.top_k == 100
    assert request.uploaded_requirement_document_codes == []

    with pytest.raises(ValidationError):
        _request(top_k=101)
    with pytest.raises(ValidationError):
        _request(items=[{
            "item_id": "invoice:1",
            "description": "Frozen mackerel",
            "hs_code": "0303540000",
            "origin_country": "NOR",
            "destination_country": "KR",
        }])


def test_hybrid_law_support_is_packaged_but_not_counted_as_requirement():
    class HybridBackend(FakeBackend):
        lookup = RequirementLookup(True, ())

        def search_law_articles(self, query, top_k, as_of):
            assert "세관장 확인 관세법" in query
            assert top_k == 8
            return (LawArticleMatch(
                row={
                    "article_pk": 226,
                    "law_id": "001556",
                    "law_name": "관세법",
                    "hierarchy": "법률",
                    "article_no": 22600,
                    "paragraph_no": None,
                    "item_no": None,
                    "title": "허가·승인 등의 증명 및 확인",
                    "content": "세관장은 수출입 요건을 확인하여야 한다.",
                    "content_hash": "f" * 64,
                    "version_mst": "mst-1",
                    "valid_from": date(2026, 1, 1),
                    "valid_to": None,
                },
                retrieval_methods=("dense", "bm25", "graph"),
                score=0.88,
                raw_scores={"dense": 0.7, "bm25": 2.1, "rrf": 0.03},
            ),)

    response = retrieve_evidence(
        _request(query_intent="required_import_documents_and_law_support"),
        HybridBackend(),
    )
    result = response.results[0]
    assert response.status == "ok"
    assert result.coverage == "complete"
    assert result.requirements_found is False
    assert len(result.evidence) == 1
    evidence = result.evidence[0]
    assert evidence.source_type == "law_article"
    assert evidence.eligibility == "supporting"
    assert evidence.article_no == "제226조"
    assert evidence.retrieval_methods == ["dense", "bm25", "graph"]


def test_requested_law_support_failure_makes_coverage_partial():
    class FailingHybridBackend(FakeBackend):
        def search_law_articles(self, query, top_k, as_of):
            raise RuntimeError("qdrant unavailable")

    response = retrieve_evidence(
        _request(query_intent="required_import_documents_and_law_support"),
        FailingHybridBackend(),
    )
    assert response.status == "partial"
    assert response.results[0].coverage == "partial"
    assert response.results[0].errors[0].code == "LAW_RETRIEVAL_FAILED"
