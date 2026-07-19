from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace
from typing import Any

from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR
from src.generation_evidence import GenerationEvidenceBackend
from src.generation_evidence import GenerationEvidenceError
from src.runtime_release import (
    EvidenceRuntimeReleaseIdentity,
    GenerationRequestLease,
    runtime_release_sha256,
)


def _lease() -> GenerationRequestLease:
    payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": "1" * 40,
        "provider_build_sha256": "2" * 64,
        "retrieval_schema_sha256": "3" * 64,
        "provider_source_manifest_sha256": "4" * 64,
        "knowledge_generation_id": "7" * 64,
        "knowledge_generation_attestation_sha256": "5" * 64,
        "knowledge_source_manifest_sha256": "6" * 64,
        "model_manifest_sha256": "8" * 64,
        "index_config_sha256": "9" * 64,
        "evaluation_report_sha256": "b" * 64,
        "seal_attestation_sha256": "c" * 64,
        "knowledge_snapshot_id": "ccct-release",
        "supported_review_as_of": "2026-07-10",
    }
    payload["runtime_release_sha256"] = runtime_release_sha256(payload)
    return GenerationRequestLease(
        identity=EvidenceRuntimeReleaseIdentity.model_validate(payload),
        generation_epoch=3,
        generation_changed_at="2026-07-19T00:00:00.000000Z",
    )


class FakeQdrant:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(points=[])


class FakeQdrantHit(FakeQdrant):
    def query_points(self, **kwargs):
        self.calls.append(kwargs)
        text = "sealed article text"
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    score=0.9,
                    payload={
                        "generation_id": "7" * 64,
                        "model_manifest_sha256": "8" * 64,
                        "article_pk": 1,
                        "law_id": "000001",
                        "article_no": 100,
                        "paragraph_no": None,
                        "text": text,
                        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    },
                )
            ]
        )


class FakeSession:
    def __init__(self) -> None:
        self.query = ""
        self.params: dict[str, Any] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def run(self, query: str, **params):
        self.query = query
        self.params = params
        return [{"code": "0303540000", "content_sha256": "a" * 64}]


class FakeDriver:
    def __init__(self) -> None:
        self.fake_session = FakeSession()
        self.closed = False

    def session(self):
        return self.fake_session

    def close(self) -> None:
        self.closed = True


class FakeMembershipCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _query: str, _params: tuple[Any, ...]) -> None:
        return None

    def fetchall(self):
        return [("0303540000", "a" * 64)]


class FakeMembershipConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return FakeMembershipCursor()


class FakeArticleCursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _query: str, _params: tuple[Any, ...]) -> None:
        return None

    def fetchall(self):
        text = "sealed article text"
        return [
            {
                "article_pk": 1,
                "law_id": "000002",
                "law_name": "Test Law",
                "hierarchy": "법률",
                "article_no": 100,
                "paragraph_no": None,
                "item_no": None,
                "title": "Purpose",
                "content": text,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "version_mst": "1",
                "valid_from": date(2026, 1, 1),
                "valid_to": None,
            }
        ]


class FakeArticleConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self, **_kwargs):
        return FakeArticleCursor()


def test_qdrant_queries_exact_generation_collection_and_filter() -> None:
    qdrant = FakeQdrant()
    backend = GenerationEvidenceBackend(
        _lease(),
        qdrant_factory=lambda: qdrant,
        dense_encoder=lambda _texts: [[0.1, 0.2]],
        sparse_encoder=lambda _text: ([1], [1.0]),
    )

    backend._qdrant_arm("customs valuation", DENSE_VECTOR, 5)
    backend._qdrant_arm("customs valuation", BM25_VECTOR, 5)

    expected_collection = "customs_law_gen_" + ("7" * 64)
    assert len(qdrant.calls) == 2
    assert all(call["collection_name"] == expected_collection for call in qdrant.calls)
    assert all(call["collection_name"] != "customs_law_active" for call in qdrant.calls)
    assert all(
        call["query_filter"].must[0].match.value == "7" * 64
        for call in qdrant.calls
    )


def test_hsk_resolution_requires_generation_and_snapshot_projection() -> None:
    driver = FakeDriver()
    backend = GenerationEvidenceBackend(
        _lease(),
        neo4j_factory=lambda: driver,
        connection_factory=FakeMembershipConnection,
    )
    backend._dataset = lambda _dataset_id: SimpleNamespace(snapshot_id="hsk-2026-01-01")  # type: ignore[method-assign]

    resolution = backend.resolve_hsk("0303540000")

    assert resolution.status == "exact"
    assert resolution.resolved_hsk10 == "0303540000"
    assert "GenerationHSK" in driver.fake_session.query
    assert "CONTAINS_HSK" in driver.fake_session.query
    assert "generation_id" in driver.fake_session.query
    assert driver.fake_session.params["generation_id"] == "7" * 64
    assert driver.fake_session.params["hsk_snapshot_id"] == "hsk-2026-01-01"
    assert (
        driver.fake_session.params["source_manifest_sha256"]
        == backend.identity.knowledge_source_manifest_sha256
    )
    assert driver.closed is True


def test_qdrant_hit_identity_must_match_postgres_generation_row() -> None:
    backend = GenerationEvidenceBackend(
        _lease(),
        qdrant_factory=FakeQdrantHit,
        connection_factory=FakeArticleConnection,
        dense_encoder=lambda _texts: [[0.1, 0.2]],
        sparse_encoder=lambda _text: ([1], [1.0]),
    )
    backend._graph_related = lambda _seeds: {}  # type: ignore[method-assign]

    try:
        backend.search_law_articles(
            "customs valuation",
            top_k=5,
            as_of=date(2026, 7, 10),
        )
    except GenerationEvidenceError as exc:
        assert "immutable PostgreSQL" in str(exc)
    else:
        raise AssertionError("stale Qdrant law identity must fail closed")
