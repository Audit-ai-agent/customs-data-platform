from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence import RequirementLookup
from src.evidence_v3 import UNAPPROVED_HUMAN_ONLY_POLICY_SHA256
from src.runtime_api_v3 import RUNTIME_V3_READINESS_PATH, RUNTIME_V3_RETRIEVE_PATH
from tools import run_three_law_v2_diagnostic as v2
from tools import run_three_law_v3_diagnostic as runner
from tools import run_three_law_v3_restricted_eval as restricted_eval
from tools.run_evidence_v2_conformance_server import VerifiedProviderRuntimeLock

_TOKEN = "preserved-three-law-v3-token-" + ("A" * 40)
_REVISION = "a" * 40
_SNAPSHOT = "ccct-preserved-three-law-v3"
_AS_OF = date(2026, 7, 10)


def _provider_lock() -> VerifiedProviderRuntimeLock:
    return VerifiedProviderRuntimeLock(
        contract="evidence-runtime-release-provider-lock",
        schema_version="1.0",
        provider_release="evidence-api-v1.2.0",
        provider_revision=_REVISION,
        provider_build_sha256="1" * 64,
        retrieval_schema_sha256="2" * 64,
        readiness_schema_sha256="3" * 64,
        provider_source_manifest_sha256="4" * 64,
    )


def _digest(label: str, value: object) -> str:
    return hashlib.sha256(f"{label}:{value}".encode()).hexdigest()


def _observation(
    *,
    article_count: int = runner.EXPECTED_DIAGNOSTIC_ARTICLE_COUNT,
    version: str = "v1",
) -> v2.ThreeLawObservation:
    qdrant_ids = _digest("qdrant", article_count)
    graph_ids = _digest("graph", article_count)
    return v2.ThreeLawObservation(
        law_ids=tuple(sorted(v2.DIAGNOSTIC_LAW_IDS)),
        total_law_count=3,
        article_count=article_count,
        qdrant_point_count=article_count,
        neo4j_article_count=article_count,
        knowledge_snapshot_id=_SNAPSHOT,
        supported_review_as_of=_AS_OF,
        law_article_manifest_sha256=_digest("articles", version),
        postgres_qdrant_ids_sha256=qdrant_ids,
        qdrant_point_ids_sha256=qdrant_ids,
        postgres_graph_ids_sha256=graph_ids,
        neo4j_article_ids_sha256=graph_ids,
        postgres_retrieval_state_sha256=_digest("postgres", version),
        qdrant_retrieval_state_sha256=_digest("qdrant-state", version),
        neo4j_retrieval_state_sha256=_digest("neo4j-state", version),
    )


def _context(tmp_path: Path) -> Path:
    context = tmp_path / "provider-context"
    for name in (
        "rag_retrieval_v3.schema.json",
        "readiness_v3.schema.json",
        "requirement_policy_v1.schema.json",
    ):
        destination = context / "contracts" / "evidence" / "v3" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((runner.ROOT / "contracts" / "evidence" / "v3" / name).read_bytes())
    return context


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "evidence-token"
    path.write_text(_TOKEN + "\n", encoding="ascii")
    return path


class _ReadyBackend(TestOnlyConformanceEvidenceBackend):
    def __init__(self, observation: v2.ThreeLawObservation) -> None:
        super().__init__(effective_date=observation.supported_review_as_of)
        self.observation = observation

    def validate_ready(self) -> None:
        return None

    def current_snapshot_id(self) -> str:
        self.retrieve_calls += 1
        return self.observation.knowledge_snapshot_id

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        assert snapshot_id == self.observation.knowledge_snapshot_id
        return self.observation.supported_review_as_of

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ) -> RequirementLookup:
        assert snapshot_id == self.observation.knowledge_snapshot_id
        return super().lookup_requirements(
            "test-only-conformance-snapshot",
            hsk10,
            trade_type,
            as_of,
        )


def _body() -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "request_id": "req-preserved-three-law-v3",
        "case_id": "case-preserved-three-law-v3",
        "review_run_id": "run-preserved-three-law-v3",
        "review_as_of": _AS_OF.isoformat(),
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


def _application(
    tmp_path: Path,
    *,
    observation_loader,
):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    return runner.create_three_law_v3_diagnostic_application(
        diagnostic_only_ack=True,
        provider_context=_context(tmp_path),
        service_token_file=_token_file(tmp_path),
        identity_output=outputs / "identity.json",
        diagnostic_manifest_output=outputs / "manifest.json",
        host="127.0.0.1",
        port=18013,
        observation_loader=observation_loader,
        provider_lock_loader=lambda _context: _provider_lock(),
        database_backend_factory=lambda observation, _deadline: _ReadyBackend(observation),
        executing_repository_attestor=lambda _deadline=None: None,
        install_readonly_boundary=False,
    )


def test_parser_requires_acknowledgement_and_literal_loopback(tmp_path: Path) -> None:
    common = [
        "--provider-context",
        str(tmp_path / "context"),
        "--service-token-file",
        str(tmp_path / "token"),
        "--identity-output",
        str(tmp_path / "identity.json"),
        "--diagnostic-manifest-output",
        str(tmp_path / "manifest.json"),
        "--host",
        "127.0.0.1",
        "--port",
        "18013",
    ]
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(common)
    for forbidden_host in ("0.0.0.0", "localhost"):
        with pytest.raises(SystemExit):
            runner.build_parser().parse_args(
                [
                    "--diagnostic-only-three-law",
                    *common[:-4],
                    "--host",
                    forbidden_host,
                    "--port",
                    "18013",
                ]
            )
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            [
                "--diagnostic-only-three-law",
                *common,
                "--expected-loaded-law-count",
                "58",
            ]
        )


def test_v3_factory_rejects_localhost_alias_before_accessing_dependencies(
    tmp_path: Path,
) -> None:
    with pytest.raises(runner.ThreeLawV3DiagnosticError, match="loopback-only"):
        runner.create_three_law_v3_diagnostic_application(
            diagnostic_only_ack=True,
            provider_context=tmp_path / "unread-provider-context",
            service_token_file=tmp_path / "unread-token",
            identity_output=tmp_path / "unwritten-identity",
            diagnostic_manifest_output=tmp_path / "unwritten-manifest",
            host="localhost",
            port=18013,
        )


def test_v3_repository_attestation_binds_new_runner_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_calls: list[VerifiedProviderRuntimeLock] = []
    monkeypatch.setattr(
        v2,
        "attest_executing_repository",
        lambda lock, **_kwargs: base_calls.append(lock),
    )
    monkeypatch.setattr(
        v2,
        "_git",
        lambda _repository, *_arguments, **_kwargs: Path(runner.__file__).read_bytes(),
    )
    runner.attest_executing_v3_repository(_provider_lock())
    assert base_calls == [_provider_lock()]

    monkeypatch.setattr(
        v2,
        "_git",
        lambda _repository, *_arguments, **_kwargs: b"different committed bytes",
    )
    with pytest.raises(runner.ThreeLawV3DiagnosticError, match="do not match"):
        runner.attest_executing_v3_repository(_provider_lock())


def test_exact_three_law_1007_preflight_is_not_a_configurable_claim(tmp_path: Path) -> None:
    with pytest.raises(runner.ThreeLawV3DiagnosticError, match="exactly 1,007"):
        _application(
            tmp_path,
            observation_loader=lambda _deadline=None: _observation(article_count=1_008),
        )


def test_v3_identity_manifest_and_diagnostic_readiness_are_bound(
    tmp_path: Path,
) -> None:
    observation = _observation()
    application = _application(
        tmp_path,
        observation_loader=lambda _deadline=None: observation,
    )
    lease = application.state.runtime_v3_release_loader.acquire()
    identity = lease.identity
    outputs = tmp_path / "outputs"
    identity_file = json.loads((outputs / "identity.json").read_text(encoding="utf-8"))
    manifest = json.loads((outputs / "manifest.json").read_text(encoding="utf-8"))

    assert len(identity.binding_headers()) == 17
    assert identity_file == identity.model_dump(mode="json")
    assert manifest["schema_version"] == "three-law-evidence-v3-diagnostic/v1"
    assert manifest["diagnostic_profile"] == runner.DIAGNOSTIC_PROFILE
    assert manifest["scope"] == "DIAGNOSTIC_ONLY_3_OF_58"
    assert manifest["law_ids"] == sorted(v2.DIAGNOSTIC_LAW_IDS)
    assert manifest["expected_production_law_count"] == 58
    assert manifest["observed_total_law_count"] == 3
    assert manifest["observed_article_count"] == 1_007
    assert manifest["qdrant_point_count"] == 1_007
    assert manifest["neo4j_article_count"] == 1_007
    assert manifest["database_access"] == "read_only"
    assert manifest["release_ready"] is False
    assert manifest["rag_quality_evaluated"] is False
    assert manifest["domain_reviewed"] is False
    assert manifest["automatic_completion_eligible"] is False
    assert manifest["evidence_runtime_release_v3"] == identity_file

    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51003),
    ) as client:
        response = client.get(
            runner.DIAGNOSTIC_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["diagnostic_profile"] == "preserved-3law-v3-diagnostic"
    assert payload["data_source"] == "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY"
    assert payload["scope"] == "DIAGNOSTIC_ONLY_3_OF_58"
    assert payload["expected_production_law_count"] == 58
    assert payload["observed_law_count"] == 3
    assert payload["observed_article_count"] == 1_007
    assert payload["rag_quality_evaluated"] is False
    assert payload["automatic_completion_eligible"] is False
    assert payload["evidence_runtime_release"] == identity_file


def test_v3_retrieval_uses_exact_identity_and_real_codes_fail_human_only(
    tmp_path: Path,
) -> None:
    application = _application(
        tmp_path,
        observation_loader=lambda _deadline=None: _observation(),
    )
    identity = application.state.runtime_v3_release_loader.acquire().identity
    body = _body()
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        "X-Request-ID": body["request_id"],
        **identity.binding_headers(),
    }

    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51004),
    ) as client:
        ready = client.get(
            RUNTIME_V3_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        retrieved = client.post(RUNTIME_V3_RETRIEVE_PATH, json=body, headers=headers)
        mismatch_headers = dict(headers)
        mismatch_headers["X-Evidence-Runtime-V3-SHA256"] = "0" * 64
        mismatched = client.post(
            RUNTIME_V3_RETRIEVE_PATH,
            json=body,
            headers=mismatch_headers,
        )

    assert ready.status_code == 200
    assert ready.json()["evidence_runtime_release"] == identity.model_dump(mode="json")
    assert retrieved.status_code == 200
    payload = retrieved.json()
    assert payload["status"] == "partial"
    assert payload["knowledge_snapshot_id"] == _SNAPSHOT
    assert payload["results"][0]["coverage"] == "partial"
    requirement = next(
        row
        for row in payload["results"][0]["evidence"]
        if row["required_document_code"] == "CCCT_DOC_IMPORT_CONFIRMATION"
    )
    assert requirement["eligibility"] == "finding"
    assert requirement["requirement_verification"] == {
        "required_document_code": "CCCT_DOC_IMPORT_CONFIRMATION",
        "verification_policy": "human_only",
        "policy_source_sha256": UNAPPROVED_HUMAN_ONLY_POLICY_SHA256,
        "policy_release": "unapproved-human-only/v1",
        "approved": False,
    }
    assert mismatched.status_code == 412


def test_store_drift_fails_closed_before_v3_readiness(tmp_path: Path) -> None:
    initial = _observation()
    changed = replace(
        initial,
        law_article_manifest_sha256=_digest("articles", "changed"),
    )
    observations = iter((initial, changed))
    application = _application(
        tmp_path,
        observation_loader=lambda _deadline=None: next(observations),
    )

    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51005),
    ) as client:
        response = client.get(
            RUNTIME_V3_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert application.state.release_ready is False


def test_identity_and_manifest_outputs_cannot_enter_source_or_context(tmp_path: Path) -> None:
    context = _context(tmp_path)
    with pytest.raises(runner.ThreeLawV3DiagnosticError, match="outside"):
        runner._validate_new_output_path(
            runner.ROOT / "forbidden-identity.json",
            context=context,
            label="identity",
        )
    context_output = context / "forbidden-manifest.json"
    with pytest.raises(runner.ThreeLawV3DiagnosticError, match="outside"):
        runner._validate_new_output_path(
            context_output,
            context=context,
            label="manifest",
        )


def _retrieve_v3_fixture(tmp_path: Path):
    application = _application(
        tmp_path,
        observation_loader=lambda _deadline=None: _observation(),
    )
    identity = application.state.runtime_v3_release_loader.acquire().identity
    body = _body()
    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51006),
    ) as client:
        response = client.post(
            RUNTIME_V3_RETRIEVE_PATH,
            json=body,
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
                "X-Request-ID": body["request_id"],
                **identity.binding_headers(),
            },
        )
    assert response.status_code == 200
    return restricted_eval.RetrieveResponseV3.model_validate(response.json())


def test_restricted_eval_projection_removes_only_v3_policy_overlay(tmp_path: Path) -> None:
    original = _retrieve_v3_fixture(tmp_path)
    projected = restricted_eval.project_v3_to_hybrid_v1(original)

    assert original.status == "partial"
    assert original.results[0].coverage == "partial"
    assert any(error.code == "REQUIREMENT_POLICY_UNAVAILABLE" for error in original.results[0].errors)
    assert projected["schema_version"] == "1.0"
    assert projected["status"] == "ok"
    assert projected["results"][0]["coverage"] == "complete"
    assert projected["results"][0]["errors"] == []
    assert all(
        "requirement_verification" not in evidence
        for evidence in projected["results"][0]["evidence"]
    )
    assert [
        evidence["evidence_id"] for evidence in projected["results"][0]["evidence"]
    ] == [evidence.evidence_id for evidence in original.results[0].evidence]


def test_restricted_structured_check_requires_real_exact_and_human_only(
    tmp_path: Path,
) -> None:
    original = _retrieve_v3_fixture(tmp_path)
    report = restricted_eval._structured_report(original)
    assert report["passed"] is True
    assert report["requirement_count"] == 1
    assert report["required_document_codes"] == ["CCCT_DOC_IMPORT_CONFIRMATION"]

    permissive = original.model_copy(
        update={
            "results": [
                original.results[0].model_copy(
                    update={
                        "evidence": [
                            evidence.model_copy(
                                update={
                                    "requirement_verification": (
                                        evidence.requirement_verification.model_copy(
                                            update={
                                                "verification_policy": "presence_only",
                                                "approved": True,
                                            }
                                        )
                                        if evidence.requirement_verification is not None
                                        else None
                                    )
                                }
                            )
                            for evidence in original.results[0].evidence
                        ]
                    }
                )
            ]
        }
    )
    assert restricted_eval._structured_report(permissive)["passed"] is False


def test_restricted_eval_refuses_non_literal_loopback() -> None:
    for value in (
        "http://localhost:18013",
        "http://0.0.0.0:18013",
        "https://127.0.0.1:18013",
        "http://example.test:18013",
    ):
        with pytest.raises(restricted_eval.RestrictedEvaluationError, match="loopback"):
            restricted_eval._literal_loopback_origin(value)


def test_restricted_hybrid_gold_scope_does_not_claim_all_three_laws() -> None:
    metadata = restricted_eval._gold_scope_metadata(
        restricted_eval.load_hybrid_gold()
    )
    assert metadata == {
        "loaded_law_ids": ["001556", "002421", "006392"],
        "hybrid_gold_relevant_law_ids": ["001556", "002421"],
        "hybrid_gold_law_coverage": "2/3",
        "all_three_laws_hybrid_gold_covered": False,
    }
