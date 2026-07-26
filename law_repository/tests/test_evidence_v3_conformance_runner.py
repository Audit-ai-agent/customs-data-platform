from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import requests
import uvicorn
from fastapi.testclient import TestClient

from src.deadline import RequestDeadline
from tools.provider_build import (
    create_runtime_provider_build_document,
    create_runtime_provider_lock_document,
)
from tools.run_evidence_v3_conformance_server import (
    TestOnlyConformanceConfigurationError as _ConfigurationError,
    build_parser,
    create_test_only_v3_conformance_application,
)

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-only-v3-file-token-" + ("a" * 40)


def _write(root: Path, relative: str, payload: bytes) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _production_context(tmp_path: Path) -> Path:
    root = tmp_path / "production-context"
    revision = "1" * 40
    retrieval_schema = b'{"title":"fixture retrieval v1"}\n'
    readiness_schema = b'{"title":"fixture readiness v2"}\n'
    source_manifest = (
        json.dumps(
            {
                "schema_version": "1.0",
                "source_repository": "customs-data-platform",
                "source_commit": revision,
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
    _write(root, "contracts/evidence/v1/rag_retrieval_v1.schema.json", retrieval_schema)
    _write(root, "contracts/evidence/v2/readiness_v2.schema.json", readiness_schema)
    for name in (
        "rag_retrieval_v3.schema.json",
        "readiness_v3.schema.json",
        "requirement_policy_v1.schema.json",
    ):
        _write(
            root,
            f"contracts/evidence/v3/{name}",
            (ROOT / "contracts/evidence/v3" / name).read_bytes(),
        )
    _write(root, "source-manifest.json", source_manifest)
    _write(root, "provider-runtime-lock.json", (json.dumps(lock, sort_keys=True) + "\n").encode())
    return root


def _token_file(tmp_path: Path) -> Path:
    path = (tmp_path / "service-token").resolve()
    path.write_text(TOKEN + "\n", encoding="ascii")
    return path


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    context = _production_context(tmp_path)
    identity_output = tmp_path / "artifacts/test-only-v3-identity.json"
    monkeypatch.setenv("EVIDENCE_SERVICE_TOKEN", "must-not-be-used-" + ("b" * 40))
    monkeypatch.setenv("EVIDENCE_REQUIREMENT_POLICY_RELEASE_FILE", "must-not-be-used")
    application = create_test_only_v3_conformance_application(
        production_context=context,
        service_token_file=_token_file(tmp_path),
        identity_output=identity_output,
        host="127.0.0.1",
        port=18011,
    )
    return application, identity_output


def _body() -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "request_id": "req-test-only-v3",
        "case_id": "case-test-only-v3",
        "review_run_id": "run-test-only-v3",
        "review_as_of": "2026-07-10",
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


def _no_requirement_body() -> dict[str, Any]:
    body = _body()
    body["request_id"] = "req-local-uat-clean"
    body["case_id"] = "case-local-uat-clean"
    body["review_run_id"] = "run-local-uat-clean"
    body["items"][0]["description"] = "Stainless steel bolts"
    body["items"][0]["hs_code"] = "7318151000"
    body["query_intent"] = "required_import_documents_and_law_support"
    return body


def test_parser_requires_ack_file_token_and_loopback(tmp_path: Path) -> None:
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
        "18011",
    ]
    with pytest.raises(SystemExit):
        build_parser().parse_args(common)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--test-only-conformance", *common, "--service-token", TOKEN])
    for forbidden_host in ("0.0.0.0", "localhost"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                [
                    "--test-only-conformance",
                    *common[:-4],
                    "--host",
                    forbidden_host,
                    "--port",
                    "18011",
                ]
            )


def test_factory_rejects_localhost_alias_even_without_cli_parser(
    tmp_path: Path,
) -> None:
    with pytest.raises(_ConfigurationError, match="loopback-only"):
        create_test_only_v3_conformance_application(
            production_context=_production_context(tmp_path),
            service_token_file=_token_file(tmp_path),
            identity_output=tmp_path / "identity.json",
            host="localhost",
            port=18011,
        )


def test_fixture_identity_is_explicitly_non_release_and_has_17_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, identity_output = _application(tmp_path, monkeypatch)
    identity = application.state.runtime_v3_release_loader.acquire().identity
    assert len(identity.binding_headers()) == 17
    assert identity.base_runtime_release.knowledge_snapshot_id.startswith(
        "test-only-conformance.no-rag-quality"
    )
    assert identity.requirement_policy_release.startswith("test-only-conformance")
    assert application.state.rag_quality_evaluated is False
    assert application.state.domain_reviewed is False
    assert application.state.release_ready is False
    assert application.state.automatic_completion_eligible is False
    assert json.loads(identity_output.read_text(encoding="utf-8")) == identity.model_dump(
        mode="json"
    )


def test_local_user_uat_profile_has_distinct_identity_and_clean_covered_case(
    tmp_path: Path,
) -> None:
    context = _production_context(tmp_path)
    token_file = _token_file(tmp_path)
    wire = create_test_only_v3_conformance_application(
        production_context=context,
        service_token_file=token_file,
        identity_output=tmp_path / "wire-identity.json",
        host="127.0.0.1",
        port=18011,
    )
    uat = create_test_only_v3_conformance_application(
        production_context=context,
        service_token_file=token_file,
        identity_output=tmp_path / "uat-identity.json",
        host="127.0.0.1",
        port=18012,
        profile="local-user-uat",
    )
    wire_identity = wire.state.runtime_v3_release_loader.acquire().identity
    uat_lease = uat.state.runtime_v3_release_loader.acquire()
    assert uat_lease.identity != wire_identity
    assert uat.state.diagnostic_profile == "local-user-uat"
    assert uat.state.no_requirement_positive_control_hsk10 == "7318151000"
    backend = uat.state.runtime_backend_factory(
        uat_lease.base_lease,
        RequestDeadline.start(
            started_at=time.monotonic(),
            request_timeout_seconds=10,
            store_timeout_seconds=1,
            model_timeout_seconds=1,
            completion_reserve_seconds=0.25,
        ),
    )
    response = backend.lookup_requirements(
        uat_lease.identity.base_runtime_release.knowledge_snapshot_id,
        "7318151000",
        "import",
        uat_lease.identity.base_runtime_release.supported_review_as_of,
    )
    assert response.covered is True
    assert response.rows == ()


def test_local_user_uat_http_reports_diagnostic_mode_and_clean_covered_result(
    tmp_path: Path,
) -> None:
    application = create_test_only_v3_conformance_application(
        production_context=_production_context(tmp_path),
        service_token_file=_token_file(tmp_path),
        identity_output=tmp_path / "uat-http-identity.json",
        host="127.0.0.1",
        port=18012,
        profile="local-user-uat",
    )
    identity = application.state.runtime_v3_release_loader.acquire().identity
    body = _no_requirement_body()
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "X-Request-ID": body["request_id"],
        **identity.binding_headers(),
    }
    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        ready = client.get(
            "/internal/rag/v3/health/uat-ready",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        response = client.post(
            "/internal/rag/v3/retrieve",
            json=body,
            headers=headers,
        )
    assert ready.status_code == 200
    assert ready.json()["mode"] == "LOCAL_USER_UAT_DIAGNOSTIC"
    assert ready.json()["release_ready"] is False
    assert ready.json()["automatic_completion_eligible"] is False
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["results"][0]["coverage"] == "complete"
    assert payload["results"][0]["requirements_found"] is False
    assert len(payload["results"][0]["evidence"]) == 1
    evidence = payload["results"][0]["evidence"][0]
    assert evidence["source_type"] == "law_article"
    assert evidence["eligibility"] == "supporting"
    assert evidence["matched_hs_code"] is None
    assert evidence["retrieval_methods"] == ["bm25"]
    assert "LOCAL UAT" in evidence["title"]


def test_output_cannot_be_written_inside_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _production_context(tmp_path)
    with pytest.raises(_ConfigurationError, match="outside"):
        create_test_only_v3_conformance_application(
            production_context=context,
            service_token_file=_token_file(tmp_path),
            identity_output=context / "identity.json",
            host="127.0.0.1",
            port=18011,
        )


def test_actual_uvicorn_socket_serves_exact_get_and_17_header_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _identity_output = _application(tmp_path, monkeypatch)
    identity = application.state.runtime_v3_release_loader.acquire().identity
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
            date_header=False,
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
            f"http://127.0.0.1:{port}/internal/rag/v3/health/ready",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=3,
        )
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "X-Request-ID": "req-test-only-v3",
            **identity.binding_headers(),
        }
        response = requests.post(
            f"http://127.0.0.1:{port}/internal/rag/v3/retrieve",
            json=_body(),
            headers=headers,
            timeout=3,
        )
        mismatch_headers = dict(headers)
        mismatch_headers["X-Evidence-Reviewer-Trust-Store-SHA256"] = "0" * 64
        mismatch = requests.post(
            f"http://127.0.0.1:{port}/internal/rag/v3/retrieve",
            json=_body(),
            headers=mismatch_headers,
            timeout=3,
        )
        assert ready.status_code == 200
        assert ready.json()["evidence_runtime_release"] == identity.model_dump(mode="json")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.headers["X-Request-ID"] == "req-test-only-v3"
        assert response.headers["X-Service-Request-ID"]
        requirement = response.json()["results"][0]["evidence"][0]
        assert requirement["requirement_verification"]["approved"] is True
        for name, expected in identity.binding_headers().items():
            assert response.headers[name] == expected
        assert mismatch.status_code == 412
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()
