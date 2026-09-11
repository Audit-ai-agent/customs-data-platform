"""Free-text statute search and catalog on the V3 loopback boundary.

The endpoints are reviewer-facing reference lookups: same store, same
authentication, no case/HS/requirement meaning, and no review-result impact.
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence import LawArticleMatch
from src.profile_evidence import (
    LOCAL_DEVELOPMENT_MODE,
    create_profile_evidence_application,
)

_TOKEN = "unit-test-service-token-0123456789abcdef"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "X-Customs-Service-Mode": LOCAL_DEVELOPMENT_MODE,
    }


def _match(article_pk: int = 1) -> LawArticleMatch:
    return LawArticleMatch(
        row={
            "article_pk": article_pk,
            "law_id": "LAW-226",
            "law_name": "관세법",
            "hierarchy": "법률",
            "article_no": 22600,
            "paragraph_no": None,
            "item_no": None,
            "title": "허가ㆍ승인 등의 증명 및 확인",
            "content": "수출입을 할 때 허가ㆍ승인 등의 조건을 갖출 필요가 있는 물품은 이를 증명하여야 한다.",
            "content_hash": "b" * 64,
            "version_mst": "m1",
            "valid_from": date(2024, 1, 1),
            "valid_to": None,
        },
        retrieval_methods=("dense", "bm25"),
        score=0.91,
        raw_scores={"rrf": 0.02},
    )


class _SearchableBackend(TestOnlyConformanceEvidenceBackend):
    def search_law_articles(self, query, top_k, as_of):  # noqa: ANN001
        assert query == "허가 승인"
        assert top_k == 5
        assert as_of == date(2026, 8, 5)
        return (_match(),)

    def list_laws(self, as_of):  # noqa: ANN001
        assert as_of == date(2026, 8, 5)
        return [
            {"law_id": "LAW-226", "law_name": "관세법", "hierarchy": "법률", "article_count": 280},
            {
                "law_id": "LAW-DEC",
                "law_name": "관세법 시행령",
                "hierarchy": "대통령령",
                "article_count": 190,
            },
        ]


def _client(backend) -> TestClient:  # noqa: ANN001
    app = create_profile_evidence_application(
        mode=LOCAL_DEVELOPMENT_MODE,
        service_token=_TOKEN,
        backend=backend,
    )
    return TestClient(app)


def _search_request() -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "request_id": "req-law-search-1",
        "query": "허가 승인",
        "top_k": 5,
        "as_of": "2026-08-05",
    }


def test_law_search_returns_labelled_articles_with_official_links() -> None:
    client = _client(_SearchableBackend(effective_date=date(2026, 8, 5)))
    response = client.post(
        "/internal/rag/v3/law-search", json=_search_request(), headers=_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["request_id"] == "req-law-search-1"
    (result,) = body["results"]
    assert result["law_name"] == "관세법"
    assert result["article_no"] == "제226조"
    assert result["title"] == "관세법 제226조(허가ㆍ승인 등의 증명 및 확인)"
    assert result["source_uri"].startswith("https://www.law.go.kr/")
    assert result["valid_to"] is None
    assert response.headers["X-Request-ID"] == "req-law-search-1"


def test_law_search_requires_authentication() -> None:
    client = _client(_SearchableBackend(effective_date=date(2026, 8, 5)))
    assert client.post("/internal/rag/v3/law-search", json=_search_request()).status_code == 401


def test_law_search_rejects_an_overlong_query() -> None:
    client = _client(_SearchableBackend(effective_date=date(2026, 8, 5)))
    payload = {**_search_request(), "query": "가" * 201}
    response = client.post("/internal/rag/v3/law-search", json=payload, headers=_headers())
    assert response.status_code == 422


def test_law_catalog_lists_searchable_statutes() -> None:
    client = _client(_SearchableBackend(effective_date=date(2026, 8, 5)))
    response = client.post(
        "/internal/rag/v3/law-catalog",
        json={"schema_version": "3.0", "request_id": "req-cat-1", "as_of": "2026-08-05"},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert [law["law_name"] for law in body["laws"]] == ["관세법", "관세법 시행령"]
    assert body["laws"][0]["article_count"] == 280


class _NoCatalogBackend(TestOnlyConformanceEvidenceBackend):
    list_laws = None


def test_dev_fixture_law_catalog_is_empty_not_unavailable() -> None:
    client = _client(TestOnlyConformanceEvidenceBackend(effective_date=date(2026, 8, 5)))
    response = client.post(
        "/internal/rag/v3/law-catalog",
        json={"schema_version": "3.0", "request_id": "req-cat-3", "as_of": "2026-08-05"},
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["laws"] == []


def test_law_catalog_fails_closed_without_backend_support() -> None:
    client = _client(_NoCatalogBackend(effective_date=date(2026, 8, 5)))
    response = client.post(
        "/internal/rag/v3/law-catalog",
        json={"schema_version": "3.0", "request_id": "req-cat-2", "as_of": "2026-08-05"},
        headers=_headers(),
    )
    assert response.status_code == 503
