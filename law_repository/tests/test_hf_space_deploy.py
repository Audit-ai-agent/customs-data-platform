from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "build_hf_space_context.py"
SPEC = importlib.util.spec_from_file_location("hf_space_context", SCRIPT)
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


def test_hf_space_dockerfile_has_no_remote_clone_or_token_argument() -> None:
    dockerfile = (ROOT / "law_repository" / "deploy" / "hf_space" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    deploy_readme = (ROOT / "law_repository" / "deploy" / "hf_space" / "README.md").read_text(
        encoding="utf-8"
    )
    combined = f"{dockerfile}\n{deploy_readme}".lower()

    assert dockerfile.splitlines()[0] == (
        "# syntax=docker/dockerfile:1.7@sha256:"
        "a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e"
    )
    assert (
        "FROM python:3.11-slim@sha256:"
        "db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93" in dockerfile
    )
    assert "vector2967" not in combined
    assert "git clone" not in combined
    assert "github_token" not in combined
    assert "github_pat" not in combined
    assert "copy law_repository/requirements.txt" in dockerfile.lower()
    assert "copy . /app" in dockerfile.lower()
    assert "workdir /app/law_repository" in dockerfile.lower()


def test_dockerignore_excludes_secret_and_local_runtime_boundaries() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".git" in patterns
    assert "**/.env*" in patterns
    assert "**/.venv" in patterns
    assert "**/data/raw" in patterns


def test_context_builder_exports_committed_allowlist_with_checksums(tmp_path: Path) -> None:
    repo = tmp_path / "provider"
    output = tmp_path / "space context"
    files = {
        ".dockerignore": "**/.env*\n",
        "law_repository/deploy/hf_space/Dockerfile": "COPY law_repository/src ./src\n",
        "law_repository/deploy/hf_space/README.md": "private space\n",
        "law_repository/requirements.txt": "fastapi==1\n",
        "contracts/evidence/v1/rag_retrieval_v1.schema.json": "{}\n",
        "contracts/evidence/v1/readiness_v1.schema.json": "{}\n",
        "law_repository/src/webapp.py": "print('ready')\n",
        "law_repository/src/webui.html": "<main>ready</main>\n",
        "law_repository/tests/not-runtime.txt": "excluded\n",
        ".env": "TOKEN=must-not-be-exported\n",
    }
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.name", "Context Test")
    _git(repo, "config", "user.email", "context@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")

    manifest = context_builder.build_context(repo, output)

    exported = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    assert exported == {
        ".dockerignore",
        "Dockerfile",
        "README.md",
        "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        "contracts/evidence/v1/readiness_v1.schema.json",
        "provider-build.json",
        "source-manifest.json",
        "law_repository/requirements.txt",
        "law_repository/src/webapp.py",
        "law_repository/src/webui.html",
    }
    assert manifest["source_commit"] == _git(repo, "rev-parse", "HEAD")
    recorded = {item["path"]: item["sha256"] for item in manifest["files"]}
    for relative_path, expected_sha in recorded.items():
        assert hashlib.sha256((output / relative_path).read_bytes()).hexdigest() == expected_sha
    serialized = json.dumps(manifest)
    assert str(repo) not in serialized
    assert "TOKEN" not in serialized
    provider_build = json.loads((output / "provider-build.json").read_text(encoding="utf-8"))
    assert provider_build["provider_revision"] == _git(repo, "rev-parse", "HEAD")
    assert provider_build["provider_release"] == "evidence-api-v1.1.0"
    assert (
        provider_build["source_manifest_sha256"]
        == hashlib.sha256((output / "source-manifest.json").read_bytes()).hexdigest()
    )
