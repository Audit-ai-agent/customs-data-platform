"""Fail-closed provenance contract for an Evidence v3 production release.

This module does not create, tag, promote, or deploy a release. It validates
the internal structure of an immutable candidate evidence bundle. Production
admission is deliberately blocked until independent externally pinned release
attestors and signed raw execution provenance are implemented; internally
consistent JSON is not release authority.
"""

from __future__ import annotations

import hashlib
import json
import shutil

# Git is resolved to an executable and invoked with a fixed argument vector.
import subprocess  # nosec B404
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.generation import (
    GenerationSpec,
    GenerationValidationError,
    SourceManifest,
    StoreAttestation,
    build_seal_attestation,
    canonical_sha256,
    load_verified_evaluation_bundle,
    validate_store_attestations,
)
from src.generation_runtime import ActiveGenerationAttestation
from src.production_context_inventory import (
    FIXED_PRODUCTION_CONTEXT_FILES,
    GENERATED_PRODUCTION_CONTEXT_FILES,
)
from src.provider_identity import load_runtime_provider_build_identity
from src.requirement_policy_release import (
    parse_requirement_policy_release,
    parse_trusted_reviewer_store,
    parse_trusted_reviewer_store_lock,
    verify_requirement_policy_release,
)
from src.runtime_release_v3 import EvidenceRuntimeReleaseIdentityV3

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
SafeName = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:/-]+$")]
ImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

MAX_JSON_BYTES = 64 * 1024 * 1024
PRODUCER_WORKFLOW_PATH = ".github/workflows/evidence-v3-release-bundle.yml"
PRODUCER_WORKFLOW_REF_PREFIX = "refs/tags/"
PRODUCTION_RELEASE_BLOCKER = (
    "NOT_IMPLEMENTED_EXTERNAL_ATTESTATION: Production release admission requires "
    "a protected producer attestation, externally pinned release-attestor trust "
    "root, two independent Ed25519 release signatures, and signed raw execution "
    "evidence. Candidate structural validation cannot grant released status."
)
RELEASE_SCHEMA_RELATIVE_PATH = Path("contracts/evidence/v3/release_provenance_v1.schema.json")
V3_RETRIEVAL_SCHEMA_RELATIVE_PATH = Path("contracts/evidence/v3/rag_retrieval_v3.schema.json")
V3_READINESS_SCHEMA_RELATIVE_PATH = Path("contracts/evidence/v3/readiness_v3.schema.json")
POLICY_SCHEMA_RELATIVE_PATH = Path("contracts/evidence/v3/requirement_policy_v1.schema.json")
TRUST_LOCK_RELATIVE_PATH = Path("contracts/evidence/v3/trusted-reviewers.lock.json")


class ReleaseProvenanceError(RuntimeError):
    """The supplied bytes do not prove one releasable Evidence v3 build."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(_StrictModel):
    path: str = Field(min_length=1, max_length=300)
    sha256: Sha256

    @model_validator(mode="after")
    def safe_relative_path(self) -> ArtifactRef:
        candidate = PurePosixPath(self.path)
        if (
            candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in self.path
            or "\x00" in self.path
        ):
            raise ValueError("artifact path must be a safe POSIX relative path")
        return self


class GitTagProvenance(_StrictModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)
    tag_name: str = Field(pattern=r"^evidence-api-v3\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?$")
    annotated: Literal[True]
    tag_object_type: Literal["tag"]
    tag_target_type: Literal["commit"]
    tag_object_sha: GitSha
    tag_target_sha: GitSha
    source_commit: GitSha
    source_commit_clean: Literal[True]

    @model_validator(mode="after")
    def annotated_tag_targets_exact_source(self) -> GitTagProvenance:
        if self.tag_object_sha == self.tag_target_sha:
            raise ValueError("annotated tag object must differ from its commit target")
        if self.tag_target_sha != self.source_commit:
            raise ValueError("annotated tag target must equal the clean source commit")
        return self


class WorkflowProvenance(_StrictModel):
    workflow_path: str = Field(
        pattern=r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$",
        max_length=200,
    )
    workflow_sha: GitSha
    workflow_file_sha256: Sha256
    workflow_ref: str = Field(
        pattern=(
            r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/"
            r"evidence-v3-release-bundle\.yml@refs/tags/"
            r"evidence-api-v3\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?$"
        ),
        max_length=400,
    )
    head_ref: str = Field(
        pattern=r"^refs/tags/evidence-api-v3\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?$",
        max_length=200,
    )
    head_sha: GitSha
    run_id: int = Field(gt=0)
    run_attempt: int = Field(gt=0)
    event: Literal["workflow_dispatch"]
    conclusion: Literal["success"]


class ProviderReleaseProof(_StrictModel):
    context: Literal["production"]
    context_path: str = Field(min_length=1, max_length=200)
    context_manifest_sha256: Sha256
    provider_release: Literal["evidence-api-v1.2.0"]
    provider_revision: GitSha
    provider_build_sha256: Sha256
    provider_source_manifest_sha256: Sha256
    runtime_identity: ArtifactRef
    v3_retrieval_schema_sha256: Sha256
    v3_readiness_schema_sha256: Sha256
    requirement_policy_schema_sha256: Sha256
    release_provenance_schema_sha256: Sha256

    @model_validator(mode="after")
    def context_path_is_safe(self) -> ProviderReleaseProof:
        _safe_relative_directory(self.context_path)
        return self


class StoreProof(_StrictModel):
    store_name: Literal["postgresql", "qdrant", "neo4j"]
    attestation: ArtifactRef
    attestation_sha256: Sha256
    sealed_projection_sha256: Sha256


class EvaluationProof(_StrictModel):
    bundle_path: str = Field(min_length=1, max_length=300)
    report_sha256: Sha256
    approval_sha256: Sha256
    gold_sha256: Sha256
    unexpected_skips: Literal[0]
    mock_count: Literal[0]

    @model_validator(mode="after")
    def safe_bundle_path(self) -> EvaluationProof:
        _safe_relative_directory(self.bundle_path)
        return self


class GenerationReleaseProof(_StrictModel):
    generation_id: Sha256
    epoch: int = Field(gt=0)
    changed_at: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?Z$"
    )
    source_manifest: ArtifactRef
    source_manifest_sha256: Sha256
    model_manifest: ArtifactRef
    model_manifest_sha256: Sha256
    index_config: ArtifactRef
    index_config_sha256: Sha256
    stores: list[StoreProof] = Field(min_length=3, max_length=3)
    evaluation: EvaluationProof
    seal_attestation: ArtifactRef
    seal_attestation_sha256: Sha256
    active_attestation_sha256: Sha256

    @model_validator(mode="after")
    def exact_store_inventory(self) -> GenerationReleaseProof:
        names = [item.store_name for item in self.stores]
        if len(names) != len(set(names)) or set(names) != {"postgresql", "qdrant", "neo4j"}:
            raise ValueError("generation requires exactly one proof for every production store")
        return self


class PolicyReleaseProof(_StrictModel):
    policy_release: str = Field(min_length=1, max_length=128)
    policy: ArtifactRef
    coverage_matrix: ArtifactRef
    trusted_reviewers: ArtifactRef
    trusted_reviewer_lock_sha256: Sha256
    reviewer_trust_store_sha256: Sha256
    verified_approval_sha256: Sha256


class SecurityReport(_StrictModel):
    schema_version: Literal["1.0"]
    gate: Literal["secret_scan", "dependency_audit", "sast"]
    status: Literal["PASS"]
    source_commit: GitSha
    tool: SafeName
    tool_version: SafeName
    critical_findings: Literal[0]
    high_findings: Literal[0]
    generated_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class SecurityProof(_StrictModel):
    secret_scan: ArtifactRef
    dependency_audit: ArtifactRef
    sast: ArtifactRef


class ContainerBuildReport(_StrictModel):
    schema_version: Literal["1.0"]
    status: Literal["PASS"]
    image_digest: ImageDigest
    source_commit: GitSha
    workflow_sha: GitSha
    workflow_run_id: int = Field(gt=0)
    sbom_sha256: Sha256
    builder_id: SafeName
    generated_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ContainerReleaseProof(_StrictModel):
    image_digest: ImageDigest
    sbom: ArtifactRef
    build_provenance: ArtifactRef


class RollbackReport(_StrictModel):
    schema_version: Literal["1.0"]
    status: Literal["PASS"]
    rollback_image_digest: ImageDigest
    rollback_generation_id: Sha256
    from_generation_id: Sha256
    restore_tested: Literal[True]
    reconciliation_tested: Literal[True]
    generated_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class RollbackProof(_StrictModel):
    rollback_image_digest: ImageDigest
    rollback_generation_id: Sha256
    report: ArtifactRef


class RuntimeIdentityClaim(_StrictModel):
    runtime_release_v3_sha256: Sha256
    provider_revision: GitSha
    provider_build_sha256: Sha256
    knowledge_generation_id: Sha256
    knowledge_generation_attestation_sha256: Sha256
    requirement_policy_manifest_sha256: Sha256
    reviewer_trust_store_sha256: Sha256


class AgentHttpConformanceReport(_StrictModel):
    schema_version: Literal["1.0"]
    status: Literal["PASS"]
    agent_repository: Literal["customs-ai-agent"]
    agent_revision: GitSha
    actual_http: Literal[True]
    mock_count: Literal[0]
    readiness_get_count: int = Field(ge=1)
    retrieval_post_count: int = Field(ge=1)
    response_identity_match: Literal[True]
    consumer_lock_sha256: Sha256
    runtime_identity: RuntimeIdentityClaim
    generated_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class AgentConformanceProof(_StrictModel):
    consumer_lock: ArtifactRef
    report: ArtifactRef


class EvidenceV3ReleaseProvenance(_StrictModel):
    contract: Literal["evidence-v3-release-provenance"]
    schema_version: Literal["1.0"]
    release_state: Literal["release_candidate", "released"]
    created_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
    git: GitTagProvenance
    workflow: WorkflowProvenance
    provider: ProviderReleaseProof
    generation: GenerationReleaseProof
    policy: PolicyReleaseProof
    security: SecurityProof
    container: ContainerReleaseProof
    rollback: RollbackProof
    agent_conformance: AgentConformanceProof
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def identities_are_internally_bound(self) -> EvidenceV3ReleaseProvenance:
        if self.workflow.head_sha != self.git.source_commit:
            raise ValueError("workflow head does not equal tagged source commit")
        if self.workflow.workflow_sha != self.git.source_commit:
            raise ValueError("workflow bytes must come from the tagged source commit")
        if self.provider.provider_revision != self.git.source_commit:
            raise ValueError("provider revision does not equal tagged source commit")
        if self.rollback.rollback_generation_id == self.generation.generation_id:
            raise ValueError("rollback generation must be a distinct previous generation")
        if self.rollback.rollback_image_digest == self.container.image_digest:
            raise ValueError("rollback image must be a distinct previous image")
        expected = release_manifest_sha256(
            self.model_dump(mode="json", exclude={"manifest_sha256"})
        )
        if self.manifest_sha256 != expected:
            raise ValueError("release manifest SHA-256 does not match canonical content")
        return self


@dataclass(frozen=True, slots=True)
class ExpectedWorkflowContext:
    repository: str
    tag_name: str
    tag_object_sha: str
    tag_target_sha: str
    source_commit: str
    workflow_path: str
    workflow_sha: str
    workflow_ref: str
    head_ref: str
    workflow_run_id: int
    workflow_run_attempt: int
    artifact_id: int
    artifact_digest: str
    event: str
    conclusion: str


@dataclass(frozen=True, slots=True)
class VerifiedEvidenceV3Release:
    manifest: EvidenceV3ReleaseProvenance
    runtime_identity: EvidenceRuntimeReleaseIdentityV3
    generation: ActiveGenerationAttestation


def release_manifest_sha256(payload_without_digest: Mapping[str, object]) -> str:
    return canonical_sha256(payload_without_digest)


def _safe_relative_directory(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("bundle directory must be a safe POSIX relative path")
    return candidate


def _resolve_bundle_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    if directory:
        _safe_relative_directory(relative)
    unresolved = root / Path(*PurePosixPath(relative).parts)
    current = unresolved
    while current != root:
        if current.is_symlink():
            raise ReleaseProvenanceError("release artifact path contains a symbolic link")
        current = current.parent
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReleaseProvenanceError("release artifact escapes bundle root") from exc
    if candidate.is_symlink() or (directory and not candidate.is_dir()):
        raise ReleaseProvenanceError("release artifact path is not an allowed directory")
    return candidate


def _artifact_bytes(root: Path, reference: ArtifactRef) -> bytes:
    path = _resolve_bundle_path(root, reference.path)
    if path.is_symlink() or not path.is_file():
        raise ReleaseProvenanceError("release artifact is not a regular file")
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_JSON_BYTES:
            raise ReleaseProvenanceError("release artifact size is invalid")
        raw = path.read_bytes()
    except OSError as exc:
        raise ReleaseProvenanceError("release artifact is unavailable") from exc
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ReleaseProvenanceError("release artifact hash changed or does not match")
    return raw


def _strict_json_bytes(raw: bytes, *, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseProvenanceError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ReleaseProvenanceError(f"{label} must be a JSON object")
    return value


def _artifact_json(root: Path, reference: ArtifactRef, *, label: str) -> dict[str, Any]:
    return _strict_json_bytes(_artifact_bytes(root, reference), label=label)


def load_release_manifest(path: Path) -> EvidenceV3ReleaseProvenance:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
            raise ReleaseProvenanceError("release manifest must be a bounded regular file")
        payload = _strict_json_bytes(path.read_bytes(), label="release manifest")
        return EvidenceV3ReleaseProvenance.model_validate(payload)
    except ReleaseProvenanceError:
        raise
    except Exception as exc:
        raise ReleaseProvenanceError("release manifest is invalid") from exc


def _verify_workflow_context(
    release: EvidenceV3ReleaseProvenance,
    expected: ExpectedWorkflowContext,
) -> None:
    expected_ref = f"{release.git.repository}/{PRODUCER_WORKFLOW_PATH}@{release.workflow.head_ref}"
    if (
        release.workflow.workflow_path != PRODUCER_WORKFLOW_PATH
        or release.workflow.head_ref != f"{PRODUCER_WORKFLOW_REF_PREFIX}{release.git.tag_name}"
        or release.workflow.workflow_ref != expected_ref
        or expected.artifact_id <= 0
        or not expected.artifact_digest.startswith("sha256:")
        or len(expected.artifact_digest) != 71
        or any(char not in "0123456789abcdef" for char in expected.artifact_digest[7:])
    ):
        raise ReleaseProvenanceError("release evidence producer identity is not allowlisted")
    observed = (
        release.git.repository,
        release.git.tag_name,
        release.git.tag_object_sha,
        release.git.tag_target_sha,
        release.git.source_commit,
        release.workflow.workflow_path,
        release.workflow.workflow_sha,
        release.workflow.workflow_ref,
        release.workflow.head_ref,
        release.workflow.run_id,
        release.workflow.run_attempt,
        release.workflow.event,
        release.workflow.conclusion,
    )
    required = (
        expected.repository,
        expected.tag_name,
        expected.tag_object_sha,
        expected.tag_target_sha,
        expected.source_commit,
        expected.workflow_path,
        expected.workflow_sha,
        expected.workflow_ref,
        expected.head_ref,
        expected.workflow_run_id,
        expected.workflow_run_attempt,
        expected.event,
        expected.conclusion,
    )
    if observed != required:
        raise ReleaseProvenanceError("release does not match the independently observed Git run")


def _verify_workflow_file(repository_root: Path, release: EvidenceV3ReleaseProvenance) -> None:
    path = repository_root / Path(*PurePosixPath(release.workflow.workflow_path).parts)
    try:
        if path.is_symlink() or not path.is_file():
            raise ReleaseProvenanceError("provenance workflow is not a regular tagged file")
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReleaseProvenanceError("provenance workflow bytes are unavailable") from exc
    if observed != release.workflow.workflow_file_sha256:
        raise ReleaseProvenanceError("provenance workflow hash does not match tagged bytes")


def _tracked_runtime_source_paths(repository_root: Path) -> tuple[str, ...]:
    """Return the exact tagged runtime-source inventory.

    Production uses Git's tree, not the mutable working-directory inventory.
    The filesystem fallback exists only for isolated unit-test repositories
    that intentionally have no Git metadata.
    """

    has_git_metadata = (repository_root / ".git").exists()
    executable = shutil.which("git")
    if has_git_metadata:
        if executable is None:
            raise ReleaseProvenanceError("git executable is unavailable for tagged source")
        # The executable and complete argument vector are fixed locally.
        result = subprocess.run(  # noqa: S603  # nosec B603
            [
                executable,
                "-C",
                str(repository_root),
                "ls-tree",
                "-r",
                "--name-only",
                "HEAD",
                "--",
                "law_repository/src",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode:
            raise ReleaseProvenanceError("tagged runtime source inventory query failed")
        paths = tuple(sorted(line for line in result.stdout.splitlines() if line))
        if not paths:
            raise ReleaseProvenanceError("tagged repository has no runtime source inventory")
        return paths

    source_root = repository_root / "law_repository" / "src"
    try:
        paths = tuple(
            sorted(
                path.relative_to(repository_root).as_posix()
                for path in source_root.rglob("*")
                if path.is_file()
                and not path.is_symlink()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
        )
    except OSError as exc:
        raise ReleaseProvenanceError("tagged runtime source inventory is unavailable") from exc
    if not paths:
        raise ReleaseProvenanceError("tagged repository has no runtime source inventory")
    return paths


def _verify_exact_production_context(
    context_root: Path,
    repository_root: Path,
    *,
    source_commit: str,
) -> tuple[bytes, bytes]:
    expected_sources = dict(FIXED_PRODUCTION_CONTEXT_FILES)
    for source_path in _tracked_runtime_source_paths(repository_root):
        expected_sources[source_path] = source_path
    destination_to_source = {
        destination: source for source, destination in expected_sources.items()
    }
    if len(destination_to_source) != len(expected_sources):
        raise ReleaseProvenanceError("production context allowlist contains a collision")

    manifest_path = context_root / "source-manifest.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ReleaseProvenanceError("production context manifest is not a regular file")
        manifest_raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ReleaseProvenanceError("production context manifest is unavailable") from exc
    manifest = _strict_json_bytes(manifest_raw, label="production context source manifest")
    if set(manifest) != {"schema_version", "source_repository", "source_commit", "files"}:
        raise ReleaseProvenanceError("production context manifest fields are not exact")
    if (
        manifest["schema_version"] != "1.0"
        or manifest["source_repository"] != "customs-data-platform"
        or manifest["source_commit"] != source_commit
        or not isinstance(manifest["files"], list)
    ):
        raise ReleaseProvenanceError("production context manifest identity is invalid")

    expected_destinations = tuple(sorted(destination_to_source))
    observed_destinations: list[str] = []
    trusted_reviewer_lock_raw: bytes | None = None
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes"}:
            raise ReleaseProvenanceError("production context manifest entry is malformed")
        destination = entry["path"]
        if not isinstance(destination, str):
            raise ReleaseProvenanceError("production context destination is invalid")
        ArtifactRef(path=destination, sha256=str(entry["sha256"]))
        source = destination_to_source.get(destination)
        if source is None:
            raise ReleaseProvenanceError("production context contains an unallowlisted file")
        tagged_path = repository_root / Path(*PurePosixPath(source).parts)
        context_path = context_root / Path(*PurePosixPath(destination).parts)
        try:
            if (
                tagged_path.is_symlink()
                or context_path.is_symlink()
                or not tagged_path.is_file()
                or not context_path.is_file()
            ):
                raise ReleaseProvenanceError("production context file is not regular")
            source_raw = tagged_path.read_bytes()
            context_raw = context_path.read_bytes()
        except OSError as exc:
            raise ReleaseProvenanceError("production context file is unavailable") from exc
        if (
            context_raw != source_raw
            or entry["bytes"] != len(source_raw)
            or entry["sha256"] != hashlib.sha256(source_raw).hexdigest()
        ):
            raise ReleaseProvenanceError("production context differs from tagged source bytes")
        if source == TRUST_LOCK_RELATIVE_PATH.as_posix():
            trusted_reviewer_lock_raw = source_raw
        observed_destinations.append(destination)
    if tuple(observed_destinations) != expected_destinations:
        raise ReleaseProvenanceError(
            "production context exact inventory does not match tagged source"
        )

    try:
        actual_files = {
            path.relative_to(context_root).as_posix()
            for path in context_root.rglob("*")
            if path.is_file()
        }
    except OSError as exc:
        raise ReleaseProvenanceError("production context inventory is unavailable") from exc
    expected_files = set(expected_destinations) | set(GENERATED_PRODUCTION_CONTEXT_FILES)
    if actual_files != expected_files:
        raise ReleaseProvenanceError("production context has missing or extra files")
    if trusted_reviewer_lock_raw is None:
        raise ReleaseProvenanceError("production context is missing the tagged reviewer lock")
    return manifest_raw, trusted_reviewer_lock_raw


def _verify_provider(
    root: Path,
    repository_root: Path,
    release: EvidenceV3ReleaseProvenance,
) -> tuple[EvidenceRuntimeReleaseIdentityV3, bytes]:
    proof = release.provider
    context_root = _resolve_bundle_path(root, proof.context_path, directory=True)
    if any(path.is_symlink() for path in context_root.rglob("*")):
        raise ReleaseProvenanceError("production provider context contains a symbolic link")
    try:
        context_raw, trusted_reviewer_lock_raw = _verify_exact_production_context(
            context_root,
            repository_root,
            source_commit=release.git.source_commit,
        )
        context_hash = hashlib.sha256(context_raw).hexdigest()
        build = load_runtime_provider_build_identity(bundle_root=context_root)
    except Exception as exc:
        raise ReleaseProvenanceError("production provider context is invalid") from exc
    if (
        context_hash != proof.context_manifest_sha256
        or build.provider_release != proof.provider_release
        or build.provider_revision != proof.provider_revision
        or build.provider_build_sha256 != proof.provider_build_sha256
        or build.source_manifest_sha256 != proof.provider_source_manifest_sha256
    ):
        raise ReleaseProvenanceError("provider context identity does not match release proof")

    schema_bindings = {
        V3_RETRIEVAL_SCHEMA_RELATIVE_PATH: proof.v3_retrieval_schema_sha256,
        V3_READINESS_SCHEMA_RELATIVE_PATH: proof.v3_readiness_schema_sha256,
        POLICY_SCHEMA_RELATIVE_PATH: proof.requirement_policy_schema_sha256,
        RELEASE_SCHEMA_RELATIVE_PATH: proof.release_provenance_schema_sha256,
    }
    for relative_path, expected_hash in schema_bindings.items():
        try:
            context_bytes = (context_root / relative_path).read_bytes()
            committed_bytes = (repository_root / relative_path).read_bytes()
        except OSError as exc:
            raise ReleaseProvenanceError("a required V3 schema is unavailable") from exc
        if (
            context_bytes != committed_bytes
            or hashlib.sha256(context_bytes).hexdigest() != expected_hash
        ):
            raise ReleaseProvenanceError("provider and tagged V3 schema bytes do not match")

    try:
        runtime = EvidenceRuntimeReleaseIdentityV3.model_validate(
            _artifact_json(root, proof.runtime_identity, label="runtime v3 identity")
        )
    except Exception as exc:
        raise ReleaseProvenanceError("runtime v3 identity is invalid") from exc
    base = runtime.base_runtime_release
    if (
        base.provider_release != proof.provider_release
        or base.provider_revision != proof.provider_revision
        or base.provider_build_sha256 != proof.provider_build_sha256
        or runtime.retrieval_schema_sha256 != proof.v3_retrieval_schema_sha256
        or runtime.readiness_schema_sha256 != proof.v3_readiness_schema_sha256
        or runtime.requirement_policy_schema_sha256 != proof.requirement_policy_schema_sha256
    ):
        raise ReleaseProvenanceError("runtime identity is not bound to provider/schema proof")
    return runtime, trusted_reviewer_lock_raw


def _store_attestation(value: Mapping[str, Any]) -> StoreAttestation:
    required = {
        "schema_version",
        "generation_id",
        "store_name",
        "article_count",
        "article_id_set_sha256",
        "article_content_set_sha256",
        "projection_sha256",
        "model_manifest_sha256",
        "store_locator",
        "store_revision",
        "attested_at",
    }
    if set(value) != required or value.get("schema_version") != "1.0":
        raise ReleaseProvenanceError("store attestation fields do not match schema")
    try:
        return StoreAttestation(**{key: value[key] for key in required - {"schema_version"}})
    except (TypeError, GenerationValidationError) as exc:
        raise ReleaseProvenanceError("store attestation is invalid") from exc


def _verify_generation(
    root: Path,
    proof: GenerationReleaseProof,
    runtime: EvidenceRuntimeReleaseIdentityV3,
) -> ActiveGenerationAttestation:
    try:
        source = SourceManifest.from_mapping(
            _artifact_json(root, proof.source_manifest, label="knowledge source manifest")
        )
        source.require_release_eligible_sources(
            as_of=runtime.base_runtime_release.supported_review_as_of.isoformat()
        )
        spec = GenerationSpec(
            source_manifest=source,
            model_manifest=_artifact_json(root, proof.model_manifest, label="model manifest"),
            index_config=_artifact_json(root, proof.index_config, label="index config"),
        )
    except (GenerationValidationError, ReleaseProvenanceError) as exc:
        raise ReleaseProvenanceError("knowledge generation inputs are invalid") from exc
    if (
        spec.generation_id != proof.generation_id
        or spec.source_manifest_sha256 != proof.source_manifest_sha256
        or spec.model_manifest_sha256 != proof.model_manifest_sha256
        or spec.index_config_sha256 != proof.index_config_sha256
    ):
        raise ReleaseProvenanceError("generation identity does not match exact input bytes")

    stores: list[StoreAttestation] = []
    for binding in proof.stores:
        item = _store_attestation(
            _artifact_json(root, binding.attestation, label=f"{binding.store_name} attestation")
        )
        if (
            item.store_name != binding.store_name
            or item.sha256 != binding.attestation_sha256
            or item.sealed_projection_sha256 != binding.sealed_projection_sha256
        ):
            raise ReleaseProvenanceError("store attestation hash/binding mismatch")
        stores.append(item)
    try:
        ordered = validate_store_attestations(
            spec.generation_id,
            spec.model_manifest_sha256,
            stores,
        )
        evaluation_root = _resolve_bundle_path(
            root,
            proof.evaluation.bundle_path,
            directory=True,
        )
        evaluation = load_verified_evaluation_bundle(
            evaluation_root,
            generation_id=spec.generation_id,
        )
    except GenerationValidationError as exc:
        raise ReleaseProvenanceError("store or RAG evaluation evidence is invalid") from exc
    if (
        evaluation.report_sha256 != proof.evaluation.report_sha256
        or evaluation.approval_sha256 != proof.evaluation.approval_sha256
        or evaluation.report["artifacts"]["gold_sha256"] != proof.evaluation.gold_sha256
        or evaluation.report["unexpected_skips"] != proof.evaluation.unexpected_skips
        or evaluation.report["mock_count"] != proof.evaluation.mock_count
    ):
        raise ReleaseProvenanceError("RAG evaluation bundle does not match release proof")

    expected_seal, expected_seal_hash = build_seal_attestation(
        spec,
        ordered,
        evaluation.report_sha256,
    )
    observed_seal = _artifact_json(root, proof.seal_attestation, label="generation seal")
    if observed_seal != expected_seal or expected_seal_hash != proof.seal_attestation_sha256:
        raise ReleaseProvenanceError("generation seal does not match exact stores/evaluation")

    active = ActiveGenerationAttestation(
        generation_id=spec.generation_id,
        epoch=proof.epoch,
        changed_at=proof.changed_at,
        source_manifest_sha256=spec.source_manifest_sha256,
        model_manifest_sha256=spec.model_manifest_sha256,
        index_config_sha256=spec.index_config_sha256,
        evaluation_report_sha256=evaluation.report_sha256,
        seal_attestation_sha256=expected_seal_hash,
        stores=ordered,
    )
    base = runtime.base_runtime_release
    if (
        active.sha256 != proof.active_attestation_sha256
        or base.knowledge_generation_id != active.generation_id
        or base.knowledge_generation_attestation_sha256 != active.sha256
        or base.knowledge_source_manifest_sha256 != active.source_manifest_sha256
        or base.model_manifest_sha256 != active.model_manifest_sha256
        or base.index_config_sha256 != active.index_config_sha256
        or base.evaluation_report_sha256 != active.evaluation_report_sha256
        or base.seal_attestation_sha256 != active.seal_attestation_sha256
    ):
        raise ReleaseProvenanceError("runtime does not identify the exact sealed generation")
    return active


def _verify_policy(
    root: Path,
    proof: PolicyReleaseProof,
    runtime: EvidenceRuntimeReleaseIdentityV3,
    generation: ActiveGenerationAttestation,
    trusted_reviewer_lock_raw: bytes,
    *,
    today: date | None,
) -> None:
    policy_raw = _artifact_bytes(root, proof.policy)
    reviewers_raw = _artifact_bytes(root, proof.trusted_reviewers)
    coverage_raw = _artifact_bytes(root, proof.coverage_matrix)
    try:
        lock_hash = hashlib.sha256(trusted_reviewer_lock_raw).hexdigest()
        release = parse_requirement_policy_release(policy_raw)
        store = parse_trusted_reviewer_store(reviewers_raw)
        lock = parse_trusted_reviewer_store_lock(trusted_reviewer_lock_raw)
        verified = verify_requirement_policy_release(
            release,
            trust_store=store,
            trust_store_lock=lock,
            today=today,
        )
    except Exception as exc:
        raise ReleaseProvenanceError("requirement policy/trust approval is invalid") from exc
    coverage_hash = hashlib.sha256(coverage_raw).hexdigest()
    if (
        lock_hash != proof.trusted_reviewer_lock_sha256
        or verified.reviewer_trust_store_sha256 != proof.reviewer_trust_store_sha256
        or verified.verified_approval_sha256 != proof.verified_approval_sha256
        or release.policy_release != proof.policy_release
        or release.coverage_matrix_sha256 != coverage_hash
        or release.knowledge_source_manifest_sha256 != generation.source_manifest_sha256
        or release.knowledge_snapshot_id != generation.generation_id
        or runtime.requirement_policy_release != release.policy_release
        or runtime.requirement_policy_manifest_sha256 != release.policy_manifest_sha256
        or runtime.coverage_matrix_sha256 != release.coverage_matrix_sha256
        or runtime.requirement_policy_approval_sha256 != verified.verified_approval_sha256
        or runtime.reviewer_trust_store_sha256 != verified.reviewer_trust_store_sha256
    ):
        raise ReleaseProvenanceError("policy approval is not bound to runtime/generation")


def _verify_security(root: Path, release: EvidenceV3ReleaseProvenance) -> None:
    expected = {
        "secret_scan": release.security.secret_scan,
        "dependency_audit": release.security.dependency_audit,
        "sast": release.security.sast,
    }
    for gate, reference in expected.items():
        try:
            report = SecurityReport.model_validate(
                _artifact_json(root, reference, label=f"{gate} report")
            )
        except Exception as exc:
            raise ReleaseProvenanceError(f"{gate} report is invalid") from exc
        if report.gate != gate or report.source_commit != release.git.source_commit:
            raise ReleaseProvenanceError(f"{gate} report is not bound to source commit")


def _verify_container(root: Path, release: EvidenceV3ReleaseProvenance) -> None:
    proof = release.container
    sbom_hash = hashlib.sha256(_artifact_bytes(root, proof.sbom)).hexdigest()
    try:
        report = ContainerBuildReport.model_validate(
            _artifact_json(root, proof.build_provenance, label="container provenance")
        )
    except Exception as exc:
        raise ReleaseProvenanceError("container provenance is invalid") from exc
    if (
        report.image_digest != proof.image_digest
        or report.source_commit != release.git.source_commit
        or report.workflow_sha != release.workflow.workflow_sha
        or report.workflow_run_id != release.workflow.run_id
        or report.sbom_sha256 != sbom_hash
    ):
        raise ReleaseProvenanceError("container image/SBOM provenance is not exact")


def _verify_rollback(root: Path, release: EvidenceV3ReleaseProvenance) -> None:
    try:
        report = RollbackReport.model_validate(
            _artifact_json(root, release.rollback.report, label="rollback report")
        )
    except Exception as exc:
        raise ReleaseProvenanceError("rollback report is invalid") from exc
    if (
        report.rollback_image_digest != release.rollback.rollback_image_digest
        or report.rollback_generation_id != release.rollback.rollback_generation_id
        or report.from_generation_id != release.generation.generation_id
    ):
        raise ReleaseProvenanceError("rollback proof is not bound to this release")


def _verify_agent_conformance(
    root: Path,
    release: EvidenceV3ReleaseProvenance,
    runtime: EvidenceRuntimeReleaseIdentityV3,
) -> None:
    proof = release.agent_conformance
    lock_hash = hashlib.sha256(_artifact_bytes(root, proof.consumer_lock)).hexdigest()
    try:
        report = AgentHttpConformanceReport.model_validate(
            _artifact_json(root, proof.report, label="Agent HTTP conformance")
        )
    except Exception as exc:
        raise ReleaseProvenanceError("Agent HTTP conformance report is invalid") from exc
    base = runtime.base_runtime_release
    expected_claim = RuntimeIdentityClaim(
        runtime_release_v3_sha256=runtime.runtime_release_v3_sha256,
        provider_revision=base.provider_revision,
        provider_build_sha256=base.provider_build_sha256,
        knowledge_generation_id=base.knowledge_generation_id,
        knowledge_generation_attestation_sha256=(base.knowledge_generation_attestation_sha256),
        requirement_policy_manifest_sha256=runtime.requirement_policy_manifest_sha256,
        reviewer_trust_store_sha256=runtime.reviewer_trust_store_sha256,
    )
    if report.consumer_lock_sha256 != lock_hash or report.runtime_identity != expected_claim:
        raise ReleaseProvenanceError("Agent consumer lock/HTTP result does not match runtime")


def verify_release_bundle(
    bundle_root: Path,
    *,
    repository_root: Path,
    expected_workflow: ExpectedWorkflowContext,
    require_released: bool = True,
    today: date | None = None,
) -> VerifiedEvidenceV3Release:
    """Structurally verify a candidate without granting Production release.

    Production admission is deliberately unavailable until protected producer
    provenance, an independently pinned release-attestor trust root, two
    release signatures, and signed raw execution evidence are implemented.
    This explicit blocker prevents internally consistent or test-only JSON from
    ever being upgraded into ``released`` status.
    """

    try:
        if bundle_root.is_symlink() or repository_root.is_symlink():
            raise ReleaseProvenanceError("bundle or repository root cannot be a symbolic link")
        root = bundle_root.resolve(strict=True)
        repo = repository_root.resolve(strict=True)
    except OSError as exc:
        raise ReleaseProvenanceError("bundle or repository root is unavailable") from exc
    if root.is_symlink() or not root.is_dir() or not repo.is_dir():
        raise ReleaseProvenanceError("bundle or repository root is invalid")
    manifest = load_release_manifest(root / "release-provenance.json")
    if require_released:
        raise ReleaseProvenanceError(PRODUCTION_RELEASE_BLOCKER)
    if manifest.release_state != "release_candidate":
        raise ReleaseProvenanceError(
            "released manifests cannot be admitted by the candidate-only structural verifier"
        )
    _verify_workflow_context(manifest, expected_workflow)
    _verify_workflow_file(repo, manifest)
    runtime, trusted_reviewer_lock_raw = _verify_provider(root, repo, manifest)
    generation = _verify_generation(root, manifest.generation, runtime)
    _verify_policy(
        root,
        manifest.policy,
        runtime,
        generation,
        trusted_reviewer_lock_raw,
        today=today,
    )
    _verify_security(root, manifest)
    _verify_container(root, manifest)
    _verify_rollback(root, manifest)
    _verify_agent_conformance(root, manifest, runtime)
    return VerifiedEvidenceV3Release(
        manifest=manifest,
        runtime_identity=runtime,
        generation=generation,
    )


__all__ = [
    "AgentConformanceProof",
    "AgentHttpConformanceReport",
    "ArtifactRef",
    "ContainerBuildReport",
    "EvidenceV3ReleaseProvenance",
    "ExpectedWorkflowContext",
    "ReleaseProvenanceError",
    "RollbackReport",
    "SecurityReport",
    "VerifiedEvidenceV3Release",
    "load_release_manifest",
    "release_manifest_sha256",
    "verify_release_bundle",
]
