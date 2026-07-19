"""Generate an Evidence provider-build identity from a clean committed bundle."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

try:
    from tools.provider_build import (
        ProviderBuildError,
        create_provider_build_document,
        serialized_document,
    )
except ModuleNotFoundError:  # direct execution with tools/ as sys.path[0]
    from provider_build import (  # type: ignore[no-redef]
        ProviderBuildError,
        create_provider_build_document,
        serialized_document,
    )

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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


def generate_provider_build(
    *,
    repository: Path,
    source_manifest_path: Path,
    retrieval_schema_path: Path,
    output_path: Path,
) -> dict[str, str]:
    repository = repository.resolve()
    if _git(repository, "status", "--porcelain"):
        raise ProviderBuildError("Data repository must be clean")
    revision = _git(repository, "rev-parse", "HEAD")
    try:
        manifest_bytes = source_manifest_path.resolve().read_bytes()
        manifest = json.loads(manifest_bytes)
        schema_bytes = retrieval_schema_path.resolve().read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderBuildError("source manifest or retrieval schema is unavailable") from exc
    if not isinstance(manifest, dict) or manifest.get("source_commit") != revision:
        raise ProviderBuildError("source manifest does not attest the current Data HEAD")
    document = create_provider_build_document(
        provider_revision=revision,
        retrieval_schema=schema_bytes,
        source_manifest=manifest_bytes,
    )
    output_path = output_path.resolve()
    if output_path.exists():
        raise ProviderBuildError("output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(serialized_document(document))
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
