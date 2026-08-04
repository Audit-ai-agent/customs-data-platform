"""Build a secret-free, attested Evidence provider context.

The context is generated only from one clean committed Data revision.  It is
an allowlist, not a copy of the working tree, and deliberately excludes
Docker, cloud/release assets, datasets, credentials, tests, local state, and
Git metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

try:
    from tools.provider_context import FIXED_PROVIDER_CONTEXT_FILES
    from tools.provider_build import (
        create_provider_build_document,
        create_runtime_provider_build_document,
        create_runtime_provider_lock_document,
        serialized_document,
    )
except ModuleNotFoundError:  # direct execution with tools/ as sys.path[0]
    from provider_context import FIXED_PROVIDER_CONTEXT_FILES  # type: ignore[no-redef]
    from provider_build import (  # type: ignore[no-redef]
        create_provider_build_document,
        create_runtime_provider_build_document,
        create_runtime_provider_lock_document,
        serialized_document,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXED_FILES = FIXED_PROVIDER_CONTEXT_FILES


class ProviderContextError(RuntimeError):
    """An immutable provider context could not be proven."""


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise ProviderContextError("git executable is unavailable")
    return executable


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - executable and argv are explicit
        [_git_executable(), "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise ProviderContextError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _committed_bytes(repo: Path, relative_path: str) -> bytes:
    result = subprocess.run(  # noqa: S603 - executable and argv are explicit
        [_git_executable(), "-C", str(repo), "show", f"HEAD:{relative_path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ProviderContextError(
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
    paths = sorted(line for line in output.splitlines() if line)
    if not paths:
        raise ProviderContextError("HEAD does not contain law_repository/src")
    return paths


def build_context(repo: Path, output: Path) -> dict[str, object]:
    repo = repo.resolve()
    output = output.resolve()
    if _is_within(output, repo):
        raise ProviderContextError("output must be outside the Data repository")
    if _git(repo, "status", "--porcelain"):
        raise ProviderContextError(
            "Data repository must be clean; context is built from committed HEAD"
        )
    if output.exists() and any(output.iterdir()):
        raise ProviderContextError("output directory must not exist or must be empty")

    revision = _git(repo, "rev-parse", "HEAD")
    mappings = dict(FIXED_FILES)
    for source_path in _source_paths(repo):
        mappings[source_path] = source_path

    output.mkdir(parents=True, exist_ok=True)
    manifest_files: list[dict[str, object]] = []
    for source_path, destination_path in sorted(
        mappings.items(),
        key=lambda item: item[1],
    ):
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
        "source_commit": revision,
        "files": manifest_files,
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    (output / "source-manifest.json").write_bytes(manifest_bytes)
    schema_bytes = (output / "contracts/evidence/v1/rag_retrieval_v1.schema.json").read_bytes()
    provider_build = create_provider_build_document(
        provider_revision=revision,
        retrieval_schema=schema_bytes,
        source_manifest=manifest_bytes,
    )
    (output / "provider-build.json").write_bytes(serialized_document(provider_build))
    runtime_provider_build = create_runtime_provider_build_document(
        provider_revision=revision,
        retrieval_schema=schema_bytes,
        source_manifest=manifest_bytes,
    )
    (output / "provider-runtime-build.json").write_bytes(
        serialized_document(runtime_provider_build)
    )
    readiness_schema_bytes = (
        output / "contracts/evidence/v2/readiness_v2.schema.json"
    ).read_bytes()
    runtime_provider_lock = create_runtime_provider_lock_document(
        runtime_provider_build=runtime_provider_build,
        retrieval_schema=schema_bytes,
        readiness_schema=readiness_schema_bytes,
        source_manifest=manifest_bytes,
    )
    (output / "provider-runtime-lock.json").write_bytes(serialized_document(runtime_provider_lock))
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = build_context(args.repository, args.output)
    files = manifest["files"]
    if not isinstance(files, list):  # defensive: build_context owns this document
        raise ProviderContextError("provider context manifest file list is invalid")
    print(
        json.dumps(
            {
                "source_commit": manifest["source_commit"],
                "file_count": len(files),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
