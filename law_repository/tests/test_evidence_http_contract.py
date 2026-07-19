from __future__ import annotations

import hashlib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence import DatabaseEvidenceBackend
from src.provider_identity import (
    HEADER_PROVIDER_BUILD_SHA256,
    HEADER_PROVIDER_RELEASE,
    HEADER_PROVIDER_REVISION,
    HEADER_SCHEMA_SHA256,
    ProviderBuildIdentity,
    create_provider_build_identity,
)
from src.webapp import (
    create_evidence_application,
    create_test_conformance_application,
)
from tools.run_evidence_conformance_server import build_parser


def _identity() -> ProviderBuildIdentity:
    return create_provider_build_identity(
        provider_revision="1" * 40,
        schema_sha256="2" * 64,
        source_manifest_sha256="3" * 64,
    )


def _request_body() -> dict:
    return {
        "schema_version": "1.0",
        "request_id": "req-http-conformance",
        "case_id": "case-http-conformance",
        "review_run_id": "run-http-conformance",
        "review_as_of": "2026-07-10",
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 5,
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


def test_readiness_exposes_exact_build_identity_and_projection_state() -> None:
    identity = _identity()
    app = create_test_conformance_application(identity=identity)
    response = TestClient(app).get("/internal/rag/v1/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["supported_review_as_of"] == "2026-07-10"
    assert payload["evidence_contract"] == identity.model_dump()
    assert payload["law_index"]["neo4j_missing"] == 0
    assert payload["law_index"]["neo4j_orphaned"] == 0


@pytest.mark.parametrize(
    "changed_header",
    [
        HEADER_SCHEMA_SHA256,
        HEADER_PROVIDER_RELEASE,
        HEADER_PROVIDER_REVISION,
        HEADER_PROVIDER_BUILD_SHA256,
    ],
)
def test_post_rejects_missing_or_mismatched_binding_before_backend_creation(
    changed_header: str,
) -> None:
    identity = _identity()
    backend = TestOnlyConformanceEvidenceBackend()
    app = create_test_conformance_application(identity=identity, backend=backend)
    factory_calls = 0

    def backend_factory():
        nonlocal factory_calls
        factory_calls += 1
        return backend

    app.state.evidence_backend_factory = backend_factory
    client = TestClient(app)

    missing = client.post("/internal/rag/v1/retrieve", json=_request_body())
    assert missing.status_code == 412
    assert factory_calls == 0
    assert backend.retrieve_calls == 0

    headers = identity.binding_headers()
    headers[changed_header] = "mismatch"
    mismatched = client.post(
        "/internal/rag/v1/retrieve",
        json=_request_body(),
        headers=headers,
    )
    assert mismatched.status_code == 412
    assert factory_calls == 0
    assert backend.retrieve_calls == 0


def test_matching_post_returns_same_four_headers_and_complete_cited_evidence() -> None:
    identity = _identity()
    backend = TestOnlyConformanceEvidenceBackend()
    app = create_test_conformance_application(identity=identity, backend=backend)
    response = TestClient(app).post(
        "/internal/rag/v1/retrieve",
        json=_request_body(),
        headers=identity.binding_headers(),
    )

    assert response.status_code == 200
    for name, value in identity.binding_headers().items():
        assert response.headers[name] == value
    assert {
        name for name in (
            HEADER_SCHEMA_SHA256,
            HEADER_PROVIDER_RELEASE,
            HEADER_PROVIDER_REVISION,
            HEADER_PROVIDER_BUILD_SHA256,
        )
        if name in response.headers
    } == set(identity.binding_headers())

    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "request_id",
        "status",
        "knowledge_snapshot_id",
        "results",
        "errors",
    }
    assert payload["status"] == "ok"
    result = payload["results"][0]
    assert result["coverage"] == "complete"
    assert result["requirements_found"] is True
    assert result["evidence"]
    evidence = result["evidence"][0]
    assert evidence["source_record_id"]
    assert evidence["source_uri"]
    assert evidence["version_hash"] == "a" * 64
    assert evidence["required_document_code"]
    assert evidence["required_document_name"]
    expected_id = "ev_" + hashlib.sha256(
        ("test_only_ccct_src_0303540000\x1f" + "a" * 64).encode()
    ).hexdigest()
    assert evidence["evidence_id"] == expected_id
    assert backend.retrieve_calls == 1


def test_production_factory_cannot_be_switched_to_fixture_by_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EVIDENCE_BACKEND", "conformance")
    monkeypatch.setenv("EVIDENCE_TEST_FIXTURE", "true")
    app = create_evidence_application()

    assert app.state.test_only_conformance is False
    assert isinstance(app.state.evidence_backend_factory(), DatabaseEvidenceBackend)


def test_identity_and_readiness_failures_return_generic_503_without_raw_error() -> None:
    app = create_evidence_application()

    def failing_identity():
        raise RuntimeError("C:/secret/provider-build.json token=do-not-return")

    app.state.provider_identity_loader = failing_identity
    response = TestClient(app).get("/internal/rag/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Evidence provider build identity is unavailable"
    }
    assert "secret" not in response.text

    class FailingReadinessBackend(TestOnlyConformanceEvidenceBackend):
        def readiness(self):
            raise RuntimeError("postgresql://user:password@example.invalid/db")

    conformance_app = create_test_conformance_application(
        identity=_identity(),
        backend=FailingReadinessBackend(effective_date=date(2026, 7, 10)),
    )
    response = TestClient(conformance_app).get("/internal/rag/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Evidence provider is not ready"}
    assert "password" not in response.text


def test_conformance_runner_requires_explicit_flag_bundle_and_loopback() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--bundle-root",
            ".",
            "--host",
            "127.0.0.1",
            "--port",
            "18010",
        ])
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--test-only-conformance",
            "--bundle-root",
            ".",
            "--host",
            "0.0.0.0",
            "--port",
            "18010",
        ])
    args = parser.parse_args([
        "--test-only-conformance",
        "--bundle-root",
        ".",
        "--host",
        "127.0.0.1",
        "--port",
        "18010",
    ])
    assert args.test_only_conformance is True
