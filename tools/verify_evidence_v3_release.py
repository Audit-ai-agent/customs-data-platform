"""Verify an Evidence v3 release bundle against the current Git checkout.

The command is intentionally read-only.  It derives the annotated tag facts
from Git, accepts independently queried workflow-run facts, and delegates all
artifact verification to the pure release-provenance verifier.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil

# Git is resolved to an executable and invoked with validated arguments.
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "law_repository"))

from src.release_provenance_v3 import (  # noqa: E402
    ExpectedWorkflowContext,
    ReleaseProvenanceError,
    verify_release_bundle,
)

GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RELEASE_TAG = re.compile(r"^evidence-api-v3\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?$")
WORKFLOW_PATH = re.compile(r"^\.github/workflows/[A-Za-z0-9._-]+\.ya?ml$")
WORKFLOW_REF = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/"
    r"evidence-v3-release-bundle\.yml@refs/tags/"
    r"evidence-api-v3\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?$"
)
HEAD_REF = re.compile(r"^refs/tags/evidence-api-v3\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?$")
ARTIFACT_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _git(repository: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ReleaseProvenanceError("git executable is unavailable")
    # Every caller supplies a fixed Git subcommand and prevalidated values.
    result = subprocess.run(  # noqa: S603  # nosec B603
        [executable, "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise ReleaseProvenanceError(result.stderr.strip() or "git provenance query failed")
    return result.stdout.strip()


def _positive_integer(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]{0,19}", value) is None:
        raise argparse.ArgumentTypeError("value must be a positive bounded integer")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--workflow-path", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--workflow-run-id", type=_positive_integer, required=True)
    parser.add_argument("--workflow-run-attempt", type=_positive_integer, required=True)
    parser.add_argument("--artifact-id", type=_positive_integer, required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--workflow-event", choices=("workflow_dispatch",), required=True)
    parser.add_argument("--workflow-conclusion", choices=("success",), required=True)
    parser.add_argument(
        "--candidate-structural-only",
        action="store_true",
        help=(
            "Validate candidate structure only; success exits 2 and never grants released status."
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if REPOSITORY.fullmatch(args.repository) is None:
        raise SystemExit("repository must be an owner/name slug")
    if RELEASE_TAG.fullmatch(args.release_tag) is None:
        raise SystemExit("release tag does not match the Evidence v3 naming policy")
    if GIT_SHA.fullmatch(args.workflow_sha) is None:
        raise SystemExit("workflow SHA must be a full lowercase Git SHA")
    if WORKFLOW_PATH.fullmatch(args.workflow_path) is None:
        raise SystemExit("workflow path is outside the repository workflow allowlist")
    if WORKFLOW_REF.fullmatch(args.workflow_ref) is None:
        raise SystemExit("workflow ref is not the protected Evidence producer tag ref")
    if HEAD_REF.fullmatch(args.head_ref) is None:
        raise SystemExit("producer head ref is not an Evidence release tag")
    if ARTIFACT_DIGEST.fullmatch(args.artifact_digest) is None:
        raise SystemExit("artifact digest must be a lowercase sha256 digest")

    repository = args.repository_root.resolve()
    try:
        source_commit = _git(repository, "rev-parse", "HEAD")
        if _git(repository, "status", "--porcelain"):
            raise ReleaseProvenanceError("release checkout is not clean")
        if _git(repository, "cat-file", "-t", args.release_tag) != "tag":
            raise ReleaseProvenanceError("release ref is not an annotated tag object")
        tag_object = _git(repository, "rev-parse", f"{args.release_tag}^{{tag}}")
        tag_target = _git(repository, "rev-parse", f"{args.release_tag}^{{commit}}")
        if source_commit != tag_target:
            raise ReleaseProvenanceError("checked-out commit is not the annotated tag target")
        verified = verify_release_bundle(
            args.bundle_root,
            repository_root=repository,
            expected_workflow=ExpectedWorkflowContext(
                repository=args.repository,
                tag_name=args.release_tag,
                tag_object_sha=tag_object,
                tag_target_sha=tag_target,
                source_commit=source_commit,
                workflow_path=args.workflow_path,
                workflow_sha=args.workflow_sha,
                workflow_ref=args.workflow_ref,
                head_ref=args.head_ref,
                workflow_run_id=args.workflow_run_id,
                workflow_run_attempt=args.workflow_run_attempt,
                artifact_id=args.artifact_id,
                artifact_digest=args.artifact_digest,
                event=args.workflow_event,
                conclusion=args.workflow_conclusion,
            ),
            require_released=not args.candidate_structural_only,
        )
    except ReleaseProvenanceError as exc:
        print(json.dumps({"status": "RELEASE_BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 1

    print(
        json.dumps(
            {
                "status": "CANDIDATE_STRUCTURAL_VALID_ONLY_NOT_RELEASED",
                "release_state": verified.manifest.release_state,
                "manifest_sha256": verified.manifest.manifest_sha256,
                "source_commit": verified.manifest.git.source_commit,
                "generation_id": verified.generation.generation_id,
                "runtime_release_v3_sha256": (verified.runtime_identity.runtime_release_v3_sha256),
                "evidence_artifact_id": args.artifact_id,
                "evidence_artifact_digest": args.artifact_digest,
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
