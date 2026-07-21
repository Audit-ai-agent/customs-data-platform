from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Callable

import pytest

from src.generation import (
    GenerationSpec,
    StoreAttestation,
    build_seal_attestation,
    canonical_json_bytes,
)
from src.generation_runtime import ActiveGenerationAttestation
from src.production_context_inventory import FIXED_PRODUCTION_CONTEXT_FILES
from src.release_provenance_v3 import (
    ExpectedWorkflowContext,
    ReleaseProvenanceError,
    release_manifest_sha256,
    verify_release_bundle,
)
from src.runtime_release import EvidenceRuntimeReleaseIdentity, runtime_release_sha256
from src.runtime_release_v3 import EvidenceRuntimeReleaseIdentityV3
from tools.provider_build import (
    create_provider_build_document,
    create_runtime_provider_build_document,
    create_runtime_provider_lock_document,
    serialized_document,
)
from tools.test_only_v3_policy import build_test_only_policy_fixture

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_COMMIT = "a" * 40
TAG_OBJECT = "b" * 40
WORKFLOW_PATH = ".github/workflows/evidence-v3-release-bundle.yml"
WORKFLOW_RUN_ID = 123456
TODAY = date(2026, 7, 20)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": path.as_posix(), "sha256": _sha(raw)}


def _relative_ref(root: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha(path.read_bytes())}


def _json_file(root: Path, relative: str, value: Any) -> dict[str, str]:
    path = root / relative
    _write(path, canonical_json_bytes(value))
    return _relative_ref(root, path)


def _rehash_manifest(bundle: Path, payload: dict[str, Any]) -> None:
    payload["manifest_sha256"] = release_manifest_sha256(
        {key: value for key, value in payload.items() if key != "manifest_sha256"}
    )
    (bundle / "release-provenance.json").write_bytes(canonical_json_bytes(payload))


def _store_attestations(spec: GenerationSpec) -> tuple[StoreAttestation, ...]:
    records = [
        (index, hashlib.sha256(f"article:{index}".encode()).hexdigest()) for index in range(1, 59)
    ]
    return tuple(
        StoreAttestation.from_article_records(
            generation_id=spec.generation_id,
            store_name=name,
            records=records,
            model_manifest_sha256=spec.model_manifest_sha256,
            store_locator=f"release-{name}",
            store_revision="release-1",
            attested_at="2026-07-20T01:00:00Z",
        )
        for name in ("postgresql", "qdrant", "neo4j")
    )


@pytest.fixture()
def complete_release_bundle(
    tmp_path: Path,
    generation_spec: GenerationSpec,
    generation_evaluation_bundle_factory: Callable[[str], Any],
) -> tuple[Path, Path, ExpectedWorkflowContext]:
    bundle = tmp_path / "release-bundle"
    repository = tmp_path / "tagged-repository"
    bundle.mkdir()
    repository.mkdir()

    workflow_bytes = (PROJECT_ROOT / WORKFLOW_PATH).read_bytes()
    _write(repository / WORKFLOW_PATH, workflow_bytes)
    for relative in (
        "contracts/evidence/v3/rag_retrieval_v3.schema.json",
        "contracts/evidence/v3/readiness_v3.schema.json",
        "contracts/evidence/v3/requirement_policy_v1.schema.json",
        "contracts/evidence/v3/release_provenance_v1.schema.json",
    ):
        _write(repository / relative, (PROJECT_ROOT / relative).read_bytes())

    evaluation_source = generation_evaluation_bundle_factory(generation_spec.generation_id)
    evaluation_root = bundle / "evaluation"
    shutil.copytree(tmp_path / generation_spec.generation_id, evaluation_root)
    stores = _store_attestations(generation_spec)
    ordered_stores = tuple(sorted(stores, key=lambda item: item.store_name))
    seal, seal_sha = build_seal_attestation(
        generation_spec,
        stores,
        evaluation_source.report_sha256,
    )
    active = ActiveGenerationAttestation(
        generation_id=generation_spec.generation_id,
        epoch=7,
        changed_at="2026-07-20T01:30:00Z",
        source_manifest_sha256=generation_spec.source_manifest_sha256,
        model_manifest_sha256=generation_spec.model_manifest_sha256,
        index_config_sha256=generation_spec.index_config_sha256,
        evaluation_report_sha256=evaluation_source.report_sha256,
        seal_attestation_sha256=seal_sha,
        stores=ordered_stores,
    )

    source_ref = _json_file(
        bundle,
        "knowledge/source-manifest.json",
        generation_spec.source_manifest.as_dict(),
    )
    model_ref = _json_file(bundle, "knowledge/model-manifest.json", generation_spec.model_manifest)
    index_ref = _json_file(bundle, "knowledge/index-config.json", generation_spec.index_config)
    seal_ref = _json_file(bundle, "knowledge/seal.json", seal)
    store_proofs = []
    for item in stores:
        ref = _json_file(bundle, f"knowledge/stores/{item.store_name}.json", item.as_dict())
        store_proofs.append(
            {
                "store_name": item.store_name,
                "attestation": ref,
                "attestation_sha256": item.sha256,
                "sealed_projection_sha256": item.sealed_projection_sha256,
            }
        )

    policy_fixture = build_test_only_policy_fixture(
        knowledge_source_manifest_sha256=generation_spec.source_manifest_sha256,
        knowledge_snapshot_id=generation_spec.generation_id,
        today=TODAY,
    )
    coverage_ref = _write(
        bundle / "policy/coverage-matrix.json",
        b"test-only-coverage-matrix/NON-PRODUCTION",
    )
    coverage_ref = _relative_ref(bundle, bundle / "policy/coverage-matrix.json")
    policy_ref = _json_file(
        bundle,
        "policy/requirement-policy.json",
        policy_fixture.verified.release.model_dump(mode="json"),
    )
    reviewers_ref = _json_file(
        bundle,
        "policy/trusted-reviewers.json",
        policy_fixture.trust_store.model_dump(mode="json"),
    )
    lock_raw = canonical_json_bytes(policy_fixture.trust_store_lock.model_dump(mode="json"))
    _write(repository / "contracts/evidence/v3/trusted-reviewers.lock.json", lock_raw)

    for source_path in FIXED_PRODUCTION_CONTEXT_FILES:
        raw = (
            lock_raw
            if source_path == "contracts/evidence/v3/trusted-reviewers.lock.json"
            else (PROJECT_ROOT / source_path).read_bytes()
        )
        _write(repository / source_path, raw)
    runtime_source = "law_repository/src/candidate_runtime.py"
    _write(repository / runtime_source, b"CANDIDATE_ONLY = True\n")

    provider_root = bundle / "provider"
    schema_relatives = (
        "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        "contracts/evidence/v3/rag_retrieval_v3.schema.json",
        "contracts/evidence/v3/readiness_v3.schema.json",
        "contracts/evidence/v3/requirement_policy_v1.schema.json",
        "contracts/evidence/v3/release_provenance_v1.schema.json",
    )
    context_sources = dict(FIXED_PRODUCTION_CONTEXT_FILES)
    context_sources[runtime_source] = runtime_source
    entries = []
    for source_path, destination_path in sorted(
        context_sources.items(),
        key=lambda item: item[1],
    ):
        raw = (repository / source_path).read_bytes()
        _write(provider_root / destination_path, raw)
        entries.append({"path": destination_path, "sha256": _sha(raw), "bytes": len(raw)})
    context_manifest = {
        "schema_version": "1.0",
        "source_repository": "customs-data-platform",
        "source_commit": SOURCE_COMMIT,
        "files": entries,
    }
    context_raw = serialized_document(context_manifest)
    _write(provider_root / "source-manifest.json", context_raw)
    v1_schema = (provider_root / schema_relatives[0]).read_bytes()
    provider_document = create_provider_build_document(
        provider_revision=SOURCE_COMMIT,
        retrieval_schema=v1_schema,
        source_manifest=context_raw,
    )
    _write(provider_root / "provider-build.json", serialized_document(provider_document))
    provider_build = create_runtime_provider_build_document(
        provider_revision=SOURCE_COMMIT,
        retrieval_schema=v1_schema,
        source_manifest=context_raw,
    )
    _write(provider_root / "provider-runtime-build.json", serialized_document(provider_build))
    runtime_lock = create_runtime_provider_lock_document(
        runtime_provider_build=provider_build,
        retrieval_schema=v1_schema,
        readiness_schema=(
            provider_root / "contracts/evidence/v2/readiness_v2.schema.json"
        ).read_bytes(),
        source_manifest=context_raw,
    )
    _write(provider_root / "provider-runtime-lock.json", serialized_document(runtime_lock))

    base_payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": SOURCE_COMMIT,
        "provider_build_sha256": provider_build["provider_build_sha256"],
        "retrieval_schema_sha256": _sha(v1_schema),
        "provider_source_manifest_sha256": _sha(context_raw),
        "knowledge_generation_id": generation_spec.generation_id,
        "knowledge_generation_attestation_sha256": active.sha256,
        "knowledge_source_manifest_sha256": generation_spec.source_manifest_sha256,
        "model_manifest_sha256": generation_spec.model_manifest_sha256,
        "index_config_sha256": generation_spec.index_config_sha256,
        "evaluation_report_sha256": evaluation_source.report_sha256,
        "seal_attestation_sha256": seal_sha,
        "knowledge_snapshot_id": generation_spec.generation_id,
        "supported_review_as_of": "2026-07-20",
    }
    base_payload["runtime_release_sha256"] = runtime_release_sha256(base_payload)
    base_identity = EvidenceRuntimeReleaseIdentity.model_validate(base_payload)
    v3_payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "3.0",
        "retrieval_schema_version": "3.0",
        "base_runtime_release": base_identity.model_dump(mode="json"),
        "retrieval_schema_sha256": _sha((provider_root / schema_relatives[1]).read_bytes()),
        "readiness_schema_sha256": _sha((provider_root / schema_relatives[2]).read_bytes()),
        "requirement_policy_schema_sha256": _sha(
            (provider_root / schema_relatives[3]).read_bytes()
        ),
        "requirement_policy_release": policy_fixture.verified.release.policy_release,
        "requirement_policy_manifest_sha256": (
            policy_fixture.verified.release.policy_manifest_sha256
        ),
        "coverage_matrix_sha256": policy_fixture.verified.release.coverage_matrix_sha256,
        "requirement_policy_approval_sha256": (policy_fixture.verified.verified_approval_sha256),
        "reviewer_trust_store_sha256": (policy_fixture.verified.reviewer_trust_store_sha256),
    }
    v3_payload["runtime_release_v3_sha256"] = release_manifest_sha256(v3_payload)
    runtime_v3 = EvidenceRuntimeReleaseIdentityV3.model_validate(v3_payload)
    runtime_ref = _json_file(
        bundle,
        "runtime/evidence-runtime-v3.json",
        runtime_v3.model_dump(mode="json"),
    )

    security_refs: dict[str, dict[str, str]] = {}
    for gate in ("secret_scan", "dependency_audit", "sast"):
        security_refs[gate] = _json_file(
            bundle,
            f"security/{gate}.json",
            {
                "schema_version": "1.0",
                "gate": gate,
                "status": "PASS",
                "source_commit": SOURCE_COMMIT,
                "tool": f"test-{gate}",
                "tool_version": "1.0.0",
                "critical_findings": 0,
                "high_findings": 0,
                "generated_at": "2026-07-20T02:00:00Z",
            },
        )

    image_digest = "sha256:" + "c" * 64
    rollback_image = "sha256:" + "d" * 64
    sbom_ref = _json_file(bundle, "container/sbom.json", {"bomFormat": "CycloneDX"})
    container_ref = _json_file(
        bundle,
        "container/provenance.json",
        {
            "schema_version": "1.0",
            "status": "PASS",
            "image_digest": image_digest,
            "source_commit": SOURCE_COMMIT,
            "workflow_sha": SOURCE_COMMIT,
            "workflow_run_id": WORKFLOW_RUN_ID,
            "sbom_sha256": sbom_ref["sha256"],
            "builder_id": "github-actions",
            "generated_at": "2026-07-20T02:00:00Z",
        },
    )
    rollback_generation = "e" * 64
    rollback_ref = _json_file(
        bundle,
        "rollback/report.json",
        {
            "schema_version": "1.0",
            "status": "PASS",
            "rollback_image_digest": rollback_image,
            "rollback_generation_id": rollback_generation,
            "from_generation_id": generation_spec.generation_id,
            "restore_tested": True,
            "reconciliation_tested": True,
            "generated_at": "2026-07-20T02:00:00Z",
        },
    )
    consumer_lock_ref = _write(bundle / "agent/consumer.lock.json", b"agent-lock-v3")
    consumer_lock_ref = _relative_ref(bundle, bundle / "agent/consumer.lock.json")
    conformance_ref = _json_file(
        bundle,
        "agent/http-conformance.json",
        {
            "schema_version": "1.0",
            "status": "PASS",
            "agent_repository": "customs-ai-agent",
            "agent_revision": "f" * 40,
            "actual_http": True,
            "mock_count": 0,
            "readiness_get_count": 1,
            "retrieval_post_count": 1,
            "response_identity_match": True,
            "consumer_lock_sha256": consumer_lock_ref["sha256"],
            "runtime_identity": {
                "runtime_release_v3_sha256": runtime_v3.runtime_release_v3_sha256,
                "provider_revision": SOURCE_COMMIT,
                "provider_build_sha256": provider_build["provider_build_sha256"],
                "knowledge_generation_id": generation_spec.generation_id,
                "knowledge_generation_attestation_sha256": active.sha256,
                "requirement_policy_manifest_sha256": (
                    policy_fixture.verified.release.policy_manifest_sha256
                ),
                "reviewer_trust_store_sha256": (
                    policy_fixture.verified.reviewer_trust_store_sha256
                ),
            },
            "generated_at": "2026-07-20T02:00:00Z",
        },
    )

    manifest: dict[str, Any] = {
        "contract": "evidence-v3-release-provenance",
        "schema_version": "1.0",
        "release_state": "release_candidate",
        "created_at": "2026-07-20T03:00:00Z",
        "git": {
            "repository": "Audit-ai-agent/customs-data-platform",
            "tag_name": "evidence-api-v3.0.0",
            "annotated": True,
            "tag_object_type": "tag",
            "tag_target_type": "commit",
            "tag_object_sha": TAG_OBJECT,
            "tag_target_sha": SOURCE_COMMIT,
            "source_commit": SOURCE_COMMIT,
            "source_commit_clean": True,
        },
        "workflow": {
            "workflow_path": WORKFLOW_PATH,
            "workflow_sha": SOURCE_COMMIT,
            "workflow_file_sha256": _sha(workflow_bytes),
            "workflow_ref": (
                "Audit-ai-agent/customs-data-platform/"
                f"{WORKFLOW_PATH}@refs/tags/evidence-api-v3.0.0"
            ),
            "head_ref": "refs/tags/evidence-api-v3.0.0",
            "head_sha": SOURCE_COMMIT,
            "run_id": WORKFLOW_RUN_ID,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "conclusion": "success",
        },
        "provider": {
            "context": "production",
            "context_path": "provider",
            "context_manifest_sha256": _sha(context_raw),
            "provider_release": "evidence-api-v1.2.0",
            "provider_revision": SOURCE_COMMIT,
            "provider_build_sha256": provider_build["provider_build_sha256"],
            "provider_source_manifest_sha256": _sha(context_raw),
            "runtime_identity": runtime_ref,
            "v3_retrieval_schema_sha256": v3_payload["retrieval_schema_sha256"],
            "v3_readiness_schema_sha256": v3_payload["readiness_schema_sha256"],
            "requirement_policy_schema_sha256": v3_payload["requirement_policy_schema_sha256"],
            "release_provenance_schema_sha256": _sha(
                (provider_root / schema_relatives[4]).read_bytes()
            ),
        },
        "generation": {
            "generation_id": generation_spec.generation_id,
            "epoch": 7,
            "changed_at": "2026-07-20T01:30:00Z",
            "source_manifest": source_ref,
            "source_manifest_sha256": generation_spec.source_manifest_sha256,
            "model_manifest": model_ref,
            "model_manifest_sha256": generation_spec.model_manifest_sha256,
            "index_config": index_ref,
            "index_config_sha256": generation_spec.index_config_sha256,
            "stores": store_proofs,
            "evaluation": {
                "bundle_path": "evaluation",
                "report_sha256": evaluation_source.report_sha256,
                "approval_sha256": evaluation_source.approval_sha256,
                "gold_sha256": evaluation_source.report["artifacts"]["gold_sha256"],
                "unexpected_skips": 0,
                "mock_count": 0,
            },
            "seal_attestation": seal_ref,
            "seal_attestation_sha256": seal_sha,
            "active_attestation_sha256": active.sha256,
        },
        "policy": {
            "policy_release": policy_fixture.verified.release.policy_release,
            "policy": policy_ref,
            "coverage_matrix": coverage_ref,
            "trusted_reviewers": reviewers_ref,
            "trusted_reviewer_lock_sha256": _sha(lock_raw),
            "reviewer_trust_store_sha256": (policy_fixture.verified.reviewer_trust_store_sha256),
            "verified_approval_sha256": policy_fixture.verified.verified_approval_sha256,
        },
        "security": security_refs,
        "container": {
            "image_digest": image_digest,
            "sbom": sbom_ref,
            "build_provenance": container_ref,
        },
        "rollback": {
            "rollback_image_digest": rollback_image,
            "rollback_generation_id": rollback_generation,
            "report": rollback_ref,
        },
        "agent_conformance": {
            "consumer_lock": consumer_lock_ref,
            "report": conformance_ref,
        },
    }
    _rehash_manifest(bundle, manifest)
    expected = ExpectedWorkflowContext(
        repository="Audit-ai-agent/customs-data-platform",
        tag_name="evidence-api-v3.0.0",
        tag_object_sha=TAG_OBJECT,
        tag_target_sha=SOURCE_COMMIT,
        source_commit=SOURCE_COMMIT,
        workflow_path=WORKFLOW_PATH,
        workflow_sha=SOURCE_COMMIT,
        workflow_ref=(
            f"Audit-ai-agent/customs-data-platform/{WORKFLOW_PATH}@refs/tags/evidence-api-v3.0.0"
        ),
        head_ref="refs/tags/evidence-api-v3.0.0",
        workflow_run_id=WORKFLOW_RUN_ID,
        workflow_run_attempt=1,
        artifact_id=987654,
        artifact_digest="sha256:" + "9" * 64,
        event="workflow_dispatch",
        conclusion="success",
    )
    return bundle, repository, expected


def test_complete_synthetic_candidate_is_structurally_valid_but_not_released(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    verified = verify_release_bundle(
        bundle,
        repository_root=repository,
        expected_workflow=expected,
        require_released=False,
        today=TODAY,
    )

    assert verified.manifest.release_state == "release_candidate"
    assert verified.generation.epoch == 7
    assert len(verified.generation.stores) == 3


def test_tagged_reviewer_lock_is_read_once_during_candidate_verification(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, repository, expected = complete_release_bundle
    lock_path = repository / "contracts/evidence/v3/trusted-reviewers.lock.json"
    original = Path.read_bytes
    reads = 0

    def counted_read(path: Path) -> bytes:
        nonlocal reads
        if path == lock_path:
            reads += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    verify_release_bundle(
        bundle,
        repository_root=repository,
        expected_workflow=expected,
        require_released=False,
        today=TODAY,
    )

    assert reads == 1


def test_public_test_keys_and_self_declared_reports_can_never_be_released(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    with pytest.raises(ReleaseProvenanceError, match="NOT_IMPLEMENTED_EXTERNAL_ATTESTATION"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            today=TODAY,
        )


def test_released_manifest_is_rejected_by_candidate_only_verifier(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    payload = json.loads((bundle / "release-provenance.json").read_text(encoding="utf-8"))
    payload["release_state"] = "released"
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="candidate-only"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_manifest_missing_store_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    payload = json.loads((bundle / "release-provenance.json").read_text(encoding="utf-8"))
    payload["generation"]["stores"].pop()
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="manifest is invalid"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


@pytest.mark.parametrize(
    ("relative_path", "needle"),
    [
        ("knowledge/source-manifest.json", "generation inputs"),
        ("evaluation/report.json", "store or RAG evaluation"),
        ("policy/coverage-matrix.json", "artifact hash"),
        ("security/secret_scan.json", "secret_scan report"),
        ("container/sbom.json", "artifact hash"),
        ("rollback/report.json", "rollback report"),
        ("agent/consumer.lock.json", "artifact hash"),
    ],
)
def test_tampered_release_artifact_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
    relative_path: str,
    needle: str,
) -> None:
    bundle, repository, expected = complete_release_bundle
    with (bundle / relative_path).open("ab") as handle:
        handle.write(b"tampered")

    with pytest.raises(ReleaseProvenanceError, match=needle):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_wrong_independent_workflow_identity_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    wrong = replace(expected, workflow_run_attempt=2)

    with pytest.raises(ReleaseProvenanceError, match="independently observed"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=wrong,
            require_released=False,
            today=TODAY,
        )


def test_wrong_independent_workflow_event_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    wrong = replace(expected, event="workflow_call")

    with pytest.raises(ReleaseProvenanceError, match="independently observed"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=wrong,
            require_released=False,
            today=TODAY,
        )


def test_tagged_reviewer_lock_drift_from_provider_context_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    lock_path = repository / "contracts/evidence/v3/trusted-reviewers.lock.json"
    lock_path.write_bytes(
        canonical_json_bytes(
            {
                "contract": "evidence-domain-reviewer-trust-store-lock",
                "schema_version": "1.0",
                "release_state": "unreleased",
                "trust_store_release": "unreleased",
                "reviewer_trust_store_sha256": None,
            }
        )
    )

    with pytest.raises(ReleaseProvenanceError, match="production provider context"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_rehashed_failed_security_report_still_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    manifest_path = bundle / "release-provenance.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_path = bundle / payload["security"]["sast"]["path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["status"] = "FAIL"
    report_path.write_bytes(canonical_json_bytes(report))
    payload["security"]["sast"]["sha256"] = _sha(report_path.read_bytes())
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="sast report is invalid"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_rehashed_non_http_agent_report_still_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    manifest_path = bundle / "release-provenance.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_path = bundle / payload["agent_conformance"]["report"]["path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["actual_http"] = False
    report_path.write_bytes(canonical_json_bytes(report))
    payload["agent_conformance"]["report"]["sha256"] = _sha(report_path.read_bytes())
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="Agent HTTP conformance report is invalid"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_rehashed_invalid_domain_signature_still_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    manifest_path = bundle / "release-provenance.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    policy_path = bundle / payload["policy"]["policy"]["path"]
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    signature = policy["approval"]["reviewer_attestations"][0]["signature_hex"]
    policy["approval"]["reviewer_attestations"][0]["signature_hex"] = (
        "0" if signature[0] != "0" else "1"
    ) + signature[1:]
    policy["approval_sha256"] = release_manifest_sha256(policy["approval"])
    policy_path.write_bytes(canonical_json_bytes(policy))
    payload["policy"]["policy"]["sha256"] = _sha(policy_path.read_bytes())
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="policy/trust approval"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_rehashed_cross_store_mismatch_still_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    manifest_path = bundle / "release-provenance.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    qdrant = next(
        item for item in payload["generation"]["stores"] if item["store_name"] == "qdrant"
    )
    store_path = bundle / qdrant["attestation"]["path"]
    store = json.loads(store_path.read_text(encoding="utf-8"))
    store["article_count"] -= 1
    store_path.write_bytes(canonical_json_bytes(store))
    changed = StoreAttestation(
        **{key: value for key, value in store.items() if key != "schema_version"}
    )
    qdrant["attestation"]["sha256"] = _sha(store_path.read_bytes())
    qdrant["attestation_sha256"] = changed.sha256
    qdrant["sealed_projection_sha256"] = changed.sealed_projection_sha256
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="store or RAG evaluation"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_provider_context_extra_unattested_file_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    (bundle / "provider/law_repository/src/backdoor.py").write_text(
        "raise RuntimeError('must never ship')\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseProvenanceError, match="production provider context"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_provider_context_missing_tagged_runtime_file_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    (bundle / "provider/law_repository/src/candidate_runtime.py").unlink()

    with pytest.raises(ReleaseProvenanceError, match="production provider context"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_tagged_runtime_byte_drift_from_provider_context_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    (repository / "law_repository/src/candidate_runtime.py").write_text(
        "CANDIDATE_ONLY = False\n",
        encoding="utf-8",
    )

    with pytest.raises(ReleaseProvenanceError, match="production provider context"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_repository_with_broken_git_metadata_never_falls_back_to_mutable_filesystem(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    (repository / ".git").mkdir()

    with pytest.raises(ReleaseProvenanceError, match="production provider context"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )


def test_non_allowlisted_producer_workflow_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    manifest_path = bundle / "release-provenance.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["workflow"]["workflow_path"] = ".github/workflows/ci.yml"
    payload["workflow"]["workflow_ref"] = (
        "Audit-ai-agent/customs-data-platform/.github/workflows/ci.yml@"
        "refs/tags/evidence-api-v3.0.0"
    )
    _rehash_manifest(bundle, payload)

    with pytest.raises(ReleaseProvenanceError, match="manifest is invalid"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=replace(
                expected,
                workflow_path=".github/workflows/ci.yml",
                workflow_ref=payload["workflow"]["workflow_ref"],
            ),
            require_released=False,
            today=TODAY,
        )


def test_invalid_external_artifact_digest_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle

    with pytest.raises(ReleaseProvenanceError, match="producer identity"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=replace(expected, artifact_digest="sha256:not-a-digest"),
            require_released=False,
            today=TODAY,
        )


def test_tagged_workflow_byte_drift_fails_closed(
    complete_release_bundle: tuple[Path, Path, ExpectedWorkflowContext],
) -> None:
    bundle, repository, expected = complete_release_bundle
    with (repository / WORKFLOW_PATH).open("ab") as handle:
        handle.write(b"\n# drift\n")

    with pytest.raises(ReleaseProvenanceError, match="workflow hash"):
        verify_release_bundle(
            bundle,
            repository_root=repository,
            expected_workflow=expected,
            require_released=False,
            today=TODAY,
        )
