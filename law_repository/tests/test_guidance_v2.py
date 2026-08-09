from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.functional_uat_kr_trade import FunctionalUatKrTradeError
from src.functional_uat_kr_trade_v2 import (
    build_kr_trade_v2_guidance_snapshot,
    inspect_kr_trade_v2_snapshot,
    query_kr_trade_v2_guidance_v3,
    require_kr_trade_v2_retrieval_snapshot,
)
from src.guidance_registry_v2 import (
    REGISTRY_V2_SCOPES,
    REGISTRY_V2_SHA256,
    REGISTRY_V2_SOURCES_BY_ID,
    registry_manifest_v2,
)
from src.guidance_v3 import GeneralGuidanceQueryRequestV3, query_general_guidance_v3
from src.conformance import TestOnlyConformanceEvidenceBackend
from src.local_profiles import ensure_functional_uat_kr_trade_profile


AS_OF = date(2026, 8, 5)


def _write(profile: Path, relative: str, value: str) -> tuple[str, str]:
    path = profile.joinpath(*Path(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")
    return relative, hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bytes(profile: Path, relative: str, value: bytes) -> tuple[str, str]:
    path = profile.joinpath(*Path(relative).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return relative, hashlib.sha256(path.read_bytes()).hexdigest()


def _request(**overrides: object) -> GeneralGuidanceQueryRequestV3:
    payload: dict[str, object] = {
        "schema_version": "3.0",
        "request_id": "guidance-v2-request",
        "query": "코드가 선택한 고정 V2 범위입니다.",
        "trade_direction": "both",
        "as_of": AS_OF.isoformat(),
        "topic_hint": "trade_terms",
        "scope_hint": "cost_risk_delivery_terms",
    }
    payload.update(overrides)
    return GeneralGuidanceQueryRequestV3.model_validate(payload)


def _stage_trade_terms_build(profile: Path, *, snapshot_id: str = "krtrade-v2-test") -> str:
    excerpt = "인코텀즈는 비용과 위험 및 인도조건을 정리하는 정형화된 거래 조건입니다."
    raw_relative, source_hash = _write(
        profile,
        "data/guidance-sources/v2-test/kotra-trade-terms.txt",
        f"공식 공개 안내\n{excerpt}\n",
    )
    source = REGISTRY_V2_SOURCES_BY_ID["v2_kotra_trade_terms"]
    payload = {
        "schema_version": "functional-uat-kr-trade-source-build-input/v2",
        "snapshot_id": snapshot_id,
        "as_of": AS_OF.isoformat(),
        "source_receipts": [
            {
                "source_id": source.source_id,
                "source_uri": source.official_uri,
                "relative_path": raw_relative,
                "sha256": source_hash,
                "collected_at": "2026-08-05T00:00:00+00:00",
                "bindings": [
                    {
                        "topic": "trade_terms",
                        "scope": "cost_risk_delivery_terms",
                        "directions": ["import", "export"],
                    }
                ],
            }
        ],
        "guidance_records": [
            {
                "source_id": source.source_id,
                "topic": "trade_terms",
                "scope": "cost_risk_delivery_terms",
                "direction": "both",
                "excerpt": excerpt,
                "locator": "가격조건",
            }
        ],
    }
    manifest_relative = "data/guidance-sources/v2-test/source-build-v2.json"
    _write(
        profile,
        manifest_relative,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )
    return manifest_relative


def test_v2_registry_is_page_level_and_declares_every_applicable_scope() -> None:
    manifest = registry_manifest_v2()

    assert manifest["schema_version"] == "2.0"
    assert manifest["registry_sha256"] == REGISTRY_V2_SHA256
    assert len(manifest["sources"]) == len(REGISTRY_V2_SOURCES_BY_ID)
    assert len(manifest["topics"]) == 11
    assert len(REGISTRY_V2_SCOPES) == 18
    assert all(source["source_id"].startswith("v2_") for source in manifest["sources"])
    assert all(source["official_uri"].startswith("https://") for source in manifest["sources"])
    assert {urlparse(source["official_uri"]).hostname for source in manifest["sources"]} <= {
        "www.law.go.kr",
        "www.customs.go.kr",
        "www2.customs.go.kr",
        "customs.go.kr",
        "dream.kotra.or.kr",
    }
    assert all(
        "relative_path" not in source and "sha256" not in source for source in manifest["sources"]
    )
    for topic in manifest["topics"]:
        for scope in topic["scopes"]:
            assert scope["required_source_ids"]
            assert scope["directions"]


def test_v2_build_is_hash_bound_and_covered_only_when_the_required_page_is_present(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    staged_relative = _stage_trade_terms_build(profile)

    status = build_kr_trade_v2_guidance_snapshot(
        staged_manifest_relative=staged_relative,
        local_root_override=tmp_path,
    )

    assert status.status == "ready"
    assert status.registry_version == "2.0"
    assert status.covered_bundle_count == 2  # import and export for this one scope
    assert status.coverage_count == sum(len(entry.directions) for entry in REGISTRY_V2_SCOPES)
    response = query_kr_trade_v2_guidance_v3(_request(), local_root_override=tmp_path)
    assert response.status == "covered"
    assert response.coverage == "covered"
    assert response.registry_version == "2.0"
    assert response.registry_sha256 == REGISTRY_V2_SHA256
    assert response.guidance_scope == "cost_risk_delivery_terms"
    assert response.results[0].registry_source_id == "v2_kotra_trade_terms"
    assert response.results[0].uri == REGISTRY_V2_SOURCES_BY_ID["v2_kotra_trade_terms"].official_uri
    assert "코드가 선택한" not in response.model_dump_json()


def test_v2_reports_partial_when_a_record_exists_but_a_required_statute_page_is_missing(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    excerpt = "수입 통관은 세관장에게 신고하고 심사하는 절차를 포함합니다."
    relative, source_hash = _write(
        profile,
        "data/guidance-sources/v2-partial/kcs-import.txt",
        f"공식 안내\n{excerpt}\n",
    )
    source = REGISTRY_V2_SOURCES_BY_ID["v2_kcs_import_clearance"]
    payload = {
        "schema_version": "functional-uat-kr-trade-source-build-input/v2",
        "snapshot_id": "krtrade-v2-partial",
        "as_of": AS_OF.isoformat(),
        "source_receipts": [
            {
                "source_id": source.source_id,
                "source_uri": source.official_uri,
                "relative_path": relative,
                "sha256": source_hash,
                "collected_at": "2026-08-05T00:00:00+00:00",
                "bindings": [
                    {
                        "topic": "customs_clearance",
                        "scope": "import_clearance_flow",
                        "directions": ["import"],
                    }
                ],
            }
        ],
        "guidance_records": [
            {
                "source_id": source.source_id,
                "topic": "customs_clearance",
                "scope": "import_clearance_flow",
                "direction": "import",
                "excerpt": excerpt,
            }
        ],
    }
    manifest_relative = "data/guidance-sources/v2-partial/source-build-v2.json"
    _write(profile, manifest_relative, json.dumps(payload, ensure_ascii=False))
    build_kr_trade_v2_guidance_snapshot(
        staged_manifest_relative=manifest_relative,
        local_root_override=tmp_path,
    )

    response = query_kr_trade_v2_guidance_v3(
        _request(
            topic_hint="customs_clearance",
            scope_hint="import_clearance_flow",
            trade_direction="import",
        ),
        local_root_override=tmp_path,
    )
    assert response.status == "partial"
    assert response.errors[0].code == "GUIDANCE_SNAPSHOT_PARTIAL_COVERAGE"
    assert response.results[0].registry_source_id == source.source_id


def test_v2_refuses_to_classify_a_query_when_the_caller_did_not_select_a_scope(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    staged_relative = _stage_trade_terms_build(profile)
    build_kr_trade_v2_guidance_snapshot(
        staged_manifest_relative=staged_relative,
        local_root_override=tmp_path,
    )

    response = query_kr_trade_v2_guidance_v3(
        _request(topic_hint=None, scope_hint=None, query="FOB와 CIF를 설명해 주세요."),
        local_root_override=tmp_path,
    )

    assert response.status == "no_coverage"
    assert response.coverage == "no_coverage"
    assert response.topic is None
    assert response.guidance_scope is None
    assert not response.results


def test_v2_statute_receipt_requires_an_effective_date_excerpt_and_as_of_validity(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    excerpt = "관세법의 통관 절차 관련 일반 조문을 위한 충분히 긴 발췌문입니다."
    effective_excerpt = "관세법 [시행 2026. 1. 1.] [법률 제00000호]"
    relative, source_hash = _write(
        profile,
        "data/guidance-sources/v2-law/customs-act.txt",
        f"{effective_excerpt}\n{excerpt}\n",
    )
    source = REGISTRY_V2_SOURCES_BY_ID["v2_law_customs_act"]
    payload = {
        "schema_version": "functional-uat-kr-trade-source-build-input/v2",
        "snapshot_id": "krtrade-v2-law",
        "as_of": AS_OF.isoformat(),
        "source_receipts": [
            {
                "source_id": source.source_id,
                "source_uri": source.official_uri,
                "relative_path": relative,
                "sha256": source_hash,
                "collected_at": "2026-08-05T00:00:00+00:00",
                "effective_from": "2026-01-01",
                "effective_to": None,
                "effective_date_excerpt": effective_excerpt,
                "bindings": [
                    {
                        "topic": "customs_clearance",
                        "scope": "import_clearance_flow",
                        "directions": ["import"],
                    }
                ],
            }
        ],
        "guidance_records": [
            {
                "source_id": source.source_id,
                "topic": "customs_clearance",
                "scope": "import_clearance_flow",
                "direction": "import",
                "excerpt": excerpt,
            }
        ],
    }
    manifest_relative = "data/guidance-sources/v2-law/source-build-v2.json"
    _write(profile, manifest_relative, json.dumps(payload, ensure_ascii=False))

    status = build_kr_trade_v2_guidance_snapshot(
        staged_manifest_relative=manifest_relative,
        local_root_override=tmp_path,
    )
    assert status.status == "ready"
    snapshot = require_kr_trade_v2_retrieval_snapshot(local_root_override=tmp_path)
    assert snapshot.records[0].legal_effective_from == date(2026, 1, 1)

    raw = profile / "data" / "guidance-sources" / "v2-law" / "customs-act.txt"
    raw.write_text("변조", encoding="utf-8")
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_V2_SOURCE_HASH_MISMATCH"):
        inspect_kr_trade_v2_snapshot(local_root_override=tmp_path)


def test_v2_law_body_cannot_claim_coverage_without_its_effective_law_page(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    excerpt = "수입신고에 관한 본문 조문을 위한 충분히 긴 공식 발췌문입니다."
    relative, source_hash = _write(
        profile,
        "data/guidance-sources/v2-law-body/customs-act-body.html",
        excerpt,
    )
    source = REGISTRY_V2_SOURCES_BY_ID["v2_law_customs_act_body"]
    payload = {
        "schema_version": "functional-uat-kr-trade-source-build-input/v2",
        "snapshot_id": "krtrade-v2-law-body",
        "as_of": AS_OF.isoformat(),
        "source_receipts": [
            {
                "source_id": source.source_id,
                "source_uri": source.official_uri,
                "relative_path": relative,
                "sha256": source_hash,
                "collected_at": "2026-08-05T00:00:00+00:00",
                "bindings": [
                    {
                        "topic": "customs_clearance",
                        "scope": "import_clearance_flow",
                        "directions": ["import"],
                    }
                ],
            }
        ],
        "guidance_records": [
            {
                "source_id": source.source_id,
                "topic": "customs_clearance",
                "scope": "import_clearance_flow",
                "direction": "import",
                "excerpt": excerpt,
            }
        ],
    }
    manifest_relative = "data/guidance-sources/v2-law-body/source-build-v2.json"
    _write(profile, manifest_relative, json.dumps(payload, ensure_ascii=False))

    with pytest.raises(
        FunctionalUatKrTradeError,
        match="KR_TRADE_V2_LAW_EFFECTIVITY_SOURCE_UNAVAILABLE",
    ):
        build_kr_trade_v2_guidance_snapshot(
            staged_manifest_relative=manifest_relative,
            local_root_override=tmp_path,
        )


def test_v2_pdf_source_requires_a_hash_bound_local_text_projection(tmp_path: Path) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    excerpt = "선하증권(B/L)부본이나 항공화물운송장(AWB)부본을 확인합니다."
    raw_relative, raw_hash = _write_bytes(
        profile,
        "data/guidance-sources/v2-pdf/kcs-qa.pdf",
        b"%PDF-1.7\noperator-staged-official-binary\n",
    )
    text_relative, text_hash = _write(
        profile,
        "data/guidance-sources/v2-pdf/kcs-qa-projection.txt",
        f"관세청 상담 안내\n{excerpt}\n",
    )
    source = REGISTRY_V2_SOURCES_BY_ID["v2_kcs_trade_documents_qa"]
    payload = {
        "schema_version": "functional-uat-kr-trade-source-build-input/v2",
        "snapshot_id": "krtrade-v2-pdf",
        "as_of": AS_OF.isoformat(),
        "source_receipts": [
            {
                "source_id": source.source_id,
                "source_uri": source.official_uri,
                "relative_path": raw_relative,
                "sha256": raw_hash,
                "text_relative_path": text_relative,
                "text_sha256": text_hash,
                "text_extraction_method": "operator_pdf_text_projection/v1",
                "collected_at": "2026-08-05T00:00:00+00:00",
                "bindings": [
                    {
                        "topic": "transport_documents",
                        "scope": "bill_of_lading",
                        "directions": ["import", "export"],
                    }
                ],
            }
        ],
        "guidance_records": [
            {
                "source_id": source.source_id,
                "topic": "transport_documents",
                "scope": "bill_of_lading",
                "direction": "both",
                "excerpt": excerpt,
                "locator": "PDF p.69",
            }
        ],
    }
    manifest_relative = "data/guidance-sources/v2-pdf/source-build-v2.json"
    _write(profile, manifest_relative, json.dumps(payload, ensure_ascii=False))

    build_kr_trade_v2_guidance_snapshot(
        staged_manifest_relative=manifest_relative,
        local_root_override=tmp_path,
    )

    projection = profile / "data" / "guidance-sources" / "v2-pdf" / "kcs-qa-projection.txt"
    projection.write_text("변조", encoding="utf-8")
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_V2_SOURCE_HASH_MISMATCH"):
        inspect_kr_trade_v2_snapshot(local_root_override=tmp_path)


def test_dev_fixture_can_return_v2_scope_without_calling_an_open_search() -> None:
    backend = TestOnlyConformanceEvidenceBackend(effective_date=AS_OF)

    response = query_general_guidance_v3(_request(), backend, profile_mode="local-development")

    assert response.status == "covered"
    assert response.registry_version == "2.0"
    assert response.guidance_scope == "cost_risk_delivery_terms"
    assert response.results[0].source_kind == "development_fixture"
    assert response.results[0].uri.endswith("/trade_terms/cost_risk_delivery_terms")
