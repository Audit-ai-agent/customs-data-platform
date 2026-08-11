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
    TemporalCoverage,
    TemporalContext,
    TemporalEvidenceSupport,
    _effective_query_intent,
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
        "items": [
            {
                "item_id": "invoice:1",
                "description": "Frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }
        ],
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


class LawHistoryFakeBackend(FakeBackend):
    def __init__(self, *, covered: bool = True) -> None:
        self.covered = covered
        self.requirement_as_of: date | None = None
        self.law_as_of: date | None = None

    def temporal_evidence_support(self, snapshot_id, review_as_of):
        # Law-history context must be available even when the mutable CCCT
        # snapshot lookup fails, so Data asks this profile-owned capability
        # before it has a snapshot ID.
        assert snapshot_id is None
        return TemporalEvidenceSupport(
            context=TemporalContext(
                law_as_of=review_as_of,
                hsk_snapshot_as_of=date(2026, 8, 5),
                requirements_snapshot_as_of=date(2026, 8, 5),
            ),
            law_coverage_available=self.covered,
            requirements_lookup_as_of=date(2026, 8, 5),
        )

    def lookup_requirements(self, snapshot_id, hsk10, trade_type, as_of):
        self.requirement_as_of = as_of
        return super().lookup_requirements(snapshot_id, hsk10, trade_type, as_of)

    def search_law_articles(self, query, top_k, as_of):
        self.law_as_of = as_of
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
    expected_id = "ev_" + hashlib.sha256(("ccct_src_1\x1f" + "a" * 64).encode("utf-8")).hexdigest()
    assert evidence.evidence_id == expected_id
    assert response.temporal_context is None


def test_law_history_keeps_law_and_requirement_dates_explicit_and_partial() -> None:
    backend = LawHistoryFakeBackend()
    response = retrieve_evidence(
        _request(
            review_as_of=date(2026, 8, 7),
            query_intent="required_import_documents_and_law_support",
        ),
        backend,
    )

    assert response.status == "partial"
    assert response.temporal_context == TemporalContext(
        law_as_of=date(2026, 8, 7),
        hsk_snapshot_as_of=date(2026, 8, 5),
        requirements_snapshot_as_of=date(2026, 8, 5),
    )
    assert backend.requirement_as_of == date(2026, 8, 5)
    assert backend.law_as_of == date(2026, 8, 7)
    assert response.results[0].coverage == "partial"
    assert "TEMPORAL_REQUIREMENT_SNAPSHOT_MISMATCH" in {
        error.code for error in response.results[0].errors
    }


def test_law_history_missing_date_skips_law_search_without_substituting_current_text() -> None:
    backend = LawHistoryFakeBackend(covered=False)
    response = retrieve_evidence(
        _request(
            review_as_of=date(2026, 8, 7),
            query_intent="required_import_documents_and_law_support",
        ),
        backend,
    )

    assert response.status == "partial"
    assert backend.requirement_as_of == date(2026, 8, 5)
    assert backend.law_as_of is None
    assert "LAW_HISTORY_COVERAGE_MISSING" in {error.code for error in response.results[0].errors}


def test_law_history_availability_failure_keeps_three_date_context() -> None:
    class SnapshotFailingHistoryBackend(LawHistoryFakeBackend):
        def current_snapshot_id(self):
            raise RuntimeError("controlled store failure")

    response = retrieve_evidence(
        _request(review_as_of=date(2026, 8, 7)),
        SnapshotFailingHistoryBackend(),
    )

    assert response.status == "failed"
    assert response.temporal_context == TemporalContext(
        law_as_of=date(2026, 8, 7),
        hsk_snapshot_as_of=date(2026, 8, 5),
        requirements_snapshot_as_of=date(2026, 8, 5),
    )


def test_archive_aware_history_never_substitutes_baseline_hsk_or_ccct_for_missing_date() -> None:
    class ArchiveMissingBackend(FakeBackend):
        def current_snapshot_id(self):
            raise AssertionError("missing archive coverage must not query mutable CCCT state")

        def temporal_evidence_support(self, _snapshot_id, review_as_of):
            return TemporalEvidenceSupport(
                context=TemporalContext(
                    law_as_of=review_as_of,
                    hsk_snapshot_as_of=date(2026, 8, 5),
                    requirements_snapshot_as_of=date(2026, 8, 5),
                ),
                law_coverage_available=True,
                requirements_lookup_as_of=date(2026, 8, 5),
                temporal_coverage=TemporalCoverage(
                    law="covered", hsk="unavailable", requirements="unavailable"
                ),
                hsk_coverage_available=False,
                requirements_coverage_available=False,
            )

        def resolve_hsk(self, _code):
            raise AssertionError("current HSK must not be used for an unavailable archive date")

        def lookup_requirements(self, *_args):
            raise AssertionError("baseline CCCT must not be substituted")

    response = retrieve_evidence(_request(review_as_of=date(2026, 8, 7)), ArchiveMissingBackend())

    assert response.status == "partial"
    assert response.temporal_coverage == TemporalCoverage(
        law="covered", hsk="unavailable", requirements="unavailable"
    )
    assert response.results[0].evidence == []
    assert response.results[0].hs_resolution == "missing"
    assert {error.code for error in response.results[0].errors} == {
        "HSK_HISTORY_COVERAGE_MISSING",
        "CCCT_HISTORY_COVERAGE_MISSING",
    }


def test_archive_missing_hsk_keeps_independently_covered_law_support() -> None:
    class ArchiveMissingBackend(FakeBackend):
        def current_snapshot_id(self):
            raise AssertionError("missing archive coverage must not query mutable CCCT state")

        def temporal_evidence_support(self, _snapshot_id, review_as_of):
            return TemporalEvidenceSupport(
                context=TemporalContext(
                    law_as_of=review_as_of,
                    hsk_snapshot_as_of=date(2026, 8, 5),
                    requirements_snapshot_as_of=date(2026, 8, 5),
                ),
                law_coverage_available=True,
                requirements_lookup_as_of=date(2026, 8, 5),
                temporal_coverage=TemporalCoverage(
                    law="covered", hsk="unavailable", requirements="unavailable"
                ),
                hsk_coverage_available=False,
                requirements_coverage_available=False,
            )

        def resolve_hsk(self, _code):
            raise AssertionError("current HSK must not be used for an unavailable archive date")

        def lookup_requirements(self, *_args):
            raise AssertionError("baseline CCCT must not be substituted")

        def search_law_articles(self, _query, _top_k, as_of):
            assert as_of == date(2026, 8, 7)
            return (
                LawArticleMatch(
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
                        "official_uri": "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123",
                    },
                    retrieval_methods=("dense", "bm25"),
                    score=0.88,
                    raw_scores={"dense": 0.7, "bm25": 2.1, "rrf": 0.03},
                ),
            )

    response = retrieve_evidence(
        _request(
            review_as_of=date(2026, 8, 7),
            query_intent="required_import_documents_and_law_support",
        ),
        ArchiveMissingBackend(),
    )

    assert response.status == "partial"
    assert response.knowledge_snapshot_id is None
    assert response.results[0].hs_resolution == "missing"
    assert [evidence.source_type for evidence in response.results[0].evidence] == ["law_article"]
    assert response.results[0].requirements_found is False


def test_archive_aware_history_uses_bound_hsk_and_ccct_snapshot_at_requested_date() -> None:
    class ArchiveBackend(FakeBackend):
        def current_snapshot_id(self):
            raise AssertionError("historical binding must not query mutable current snapshot")

        def temporal_evidence_support(self, _snapshot_id, review_as_of):
            return TemporalEvidenceSupport(
                context=TemporalContext(
                    law_as_of=review_as_of,
                    hsk_snapshot_as_of=review_as_of,
                    requirements_snapshot_as_of=review_as_of,
                ),
                law_coverage_available=True,
                requirements_lookup_as_of=review_as_of,
                temporal_coverage=TemporalCoverage(
                    law="covered", hsk="covered", requirements="covered"
                ),
                hsk_snapshot_id="hsk_" + "b" * 64,
                requirements_snapshot_id="ccct_archive_snapshot",
            )

        def resolve_hsk_as_of(self, code, review_as_of):
            assert code == "0303540000"
            assert review_as_of == date(2026, 8, 7)
            return self.resolution

        def lookup_requirements(self, snapshot_id, hsk10, trade_type, as_of):
            assert snapshot_id == "ccct_archive_snapshot"
            assert hsk10 == "0303540000"
            assert trade_type == "import"
            assert as_of == date(2026, 8, 7)
            return self.lookup

    response = retrieve_evidence(_request(review_as_of=date(2026, 8, 7)), ArchiveBackend())

    assert response.status == "ok"
    assert response.knowledge_snapshot_id == "ccct_archive_snapshot"
    assert response.temporal_coverage == TemporalCoverage(
        law="covered", hsk="covered", requirements="covered"
    )
    assert response.results[0].requirements_found is True


def test_no_requirement_is_complete_only_when_query_was_covered():
    backend = FakeBackend()
    backend.lookup = RequirementLookup(True, ())
    response = retrieve_evidence(_request(), backend)
    result = response.results[0]
    assert response.status == "ok"
    assert result.coverage == "complete"
    assert result.requirements_found is False
    assert result.errors == []


def test_export_uses_export_document_intent_and_keeps_foreign_destination_limit_explicit():
    class ExportBackend(FakeBackend):
        def lookup_requirements(self, snapshot_id, hsk10, trade_type, as_of):
            assert snapshot_id == self.snapshot
            assert hsk10 == "0303540000"
            assert trade_type == "export"
            assert as_of == self.effective_date
            row = _row() | {
                "trade_type": "export",
                "law_name": "수출 절차 확인용 합성 기준",
                "document_code": "CCCT_DOC_EXPORT_DECLARATION",
                "document_name": "수출신고 관련 확인서",
                "source_record_id": "ccct_export_src_1",
            }
            return RequirementLookup(True, (row,))

    request = _request(
        trade_type="export",
        query_intent="required_export_documents",
        items=[
            {
                "item_id": "invoice:1",
                "description": "Frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "KR",
                "destination_country": "US",
            }
        ],
    )
    response = retrieve_evidence(request, ExportBackend())

    assert response.status == "partial"
    result = response.results[0]
    assert result.requirements_found is True
    assert result.coverage == "partial"
    assert result.evidence[0].trade_type == "export"
    assert [error.code for error in result.errors] == [
        "FOREIGN_DESTINATION_REQUIREMENTS_UNAVAILABLE"
    ]
    assert _effective_query_intent(request) == "required_export_documents"


def test_legacy_import_intent_is_normalized_for_export_without_foreign_coverage_claim():
    request = _request(
        trade_type="export",
        items=[
            {
                "item_id": "invoice:1",
                "description": "Frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "KR",
                "destination_country": "US",
            }
        ],
    )
    assert request.query_intent == "required_import_documents"
    assert _effective_query_intent(request) == "required_export_documents"


def test_export_with_kr_destination_is_partial_and_skips_requirement_lookup():
    class NoLookupBackend(FakeBackend):
        def lookup_requirements(self, *_args):
            raise AssertionError("invalid export destination must not query Korean requirements")

    response = retrieve_evidence(
        _request(trade_type="export", query_intent="required_export_documents"),
        NoLookupBackend(),
    )

    assert response.status == "partial"
    result = response.results[0]
    assert result.coverage == "partial"
    assert result.requirements_found is False
    assert result.evidence == []
    assert result.errors[0].code == "EXPORT_DESTINATION_COUNTRY_INVALID"


def test_import_outside_korean_destination_is_partial_and_keeps_existing_kr_path_unchanged():
    class NoLookupBackend(FakeBackend):
        def lookup_requirements(self, *_args):
            raise AssertionError("foreign import destination must not query KR-only requirements")

    response = retrieve_evidence(
        _request(
            items=[
                {
                    "item_id": "invoice:1",
                    "description": "Frozen mackerel",
                    "hs_code": "0303540000",
                    "origin_country": "NO",
                    "destination_country": "US",
                }
            ]
        ),
        NoLookupBackend(),
    )

    assert response.status == "partial"
    assert response.results[0].errors[0].code == "IMPORT_DESTINATION_COUNTRY_UNSUPPORTED"


def test_missing_coverage_is_partial_not_no_requirement():
    backend = FakeBackend()
    backend.lookup = RequirementLookup(False, ())
    response = retrieve_evidence(_request(), backend)
    assert response.status == "partial"
    assert response.results[0].coverage == "partial"
    assert response.results[0].errors[0].code == "CCCT_COVERAGE_MISSING"


def test_ambiguous_hs6_never_returns_evidence():
    backend = FakeBackend()
    backend.resolution = HskResolution("ambiguous", "731815", None, ("7318151000", "7318159000"))
    response = retrieve_evidence(
        _request(
            items=[
                {
                    "item_id": "invoice:1",
                    "description": "Bolts",
                    "hs_code": "731815",
                    "origin_country": "CN",
                    "destination_country": "KR",
                }
            ]
        ),
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


def test_backend_exception_details_never_cross_the_evidence_contract():
    secret = "postgresql://user:password@example.invalid/db?token=secret"

    class StoreFailure(FakeBackend):
        def current_snapshot_id(self):
            raise RuntimeError(secret)

    class SnapshotMismatch(FakeBackend):
        def current_snapshot_id(self):
            raise KnowledgeSnapshotMismatch(secret)

    class MetadataFailure(FakeBackend):
        def snapshot_effective_date(self, snapshot_id):
            raise RuntimeError(secret)

    class HsFailure(FakeBackend):
        def resolve_hsk(self, code):
            raise RuntimeError(secret)

    class RequirementFailure(FakeBackend):
        def lookup_requirements(self, snapshot_id, hsk10, trade_type, as_of):
            raise RuntimeError(secret)

    class LawFailure(FakeBackend):
        def search_law_articles(self, query, top_k, as_of):
            raise RuntimeError(secret)

    cases = [
        (
            StoreFailure(),
            _request(),
            "KNOWLEDGE_STORE_UNAVAILABLE",
            "Knowledge store is unavailable",
        ),
        (
            SnapshotMismatch(),
            _request(),
            "KNOWLEDGE_SNAPSHOT_MISMATCH",
            "Knowledge snapshot consistency check failed",
        ),
        (
            MetadataFailure(),
            _request(),
            "KNOWLEDGE_SNAPSHOT_METADATA_UNAVAILABLE",
            "Knowledge snapshot metadata is unavailable",
        ),
        (
            HsFailure(),
            _request(),
            "HS_MASTER_UNAVAILABLE",
            "HS master is unavailable",
        ),
        (
            RequirementFailure(),
            _request(),
            "REQUIREMENT_LOOKUP_FAILED",
            "Requirement lookup failed",
        ),
        (
            LawFailure(),
            _request(query_intent="required_import_documents_and_law_support"),
            "LAW_RETRIEVAL_FAILED",
            "Law retrieval failed",
        ),
    ]
    for backend, request, code, public_message in cases:
        response = retrieve_evidence(request, backend)
        serialized = response.model_dump_json()
        assert secret not in serialized
        errors = [
            error for result in response.results for error in result.errors if error.code == code
        ]
        errors.extend(error for error in response.errors if error.code == code)
        assert errors
        assert {error.message for error in errors} == {public_message}


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
    second = (
        retrieve_evidence(
            _request(
                request_id="req-2",
                items=[
                    {
                        "item_id": "invoice:99",
                        "description": "Frozen mackerel",
                        "hs_code": "0303540000",
                        "origin_country": "NO",
                        "destination_country": "KR",
                    }
                ],
            ),
            FakeBackend(),
        )
        .results[0]
        .evidence[0]
    )
    assert first.evidence_id == second.evidence_id
    assert second.item_id == "invoice:99"


def test_runtime_readiness_is_lightweight_and_full_store_check_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Health probes must not turn into full Qdrant/Neo4j ID scans."""

    from src.evidence import DatabaseEvidenceBackend

    backend = DatabaseEvidenceBackend()
    monkeypatch.setattr(backend, "current_snapshot_id", lambda: "ccct_snapshot_1")
    monkeypatch.setattr(backend, "snapshot_effective_date", lambda _snapshot: date(2026, 8, 5))

    assert backend.readiness() == {
        "status": "ready",
        "knowledge_snapshot_id": "ccct_snapshot_1",
        "supported_review_as_of": date(2026, 8, 5),
    }


def test_request_limits_match_central_wire_contract():
    request = _request(top_k=100)
    assert request.top_k == 100
    assert request.uploaded_requirement_document_codes == []

    with pytest.raises(ValidationError):
        _request(top_k=101)
    with pytest.raises(ValidationError):
        _request(
            items=[
                {
                    "item_id": "invoice:1",
                    "description": "Frozen mackerel",
                    "hs_code": "0303540000",
                    "origin_country": "NOR",
                    "destination_country": "KR",
                }
            ]
        )


def test_hybrid_law_support_is_packaged_but_not_counted_as_requirement():
    class HybridBackend(FakeBackend):
        lookup = RequirementLookup(True, ())

        def search_law_articles(self, query, top_k, as_of):
            assert query == "수입 신고 제출서류 세관장 확인 허가 승인 증명 관세법"
            assert top_k == 8
            return (
                LawArticleMatch(
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
                        "official_uri": "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123",
                    },
                    retrieval_methods=("dense", "bm25", "graph"),
                    score=0.88,
                    raw_scores={"dense": 0.7, "bm25": 2.1, "rrf": 0.03},
                ),
            )

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
    assert str(evidence.source_uri) == "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=123"


def test_law_support_query_uses_issue_meaning_without_product_noise():
    from src.evidence import _law_query

    request = _request(
        validation_issue_codes=["INVOICE_NO_MISMATCH"],
        query_intent="law_support",
    )
    query = _law_query(request, request.items[0], "0303540000")

    assert query == (
        "수입 신고 제출서류 세관장 확인 허가 승인 증명 관세법 "
        "송장번호 불일치 신고사항 보완 상업송장 대조"
    )
    assert "0303540000" not in query
    assert request.items[0].description not in query


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
