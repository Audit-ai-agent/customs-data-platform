from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools.build_hf_space_context import build_context
from tools.generate_provider_build import generate_provider_build
from tools.provider_build import ProviderBuildError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "provider"
    files = {
        ".dockerignore": "**/.env*\n",
        "law_repository/deploy/hf_space/Dockerfile": "COPY law_repository/src ./src\n",
        "law_repository/deploy/hf_space/README.md": "private space\n",
        "pyproject.toml": (
            "[project]\nname='fixture'\nversion='1.0.0'\n"
            "requires-python='==3.11.*'\ndependencies=['fastapi==1','neo4j==1']\n"
            "[project.optional-dependencies]\nrag=['numpy==1']\n"
        ),
        "uv.lock": "version = 1\nrevision = 3\nrequires-python = '==3.11.*'\n",
        "contracts/evidence/v1/rag_retrieval_v1.schema.json": '{"title":"retrieval"}\n',
        "contracts/evidence/v1/readiness_v1.schema.json": '{"title":"readiness"}\n',
        "law_repository/src/webapp.py": "COMMITTED = True\n",
        "law_repository/src/evidence.py": "EVIDENCE = True\n",
    }
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "Provider Build Test")
    _git(repo, "config", "user.email", "provider-build@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _build_without_identity(repo: Path, bundle: Path) -> bytes:
    build_context(repo, bundle)
    identity_path = bundle / "provider-build.json"
    expected = identity_path.read_bytes()
    identity_path.unlink()
    return expected


def _generate(repo: Path, bundle: Path) -> dict[str, str]:
    return generate_provider_build(
        repository=repo,
        source_manifest_path=bundle / "source-manifest.json",
        retrieval_schema_path=(bundle / "contracts/evidence/v1/rag_retrieval_v1.schema.json"),
        output_path=bundle / "provider-build.json",
    )


def test_standalone_generator_accepts_only_the_safe_builder_bundle(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    bundle = tmp_path / "bundle"
    expected_identity = _build_without_identity(repo, bundle)

    document = _generate(repo, bundle)

    assert (bundle / "provider-build.json").read_bytes() == expected_identity
    assert document["provider_revision"] == _git(repo, "rev-parse", "HEAD")


def test_standalone_generator_rejects_arbitrary_webapp_bytes_even_if_manifest_matches(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path)
    bundle = tmp_path / "bundle"
    _build_without_identity(repo, bundle)
    webapp_path = bundle / "law_repository/src/webapp.py"
    arbitrary_webapp = b"ARBITRARY = 'not from committed HEAD'\n"
    webapp_path.write_bytes(arbitrary_webapp)

    manifest_path = bundle / "source-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(
        item for item in manifest["files"] if item["path"] == "law_repository/src/webapp.py"
    )
    entry["sha256"] = hashlib.sha256(arbitrary_webapp).hexdigest()
    entry["bytes"] = len(arbitrary_webapp)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ProviderBuildError,
        match="canonical committed provider allowlist",
    ):
        _generate(repo, bundle)


@pytest.mark.parametrize("input_name", ["schema", "output"])
def test_standalone_generator_rejects_noncanonical_or_self_overwriting_paths(
    tmp_path: Path,
    input_name: str,
) -> None:
    repo = _repository(tmp_path)
    bundle = tmp_path / "bundle"
    _build_without_identity(repo, bundle)
    schema_path = bundle / "contracts/evidence/v1/rag_retrieval_v1.schema.json"
    output_path = bundle / "provider-build.json"
    if input_name == "schema":
        external_schema = tmp_path / "external-schema.json"
        external_schema.write_bytes(schema_path.read_bytes())
        schema_path = external_schema
    else:
        output_path = bundle / "source-manifest.json"

    with pytest.raises(ProviderBuildError):
        generate_provider_build(
            repository=repo,
            source_manifest_path=bundle / "source-manifest.json",
            retrieval_schema_path=schema_path,
            output_path=output_path,
        )
