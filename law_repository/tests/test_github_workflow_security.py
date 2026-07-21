from __future__ import annotations

import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
IMMUTABLE_ACTION = re.compile(r"^[^#]+@[0-9a-f]{40}(?:\s+#.*)?$")
IMMUTABLE_CONTAINER_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")


def test_every_external_action_is_pinned_to_an_immutable_commit() -> None:
    floating: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped.startswith("uses:") and not stripped.startswith("- uses:"):
                continue
            reference = stripped.split("uses:", 1)[1].strip()
            if reference.startswith("./"):
                continue
            if not IMMUTABLE_ACTION.fullmatch(reference):
                floating.append(f"{workflow.name}:{line_number}: {reference}")

    assert floating == []


def test_dispatch_input_is_validated_before_safe_shell_use() -> None:
    workflow = (WORKFLOWS / "live-integration.yml").read_text(encoding="utf-8")

    # The untrusted expression may only populate an environment variable. It
    # must never be interpolated into a run block on a persistent runner.
    assert workflow.count("${{ inputs.delta_since }}") == 1
    assert "DELTA_SINCE: ${{ inputs.delta_since }}" in workflow
    assert '--since "${{ inputs.delta_since }}"' not in workflow

    assert '[[ "$DELTA_SINCE" =~ ^[0-9]{8}$ ]]' in workflow
    assert '--since "$DELTA_SINCE"' in workflow


def test_live_workflow_is_exact_ref_secret_scoped_and_storage_isolated() -> None:
    workflow = (WORKFLOWS / "live-integration.yml").read_text(encoding="utf-8")
    compose = (REPOSITORY_ROOT / "law_repository" / "docker-compose.live-ci.yml").read_text(
        encoding="utf-8"
    )

    assert "environment: data-platform-live-integration" in workflow
    assert "[self-hosted, customs-data-live-ephemeral]" in workflow
    assert '[[ "$RUNNER_OS" == "Linux" ]]' in workflow
    assert '[[ "$RUNNER_ARCH" == "X64" ]]' in workflow
    assert '[[ "$SOURCE_REF" == "refs/heads/main" ]]' in workflow
    assert '[[ "$SOURCE_WORKFLOW_SHA" == "$SOURCE_SHA" ]]' in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "LAW_API_OC: ${{ secrets.LAW_API_OC }}" in workflow
    assert workflow.count("${{ secrets.LAW_API_OC }}") == 1
    assert workflow.count("${{ secrets.DATA_GO_KR_SERVICE_KEY }}") == 1
    assert "docker-compose.live-ci.yml" in workflow
    assert '--project-name "$COMPOSE_PROJECT_NAME"' in workflow
    assert "down -v" not in workflow
    assert "down --volumes" not in workflow

    assert "container_name:" not in compose
    assert "volumes:" not in compose
    assert '"127.0.0.1::5432"' in compose
    assert "tmpfs:" in compose
    assert compose.count("healthcheck:") == 3
    assert "/dev/tcp/127.0.0.1/6333" in compose
    assert "/var/lib/neo4j/bin/cypher-shell" in compose

    image_references = [
        line.split("image:", 1)[1].strip()
        for line in compose.splitlines()
        if line.strip().startswith("image:")
    ]
    assert len(image_references) == 3
    assert all(IMMUTABLE_CONTAINER_IMAGE.fullmatch(image) for image in image_references)
    assert image_references == [
        "pgvector/pgvector:pg16@sha256:"
        "1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb",
        "qdrant/qdrant:v1.15.4@sha256:"
        "6ac4807063bbecddca0250bfbcff52acf18c22263b904d12919349e6d0a408f1",
        "neo4j:5@sha256:4bae36aff76271e27fd6a6ed0835413f86a284cd179cfb1cb7d188f5f7533aca",
    ]


def test_legacy_mutable_delta_workflow_is_fail_closed() -> None:
    workflow = (WORKFLOWS / "delta-sync.yml").read_text(encoding="utf-8")

    assert "legacy-delta-sync-disabled" in workflow
    assert "exit 1" in workflow
    assert "src.sync.delta" not in workflow
    assert "pip install" not in workflow
    assert "secrets." not in workflow


def test_v3_release_workflow_is_read_only_and_requires_external_evidence() -> None:
    workflow = (WORKFLOWS / "evidence-v3-release.yml").read_text(encoding="utf-8")

    # Dispatch values are introduced exactly once through env and then quoted
    # after strict validation. They are never interpolated into a run block.
    assert workflow.count("${{ inputs.release_tag }}") == 1
    assert workflow.count("${{ inputs.evidence_run_id }}") == 1
    assert "RELEASE_TAG: ${{ inputs.release_tag }}" in workflow
    assert "EVIDENCE_RUN_ID: ${{ inputs.evidence_run_id }}" in workflow
    assert '[[ "$EVIDENCE_RUN_ID" =~ ^[1-9][0-9]{0,19}$ ]]' in workflow
    assert "environment: evidence-v3-production-release" in workflow
    assert '[[ "$VERIFIER_REF" == "$expected_ref" ]]' in workflow
    assert '[[ "$VERIFIER_WORKFLOW_SHA" == "$VERIFIER_SHA" ]]' in workflow
    assert "evidence-v3-release-bundle.yml" in workflow
    assert 'run["path"] != producer_path' in workflow
    assert 'run["head_sha"] != source_commit' in workflow
    assert 'run["head_branch"] != release_tag' in workflow
    assert 'git cat-file -t "refs/tags/$RELEASE_TAG"' in workflow

    # Current repository state contains no external approved bundle, so the
    # workflow must download it from a completed successful run and fail when
    # the named artifact/manifest is absent.
    assert '"repos/$GH_REPO/actions/runs/$EVIDENCE_RUN_ID/artifacts?per_page=100"' in workflow
    assert '"repos/$GH_REPO/actions/artifacts/$EVIDENCE_ARTIFACT_ID/zip"' in workflow
    assert '[[ "$observed" == "$EVIDENCE_ARTIFACT_DIGEST" ]]' in workflow
    assert "zipfile.ZipFile" in workflow
    assert "stat.S_ISLNK" in workflow
    assert "verify_evidence_v3_release.py" in workflow
    assert '--workflow-event "$EVIDENCE_RUN_EVENT"' in workflow
    assert '--workflow-conclusion "$EVIDENCE_RUN_CONCLUSION"' in workflow
    assert '--artifact-id "$EVIDENCE_ARTIFACT_ID"' in workflow
    assert '--artifact-digest "$EVIDENCE_ARTIFACT_DIGEST"' in workflow
    assert "--candidate-structural-only" not in workflow

    forbidden_mutations = (
        "git push",
        "git tag ",
        "docker push",
        "kubectl ",
        "generation promote",
        "generation rollback",
        "psql ",
    )
    assert all(command not in workflow for command in forbidden_mutations)


def test_v3_bundle_producer_is_protected_and_unconditionally_blocked() -> None:
    workflow = (WORKFLOWS / "evidence-v3-release-bundle.yml").read_text(encoding="utf-8")

    assert "environment: evidence-v3-production-bundle" in workflow
    assert '[[ "$PRODUCER_REF" == "$expected_ref" ]]' in workflow
    assert '[[ "$PRODUCER_WORKFLOW_SHA" == "$PRODUCER_SHA" ]]' in workflow
    assert "NOT_IMPLEMENTED_EXTERNAL_ATTESTATION" in workflow
    assert "exit 1" in workflow
    assert "upload-artifact" not in workflow
    assert "secrets." not in workflow
