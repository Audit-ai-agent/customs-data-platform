from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import types
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
    _public_uat_manifest,
    _verify_source_file,
    discard_active_functional_uat_manifest,
    prefetch_models,
    verify_functional_uat_store,
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
    FUNCTIONAL_UAT_MODE,
    LOCAL_DEVELOPMENT_MODE,
    SERVICE_MODE_HEADER,
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
