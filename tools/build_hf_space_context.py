"""Build a secret-minimized HF Space context from committed Data source only."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

try:
    from tools.provider_build import (
        create_provider_build_document,
        serialized_document,
    )
except ModuleNotFoundError:  # direct execution with tools/ as sys.path[0]
    from provider_build import (  # type: ignore[no-redef]
        create_provider_build_document,
        serialized_document,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXED_FILES = {
    ".dockerignore": ".dockerignore",
    "contracts/evidence/v1/rag_retrieval_v1.schema.json": (
        "contracts/evidence/v1/rag_retrieval_v1.schema.json"
    ),
    "contracts/evidence/v1/readiness_v1.schema.json": (
        "contracts/evidence/v1/readiness_v1.schema.json"
    ),
    "law_repository/deploy/hf_space/Dockerfile": "Dockerfile",
    "law_repository/deploy/hf_space/README.md": "README.md",
    "pyproject.toml": "pyproject.toml",
    "uv.lock": "uv.lock",
}


class ContextBuildError(RuntimeError):
    """Raised when a deploy context cannot be built without weakening provenance."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise ContextBuildError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _committed_bytes(repo: Path, relative_path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relative_path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ContextBuildError(
            result.stderr.decode("utf-8", errors="replace").strip()
            or f"committed file is missing: {relative_path}"
        )
    return result.stdout


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _source_paths(repo: Path) -> list[str]:
    output = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "law_repository/src")
    paths = sorted(line.strip() for line in output.splitlines() if line.strip())
    if not paths:
        raise ContextBuildError("HEAD does not contain law_repository/src")
    return paths


def build_context(repo: Path, output: Path) -> dict[str, object]:
    repo = repo.resolve()
    output = output.resolve()
    if _is_within(output, repo):
        raise ContextBuildError("output must be outside the Data repository")
    status = _git(repo, "status", "--porcelain")
    if status.strip():
        raise ContextBuildError(
            "Data repository must be clean; context is built from committed HEAD"
        )
    if output.exists() and any(output.iterdir()):
        raise ContextBuildError("output directory must not exist or must be empty")

    source_commit = _git(repo, "rev-parse", "HEAD").strip()
    mappings = dict(FIXED_FILES)
    for source_path in _source_paths(repo):
        mappings[source_path] = source_path

    output.mkdir(parents=True, exist_ok=True)
    manifest_files: list[dict[str, object]] = []
    for source_path, destination_path in sorted(mappings.items(), key=lambda item: item[1]):
        content = _committed_bytes(repo, source_path)
        destination = output / destination_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        manifest_files.append(
            {
                "path": destination_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )

    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "source_repository": "customs-data-platform",
        "source_commit": source_commit,
        "files": manifest_files,
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    (output / "source-manifest.json").write_bytes(manifest_bytes)
    provider_build = create_provider_build_document(
        provider_revision=source_commit,
        retrieval_schema=(
            output / FIXED_FILES["contracts/evidence/v1/rag_retrieval_v1.schema.json"]
        ).read_bytes(),
        source_manifest=manifest_bytes,
    )
    (output / "provider-build.json").write_bytes(serialized_document(provider_build))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = build_context(args.repository, args.output)
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
