from __future__ import annotations

import os
import subprocess
import sys
from hashlib import sha256
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.functional_uat import (
    FUNCTIONAL_UAT_AS_OF,
    FUNCTIONAL_UAT_LAWS,
    FunctionalUatEvidenceBackend,
    FunctionalUatError,
    _verify_source_file,
    discard_active_functional_uat_manifest,
    prefetch_models,
)
from src.local_profiles import (
    DEV_FIXTURE_PROFILE,
    FUNCTIONAL_UAT_PROFILE,
    LocalProfileError,
    _apply_profile_values,
    _clear_law_api_credential,
    ensure_functional_uat_profile,
    ensure_service_token,
    load_config,
    read_service_token,
    validate_functional_uat_law_api_credential,
)
from src.profile_evidence import (
    LOCAL_DEVELOPMENT_MODE,
    SERVICE_MODE_HEADER,
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
