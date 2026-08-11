from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence import LawArticleMatch
from src.guidance_registry import (
    REGISTRY_SHA256,
    REGISTRY_VERSION,
    active_sources_for_topic,
    kr_trade_source_ids,
    legacy_3law_source_ids,
    registry_manifest,
    resolve_guidance_topic,
)
from src.guidance_v3 import (
    GeneralGuidanceQueryRequestV3,
    GeneralGuidanceQueryResponseV3,
    GuidanceSourceV3,
    query_general_guidance_v3,
)
from src.profile_evidence import (
    LOCAL_DEVELOPMENT_MODE,
    SERVICE_MODE_HEADER,
    create_dev_fixture_application,
)


AS_OF = date(2026, 8, 5)
TOKEN = "a" * 32


def _request(**overrides: object) -> GeneralGuidanceQueryRequestV3:
    payload: dict[str, object] = {
        "schema_version": "3.0",
        "request_id": "guidance-request-1",
        "query": "수입 통관 절차를 설명해 주세요.",
        "trade_direction": "import",
        "as_of": AS_OF.isoformat(),
        "top_k": 3,
    }
    payload.update(overrides)
    return GeneralGuidanceQueryRequestV3.model_validate(payload)


class _FunctionalBackend:
    def __init__(self, *, matches: tuple[LawArticleMatch, ...] = ()) -> None:
        self.matches = matches
        self.search_calls: list[tuple[str, int, date]] = []

    def current_snapshot_id(self) -> str:
        return "functional-guidance-snapshot"

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        assert snapshot_id == "functional-guidance-snapshot"
        return AS_OF

    def search_law_articles(
        self, query: str, top_k: int, as_of: date
    ) -> tuple[LawArticleMatch, ...]:
        self.search_calls.append((query, top_k, as_of))
        return self.matches


def _match(*, law_id: str = "001556", content_hash: str = "b" * 64) -> LawArticleMatch:
    return LawArticleMatch(
        row={
            "article_pk": 226,
            "law_id": law_id,
            "law_name": "관세법",
            "article_no": 22600,
            "paragraph_no": None,
            "title": "수입신고",
            "content": "수입신고와 통관 절차의 기준을 설명하는 법령 원문입니다.",
            "content_hash": content_hash,
            "valid_from": date(2020, 1, 1),
            "valid_to": None,
        },
        retrieval_methods=("dense", "bm25"),
        score=0.82,
        raw_scores={"dense": 0.8, "bm25": 0.9},
    )


def test_registry_is_versioned_and_declares_source_level_provenance_and_scope() -> None:
    manifest = registry_manifest()

    assert manifest["schema_version"] == REGISTRY_VERSION == "1.0"
    assert manifest["jurisdiction"] == "KR"
    assert manifest["registry_sha256"]
    sources = {source["source_id"]: source for source in manifest["sources"]}
    assert sources["law_001556"] == {
        "source_id": "law_001556",
        "source_kind": "official_law",
        "title": "관세법",
        "official_uri": "https://www.law.go.kr/법령/관세법",
        "authority": "국가법령정보센터",
        "jurisdiction": "KR",
        "directions": ["import", "export"],
        "topics": [
            "customs_clearance",
            "customs_declaration",
            "customs_duty",
            "hs_classification",
            "origin_verification",
            "trade_documents",
            "import_requirements",
        ],
        "availability": "available_in_legacy_3law",
        "law_id": "001556",
    }
    assert sources["kcs_customs_guidance"]["availability"] == "requires_kr_trade_snapshot"
    assert sources["kcs_customs_guidance"]["official_uri"].startswith("https://")
    assert manifest["coverage_profiles"] == {
        "functional-uat-3law": {
            "status": "limited",
            "source_ids": ["law_001556", "law_002421", "law_006392"],
        },
        "functional-uat-kr-trade": {
            "status": "source_build_required",
            "source_ids": sorted(kr_trade_source_ids()),
        },
    }
    topic = resolve_guidance_topic("HS 코드의 품목분류를 알려 주세요.", direction="import")
    assert topic is not None
    assert (
        active_sources_for_topic(
            topic,
            direction="import",
            active_source_ids=legacy_3law_source_ids(),
        )
        == legacy_3law_source_ids()
    )
    # A cross-direction request may return separately scoped import/export
    # sources; it must not discard an otherwise registered import-only topic.
    both_topic = resolve_guidance_topic("수입 요건을 알려 주세요.", direction="both")
    assert both_topic is not None
    assert both_topic.topic == "import_requirements"


def test_dev_fixture_guidance_is_deterministic_and_profile_bound() -> None:
    app = create_dev_fixture_application(service_token=TOKEN)
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        SERVICE_MODE_HEADER: LOCAL_DEVELOPMENT_MODE,
    }
    response = client.post(
        "/internal/rag/v3/guidance/query", json=_request().model_dump(mode="json"), headers=headers
    )

    assert response.status_code == 200
    assert response.headers[SERVICE_MODE_HEADER] == LOCAL_DEVELOPMENT_MODE
    assert response.headers["X-Request-ID"] == "guidance-request-1"
    body = response.json()
    assert body["status"] == "covered"
    assert body["coverage"] == "covered"
    assert body["registry_version"] == "1.0"
    assert body["registry_sha256"] == REGISTRY_SHA256
    assert body["topic"] == "customs_clearance"
    assert len(body["results"]) == 1
    assert body["results"][0]["source_kind"] == "development_fixture"
    # The synthetic fixture names the stable registry family that scopes its
    # topic/direction. It does not claim that the family was collected.
    assert body["results"][0]["registry_source_id"] == "law_001556"
    assert body["results"][0]["law_id"] is None
    assert body["results"][0]["uri"].startswith("https://customs.local.invalid/")

    no_coverage = client.post(
        "/internal/rag/v3/guidance/query",
        json=_request(query="해상 보험의 보장 범위를 알려 주세요.").model_dump(mode="json"),
        headers=headers,
    )
    assert no_coverage.status_code == 200
    assert no_coverage.json()["status"] == "no_coverage"
    assert no_coverage.json()["registry_sha256"] == REGISTRY_SHA256
    assert no_coverage.json()["results"] == []

    missing = client.post(
        "/internal/rag/v3/guidance/query", json=_request().model_dump(mode="json")
    )
    assert missing.status_code == 401
    wrong_mode = client.post(
        "/internal/rag/v3/guidance/query",
        json=_request().model_dump(mode="json"),
        headers={"Authorization": f"Bearer {TOKEN}", SERVICE_MODE_HEADER: "functional-uat"},
    )
    assert wrong_mode.status_code == 412


def test_functional_guidance_uses_only_registry_owned_search_terms_and_official_rows() -> None:
    backend = _FunctionalBackend(matches=(_match(),))
    request = _request(query="수입 통관 절차를 설명해 주세요. unique-user-phrase-should-not-search")

    response = query_general_guidance_v3(request, backend, profile_mode="functional-uat")  # type: ignore[arg-type]

    assert response.status == "covered"
    assert response.snapshot_id == "functional-guidance-snapshot"
    assert response.registry_sha256 == REGISTRY_SHA256
    assert response.topic == "customs_clearance"
    assert len(response.results) == 1
    source = response.results[0]
    assert source.source_kind == "official_law"
    assert source.registry_source_id == "law_001556"
    assert source.law_id == "001556"
    assert source.law_name == "관세법"
    assert source.article_no == "제226조"
    assert source.retrieval_methods == ["registry", "dense", "bm25"]
    assert backend.search_calls == [("수출입 통관 절차 세관 신고", 3, AS_OF)]
    assert "unique-user-phrase-should-not-search" not in response.model_dump_json()


def test_topic_hint_uses_only_the_exact_registered_topic_without_reclassification() -> None:
    backend = _FunctionalBackend(matches=(_match(),))
    request = _request(
        query="사용자 표현이 바뀌어도 Data가 다시 분류하면 안 됩니다.",
        topic_hint="customs_clearance",
    )

    response = query_general_guidance_v3(request, backend, profile_mode="functional-uat")  # type: ignore[arg-type]

    assert response.status == "covered"
    assert response.topic == "customs_clearance"
    assert backend.search_calls == [("수출입 통관 절차 세관 신고", 3, AS_OF)]
    assert "사용자 표현이 바뀌어도" not in response.model_dump_json()


def test_non_law_official_source_kinds_are_explicit_and_cannot_claim_law_article() -> None:
    common = {
        "source_id": "guide_" + "a" * 64,
        "title": "관세청 수출입 통관 공개 안내",
        "uri": "https://www.customs.go.kr/",
        "topic": "customs_clearance",
        "direction": "import",
        "excerpt": "수입 통관 신고는 거래 서류와 물품 정보를 확인하여 진행합니다.",
        "version_hash": "b" * 64,
        "retrieval_methods": ["registry"],
    }
    registry_family_by_kind = {
        "official_law_catalog": ("law_kr_trade_fta_scope", "customs_clearance"),
        "official_agency_guidance": ("kcs_customs_guidance", "customs_clearance"),
        "official_requirement_reference": ("ccct_public_requirements", "import_requirements"),
        "official_public_guidance": ("kotra_trade_guidance", "customs_clearance"),
    }
    for source_kind, (registry_source_id, topic) in registry_family_by_kind.items():
        source = GuidanceSourceV3.model_validate(
            {
                **common,
                "source_kind": source_kind,
                "registry_source_id": registry_source_id,
                "topic": topic,
            }
        )
        assert source.source_kind == source_kind
        assert source.registry_source_id == registry_source_id
    with pytest.raises(ValueError, match="must not claim law provenance"):
        GuidanceSourceV3.model_validate(
            {
                **common,
                "source_kind": "official_agency_guidance",
                "registry_source_id": "kcs_customs_guidance",
                "law_id": "001556",
            }
        )


def test_guidance_result_rejects_unregistered_or_mismatched_registry_family() -> None:
    common = {
        "source_id": "guide_" + "a" * 64,
        "source_kind": "official_agency_guidance",
        "title": "관세청 수출입 통관 공개 안내",
        "uri": "https://www.customs.go.kr/",
        "topic": "customs_clearance",
        "direction": "import",
        "excerpt": "수입 통관 신고는 거래 서류와 물품 정보를 확인하여 진행합니다.",
        "version_hash": "b" * 64,
        "retrieval_methods": ["registry"],
    }
    with pytest.raises(ValueError, match="unknown registry family"):
        GuidanceSourceV3.model_validate({**common, "registry_source_id": "not_a_registry_source"})
    with pytest.raises(ValueError, match="kind does not match"):
        GuidanceSourceV3.model_validate({**common, "registry_source_id": "kotra_trade_guidance"})


def test_guidance_response_rejects_a_registry_hash_from_another_manifest() -> None:
    with pytest.raises(ValueError, match="registry hash does not match"):
        GeneralGuidanceQueryResponseV3(
            request_id="guidance-response-hash-mismatch",
            status="no_coverage",
            snapshot_id="functional-guidance-snapshot",
            as_of=AS_OF,
            coverage="no_coverage",
            registry_sha256="0" * 64,
            topic=None,
        )


def test_functional_guidance_never_exposes_out_of_registry_law_hits_as_coverage() -> None:
    backend = _FunctionalBackend(matches=(_match(law_id="999999"),))

    response = query_general_guidance_v3(_request(), backend, profile_mode="functional-uat")  # type: ignore[arg-type]

    assert response.status == "no_coverage"
    assert response.coverage == "no_coverage"
    assert response.results == []


def test_functional_guidance_with_no_active_snapshot_sources_does_not_fall_back_to_legacy() -> None:
    backend = _FunctionalBackend(matches=(_match(),))

    response = query_general_guidance_v3(
        _request(),
        backend,
        profile_mode="functional-uat",  # type: ignore[arg-type]
        active_source_ids=frozenset(),
    )

    assert response.status == "no_coverage"
    assert response.coverage == "no_coverage"
    assert response.results == []
    assert backend.search_calls == []


def test_legacy_three_law_scope_returns_no_coverage_for_future_trade_topics() -> None:
    backend = _FunctionalBackend(matches=(_match(),))

    response = query_general_guidance_v3(
        _request(query="FOB 조건과 CIF 조건의 차이를 알려 주세요.", trade_direction="export"),
        backend,
        profile_mode="functional-uat",  # type: ignore[arg-type]
    )

    assert response.topic == "trade_terms"
    assert response.status == "no_coverage"
    assert response.coverage == "no_coverage"
    assert response.results == []
    assert backend.search_calls == []


def test_functional_guidance_rejects_unknown_snapshot_source_scope() -> None:
    backend = _FunctionalBackend(matches=(_match(),))

    response = query_general_guidance_v3(
        _request(),
        backend,
        profile_mode="functional-uat",  # type: ignore[arg-type]
        active_source_ids=frozenset({"not_registered"}),
    )

    assert response.status == "failed"
    assert response.coverage == "unavailable"
    assert response.errors[0].code == "GUIDANCE_SNAPSHOT_SCOPE_INVALID"


def test_guidance_snapshot_and_search_failures_are_safe_and_distinct() -> None:
    class _BadDateBackend(_FunctionalBackend):
        def snapshot_effective_date(self, _snapshot_id: str) -> date:
            return date(2026, 8, 4)

    mismatch = query_general_guidance_v3(
        _request(),
        _BadDateBackend(),
        profile_mode="functional-uat",  # type: ignore[arg-type]
    )
    assert mismatch.status == "failed"
    assert mismatch.coverage == "unavailable"
    assert mismatch.errors[0].code == "UNSUPPORTED_GUIDANCE_AS_OF"

    class _FailingSearchBackend(_FunctionalBackend):
        def search_law_articles(self, *_args: object) -> tuple[LawArticleMatch, ...]:
            raise RuntimeError("postgresql://user:secret@127.0.0.1:55432/customs")

    failed = query_general_guidance_v3(
        _request(query="통관 절차 secret-marker"),
        _FailingSearchBackend(),
        profile_mode="functional-uat",  # type: ignore[arg-type]
    )
    serialized = failed.model_dump_json()
    assert failed.status == "failed"
    assert failed.errors[0].code == "GUIDANCE_SEARCH_FAILED"
    assert "secret" not in serialized
    assert "secret-marker" not in serialized


def test_v1_guidance_never_ignores_an_unselectable_v2_snapshot_pin() -> None:
    response = query_general_guidance_v3(
        _request(expected_snapshot_id="krtrade-v2-history-one"),
        _FunctionalBackend(matches=(_match(),)),
        profile_mode="functional-uat",  # type: ignore[arg-type]
    )

    assert response.status == "failed"
    assert response.coverage == "unavailable"
    assert response.snapshot_id == "functional-guidance-snapshot"
    assert response.errors[0].code == "KNOWLEDGE_SNAPSHOT_MISMATCH"


def test_guidance_request_bounds_are_enforced_before_profile_execution() -> None:
    try:
        _request(top_k=4)
    except ValueError as error:
        assert "top_k" in str(error)
    else:
        raise AssertionError("guidance top_k above the bounded maximum must be rejected")

    backend = TestOnlyConformanceEvidenceBackend(effective_date=AS_OF)
    response = query_general_guidance_v3(_request(), backend, profile_mode="local-development")
    assert response.status == "covered"
