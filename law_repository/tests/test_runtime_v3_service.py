from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.deadline import RequestDeadline
from src.runtime_service import create_test_runtime_v3_application
from src.runtime_settings import RuntimeSettings
from src.requirement_policy_release import VerifiedRequirementPolicyRelease
from src.runtime_api_v3 import (
    RUNTIME_V3_LIVENESS_PATH,
    RUNTIME_V3_READINESS_PATH,
    RUNTIME_V3_RETRIEVE_PATH,
)
from src.runtime_release import (
    EvidenceRuntimeReleaseIdentity,
    GenerationRequestLease,
    runtime_release_sha256,
)
from src.runtime_release_v3 import (
    REQUIREMENT_POLICY_SCHEMA_PATH,
    V3_READINESS_SCHEMA_PATH,
    V3_RETRIEVAL_SCHEMA_PATH,
    RuntimeV3ContractHashes,
    RuntimeV3LeaseLoader,
    load_runtime_v3_contract_hashes,
)
from tools.test_only_v3_policy import build_test_only_policy_fixture

_TOKEN = "runtime-v3-token-" + ("a" * 40)
_ROOT = Path(__file__).resolve().parents[2]


def _base_identity() -> EvidenceRuntimeReleaseIdentity:
    payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": "1" * 40,
        "provider_build_sha256": "2" * 64,
        "retrieval_schema_sha256": "3" * 64,
        "provider_source_manifest_sha256": "4" * 64,
        "knowledge_generation_id": "5" * 64,
        "knowledge_generation_attestation_sha256": "6" * 64,
        "knowledge_source_manifest_sha256": "7" * 64,
        "model_manifest_sha256": "8" * 64,
        "index_config_sha256": "9" * 64,
        "evaluation_report_sha256": "a" * 64,
        "seal_attestation_sha256": "b" * 64,
        "knowledge_snapshot_id": "test-only-conformance-snapshot",
        "supported_review_as_of": "2026-07-10",
    }
    payload["runtime_release_sha256"] = runtime_release_sha256(payload)
    return EvidenceRuntimeReleaseIdentity.model_validate(payload)


class _BaseLoader:
    def __init__(self) -> None:
        self.lease = GenerationRequestLease(
            identity=_base_identity(),
            generation_epoch=1,
            generation_changed_at="2026-07-21T00:00:00Z",
        )

    def acquire(self, _deadline: RequestDeadline | None = None) -> GenerationRequestLease:
        return self.lease

    def assert_unchanged(
        self,
        lease: GenerationRequestLease,
        _deadline: RequestDeadline | None = None,
    ) -> None:
        if lease != self.lease:
            raise RuntimeError("base runtime changed")


def _approved_policy() -> VerifiedRequirementPolicyRelease:
    return build_test_only_policy_fixture(
        knowledge_source_manifest_sha256="7" * 64,
        knowledge_snapshot_id="test-only-conformance-snapshot",
        required_document_code="CCCT_DOC_IMPORT_CONFIRMATION",
    ).verified


class _ReadyBackend(TestOnlyConformanceEvidenceBackend):
    def validate_ready(self) -> None:
        return None


def _settings() -> RuntimeSettings:
    return RuntimeSettings(
        _env_file=None,
        service_token=_TOKEN,
        transport_mode="direct_tls",
        max_concurrency=2,
        request_deadline_seconds=5,
        store_operation_timeout_seconds=1,
        model_inference_timeout_seconds=1,
        model_termination_timeout_seconds=1,
        shutdown_grace_seconds=5,
    )


def _loader(policy: VerifiedRequirementPolicyRelease | None = None) -> RuntimeV3LeaseLoader:
    return RuntimeV3LeaseLoader(
        base_loader=_BaseLoader(),
        policy_loader=lambda: policy or _approved_policy(),
        contract_hash_loader=lambda: RuntimeV3ContractHashes(
            retrieval_schema_sha256="6" * 64,
            readiness_schema_sha256="7" * 64,
            requirement_policy_schema_sha256="8" * 64,
        ),
    )


def _body() -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "request_id": "req-runtime-v3",
        "case_id": "case-runtime-v3",
        "review_run_id": "run-runtime-v3",
        "review_as_of": "2026-07-10",
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 5,
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


def _app(loader: RuntimeV3LeaseLoader, backend: _ReadyBackend | None = None):
    selected = backend or _ReadyBackend()
    return create_test_runtime_v3_application(
        service_settings=_settings(),
        runtime_release_loader=loader,
        backend_factory=lambda _lease, _deadline: selected,
    )


def test_runtime_v3_readiness_and_retrieval_are_exactly_bound() -> None:
    loader = _loader()
    identity = loader.acquire().identity
    app = _app(loader)
    with TestClient(app, base_url="https://evidence.internal") as client:
        assert client.get(RUNTIME_V3_READINESS_PATH).status_code == 401
        readiness = client.get(
            RUNTIME_V3_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        response = client.post(
            RUNTIME_V3_RETRIEVE_PATH,
            json=_body(),
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
                **identity.binding_headers(),
            },
        )
    assert readiness.status_code == 200
    assert readiness.json()["evidence_runtime_release"]["schema_version"] == "3.0"
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    assert response.headers["X-Service-Request-ID"]
    payload = response.json()
    requirement = next(
        row
        for result in payload["results"]
        for row in result["evidence"]
        if row["required_document_code"]
    )
    verification = requirement["requirement_verification"]
    assert verification["required_document_code"] == "CCCT_DOC_IMPORT_CONFIRMATION"
    assert verification["verification_policy"] == "content_match_required"
    assert len(verification["policy_source_sha256"]) == 64
    assert verification["policy_release"].startswith("test-only-conformance")
    assert verification["approved"] is True
    for name, value in identity.binding_headers().items():
        assert response.headers[name] == value


@pytest.mark.parametrize(
    "missing_header",
    sorted(_loader().acquire().identity.binding_headers()),
)
def test_runtime_v3_missing_binding_is_412_before_retrieval(
    missing_header: str,
) -> None:
    loader = _loader()
    identity = loader.acquire().identity
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        **identity.binding_headers(),
    }
    headers.pop(missing_header)
    backend = _ReadyBackend()
    with TestClient(_app(loader, backend), base_url="https://evidence.internal") as client:
        response = client.post(RUNTIME_V3_RETRIEVE_PATH, json=_body(), headers=headers)
    assert response.status_code == 412
    assert backend.retrieve_calls == 0


@pytest.mark.parametrize(
    "mismatched_header",
    sorted(_loader().acquire().identity.binding_headers()),
)
def test_runtime_v3_mismatched_binding_is_412_before_retrieval(
    mismatched_header: str,
) -> None:
    loader = _loader()
    identity = loader.acquire().identity
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        **identity.binding_headers(),
    }
    headers[mismatched_header] = "mismatched-release-identity"
    backend = _ReadyBackend()
    with TestClient(_app(loader, backend), base_url="https://evidence.internal") as client:
        response = client.post(RUNTIME_V3_RETRIEVE_PATH, json=_body(), headers=headers)
    assert response.status_code == 412
    assert backend.retrieve_calls == 0


def test_runtime_v3_duplicate_new_binding_is_412_before_retrieval() -> None:
    loader = _loader()
    headers = [
        ("Authorization", f"Bearer {_TOKEN}"),
        ("Content-Type", "application/json"),
        *loader.acquire().identity.binding_headers().items(),
        ("X-Evidence-Runtime-V3-SHA256", "0" * 64),
    ]
    backend = _ReadyBackend()
    with TestClient(_app(loader, backend), base_url="https://evidence.internal") as client:
        response = client.post(RUNTIME_V3_RETRIEVE_PATH, json=_body(), headers=headers)
    assert response.status_code == 412
    assert backend.retrieve_calls == 0


def test_runtime_v3_unreleased_policy_is_fail_closed_but_live() -> None:
    def unavailable_policy() -> VerifiedRequirementPolicyRelease:
        raise ValueError("test-only unreleased policy is not verified")

    loader = RuntimeV3LeaseLoader(
        base_loader=_BaseLoader(),
        policy_loader=unavailable_policy,
        contract_hash_loader=lambda: RuntimeV3ContractHashes(
            retrieval_schema_sha256="6" * 64,
            readiness_schema_sha256="7" * 64,
            requirement_policy_schema_sha256="8" * 64,
        ),
    )
    with TestClient(_app(loader), base_url="https://evidence.internal") as client:
        assert client.get(RUNTIME_V3_LIVENESS_PATH).status_code == 200
        response = client.get(
            RUNTIME_V3_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert response.status_code == 503


def test_runtime_v3_contract_hashes_are_derived_from_frozen_bytes() -> None:
    hashes = load_runtime_v3_contract_hashes(_ROOT)

    assert (
        hashes.retrieval_schema_sha256
        == hashlib.sha256((_ROOT / V3_RETRIEVAL_SCHEMA_PATH).read_bytes()).hexdigest()
    )
    assert (
        hashes.readiness_schema_sha256
        == hashlib.sha256((_ROOT / V3_READINESS_SCHEMA_PATH).read_bytes()).hexdigest()
    )
    assert (
        hashes.requirement_policy_schema_sha256
        == hashlib.sha256((_ROOT / REQUIREMENT_POLICY_SCHEMA_PATH).read_bytes()).hexdigest()
    )


def test_runtime_v3_rejects_policy_for_another_knowledge_generation() -> None:
    original = _approved_policy()
    policy = VerifiedRequirementPolicyRelease(
        release=original.release.model_copy(update={"knowledge_snapshot_id": "another-snapshot"}),
        reviewer_trust_store_sha256=original.reviewer_trust_store_sha256,
    )

    try:
        _loader(policy).acquire()
    except ValueError as exc:
        assert "does not match knowledge generation" in str(exc)
    else:
        raise AssertionError("cross-generation requirement policy must be rejected")
