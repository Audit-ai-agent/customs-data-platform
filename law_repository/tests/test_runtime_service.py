from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.config import Settings as DataStoreSettings
from src.conformance import TestOnlyConformanceEvidenceBackend
from src.deadline import RequestDeadline
from src.runtime_service import (
    create_runtime_v2_application,
    create_test_runtime_v1_application,
)
from src.runtime_settings import (
    RuntimeConfigurationError,
    RuntimeSettings,
    apply_runtime_data_store_secrets,
    validate_runtime_data_store_settings,
)
from src.provider_identity import (
    HEADER_PROVIDER_REVISION,
    ProviderBuildIdentity,
    create_provider_build_identity,
)

_TOKEN = "service-token-" + ("a" * 40)
_RETRIEVE_PATH = "/internal/rag/v1/retrieve"
_LIVENESS_PATH = "/internal/rag/v1/health/live"
_METRICS_PATH = "/internal/rag/v1/metrics"
_OPENAPI_PATH = "/internal/rag/v1/openapi.json"


def _identity() -> ProviderBuildIdentity:
    return create_provider_build_identity(
        provider_revision="1" * 40,
        schema_sha256="2" * 64,
        source_manifest_sha256="3" * 64,
    )


def _settings(**overrides: Any) -> RuntimeSettings:
    values: dict[str, Any] = {
        "service_token": _TOKEN,
        "transport_mode": "direct_tls",
        "max_concurrency": 2,
        "request_deadline_seconds": 5,
        "store_operation_timeout_seconds": 1,
        "model_inference_timeout_seconds": 1,
        "model_termination_timeout_seconds": 1,
        "shutdown_grace_seconds": 10,
    }
    values.update(overrides)
    return RuntimeSettings(_env_file=None, **values)


def _request_body(*, request_id: str = "req-production-1") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "case_id": "case-production",
        "review_run_id": "run-production",
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


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _retrieval_headers(
    *,
    request_id: str = "req-production-1",
) -> dict[str, str]:
    return {
        **_auth_headers(),
        **_identity().binding_headers(),
        "Content-Type": "application/json; charset=utf-8",
        "X-Request-ID": request_id,
    }


def _app(
    *,
    settings: RuntimeSettings | None = None,
    backend: TestOnlyConformanceEvidenceBackend | None = None,
):
    return create_test_runtime_v1_application(
        service_settings=settings or _settings(),
        identity=_identity(),
        backend=backend or TestOnlyConformanceEvidenceBackend(),
    )


def test_runtime_settings_require_one_strong_service_secret(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        RuntimeSettings(
            _env_file=None,
            transport_mode="direct_tls",
        )
    with pytest.raises(ValidationError, match="exactly one"):
        RuntimeSettings(
            _env_file=None,
            service_token=_TOKEN,
            service_token_file=tmp_path / "token",
            transport_mode="direct_tls",
        )
    weak = RuntimeSettings(
        _env_file=None,
        service_token="short",
        transport_mode="direct_tls",
    )
    with pytest.raises(RuntimeConfigurationError, match="32-512"):
        weak.resolve_secrets()

    secret_file = tmp_path / "service-token"
    secret_file.write_text(_TOKEN + "\n", encoding="ascii")
    from_file = RuntimeSettings(
        _env_file=None,
        service_token_file=secret_file.resolve(),
        transport_mode="direct_tls",
    )
    assert from_file.resolve_secrets().service_token == _TOKEN.encode("ascii")
    assert _TOKEN not in repr(from_file)


def test_loopback_dev_is_explicit_and_cannot_bind_wildcard() -> None:
    with pytest.raises(ValidationError, match="requires"):
        RuntimeSettings(
            _env_file=None,
            service_token=_TOKEN,
            transport_mode="loopback_dev",
            bind_host="127.0.0.1",
        )
    with pytest.raises(ValidationError, match="loopback"):
        RuntimeSettings(
            _env_file=None,
            service_token=_TOKEN,
            transport_mode="loopback_dev",
            allow_insecure_loopback_dev=True,
            bind_host="0.0.0.0",
        )


def test_trusted_proxy_mode_rejects_missing_or_global_networks() -> None:
    with pytest.raises(ValidationError, match="TRUSTED_PROXY_CIDRS"):
        RuntimeSettings(
            _env_file=None,
            service_token=_TOKEN,
            transport_mode="trusted_proxy_tls",
        )
    with pytest.raises(ValidationError, match="entire address space"):
        RuntimeSettings(
            _env_file=None,
            service_token=_TOKEN,
            transport_mode="trusted_proxy_tls",
            trusted_proxy_cidrs="0.0.0.0/0",
        )


def test_data_store_validation_rejects_weak_or_cleartext_remote_settings() -> None:
    service = _settings()
    weak = DataStoreSettings(_env_file=None)
    with pytest.raises(RuntimeConfigurationError, match="PostgreSQL password"):
        validate_runtime_data_store_settings(service, weak)

    cleartext = DataStoreSettings(
        _env_file=None,
        postgres_host="db.internal",
        postgres_password="strong-postgres-secret",
        postgres_sslmode="",
        qdrant_url="http://qdrant.internal:6333",
        neo4j_uri="bolt://neo4j.internal:7687",
        neo4j_password="strong-neo4j-secret",
    )
    with pytest.raises(RuntimeConfigurationError, match="PostgreSQL"):
        validate_runtime_data_store_settings(service, cleartext)

    encrypted = DataStoreSettings(
        _env_file=None,
        postgres_host="db.internal",
        postgres_password="strong-postgres-secret",
        postgres_sslmode="verify-full",
        qdrant_url="https://qdrant.internal:6333",
        qdrant_api_key="strong-qdrant-api-key",
        neo4j_uri="neo4j+s://neo4j.internal:7687",
        neo4j_password="strong-neo4j-secret",
    )
    validate_runtime_data_store_settings(service, encrypted)


def test_mounted_data_store_secrets_are_resolved_without_logging(
    tmp_path: Path,
) -> None:
    paths = {}
    for name, value in {
        "postgres": "postgres-file-secret",
        "qdrant": "qdrant-file-secret",
        "neo4j": "neo4j-file-secret",
    }.items():
        path = tmp_path / name
        path.write_text(value + "\n", encoding="utf-8")
        paths[name] = path.resolve()
    service = _settings(
        postgres_password_file=paths["postgres"],
        qdrant_api_key_file=paths["qdrant"],
        neo4j_password_file=paths["neo4j"],
    )
    data = DataStoreSettings(_env_file=None)

    apply_runtime_data_store_secrets(service, data)

    assert data.postgres_password == "postgres-file-secret"
    assert data.qdrant_api_key == "qdrant-file-secret"
    assert data.neo4j_password == "neo4j-file-secret"
    serialized = repr(service)
    assert "postgres-file-secret" not in serialized
    assert "qdrant-file-secret" not in serialized
    assert "neo4j-file-secret" not in serialized


def test_real_runtime_factory_cannot_enable_fixture_from_environment(
    monkeypatch,
) -> None:
    from src import runtime_service
    from src.generation_evidence import GenerationEvidenceBackend

    safe_values = {
        "postgres_host": "db.internal",
        "postgres_hostaddr": "192.0.2.10",
        "postgres_password": "runtime-postgres-credential",
        "postgres_sslmode": "verify-full",
        "qdrant_url": "https://qdrant.internal:6333",
        "qdrant_api_key": "runtime-qdrant-credential",
        "neo4j_uri": "neo4j+s://neo4j.internal:7687",
        "neo4j_password": "runtime-neo4j-credential",
    }
    for name, value in safe_values.items():
        monkeypatch.setattr(runtime_service.data_store_settings, name, value)
    monkeypatch.setenv("EVIDENCE_BACKEND", "conformance")
    monkeypatch.setenv("EVIDENCE_TEST_FIXTURE", "true")

    app = create_runtime_v2_application(_settings())

    assert app.state.test_only_runtime_conformance is False
    assert app.state.test_only_runtime_shell is False
    paths = set(app.openapi()["paths"])
    assert "/internal/rag/v1/retrieve" not in paths
    assert "/internal/rag/v2/retrieve" in paths
    lease = SimpleNamespace(identity=SimpleNamespace())
    deadline = RequestDeadline.start(
        started_at=time.monotonic(),
        request_timeout_seconds=5,
        store_timeout_seconds=1,
        model_timeout_seconds=1,
        completion_reserve_seconds=0.25,
    )
    assert isinstance(
        app.state.runtime_backend_factory(lease, deadline),
        GenerationEvidenceBackend,
    )


def test_liveness_is_public_but_still_requires_tls() -> None:
    app = _app()
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.get("/internal/rag/v1/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-service-request-id"].startswith("svc-")

    with TestClient(app, base_url="http://evidence.internal") as client:
        response = client.get("/internal/rag/v1/health/live")
        assert response.status_code == 426
        assert response.json()["error"]["code"] == "TLS_REQUIRED"


def test_readiness_requires_exact_bearer_and_query_tokens_are_forbidden() -> None:
    app = _app()
    with TestClient(app, base_url="https://evidence.internal") as client:
        missing = client.get("/internal/rag/v1/health/ready")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"

        wrong = client.get(
            "/internal/rag/v1/health/ready",
            headers={"Authorization": "Bearer " + ("z" * 48)},
        )
        assert wrong.status_code == 401

        query = client.get(
            f"/internal/rag/v1/health/ready?token={_TOKEN}",
            headers=_auth_headers(),
        )
        assert query.status_code == 400
        assert query.json()["error"]["code"] == "QUERY_PARAMETERS_FORBIDDEN"

        accepted = client.get(
            "/internal/rag/v1/health/ready",
            headers=_auth_headers(),
        )
        assert accepted.status_code == 200


def test_duplicate_authorization_fails_closed() -> None:
    app = _app()
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.get(
            "/internal/rag/v1/health/ready",
            headers=[
                ("Authorization", f"Bearer {_TOKEN}"),
                ("Authorization", f"Bearer {_TOKEN}"),
            ],
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_raw_duplicate_authorization_is_rejected_before_body() -> None:
    app = _app()

    async def run():
        async with app.router.lifespan_context(app):
            return await _invoke(
                app,
                method="POST",
                path="/internal/rag/v1/retrieve",
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
                    (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
                ],
                receive_must_not_run=True,
            )

    messages = asyncio.run(run())
    assert _response_status(messages) == 401
    assert _response_json(messages)["error"]["code"] == "UNAUTHORIZED"


def test_successful_retrieval_preserves_contract_and_binding_headers() -> None:
    app = _app()
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.post(
            "/internal/rag/v1/retrieve",
            content=json.dumps(_request_body()).encode("utf-8"),
            headers=_retrieval_headers(),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["request_id"] == "req-production-1"
    assert response.headers["x-service-request-id"].startswith("svc-")
    assert response.headers["x-service-request-id"] != "req-production-1"
    for name, value in _identity().binding_headers().items():
        assert response.headers[name] == value


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_code"),
    [
        (
            {"Content-Type": "text/plain"},
            415,
            "UNSUPPORTED_MEDIA_TYPE",
        ),
        (
            {
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            415,
            "UNSUPPORTED_CONTENT_ENCODING",
        ),
    ],
)
def test_retrieval_rejects_unsafe_media_before_backend(
    headers: dict[str, str],
    expected_status: int,
    expected_code: str,
) -> None:
    backend = TestOnlyConformanceEvidenceBackend()
    app = _app(backend=backend)
    request_headers = {
        **_auth_headers(),
        **_identity().binding_headers(),
        "X-Request-ID": "req-production-1",
        **headers,
    }
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.post(
            "/internal/rag/v1/retrieve",
            content=b"not-consumed",
            headers=request_headers,
        )
    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert backend.retrieve_calls == 0


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (
            b'{"request_id":"req-production-1","request_id":"duplicate"}',
            "INVALID_JSON",
        ),
        (
            b'{"request_id":"req-production-1","top_k":NaN}',
            "INVALID_JSON",
        ),
        (
            b'{"request_id":"different"}',
            "REQUEST_ID_MISMATCH",
        ),
    ],
)
def test_strict_json_and_request_id_binding(
    body: bytes,
    expected_code: str,
) -> None:
    backend = TestOnlyConformanceEvidenceBackend()
    app = _app(backend=backend)
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.post(
            "/internal/rag/v1/retrieve",
            content=body,
            headers=_retrieval_headers(),
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code
    assert backend.retrieve_calls == 0


async def _invoke(
    app,
    *,
    method: str,
    path: str,
    raw_headers: list[tuple[bytes, bytes]],
    chunks: list[bytes] | None = None,
    receive_must_not_run: bool = False,
    client_host: str = "127.0.0.1",
) -> list[dict[str, Any]]:
    pending = list(chunks or [b""])
    sent: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": raw_headers,
        "client": (client_host, 12345),
        "server": ("127.0.0.1", 443),
        "root_path": "",
        "state": {},
    }

    async def receive():
        if receive_must_not_run:
            raise AssertionError("request body must not be received")
        body = pending.pop(0) if pending else b""
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(pending),
        }

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def test_trusted_proxy_requires_exact_cidr_and_single_https_assertion() -> None:
    app = _app(
        settings=_settings(
            transport_mode="trusted_proxy_tls",
            trusted_proxy_cidrs="10.42.0.0/24",
        )
    )

    async def run():
        async with app.router.lifespan_context(app):
            accepted = await _invoke(
                app,
                method="GET",
                path=_LIVENESS_PATH,
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"x-forwarded-proto", b"https"),
                ],
                client_host="10.42.0.9",
            )
            untrusted = await _invoke(
                app,
                method="GET",
                path=_LIVENESS_PATH,
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"x-forwarded-proto", b"https"),
                ],
                client_host="10.43.0.9",
            )
            duplicate = await _invoke(
                app,
                method="GET",
                path=_LIVENESS_PATH,
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"x-forwarded-proto", b"https"),
                    (b"x-forwarded-proto", b"https"),
                ],
                client_host="10.42.0.9",
            )
            return accepted, untrusted, duplicate

    accepted, untrusted, duplicate = asyncio.run(run())
    assert _response_status(accepted) == 200
    assert _response_status(untrusted) == 426
    assert _response_json(untrusted)["error"]["code"] == "TLS_REQUIRED"
    assert _response_status(duplicate) == 400
    assert _response_json(duplicate)["error"]["code"] == "INVALID_HEADERS"


def _raw_retrieval_headers(
    *,
    content_length: int | None = None,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"host", b"evidence.internal"),
        (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
        (b"content-type", b"application/json"),
        (b"x-request-id", b"req-production-1"),
    ]
    headers.extend(
        (name.lower().encode("ascii"), value.encode("ascii"))
        for name, value in _identity().binding_headers().items()
    )
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return headers


def _response_status(messages: list[dict[str, Any]]) -> int:
    return next(
        int(message["status"]) for message in messages if message["type"] == "http.response.start"
    )


def _response_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return json.loads(body)


def test_declared_oversized_body_is_rejected_without_receive() -> None:
    app = _app(settings=_settings(max_request_body_bytes=4_096))

    async def run():
        async with app.router.lifespan_context(app):
            return await _invoke(
                app,
                method="POST",
                path="/internal/rag/v1/retrieve",
                raw_headers=_raw_retrieval_headers(content_length=4_097),
                receive_must_not_run=True,
            )

    messages = asyncio.run(run())
    assert _response_status(messages) == 413
    assert _response_json(messages)["error"]["code"] == "REQUEST_TOO_LARGE"


def test_streamed_oversized_body_is_bounded_without_content_length() -> None:
    backend = TestOnlyConformanceEvidenceBackend()
    app = _app(
        settings=_settings(max_request_body_bytes=4_096),
        backend=backend,
    )

    async def run():
        async with app.router.lifespan_context(app):
            return await _invoke(
                app,
                method="POST",
                path="/internal/rag/v1/retrieve",
                raw_headers=_raw_retrieval_headers(),
                chunks=[b"{" + (b"x" * 3_000), b"x" * 2_000],
            )

    messages = asyncio.run(run())
    assert _response_status(messages) == 413
    assert _response_json(messages)["error"]["code"] == "REQUEST_TOO_LARGE"
    assert backend.retrieve_calls == 0


@pytest.mark.parametrize("mode", ["duplicate_length", "length_and_transfer"])
def test_ambiguous_request_framing_fails_before_receive(mode: str) -> None:
    headers = _raw_retrieval_headers(content_length=100)
    expected = "INVALID_HEADERS"
    if mode == "duplicate_length":
        headers.append((b"content-length", b"100"))
    else:
        headers.append((b"transfer-encoding", b"chunked"))
        expected = "AMBIGUOUS_REQUEST_FRAMING"
    app = _app()

    async def run():
        async with app.router.lifespan_context(app):
            return await _invoke(
                app,
                method="POST",
                path="/internal/rag/v1/retrieve",
                raw_headers=headers,
                receive_must_not_run=True,
            )

    messages = asyncio.run(run())
    assert _response_status(messages) == 400
    assert _response_json(messages)["error"]["code"] == expected


def test_duplicate_binding_fails_before_receive_and_backend() -> None:
    backend = TestOnlyConformanceEvidenceBackend()
    headers = _raw_retrieval_headers(content_length=100)
    headers.append(
        (
            HEADER_PROVIDER_REVISION.lower().encode("ascii"),
            _identity().provider_revision.encode("ascii"),
        )
    )
    app = _app(backend=backend)

    async def run():
        async with app.router.lifespan_context(app):
            return await _invoke(
                app,
                method="POST",
                path="/internal/rag/v1/retrieve",
                raw_headers=headers,
                receive_must_not_run=True,
            )

    messages = asyncio.run(run())
    assert _response_status(messages) == 412
    assert _response_json(messages)["error"]["code"] == "PROVIDER_BINDING_MISMATCH"
    assert backend.retrieve_calls == 0


class _BlockingBackend(TestOnlyConformanceEvidenceBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def current_snapshot_id(self) -> str:
        self.started.set()
        assert self.release.wait(timeout=5)
        return super().current_snapshot_id()


def test_concurrency_limit_rejects_without_unbounded_queue() -> None:
    backend = _BlockingBackend()
    app = _app(
        settings=_settings(max_concurrency=1),
        backend=backend,
    )
    body = json.dumps(_request_body()).encode("utf-8")

    async def run():
        async with app.router.lifespan_context(app):
            first = asyncio.create_task(
                _invoke(
                    app,
                    method="POST",
                    path="/internal/rag/v1/retrieve",
                    raw_headers=_raw_retrieval_headers(content_length=len(body)),
                    chunks=[body],
                )
            )
            started = await asyncio.to_thread(backend.started.wait, 2)
            assert started
            second = await _invoke(
                app,
                method="GET",
                path="/internal/rag/v1/health/ready",
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
                ],
            )
            backend.release.set()
            return await first, second

    first, second = asyncio.run(run())
    assert _response_status(first) == 200
    assert _response_status(second) == 503
    assert _response_json(second)["error"]["code"] == "SERVICE_BUSY"


class _SlowBackend(TestOnlyConformanceEvidenceBackend):
    def current_snapshot_id(self) -> str:
        time.sleep(1.2)
        return super().current_snapshot_id()


def test_deadline_returns_504_and_keeps_capacity_until_work_finishes() -> None:
    app = _app(
        settings=_settings(
            max_concurrency=1,
            request_deadline_seconds=1,
            shutdown_grace_seconds=2,
        ),
        backend=_SlowBackend(),
    )
    body = json.dumps(_request_body()).encode("utf-8")

    async def run():
        async with app.router.lifespan_context(app):
            timed_out = await _invoke(
                app,
                method="POST",
                path="/internal/rag/v1/retrieve",
                raw_headers=_raw_retrieval_headers(content_length=len(body)),
                chunks=[body],
            )
            busy = await _invoke(
                app,
                method="GET",
                path="/internal/rag/v1/health/ready",
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"authorization", f"Bearer {_TOKEN}".encode("ascii")),
                ],
            )
            await asyncio.sleep(0.3)
            return timed_out, busy

    timed_out, busy = asyncio.run(run())
    assert _response_status(timed_out) == 504
    assert _response_json(timed_out)["error"]["code"] == "DEADLINE_EXCEEDED"
    assert _response_status(busy) == 503
    assert _response_json(busy)["error"]["code"] == "SERVICE_BUSY"


def test_metrics_openapi_and_logs_do_not_expose_body_or_token(caplog) -> None:
    caplog.set_level("INFO", logger="customs_evidence.runtime")
    app = _app(settings=_settings(enable_openapi=True))
    with TestClient(app, base_url="https://evidence.internal") as client:
        request = _request_body(request_id="req-safe-observability")
        request["case_id"] = "case-secret-canary"
        request["items"][0]["description"] = "document-secret-canary"
        response = client.post(
            "/internal/rag/v1/retrieve",
            content=json.dumps(request).encode("utf-8"),
            headers=_retrieval_headers(request_id="req-safe-observability"),
        )
        assert response.status_code == 200

        metrics = client.get(_METRICS_PATH, headers=_auth_headers())
        assert metrics.status_code == 200
        assert "evidence_http_requests_total" in metrics.text
        assert "case-secret-canary" not in metrics.text
        assert "document-secret-canary" not in metrics.text
        assert _TOKEN not in metrics.text

        openapi = client.get(_OPENAPI_PATH, headers=_auth_headers())
        assert openapi.status_code == 200
        schema = openapi.json()
        assert schema["components"]["securitySchemes"]["ServiceBearer"]["scheme"] == "bearer"
        assert schema["paths"][_RETRIEVE_PATH]["post"]["security"] == [{"ServiceBearer": []}]
        assert "security" not in schema["paths"][_LIVENESS_PATH]["get"]

    logs = caplog.text
    assert "req-safe-observability" not in logs
    assert "case-secret-canary" not in logs
    assert "document-secret-canary" not in logs
    assert _TOKEN not in logs


def test_safe_readiness_error_does_not_return_or_log_exception_secret(caplog) -> None:
    class FailingBackend(TestOnlyConformanceEvidenceBackend):
        def readiness(self):
            raise RuntimeError("postgresql://operator:password@db.internal/db?token=secret")

    caplog.set_level("INFO")
    app = _app(backend=FailingBackend(effective_date=date(2026, 7, 10)))
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.get(
            "/internal/rag/v1/health/ready",
            headers=_auth_headers(),
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "password" not in response.text
    assert "token" not in response.text
    assert "password" not in caplog.text
    assert "token=secret" not in caplog.text
