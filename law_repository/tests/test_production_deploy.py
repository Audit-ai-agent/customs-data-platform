from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "build_production_context.py"
SPEC = importlib.util.spec_from_file_location("production_context", SCRIPT)
assert SPEC and SPEC.loader
context_builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = context_builder
SPEC.loader.exec_module(context_builder)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "provider"
    files = {
        ".dockerignore": "**/.env*\n.git\n",
        "pyproject.toml": (
            "[project]\nname='fixture'\nversion='1.0.0'\nrequires-python='==3.11.*'\n"
        ),
        "uv.lock": "version = 1\nrevision = 3\nrequires-python = '==3.11.*'\n",
        "law_repository/deploy/production/Dockerfile": (
            "ARG PYTHON_IMAGE\nFROM ${PYTHON_IMAGE}\nCOPY . /app\n"
        ),
        "law_repository/deploy/production/README.md": "production only\n",
        "contracts/evidence/v1/rag_retrieval_v1.schema.json": "{}\n",
        "contracts/evidence/v1/readiness_v1.schema.json": "{}\n",
        "contracts/evidence/v2/readiness_v2.schema.json": "{}\n",
        "law_repository/src/production.py": "print('production')\n",
        "law_repository/src/provider_identity.py": "PROVIDER = 'identity'\n",
        "law_repository/tests/not-runtime.txt": "excluded\n",
        "law_repository/migrations/not-runtime.sql": "excluded\n",
        ".env": "TOKEN=must-not-be-exported\n",
    }
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "Production Context Test")
    _git(repo, "config", "user.email", "production-context@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def test_production_context_exports_only_committed_runtime_allowlist(
    tmp_path: Path,
) -> None:
    repo = _fixture_repo(tmp_path)
    output = tmp_path / "production context"

    manifest = context_builder.build_context(repo, output)

    exported = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    assert exported == {
        ".dockerignore",
        "Dockerfile",
        "README.md",
        "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        "contracts/evidence/v1/readiness_v1.schema.json",
        "contracts/evidence/v2/readiness_v2.schema.json",
        "law_repository/src/production.py",
        "law_repository/src/provider_identity.py",
        "provider-build.json",
        "provider-runtime-build.json",
        "provider-runtime-lock.json",
        "pyproject.toml",
        "source-manifest.json",
        "uv.lock",
    }
    assert manifest["source_commit"] == _git(repo, "rev-parse", "HEAD")
    recorded = {item["path"]: item["sha256"] for item in manifest["files"]}
    for relative_path, expected_sha in recorded.items():
        assert hashlib.sha256((output / relative_path).read_bytes()).hexdigest() == expected_sha
    serialized = json.dumps(manifest)
    assert str(repo) not in serialized
    assert "must-not-be-exported" not in serialized
    identity = json.loads((output / "provider-build.json").read_text(encoding="utf-8"))
    assert identity["provider_revision"] == _git(repo, "rev-parse", "HEAD")
    assert (
        identity["source_manifest_sha256"]
        == hashlib.sha256((output / "source-manifest.json").read_bytes()).hexdigest()
    )
    runtime_identity = json.loads(
        (output / "provider-runtime-build.json").read_text(encoding="utf-8")
    )
    assert runtime_identity["provider_release"] == "evidence-api-v1.2.0"
    assert runtime_identity["provider_revision"] == identity["provider_revision"]
    runtime_lock = json.loads((output / "provider-runtime-lock.json").read_text(encoding="utf-8"))
    assert set(runtime_lock) == {
        "contract",
        "schema_version",
        "provider_release",
        "provider_revision",
        "provider_build_sha256",
        "retrieval_schema_sha256",
        "readiness_schema_sha256",
        "provider_source_manifest_sha256",
    }
    assert runtime_lock["contract"] == "evidence-runtime-release-provider-lock"
    assert runtime_lock["provider_revision"] == _git(repo, "rev-parse", "HEAD")
    assert runtime_lock["provider_build_sha256"] == runtime_identity["provider_build_sha256"]
    assert (
        runtime_lock["retrieval_schema_sha256"]
        == hashlib.sha256(
            (output / "contracts/evidence/v1/rag_retrieval_v1.schema.json").read_bytes()
        ).hexdigest()
    )
    assert (
        runtime_lock["readiness_schema_sha256"]
        == hashlib.sha256(
            (output / "contracts/evidence/v2/readiness_v2.schema.json").read_bytes()
        ).hexdigest()
    )
    assert (
        runtime_lock["provider_source_manifest_sha256"]
        == hashlib.sha256((output / "source-manifest.json").read_bytes()).hexdigest()
    )


def test_production_context_rejects_dirty_repo_and_in_repo_output(
    tmp_path: Path,
) -> None:
    repo = _fixture_repo(tmp_path)
    with pytest.raises(context_builder.ProductionContextError, match="outside"):
        context_builder.build_context(repo, repo / "context")

    (repo / "untracked-secret.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(context_builder.ProductionContextError, match="clean"):
        context_builder.build_context(repo, tmp_path / "outside")


def test_production_dockerfile_requires_immutable_inputs_and_non_root() -> None:
    dockerfile = (ROOT / "law_repository" / "deploy" / "production" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    lowered = dockerfile.lower()

    assert "arg uv_image\n" in lowered
    assert "arg python_image\n" in lowered
    assert "from ${uv_image}" in lowered
    assert "from ${python_image}" in lowered
    assert "uv sync --locked --no-dev" in lowered
    assert "hf_hub_offline=1" in lowered
    assert "transformers_offline=1" in lowered
    assert "user 65532:65532" in lowered
    assert 'cmd ["python", "-m", "src.production"]' in lowered
    assert "git clone" not in lowered
    assert "pip install" not in lowered
    assert "arg hf_token" not in lowered


def test_production_compose_does_not_publish_ports_or_create_team_stores() -> None:
    compose = (ROOT / "law_repository" / "deploy" / "production" / "compose.example.yml").read_text(
        encoding="utf-8"
    )
    lowered = compose.lower()

    assert "\n    ports:" not in lowered
    assert "read_only: true" in lowered
    assert "no-new-privileges:true" in lowered
    assert "cap_drop:" in lowered
    assert 'user: "65532:65532"' in lowered
    assert "evidence_service_token_file" in lowered
    assert "postgres_password_file" in lowered
    assert "qdrant_api_key_file" in lowered
    assert "neo4j_password_file" in lowered
    assert "postgres:" not in lowered
    assert "qdrant:" not in lowered
    assert "neo4j:" not in lowered
    assert "change_me" not in lowered
    assert "?token=" not in lowered
