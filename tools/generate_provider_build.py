"""Generate an Evidence provider-build identity from a clean committed bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

try:
    from tools.provider_context import (
        FIXED_PROVIDER_CONTEXT_FILES,
        GENERATED_PROVIDER_CONTEXT_FILES,
    )
    from tools.provider_build import (
        ProviderBuildError,
        create_provider_build_document,
        serialized_document,
    )
except ModuleNotFoundError:  # direct execution with tools/ as sys.path[0]
    from provider_context import (  # type: ignore[no-redef]
        FIXED_PROVIDER_CONTEXT_FILES,
        GENERATED_PROVIDER_CONTEXT_FILES,
    )
    from provider_build import (  # type: ignore[no-redef]
        ProviderBuildError,
        create_provider_build_document,
        serialized_document,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_MANIFEST_NAME = "source-manifest.json"
PROVIDER_BUILD_NAME = "provider-build.json"
RETRIEVAL_SCHEMA_SOURCE = "contracts/evidence/v1/rag_retrieval_v1.schema.json"
FIXED_FILES = FIXED_PROVIDER_CONTEXT_FILES


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
        raise ProviderBuildError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _committed_bytes(repo: Path, relative_path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relative_path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ProviderBuildError(
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


def _canonical_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ProviderBuildError("provider bundle mapping contains an unsafe path")
    return value


def _source_paths(repo: Path) -> list[str]:
    output = _git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "law_repository/src")
    paths = sorted(line.strip() for line in output.splitlines() if line.strip())
    if not paths:
        raise ProviderBuildError("HEAD does not contain law_repository/src")
    return paths


def _expected_mappings(repo: Path) -> dict[str, str]:
    mappings = dict(FIXED_FILES)
    for source_path in _source_paths(repo):
        previous = mappings.get(source_path)
        if previous is not None and previous != source_path:
            raise ProviderBuildError("provider bundle source mappings are not unique")
        mappings[source_path] = source_path

    destinations: dict[str, str] = {}
    for source_path, destination_path in mappings.items():
        source_path = _canonical_relative_path(source_path)
        destination_path = _canonical_relative_path(destination_path)
        previous = destinations.get(destination_path)
        if previous is not None and previous != source_path:
            raise ProviderBuildError("provider bundle destinations are not unique")
        destinations[destination_path] = source_path
    return destinations


def _retrieval_schema_destination() -> str:
    try:
        return FIXED_FILES[RETRIEVAL_SCHEMA_SOURCE]
    except KeyError as exc:
        raise ProviderBuildError(
            "safe builder allowlist does not contain the retrieval schema"
        ) from exc


def _expected_manifest(
    repo: Path,
    *,
    revision: str,
    mappings: dict[str, str],
) -> tuple[bytes, dict[str, bytes]]:
    committed_by_destination: dict[str, bytes] = {}
    manifest_files: list[dict[str, object]] = []
    for destination_path, source_path in sorted(mappings.items()):
        content = _committed_bytes(repo, source_path)
        committed_by_destination[destination_path] = content
        manifest_files.append(
            {
                "path": destination_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
            }
        )
    manifest = {
        "schema_version": "1.0",
        "source_repository": "customs-data-platform",
        "source_commit": revision,
        "files": manifest_files,
    }
    serialized = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    return serialized, committed_by_destination


def _bundle_file(bundle_root: Path, relative_path: str) -> Path:
    relative_path = _canonical_relative_path(relative_path)
    candidate = bundle_root.joinpath(*PurePosixPath(relative_path).parts)
    if not _is_within(candidate, bundle_root):
        raise ProviderBuildError("provider bundle path escapes the bundle root")
    if candidate.is_symlink() or not candidate.is_file():
        raise ProviderBuildError(f"provider bundle file is unavailable: {relative_path}")
    current = candidate.parent
    while current != bundle_root:
        if current.is_symlink():
            raise ProviderBuildError("provider bundle cannot contain symlinked directories")
        current = current.parent
    return candidate


def _bundle_inventory(bundle_root: Path) -> set[str]:
    inventory: set[str] = set()
    for candidate in bundle_root.rglob("*"):
        if candidate.is_symlink():
            raise ProviderBuildError("provider bundle cannot contain symlinks")
        if candidate.is_file():
            resolved = candidate.resolve()
            if not _is_within(resolved, bundle_root):
                raise ProviderBuildError("provider bundle path escapes the bundle root")
            inventory.add(resolved.relative_to(bundle_root).as_posix())
    return inventory


def generate_provider_build(
    *,
    repository: Path,
    source_manifest_path: Path,
    retrieval_schema_path: Path,
    output_path: Path,
) -> dict[str, str]:
    repository = repository.resolve()
    source_manifest_path = source_manifest_path.resolve()
    retrieval_schema_path = retrieval_schema_path.resolve()
    output_path = output_path.resolve()
    bundle_root = source_manifest_path.parent

    if source_manifest_path != bundle_root / SOURCE_MANIFEST_NAME:
        raise ProviderBuildError("source manifest must be the canonical bundle manifest")
    canonical_schema_path = bundle_root.joinpath(
        *PurePosixPath(_retrieval_schema_destination()).parts
    )
    if retrieval_schema_path != canonical_schema_path:
        raise ProviderBuildError("retrieval schema must use its canonical bundle path")
    if output_path != bundle_root / PROVIDER_BUILD_NAME:
        raise ProviderBuildError("output must be the canonical provider-build path")
    if output_path.exists() or output_path.is_symlink():
        raise ProviderBuildError("output already exists")
    if _is_within(bundle_root, repository) or _is_within(repository, bundle_root):
        raise ProviderBuildError("provider bundle and Data repository must be disjoint")

    if _git(repository, "status", "--porcelain"):
        raise ProviderBuildError("Data repository must be clean")
    revision = _git(repository, "rev-parse", "HEAD")

    mappings = _expected_mappings(repository)
    expected_manifest_bytes, committed_files = _expected_manifest(
        repository,
        revision=revision,
        mappings=mappings,
    )
    try:
        manifest_bytes = _bundle_file(bundle_root, SOURCE_MANIFEST_NAME).read_bytes()
        json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderBuildError("source manifest or retrieval schema is unavailable") from exc
    if manifest_bytes != expected_manifest_bytes:
        raise ProviderBuildError(
            "source manifest is not the canonical committed provider allowlist"
        )

    expected_inventory = set(mappings) | (
        GENERATED_PROVIDER_CONTEXT_FILES - {PROVIDER_BUILD_NAME}
    )
    if _bundle_inventory(bundle_root) != expected_inventory:
        raise ProviderBuildError("provider bundle file inventory is not the safe builder allowlist")
    for destination_path, committed_content in committed_files.items():
        bundled_content = _bundle_file(bundle_root, destination_path).read_bytes()
        if bundled_content != committed_content:
            raise ProviderBuildError(
                f"provider bundle file differs from committed HEAD: {destination_path}"
            )

    schema_bytes = _bundle_file(
        bundle_root,
        _retrieval_schema_destination(),
    ).read_bytes()
    if schema_bytes != _committed_bytes(repository, RETRIEVAL_SCHEMA_SOURCE):
        raise ProviderBuildError("retrieval schema differs from canonical committed bytes")

    document = create_provider_build_document(
        provider_revision=revision,
        retrieval_schema=schema_bytes,
        source_manifest=manifest_bytes,
    )
    try:
        with output_path.open("xb") as stream:
            stream.write(serialized_document(document))
    except OSError as exc:
        raise ProviderBuildError("provider-build output could not be created exclusively") from exc
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--retrieval-schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    document = generate_provider_build(
        repository=args.repository,
        source_manifest_path=args.source_manifest,
        retrieval_schema_path=args.retrieval_schema,
        output_path=args.output,
    )
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
