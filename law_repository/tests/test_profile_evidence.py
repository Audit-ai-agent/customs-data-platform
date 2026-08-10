from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from hashlib import sha256
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.functional_uat import (
    FUNCTIONAL_UAT_AS_OF,
    FUNCTIONAL_UAT_LAWS,
    FunctionalUatEvidenceBackend,
    FunctionalUatError,
    _public_uat_manifest,
    _verify_source_file,
    discard_active_functional_uat_manifest,
    prefetch_models,
    validate_functional_uat_guidance_state,
    verify_functional_uat_store,
)
from src.functional_uat_kr_trade import (
    FunctionalUatKrTradeError,
    build_kr_trade_guidance_snapshot,
    inspect_kr_trade_snapshot,
    query_kr_trade_guidance_v3,
    register_kr_trade_source_receipts,
    require_kr_trade_retrieval_snapshot,
)
from src.guidance_v3 import GeneralGuidanceQueryRequestV3
from src.guidance_registry import REGISTRY_SHA256, legacy_3law_source_ids
from src.local_profiles import (
    DEV_FIXTURE_PROFILE,
    FUNCTIONAL_UAT_KR_TRADE_PROFILE,
    FUNCTIONAL_UAT_PROFILE,
    LocalProfileError,
    _apply_profile_values,
    _clear_law_api_credential,
    ensure_functional_uat_profile,
    ensure_functional_uat_kr_trade_profile,
    ensure_service_token,
    load_config,
    read_service_token,
    validate_functional_uat_law_api_credential,
)
from src.profile_evidence import (
    FUNCTIONAL_UAT_MODE,
    LOCAL_DEVELOPMENT_MODE,
    ProfileEvidenceRestartRequired,
    SERVICE_MODE_HEADER,
    create_guidance_snapshot_application,
    create_profile_evidence_application,
    create_dev_fixture_application,
)
from src.profile_wire import EvidenceRuntimeReadinessV3


def _request() -> dict[str, object]:
    return {
        "schema_version": "3.0",
        "request_id": "req-dev-fixture",
        "case_id": "case-dev-fixture",
        "review_run_id": "run-dev-fixture",
        "review_as_of": "2026-08-05",
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 5,
        "items": [
            {
                "item_id": "fixture-item-1",
                "description": "synthetic frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }
        ],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
    }


def test_dev_fixture_preserves_v3_wire_and_requires_mode_and_token(tmp_path: Path) -> None:
    token_path = ensure_service_token(DEV_FIXTURE_PROFILE, root=tmp_path)
    assert token_path.is_file()
    token = read_service_token(DEV_FIXTURE_PROFILE, root=tmp_path)
    app = create_dev_fixture_application(service_token=token)
    client = TestClient(app)

    missing = client.post("/internal/rag/v3/retrieve", json=_request())
    assert missing.status_code == 401
    assert missing.headers[SERVICE_MODE_HEADER] == LOCAL_DEVELOPMENT_MODE

    wrong_mode = client.post(
        "/internal/rag/v3/retrieve",
        json=_request(),
        headers={
            "Authorization": f"Bearer {token}",
            SERVICE_MODE_HEADER: "functional-uat",
        },
    )
    assert wrong_mode.status_code == 412

    headers = {
        "Authorization": f"Bearer {token}",
        SERVICE_MODE_HEADER: LOCAL_DEVELOPMENT_MODE,
    }
    response = client.post("/internal/rag/v3/retrieve", json=_request(), headers=headers)
    assert response.status_code == 200
    assert response.headers[SERVICE_MODE_HEADER] == LOCAL_DEVELOPMENT_MODE
    assert response.headers["X-Request-ID"] == "req-dev-fixture"
    body = response.json()
    assert body["schema_version"] == "3.0"
    assert body["status"] == "ok"
    requirement = body["results"][0]["evidence"][0]
    assert requirement["required_document_code"] == "CCCT_DOC_IMPORT_CONFIRMATION"
    assert "requirement_verification" not in requirement

    ready = client.get("/internal/rag/v3/health/ready", headers=headers)
    assert ready.status_code == 200
    assert ready.headers[SERVICE_MODE_HEADER] == LOCAL_DEVELOPMENT_MODE
    parsed = EvidenceRuntimeReadinessV3.model_validate(ready.json())
    assert parsed.status == "ready"
    assert parsed.model_dump(mode="json") == {"status": "ready"}


def test_dev_fixture_import_path_does_not_load_store_or_model_clients() -> None:
    """A fresh default setup must not need Docker client or model packages."""

    repository_root = Path(__file__).resolve().parents[2]
    program = "\n".join(
        (
            "from pathlib import Path",
            "import sys",
            "root = Path.cwd()",
            "sys.path[:0] = [str(root), str(root / 'law_repository')]",
            "import src.profile_evidence",
            "blocked = {'psycopg', 'neo4j', 'qdrant_client', 'pydantic_settings', ",
            "           'torch', 'numpy', 'FlagEmbedding', 'huggingface_hub'}",
            "loaded = sorted(name for name in blocked if any(",
            "    module == name or module.startswith(name + '.') for module in sys.modules",
            "))",
            "if loaded: raise SystemExit(','.join(loaded))",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_functional_profile_config_is_local_and_never_uses_default_passwords(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_profile(root=tmp_path)
    config = load_config(profile / "config.env")
    assert profile == tmp_path / "profiles" / FUNCTIONAL_UAT_PROFILE
    assert config["CUSTOMS_DATA_PROFILE"] == FUNCTIONAL_UAT_PROFILE
    assert config["POSTGRES_PASSWORD"]
    assert config["NEO4J_PASSWORD"]
    assert config["POSTGRES_PASSWORD"] != "change_me"
    assert config["NEO4J_PASSWORD"] != "change_me"
    assert (profile / "evidence-service-token").is_file()


def test_kr_trade_profile_is_separate_from_legacy_three_law_profile(tmp_path: Path) -> None:
    legacy = ensure_functional_uat_profile(root=tmp_path)
    trade = ensure_functional_uat_kr_trade_profile(root=tmp_path)

    assert legacy == tmp_path / "profiles" / FUNCTIONAL_UAT_PROFILE
    assert trade == tmp_path / "profiles" / FUNCTIONAL_UAT_KR_TRADE_PROFILE
    assert load_config(legacy / "config.env")["POSTGRES_PORT"] == "55452"
    assert load_config(trade / "config.env")["POSTGRES_PORT"] == "55462"
    assert (legacy / "evidence-service-token").is_file()
    assert (trade / "evidence-service-token").is_file()


def test_kr_trade_profile_fails_closed_until_explicit_source_build(tmp_path: Path) -> None:
    status = inspect_kr_trade_snapshot(local_root_override=tmp_path)

    assert status.profile == FUNCTIONAL_UAT_KR_TRADE_PROFILE
    assert status.status == "source_build_required"
    assert status.source_count == 0
    assert status.as_of is None
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_SOURCE_BUILD_REQUIRED"):
        require_kr_trade_retrieval_snapshot(local_root_override=tmp_path)


def test_kr_trade_registered_source_receipt_is_hash_checked_but_not_treated_as_retrieval_ready(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    raw = profile / "data" / "guidance-sources" / "test-run" / "kcs-guide.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("공개 안내 테스트 자료", encoding="utf-8")
    receipt = {
        "source_id": "kcs_customs_guidance",
        "source_uri": "https://www.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=817&mi=2819",
        "relative_path": "data/guidance-sources/test-run/kcs-guide.txt",
        "sha256": sha256(raw.read_bytes()).hexdigest(),
        "collected_at": datetime(2026, 8, 5, tzinfo=timezone.utc).isoformat(),
    }
    staged_manifest = raw.parent / "source-manifest.json"
    staged_manifest.write_text(
        json.dumps(
            {
                "schema_version": "functional-uat-kr-trade-source-input/v1",
                "as_of": "2026-08-05",
                "source_receipts": [receipt],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registered = register_kr_trade_source_receipts(
        staged_manifest_relative="data/guidance-sources/test-run/source-manifest.json",
        local_root_override=tmp_path,
    )
    assert registered.status == "registered_not_indexed"
    status = inspect_kr_trade_snapshot(local_root_override=tmp_path)
    assert status.status == "registered_not_indexed"
    assert status.source_count == 1
    assert status.as_of == date(2026, 8, 5)
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_RETRIEVAL_PROJECTION_REQUIRED"):
        require_kr_trade_retrieval_snapshot(local_root_override=tmp_path)

    raw.write_text("변조", encoding="utf-8")
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_SOURCE_HASH_MISMATCH"):
        inspect_kr_trade_snapshot(local_root_override=tmp_path)


def _kr_trade_build_manifest(profile: Path) -> str:
    """Stage one small official-source fixture exactly as an operator would."""

    raw = profile / "data" / "guidance-sources" / "test-build" / "kcs-guide.txt"
    raw.parent.mkdir(parents=True)
    excerpt = "수입 통관 신고는 거래 서류와 물품 정보를 확인하여 진행합니다."
    raw.write_text(f"관세청 공개 안내\n{excerpt}\n추가 안내", encoding="utf-8")
    receipt = {
        "source_id": "kcs_customs_guidance",
        "source_uri": "https://www.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=817&mi=2819",
        "relative_path": "data/guidance-sources/test-build/kcs-guide.txt",
        "sha256": sha256(raw.read_bytes()).hexdigest(),
        "collected_at": datetime(2026, 8, 5, tzinfo=timezone.utc).isoformat(),
        "topics": ["customs_clearance"],
        "directions": ["import"],
    }
    manifest = {
        "schema_version": "functional-uat-kr-trade-source-build-input/v1",
        "snapshot_id": "krtrade-20260805-test",
        "as_of": "2026-08-05",
        "source_receipts": [receipt],
        "guidance_records": [
            {
                "source_id": "kcs_customs_guidance",
                "topic": "customs_clearance",
                "direction": "import",
                "excerpt": excerpt,
                "locator": "통관 절차 안내",
            }
        ],
    }
    staged = raw.parent / "source-build.json"
    staged.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return "data/guidance-sources/test-build/source-build.json"


def test_kr_trade_explicit_build_creates_hash_verified_query_snapshot(tmp_path: Path) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    staged_relative = _kr_trade_build_manifest(profile)

    status = build_kr_trade_guidance_snapshot(
        staged_manifest_relative=staged_relative,
        local_root_override=tmp_path,
    )

    assert status.status == "ready"
    assert status.snapshot_id == "krtrade-20260805-test"
    assert status.as_of == date(2026, 8, 5)
    assert status.source_count == 1
    assert status.record_count == 1
    assert status.coverage_count == 1
    snapshot = require_kr_trade_retrieval_snapshot(local_root_override=tmp_path)
    assert snapshot.snapshot_id == status.snapshot_id
    assert snapshot.records[0].source_id == "kcs_customs_guidance"
    assert (
        profile / "state" / "guidance-snapshots" / "krtrade-20260805-test" / "manifest.json"
    ).is_file()

    raw = profile / "data" / "guidance-sources" / "test-build" / "kcs-guide.txt"
    raw.write_text("변조됨", encoding="utf-8")
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_SOURCE_HASH_MISMATCH"):
        require_kr_trade_retrieval_snapshot(local_root_override=tmp_path)


def test_kr_trade_guidance_only_endpoint_requires_built_snapshot_and_preserves_direction(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    build_kr_trade_guidance_snapshot(
        staged_manifest_relative=_kr_trade_build_manifest(profile),
        local_root_override=tmp_path,
    )
    token = read_service_token(FUNCTIONAL_UAT_KR_TRADE_PROFILE, root=tmp_path)
    app = create_guidance_snapshot_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=token,
        runtime_state_validator=lambda: require_kr_trade_retrieval_snapshot(
            local_root_override=tmp_path
        ),
        guidance_query_handler=lambda request: query_kr_trade_guidance_v3(
            request, local_root_override=tmp_path
        ),
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {token}",
        SERVICE_MODE_HEADER: FUNCTIONAL_UAT_MODE,
    }
    request = GeneralGuidanceQueryRequestV3.model_validate(
        {
            "schema_version": "3.0",
            "request_id": "kr-trade-guidance-1",
            "query": "분류와 무관한 표현",
            "topic_hint": "customs_clearance",
            "trade_direction": "both",
            "as_of": "2026-08-05",
            "top_k": 3,
        }
    )

    response = client.post(
        "/internal/rag/v3/guidance/query", json=request.model_dump(mode="json"), headers=headers
    )

    assert response.status_code == 200
    assert response.headers[SERVICE_MODE_HEADER] == FUNCTIONAL_UAT_MODE
    body = response.json()
    assert body["status"] == "partial"
    assert body["coverage"] == "partial"
    assert body["registry_sha256"] == REGISTRY_SHA256
    assert body["topic"] == "customs_clearance"
    assert body["results"][0]["source_kind"] == "official_agency_guidance"
    assert body["results"][0]["registry_source_id"] == "kcs_customs_guidance"
    assert body["results"][0]["direction"] == "import"
    assert (
        body["results"][0]["excerpt"]
        == "수입 통관 신고는 거래 서류와 물품 정보를 확인하여 진행합니다."
    )
    assert body["errors"][0]["code"] == "GUIDANCE_SNAPSHOT_PARTIAL_COVERAGE"
    assert (
        client.post("/internal/rag/v3/retrieve", json=_request(), headers=headers).status_code
        == 404
    )
    assert (
        client.post(
            "/internal/rag/v3/guidance/query", json=request.model_dump(mode="json")
        ).status_code
        == 401
    )
    raw = profile / "data" / "guidance-sources" / "test-build" / "kcs-guide.txt"
    raw.write_text("변조됨", encoding="utf-8")
    blocked = client.get("/internal/rag/v3/health/ready", headers=headers)
    assert blocked.status_code == 503
    assert blocked.json() == {"detail": "local guidance snapshot is unavailable"}


def test_kr_trade_build_rejects_untraceable_excerpt_and_legacy_source(tmp_path: Path) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    staged_relative = _kr_trade_build_manifest(profile)
    staged = profile / staged_relative
    payload = json.loads(staged.read_text(encoding="utf-8"))
    payload["guidance_records"][0]["excerpt"] = "원문에 존재하지 않는 충분히 긴 주장 문장입니다."
    staged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_EXCERPT_NOT_IN_SOURCE"):
        build_kr_trade_guidance_snapshot(
            staged_manifest_relative=staged_relative,
            local_root_override=tmp_path,
        )

    payload["guidance_records"][0]["excerpt"] = (
        "수입 통관 신고는 거래 서류와 물품 정보를 확인하여 진행합니다."
    )
    payload["source_receipts"][0]["source_id"] = "law_001556"
    payload["guidance_records"][0]["source_id"] = "law_001556"
    staged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_SNAPSHOT_INVALID"):
        build_kr_trade_guidance_snapshot(
            staged_manifest_relative=staged_relative,
            local_root_override=tmp_path,
        )


def test_kr_trade_build_requires_exact_registry_source_uri(tmp_path: Path) -> None:
    profile = ensure_functional_uat_kr_trade_profile(root=tmp_path)
    staged_relative = _kr_trade_build_manifest(profile)
    staged = profile / staged_relative
    payload = json.loads(staged.read_text(encoding="utf-8"))
    payload["source_receipts"][0]["source_uri"] = "https://example.invalid/not-the-registry-source"
    staged.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(FunctionalUatKrTradeError, match="KR_TRADE_SNAPSHOT_INVALID"):
        build_kr_trade_guidance_snapshot(
            staged_manifest_relative=staged_relative,
            local_root_override=tmp_path,
        )


def test_functional_credential_preflight_does_not_export_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ensure_functional_uat_profile(root=tmp_path)
    config_path = profile / "config.env"
    monkeypatch.setenv("LAW_API_OC", "inherited-terminal-key")

    with pytest.raises(LocalProfileError, match="law API credential"):
        validate_functional_uat_law_api_credential(root=tmp_path)
    assert "LAW_API_OC" not in os.environ

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "LAW_API_OC=\n", "LAW_API_OC=unit-test-law-api-key\n"
        ),
        encoding="utf-8",
    )
    assert validate_functional_uat_law_api_credential(root=tmp_path) == profile
    assert "LAW_API_OC" not in os.environ


def test_functional_runtime_scrubs_law_api_key_outside_collection_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The long-lived Evidence process must never inherit a collection key."""

    values = {
        "POSTGRES_PASSWORD": "unit-test-db-password",
        "NEO4J_PASSWORD": "unit-test-neo4j-password",
        "LAW_API_OC": "unit-test-law-api-key",
    }
    monkeypatch.setenv("LAW_API_OC", "inherited-terminal-key")
    _apply_profile_values(values, include_law_api_credential=False)
    assert "LAW_API_OC" not in os.environ

    _apply_profile_values(values, include_law_api_credential=True)
    assert os.environ.get("LAW_API_OC") == "unit-test-law-api-key"

    _clear_law_api_credential()
    assert "LAW_API_OC" not in os.environ


def test_data_profile_does_not_expose_checkout_raw_law_cache_helpers() -> None:
    """Official raw law persistence belongs only to the bounded UAT builder."""

    source = (Path(__file__).resolve().parents[1] / "src" / "lawgo.py").read_text(encoding="utf-8")
    assert "RAW_DIR" not in source
    assert "def fetch_current_law" not in source
    assert "def fetch_law_by_mst" not in source


def test_functional_source_validation_rejects_escape_and_hash_drift(tmp_path: Path) -> None:
    profile = ensure_functional_uat_profile(root=tmp_path)
    source = profile / "data" / "laws" / "run" / "law_effective_001556.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"official":"source"}\n')
    digest = sha256(source.read_bytes()).hexdigest()

    _verify_source_file(
        profile=profile,
        relative="data/laws/run/law_effective_001556.json",
        expected_sha256=digest,
    )
    with pytest.raises(FunctionalUatError, match="SOURCE_PATH_INVALID"):
        _verify_source_file(profile=profile, relative="../outside.json", expected_sha256=digest)
    with pytest.raises(FunctionalUatError, match="SOURCE_HASH_MISMATCH"):
        _verify_source_file(
            profile=profile,
            relative="data/laws/run/law_effective_001556.json",
            expected_sha256="0" * 64,
        )


def test_explicit_reset_removes_only_the_active_marker_and_keeps_raw_sources(
    tmp_path: Path,
) -> None:
    profile = ensure_functional_uat_profile(root=tmp_path)
    raw_source = profile / "data" / "laws" / "previous-run" / "law.json"
    raw_source.parent.mkdir(parents=True)
    raw_source.write_text("local source", encoding="utf-8")
    active = profile / "state" / "active-manifest.json"
    active.parent.mkdir(parents=True)
    active.write_text('{"profile":"functional-uat-3law"}\n', encoding="utf-8")

    assert discard_active_functional_uat_manifest(local_root_override=tmp_path) is True
    assert not active.exists()
    assert raw_source.read_text(encoding="utf-8") == "local source"
    assert discard_active_functional_uat_manifest(local_root_override=tmp_path) is False


def test_model_prefetch_uses_only_pinned_identities_and_local_cache(tmp_path: Path) -> None:
    profile = ensure_functional_uat_profile(root=tmp_path)
    calls: list[dict[str, object]] = []

    def downloader(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(tmp_path / "cache")

    inventory = prefetch_models(profile_directory=profile, downloader=downloader)
    assert set(inventory) == {"dense", "reranker"}
    assert {call["repo_id"] for call in calls} == {
        inventory["dense"]["model_id"],
        inventory["reranker"]["model_id"],
    }
    assert all(call["local_files_only"] is False for call in calls)
    assert all(call["cache_dir"] == str(tmp_path / "cache" / "huggingface") for call in calls)
    assert FUNCTIONAL_UAT_AS_OF == date(2026, 8, 5)
    assert tuple(law_id for law_id, _ in FUNCTIONAL_UAT_LAWS) == (
        "001556",
        "002421",
        "006392",
    )


def test_functional_backend_uses_the_validated_manifest_review_date() -> None:
    backend = FunctionalUatEvidenceBackend(review_as_of=FUNCTIONAL_UAT_AS_OF)

    assert backend.snapshot_effective_date("local-ccct-snapshot") == FUNCTIONAL_UAT_AS_OF
    with pytest.raises(FunctionalUatError, match="AS_OF_MISMATCH"):
        FunctionalUatEvidenceBackend(review_as_of=date(2026, 8, 4))


def test_functional_runtime_readiness_keeps_full_store_scan_out_of_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FunctionalUatEvidenceBackend(review_as_of=FUNCTIONAL_UAT_AS_OF)
    monkeypatch.setattr(backend, "current_snapshot_id", lambda: "ccct_snapshot_1")
    monkeypatch.setattr(backend, "snapshot_effective_date", lambda _snapshot: FUNCTIONAL_UAT_AS_OF)
    monkeypatch.setattr(
        "src.store_consistency.verify_complete_store_consistency",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("full store scan must be explicit")),
    )

    assert backend.readiness() == {
        "status": "ready",
        "knowledge_snapshot_id": "ccct_snapshot_1",
        "supported_review_as_of": FUNCTIONAL_UAT_AS_OF,
    }


def test_functional_ready_revalidates_local_state_after_authentication_without_deep_scan() -> None:
    """A running UAT server must not trust only the launch-time validation."""

    token = "a" * 32
    calls: list[str] = []

    def state_validator() -> None:
        calls.append("state")
        raise RuntimeError("postgresql://trade:secret@127.0.0.1:55452/trade_doc")

    from src.conformance import TestOnlyConformanceEvidenceBackend

    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=token,
        backend=TestOnlyConformanceEvidenceBackend(effective_date=FUNCTIONAL_UAT_AS_OF),
        runtime_state_validator=state_validator,
    )
    client = TestClient(app)
    unauthenticated = client.get("/internal/rag/v3/health/ready")
    assert unauthenticated.status_code == 401
    assert calls == []

    response = client.get(
        "/internal/rag/v3/health/ready",
        headers={
            "Authorization": f"Bearer {token}",
            SERVICE_MODE_HEADER: FUNCTIONAL_UAT_MODE,
        },
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "local Evidence profile is unavailable"}
    assert "secret" not in response.text
    assert calls == ["state"]


def test_functional_retrieve_revalidates_local_state_before_evidence_dispatch() -> None:
    """A stale active source marker cannot be bypassed after server startup."""

    token = "a" * 32
    calls: list[str] = []

    def state_validator() -> None:
        calls.append("state")
        raise RuntimeError("postgresql://trade:secret@127.0.0.1:55452/trade_doc")

    from src.conformance import TestOnlyConformanceEvidenceBackend

    backend = TestOnlyConformanceEvidenceBackend(effective_date=FUNCTIONAL_UAT_AS_OF)
    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=token,
        backend=backend,
        runtime_state_validator=state_validator,
    )
    client = TestClient(app)
    unauthenticated = client.post("/internal/rag/v3/retrieve", json=_request())
    assert unauthenticated.status_code == 401
    assert calls == []
    assert backend.retrieve_calls == 0

    response = client.post(
        "/internal/rag/v3/retrieve",
        json=_request(),
        headers={
            "Authorization": f"Bearer {token}",
            SERVICE_MODE_HEADER: FUNCTIONAL_UAT_MODE,
        },
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "local Evidence profile is unavailable"}
    assert "secret" not in response.text
    assert calls == ["state"]
    assert backend.retrieve_calls == 0


def test_functional_restart_required_is_a_safe_distinct_503_response() -> None:
    token = "a" * 32

    def restart_validator() -> None:
        raise ProfileEvidenceRestartRequired("LAW_HISTORY_SERVICE_RESTART_REQUIRED")

    from src.conformance import TestOnlyConformanceEvidenceBackend

    backend = TestOnlyConformanceEvidenceBackend(effective_date=FUNCTIONAL_UAT_AS_OF)
    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=token,
        backend=backend,
        runtime_state_validator=restart_validator,
    )
    client = TestClient(app)
    headers = {
        "Authorization": f"Bearer {token}",
        SERVICE_MODE_HEADER: FUNCTIONAL_UAT_MODE,
    }

    ready = client.get("/internal/rag/v3/health/ready", headers=headers)
    response = client.post("/internal/rag/v3/retrieve", json=_request(), headers=headers)

    assert ready.status_code == 503
    assert response.status_code == 503
    assert ready.json() == {"detail": "local Evidence profile restart required"}
    assert response.json() == {"detail": "local Evidence profile restart required"}
    assert backend.retrieve_calls == 0


def test_functional_guidance_revalidates_narrow_source_scope_after_authentication() -> None:
    token = "a" * 32
    calls: list[str] = []

    def guidance_state_validator() -> frozenset[str]:
        calls.append("guidance")
        raise RuntimeError("postgresql://trade:secret@127.0.0.1:55452/trade_doc")

    from src.conformance import TestOnlyConformanceEvidenceBackend

    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token=token,
        backend=TestOnlyConformanceEvidenceBackend(effective_date=FUNCTIONAL_UAT_AS_OF),
        guidance_state_validator=guidance_state_validator,
    )
    client = TestClient(app)
    payload = {
        "schema_version": "3.0",
        "request_id": "guidance-state-test",
        "query": "수입 통관 절차를 알려 주세요.",
        "trade_direction": "import",
        "as_of": "2026-08-05",
        "top_k": 1,
    }

    unauthenticated = client.post("/internal/rag/v3/guidance/query", json=payload)
    assert unauthenticated.status_code == 401
    assert calls == []
    response = client.post(
        "/internal/rag/v3/guidance/query",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            SERVICE_MODE_HEADER: FUNCTIONAL_UAT_MODE,
        },
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "local Evidence profile is unavailable"}
    assert "secret" not in response.text
    assert calls == ["guidance"]


def test_legacy_guidance_state_validator_checks_only_active_source_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assets: list[dict[str, str]] = []
    source_hashes: dict[str, str] = {}
    for law_id, _name in FUNCTIONAL_UAT_LAWS:
        relative = f"data/laws/guidance/{law_id}.json"
        raw = tmp_path / relative
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f'{{"law_id":"{law_id}"}}', encoding="utf-8")
        digest = sha256(raw.read_bytes()).hexdigest()
        source_hashes[law_id] = digest
        assets.append(
            {
                "law_id": law_id,
                "current_file": relative,
                "effective_file": relative,
                "effective_sha256": digest,
            }
        )
    manifest = {
        "source_hashes": source_hashes,
        "source_assets": assets,
    }
    monkeypatch.setattr(
        "src.functional_uat.ensure_functional_uat_profile", lambda **_kwargs: tmp_path
    )
    monkeypatch.setattr(
        "src.functional_uat._load_active_manifest",
        lambda _profile: (manifest, "digest"),
    )

    assert validate_functional_uat_guidance_state() == legacy_3law_source_ids()
    (tmp_path / assets[0]["effective_file"]).write_text("drift", encoding="utf-8")
    with pytest.raises(FunctionalUatError, match="SOURCE_HASH_MISMATCH"):
        validate_functional_uat_guidance_state()


def test_public_functional_uat_manifest_excludes_private_source_and_image_metadata() -> None:
    manifest = _public_uat_manifest(
        as_of=FUNCTIONAL_UAT_AS_OF,
        source_hashes={law_id: "a" * 64 for law_id, _name in FUNCTIONAL_UAT_LAWS},
        models={
            "dense": {"model_id": "example/dense", "revision": "b" * 40},
            "reranker": {"model_id": "example/reranker", "revision": "c" * 40},
        },
        store_counts={"law_count": 3, "postgresql": 12, "qdrant": 12, "neo4j": 12},
        data_build_timestamp="2026-08-05T00:00:00+00:00",
    )

    assert set(manifest) == {
        "profile",
        "as_of",
        "law_ids",
        "source_hashes",
        "model_ids/revisions",
        "store_counts",
        "data_build_timestamp",
    }
    assert "source_assets" not in manifest
    assert "image_digests" not in manifest
    assert manifest["law_ids"] == [law_id for law_id, _name in FUNCTIONAL_UAT_LAWS]


def test_functional_store_verification_redacts_backend_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://trade:secret@127.0.0.1:55452/trade_doc"

    monkeypatch.setattr(
        "src.functional_uat.validate_functional_uat_state",
        lambda **_kwargs: (
            {
                "source_assets": [
                    {"law_id": law_id, "article_count": 1} for law_id, _name in FUNCTIONAL_UAT_LAWS
                ]
            },
            "digest",
        ),
    )
    monkeypatch.setattr(
        "src.functional_uat._functional_source_projection",
        lambda *_args, **_kwargs: (
            {law_id: 1 for law_id, _name in FUNCTIONAL_UAT_LAWS},
            {(law_id, 1, None, None): "a" * 64 for law_id, _name in FUNCTIONAL_UAT_LAWS},
        ),
    )

    def fail(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr("src.store_consistency.verify_complete_store_consistency", fail)

    with pytest.raises(FunctionalUatError) as error:
        verify_functional_uat_store()

    assert str(error.value) == "FUNCTIONAL_UAT_STORE_INTEGRITY_FAILED"
    assert secret not in str(error.value)


def test_functional_store_verification_binds_raw_source_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_assets = [{"law_id": law_id, "article_count": 1} for law_id, _ in FUNCTIONAL_UAT_LAWS]
    expected_counts = {law_id: 1 for law_id, _ in FUNCTIONAL_UAT_LAWS}
    expected_hashes = {(law_id, 1, None, None): "b" * 64 for law_id, _ in FUNCTIONAL_UAT_LAWS}
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        "src.functional_uat.validate_functional_uat_state",
        lambda **_kwargs: ({"source_assets": source_assets}, "digest"),
    )
    monkeypatch.setattr(
        "src.functional_uat.ensure_functional_uat_profile", lambda **_kwargs: tmp_path
    )
    monkeypatch.setattr(
        "src.functional_uat._functional_source_projection",
        lambda *_args, **_kwargs: (expected_counts, expected_hashes),
    )
    monkeypatch.setattr(
        "src.store_consistency.verify_complete_store_consistency",
        lambda **kwargs: observed.update(kwargs) or {"laws": 3},
    )

    assert verify_functional_uat_store() == {"laws": 3}
    assert observed == {
        "expected_law_ids": tuple(law_id for law_id, _ in FUNCTIONAL_UAT_LAWS),
        "expected_current_article_counts": expected_counts,
        "expected_current_article_hashes": expected_hashes,
    }


def test_functional_source_projection_derives_identity_hashes_from_effective_raw_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.functional_uat import _functional_source_projection

    stub = types.ModuleType("src.ingest.laws")
    stub.build_meta = lambda body, _mst: {"law_id": body["law_id"]}  # type: ignore[attr-defined]
    stub.parse_articles = lambda body, _meta: [  # type: ignore[attr-defined]
        {
            "article_no": 100,
            "paragraph_no": None,
            "content": f"effective:{body['law_id']}",
            "children": [],
        }
    ]
    monkeypatch.setitem(sys.modules, "src.ingest.laws", stub)
    assets: list[dict[str, object]] = []
    for law_id, _name in FUNCTIONAL_UAT_LAWS:
        relative = f"data/laws/{law_id}.json"
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(f'{{"law_id":"{law_id}"}}', encoding="utf-8")
        assets.append(
            {
                "law_id": law_id,
                "article_count": 1,
                "effective_mst": "mst",
                "effective_file": relative,
            }
        )

    counts, hashes = _functional_source_projection(assets, profile=tmp_path)

    assert counts == {law_id: 1 for law_id, _name in FUNCTIONAL_UAT_LAWS}
    assert hashes == {
        (law_id, 100, None, None): sha256(f"effective:{law_id}".encode("utf-8")).hexdigest()
        for law_id, _name in FUNCTIONAL_UAT_LAWS
    }


def test_dev_store_verify_is_explicit_and_redacts_store_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "dev_store.py"
    spec = importlib.util.spec_from_file_location("test_dev_store_tool", tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.build_parser().parse_args(["verify"]).command == "verify"
    monkeypatch.setattr(module, "apply_data_store_environment", lambda _profile: Path("profile"))

    observed: dict[str, object] = {}

    def succeed(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"status": "ready"}

    monkeypatch.setattr(
        "src.store_consistency.verify_complete_store_consistency",
        succeed,
    )
    assert module.main(["verify"]) == 0
    assert "dev-store full integrity verified" in capsys.readouterr().out
    assert observed == {"verify_source_inputs": False}

    secret = "postgresql://trade:secret@127.0.0.1:55432/trade_doc"

    def fail(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr("src.store_consistency.verify_complete_store_consistency", fail)
    assert module.main(["verify"]) == 2
    captured = capsys.readouterr()
    assert captured.err == "BLOCKED: dev-store integrity verification failed\n"
    assert secret not in captured.err


def _law_history_uat_tool() -> types.ModuleType:
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "functional_uat_law_history.py"
    spec = importlib.util.spec_from_file_location("test_law_history_uat_tool", tool_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The tool defines a dataclass under postponed annotations; registering
    # the temporary module mirrors normal import semantics for dataclasses.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("changed_manifest", "changed_digest"),
    [
        (
            {"coverage_through": "2026-08-05", "retrieval_strategy": "rrf_lexical"},
            "b" * 64,
        ),
        (
            {"coverage_through": "2026-08-06", "retrieval_strategy": "rrf_lexical"},
            "a" * 64,
        ),
        (
            {
                "coverage_through": "2026-08-05",
                "retrieval_strategy": "cross_encoder_rerank",
            },
            "a" * 64,
        ),
    ],
)
def test_law_history_server_binding_rejects_manifest_digest_or_backend_config_drift(
    monkeypatch: pytest.MonkeyPatch,
    changed_manifest: dict[str, str],
    changed_digest: str,
) -> None:
    tool = _law_history_uat_tool()
    startup_manifest = {
        "coverage_through": "2026-08-05",
        "retrieval_strategy": "rrf_lexical",
    }
    calls = 0

    def validate() -> tuple[dict[str, str], str]:
        nonlocal calls
        calls += 1
        return (startup_manifest, "a" * 64) if calls == 1 else (changed_manifest, changed_digest)

    monkeypatch.setattr(tool, "validate_law_history_state", validate)
    binding = tool._capture_law_history_service_binding()

    assert binding.coverage_through == date(2026, 8, 5)
    assert binding.retrieval_strategy == "rrf_lexical"
    with pytest.raises(ProfileEvidenceRestartRequired, match="SERVICE_RESTART_REQUIRED"):
        binding.validate_runtime()


def test_law_history_server_binding_returns_restart_503_before_evidence_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _law_history_uat_tool()
    calls = 0

    def validate() -> tuple[dict[str, str], str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return (
                {"coverage_through": "2026-08-05", "retrieval_strategy": "rrf_lexical"},
                "a" * 64,
            )
        return (
            {"coverage_through": "2026-08-06", "retrieval_strategy": "rrf_lexical"},
            "a" * 64,
        )

    monkeypatch.setattr(tool, "validate_law_history_state", validate)
    binding = tool._capture_law_history_service_binding()
    from src.conformance import TestOnlyConformanceEvidenceBackend

    backend = TestOnlyConformanceEvidenceBackend(effective_date=FUNCTIONAL_UAT_AS_OF)
    app = create_profile_evidence_application(
        mode=FUNCTIONAL_UAT_MODE,
        service_token="a" * 32,
        backend=backend,
        runtime_state_validator=binding.validate_runtime,
    )
    client = TestClient(app)
    response = client.post(
        "/internal/rag/v3/retrieve",
        json=_request(),
        headers={
            "Authorization": "Bearer " + "a" * 32,
            SERVICE_MODE_HEADER: FUNCTIONAL_UAT_MODE,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "local Evidence profile restart required"}
    assert backend.retrieve_calls == 0
