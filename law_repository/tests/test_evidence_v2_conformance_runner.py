from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import requests
import uvicorn
from fastapi.testclient import TestClient

from tools.provider_build import (
    create_runtime_provider_build_document,
    create_runtime_provider_lock_document,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "run_evidence_v2_conformance_server.py"
SPEC = importlib.util.spec_from_file_location("evidence_v2_conformance_runner", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

_TOKEN = "test-only-file-token-" + ("a" * 40)
_ENV_TOKEN = "must-not-be-used-env-token-" + ("b" * 40)


def _write_bytes(root: Path, relative_path: str, payload: bytes) -> None:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


def _production_context(
    tmp_path: Path,
    *,
    source_commit: str | None = None,
) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "production-context"
    revision = "1" * 40
    retrieval_schema = b'{"title":"fixture retrieval v1"}\n'
    readiness_schema = b'{"title":"fixture readiness v2"}\n'
    source_manifest = (
        json.dumps(
            {
                "schema_version": "1.0",
                "source_repository": "customs-data-platform",
                "source_commit": source_commit or revision,
                "files": [{"path": "fixture", "sha256": "2" * 64, "bytes": 1}],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    build = create_runtime_provider_build_document(
        provider_revision=revision,
        retrieval_schema=retrieval_schema,
        source_manifest=source_manifest,
    )
    lock = create_runtime_provider_lock_document(
        runtime_provider_build=build,
        retrieval_schema=retrieval_schema,
        readiness_schema=readiness_schema,
        source_manifest=source_manifest,
    )
    _write_bytes(
        root,
        "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        retrieval_schema,
    )
    _write_bytes(
        root,
        "contracts/evidence/v2/readiness_v2.schema.json",
        readiness_schema,
    )
    _write_bytes(root, "source-manifest.json", source_manifest)
    _write_bytes(
        root,
        "provider-runtime-lock.json",
        (json.dumps(lock, sort_keys=True) + "\n").encode(),
    )
    return root, lock


def _token_file(tmp_path: Path, token: str = _TOKEN) -> Path:
    path = tmp_path / "service-token"
    path.write_text(token + "\n", encoding="ascii")
    return path


def _application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    context, lock = _production_context(tmp_path)
    token_file = _token_file(tmp_path)
    identity_output = tmp_path / "artifacts" / "test-only-identity.json"
    monkeypatch.setenv("EVIDENCE_SERVICE_TOKEN", _ENV_TOKEN)
    monkeypatch.setenv("EVIDENCE_SERVICE_TOKEN_FILE", str(tmp_path / "not-used"))
    application = runner.create_test_only_v2_conformance_application(
        production_context=context,
        service_token_file=token_file,
        identity_output=identity_output,
        host="127.0.0.1",
        port=18010,
    )
    return application, context, lock, identity_output


def _body(*, review_as_of: str = "2026-07-10") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": "req-test-only-v2",
        "case_id": "case-test-only-v2",
        "review_run_id": "run-test-only-v2",
        "review_as_of": review_as_of,
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 5,
        "items": [
            {
                "item_id": "invoice:1",
                "description": "Frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }
        ],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "required_import_documents",
    }


def _headers(identity) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        "X-Request-ID": "req-test-only-v2",
        **identity.binding_headers(),
    }


def test_parser_requires_explicit_test_only_ack_and_loopback_host(
    tmp_path: Path,
) -> None:
    common = [
        "--production-context",
        str(tmp_path),
        "--service-token-file",
        str(tmp_path / "token"),
        "--identity-output",
        str(tmp_path / "identity.json"),
        "--host",
        "127.0.0.1",
        "--port",
        "18010",
    ]
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(common)
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            ["--test-only-conformance", *common, "--service-token", _TOKEN]
        )
    non_loopback = [
        "--test-only-conformance",
        *common[:-4],
        "--host",
        "0.0.0.0",
        "--port",
        "18010",
    ]
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(non_loopback)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_field",
        "wrong_release",
        "wrong_build",
        "schema_bytes",
        "duplicate_key",
        "source_revision",
    ],
)
def test_provider_context_mismatch_fails_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    context, lock = _production_context(
        tmp_path,
        source_commit="3" * 40 if mutation == "source_revision" else None,
    )
    lock_path = context / "provider-runtime-lock.json"
    if mutation == "extra_field":
        lock["unexpected"] = "forbidden"
    elif mutation == "wrong_release":
        lock["provider_release"] = "evidence-api-v1.1.0"
    elif mutation == "wrong_build":
        lock["provider_build_sha256"] = "0" * 64
    elif mutation == "schema_bytes":
        (context / "contracts/evidence/v1/rag_retrieval_v1.schema.json").write_bytes(b"tampered")
    elif mutation == "duplicate_key":
        lock_path.write_text(
            '{"contract":"evidence-runtime-release-provider-lock",'
            '"contract":"evidence-runtime-release-provider-lock"}',
            encoding="utf-8",
        )
    if mutation in {"extra_field", "wrong_release", "wrong_build"}:
        lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(runner.TestOnlyConformanceConfigurationError):
        runner.load_verified_provider_runtime_lock(context)


@pytest.mark.parametrize(
    "payload",
    [
        " too-short",
        "a" * 31,
        "a" * 513,
        "valid-token-" + ("a" * 32) + "\nsecond-line",
        "유니코드-" + ("a" * 32),
    ],
)
def test_bearer_token_file_has_strict_size_and_format(
    tmp_path: Path,
    payload: str,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(payload, encoding="utf-8")
    with pytest.raises(runner.TestOnlyConformanceConfigurationError):
        runner.load_bearer_token_file(token_file)

    with pytest.raises(runner.TestOnlyConformanceConfigurationError):
        runner.load_bearer_token_file(Path("relative-token"))


def test_identity_is_deterministic_test_only_and_nonsecret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, context, lock_payload, identity_output = _application(
        tmp_path,
        monkeypatch,
    )
    verified = runner.load_verified_provider_runtime_lock(context)
    first = runner.derive_test_only_runtime_lease(verified)
    second = runner.derive_test_only_runtime_lease(verified)
    written = json.loads(identity_output.read_text(encoding="utf-8"))

    assert first == second
    assert first.identity.model_dump(mode="json") == written
    assert first.identity.provider_revision == lock_payload["provider_revision"]
    assert first.identity.provider_build_sha256 == lock_payload["provider_build_sha256"]
    assert first.identity.knowledge_snapshot_id.startswith("test-only-conformance.no-rag-quality.")
    assert first.identity.supported_review_as_of.isoformat() == "2026-07-10"
    assert _TOKEN not in identity_output.read_text(encoding="utf-8")
    assert _ENV_TOKEN not in identity_output.read_text(encoding="utf-8")
    assert application.state.test_only_production_shell is True
    assert application.state.test_only_runtime_conformance is True
    assert application.state.rag_quality_evaluated is False
    assert application.state.release_ready is False


def test_file_token_overrides_no_environment_and_test_client_v2_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _context, _lock, identity_output = _application(tmp_path, monkeypatch)
    identity = application.state.runtime_release_loader.acquire().identity
    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51000),
    ) as client:
        assert client.get("/internal/rag/v2/health/ready").status_code == 401
        env_auth = client.get(
            "/internal/rag/v2/health/ready",
            headers={"Authorization": f"Bearer {_ENV_TOKEN}"},
        )
        assert env_auth.status_code == 401
        ready = client.get(
            "/internal/rag/v2/health/ready",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        response = client.post(
            "/internal/rag/v2/retrieve",
            content=json.dumps(_body()).encode(),
            headers=_headers(identity),
        )
        wrong_date = client.post(
            "/internal/rag/v2/retrieve",
            content=json.dumps(_body(review_as_of="2026-07-11")).encode(),
            headers=_headers(identity),
        )
        mismatched_headers = _headers(identity)
        mismatched_headers["X-Evidence-Runtime-Release-SHA256"] = "0" * 64
        mismatch = client.post(
            "/internal/rag/v2/retrieve",
            content=json.dumps(_body()).encode(),
            headers=mismatched_headers,
        )

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "evidence_runtime_release": json.loads(identity_output.read_text(encoding="utf-8")),
    }
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["knowledge_snapshot_id"] == identity.knowledge_snapshot_id
    assert response.json()["results"][0]["coverage"] == "complete"
    assert response.json()["results"][0]["evidence"][0]["source_record_id"].startswith(
        "test_only_v2_conformance"
    )
    for name, expected in identity.binding_headers().items():
        assert response.headers[name] == expected
    assert wrong_date.status_code == 200
    assert wrong_date.json()["status"] == "failed"
    assert wrong_date.json()["errors"][0]["code"] == "UNSUPPORTED_REVIEW_AS_OF"
    assert mismatch.status_code == 412


def test_actual_uvicorn_socket_is_loopback_and_serves_exact_get_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _context, _lock, _identity_output = _application(tmp_path, monkeypatch)
    identity = application.state.runtime_release_loader.acquire().identity
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            log_level="critical",
            access_log=False,
            proxy_headers=False,
            server_header=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started
        ready = requests.get(
            f"http://127.0.0.1:{port}/internal/rag/v2/health/ready",
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=3,
        )
        response = requests.post(
            f"http://127.0.0.1:{port}/internal/rag/v2/retrieve",
            json=_body(),
            headers=_headers(identity),
            timeout=3,
        )
        assert ready.status_code == 200
        assert ready.json()["evidence_runtime_release"] == identity.model_dump(mode="json")
        assert response.status_code == 200
        assert response.json()["knowledge_snapshot_id"] == identity.knowledge_snapshot_id
        assert response.json()["status"] == "ok"
        for name, expected in identity.binding_headers().items():
            assert response.headers[name] == expected
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()


def test_output_cannot_mutate_context_or_replace_different_identity(
    tmp_path: Path,
) -> None:
    context, _lock = _production_context(tmp_path)
    verified = runner.load_verified_provider_runtime_lock(context)
    identity = runner.derive_test_only_runtime_lease(verified).identity

    with pytest.raises(runner.TestOnlyConformanceConfigurationError):
        runner.write_identity_json(
            identity,
            output=context / "identity.json",
            production_context=context,
        )

    output = tmp_path / "identity.json"
    output.write_text('{"different":true}\n', encoding="utf-8")
    with pytest.raises(runner.TestOnlyConformanceConfigurationError):
        runner.write_identity_json(
            identity,
            output=output,
            production_context=context,
        )
