from __future__ import annotations

import asyncio
import json
import threading
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.production import create_test_runtime_v2_application
from src.production_settings import ProductionSettings
from src.runtime_api import (
    RUNTIME_LIVENESS_PATH,
    RUNTIME_READINESS_PATH,
    RUNTIME_RETRIEVE_PATH,
)
from src.runtime_release import (
    EvidenceRuntimeReleaseIdentity,
    GenerationRequestLease,
    RuntimeReleaseError,
    runtime_release_sha256,
)

_TOKEN = "runtime-service-token-" + ("a" * 40)


def _identity(
    *,
    generation: str = "7" * 64,
    evaluation: str = "b" * 64,
    snapshot: str = "test-only-snapshot",
) -> EvidenceRuntimeReleaseIdentity:
    payload: dict[str, Any] = {
        "contract": "evidence-runtime-release",
        "schema_version": "2.0",
        "retrieval_schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": "1" * 40,
        "provider_build_sha256": "2" * 64,
        "retrieval_schema_sha256": "3" * 64,
        "provider_source_manifest_sha256": "4" * 64,
        "knowledge_generation_id": generation,
        "knowledge_generation_attestation_sha256": "5" * 64,
        "knowledge_source_manifest_sha256": "6" * 64,
        "model_manifest_sha256": "8" * 64,
        "index_config_sha256": "9" * 64,
        "evaluation_report_sha256": evaluation,
        "seal_attestation_sha256": "c" * 64,
        "knowledge_snapshot_id": snapshot,
        "supported_review_as_of": "2026-07-10",
    }
    payload["runtime_release_sha256"] = runtime_release_sha256(payload)
    return EvidenceRuntimeReleaseIdentity.model_validate(payload)


def _lease(
    *,
    identity: EvidenceRuntimeReleaseIdentity | None = None,
    epoch: int = 11,
) -> GenerationRequestLease:
    return GenerationRequestLease(
        identity=identity or _identity(),
        generation_epoch=epoch,
        generation_changed_at=f"2026-07-19T00:00:{epoch:02d}.000000Z",
    )


class FakeRuntimeLoader:
    def __init__(self, lease: GenerationRequestLease | None = None) -> None:
        self.current = lease or _lease()
        self.fail_acquire: Exception | None = None
        self.fail_postcheck: Exception | None = None
        self.acquire_calls = 0
        self.assert_calls = 0

    def acquire(self) -> GenerationRequestLease:
        self.acquire_calls += 1
        if self.fail_acquire is not None:
            raise self.fail_acquire
        return self.current

    def assert_unchanged(self, lease: GenerationRequestLease) -> None:
        self.assert_calls += 1
        if self.fail_postcheck is not None:
            raise self.fail_postcheck
        if lease.token != self.current.token or lease.identity != self.current.identity:
            raise RuntimeReleaseError("runtime release changed during request")


class ReadyConformanceBackend(TestOnlyConformanceEvidenceBackend):
    def __init__(self, *, ready: bool = True, snapshot: str = "test-only-snapshot") -> None:
        super().__init__(effective_date=date(2026, 7, 10))
        self.snapshot = snapshot
        self.ready = ready
        self.readiness_calls = 0

    def validate_ready(self) -> None:
        self.readiness_calls += 1
        if not self.ready:
            raise RuntimeError("3/58 or no active generation")

    def current_snapshot_id(self) -> str:
        self.retrieve_calls += 1
        return self.snapshot

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        assert snapshot_id == self.snapshot
        return self.effective_date

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ):
        # Reuse the deterministic fixture rows after mapping the request-scoped
        # generation snapshot to the fixture's internal constant.
        assert snapshot_id == self.snapshot
        return super().lookup_requirements(
            "test-only-conformance-snapshot",
            hsk10,
            trade_type,
            as_of,
        )


def _settings(**overrides: Any) -> ProductionSettings:
    values: dict[str, Any] = {
        "service_token": _TOKEN,
        "transport_mode": "direct_tls",
        "max_concurrency": 2,
        "request_deadline_seconds": 5,
        "shutdown_grace_seconds": 5,
    }
    values.update(overrides)
    return ProductionSettings(
        _env_file=None,
        **values,
    )


def _body() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": "req-runtime-v2",
        "case_id": "case-runtime-v2",
        "review_run_id": "run-runtime-v2",
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


def _headers(identity: EvidenceRuntimeReleaseIdentity | None = None) -> dict[str, str]:
    selected = identity or _identity()
    return {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        "X-Request-ID": "req-runtime-v2",
        **selected.binding_headers(),
    }


def _app(
    *,
    loader: FakeRuntimeLoader | None = None,
    backend: ReadyConformanceBackend | None = None,
    settings: ProductionSettings | None = None,
):
    selected_loader = loader or FakeRuntimeLoader()
    selected_backend = backend or ReadyConformanceBackend()
    return create_test_runtime_v2_application(
        service_settings=settings or _settings(),
        runtime_release_loader=selected_loader,
        backend_factory=lambda _lease: selected_backend,
    )


def test_readiness_is_authenticated_and_exact_runtime_wrapper() -> None:
    loader = FakeRuntimeLoader()
    backend = ReadyConformanceBackend()
    app = _app(loader=loader, backend=backend)
    with TestClient(app, base_url="https://evidence.internal") as client:
        assert client.get(RUNTIME_READINESS_PATH).status_code == 401
        response = client.get(
            RUNTIME_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "evidence_runtime_release": loader.current.identity.model_dump(mode="json"),
    }
    assert backend.readiness_calls == 1
    assert loader.assert_calls == 1


def test_liveness_is_public_but_requires_approved_transport() -> None:
    app = _app()
    with TestClient(app, base_url="https://evidence.internal") as client:
        accepted = client.get(RUNTIME_LIVENESS_PATH)
    assert accepted.status_code == 200
    with TestClient(app, base_url="http://evidence.internal") as client:
        rejected = client.get(RUNTIME_LIVENESS_PATH)
    assert rejected.status_code == 426
    assert rejected.json()["error"]["code"] == "TLS_REQUIRED"


def test_v2_retrieval_preserves_v1_body_and_returns_all_eight_headers() -> None:
    loader = FakeRuntimeLoader()
    backend = ReadyConformanceBackend()
    app = _app(loader=loader, backend=backend)
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.post(
            RUNTIME_RETRIEVE_PATH,
            content=json.dumps(_body()).encode(),
            headers=_headers(),
        )
    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.0"
    assert response.json()["knowledge_snapshot_id"] == "test-only-snapshot"
    assert backend.retrieve_calls == 1
    assert set(loader.current.identity.binding_headers()) == {
        name for name in loader.current.identity.binding_headers() if name in response.headers
    }
    for name, expected in loader.current.identity.binding_headers().items():
        assert response.headers[name] == expected


@pytest.mark.parametrize(
    "mutation",
    ["missing", "mismatch", "duplicate"],
)
def test_binding_is_rejected_before_body_and_backend(mutation: str) -> None:
    loader = FakeRuntimeLoader()
    backend = ReadyConformanceBackend()
    app = _app(loader=loader, backend=backend)
    raw = [
        (name.lower().encode("ascii"), value.encode("ascii")) for name, value in _headers().items()
    ]
    target = b"x-evidence-runtime-release-sha256"
    if mutation == "missing":
        raw = [(name, value) for name, value in raw if name != target]
    elif mutation == "mismatch":
        raw = [(name, b"0" * 64 if name == target else value) for name, value in raw]
    else:
        raw.append((target, loader.current.identity.runtime_release_sha256.encode("ascii")))

    messages, received = asyncio.run(_invoke_without_body(app, raw_headers=raw))
    assert received is False
    assert next(item["status"] for item in messages if item["type"] == "http.response.start") == 412
    assert backend.retrieve_calls == 0


def test_get_a_post_b_is_rejected_before_retrieval() -> None:
    loader = FakeRuntimeLoader()
    backend = ReadyConformanceBackend()
    app = _app(loader=loader, backend=backend)
    identity_a = loader.current.identity
    with TestClient(app, base_url="https://evidence.internal") as client:
        ready = client.get(
            RUNTIME_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert ready.status_code == 200
        loader.current = _lease(identity=_identity(generation="d" * 64), epoch=12)
        response = client.post(
            RUNTIME_RETRIEVE_PATH,
            content=json.dumps(_body()).encode(),
            headers=_headers(identity_a),
        )
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "RUNTIME_BINDING_MISMATCH"
    assert backend.retrieve_calls == 0


def test_mid_request_epoch_change_discards_retrieval_result() -> None:
    loader = FakeRuntimeLoader()
    loader.fail_postcheck = RuntimeReleaseError("A to B to A")
    backend = ReadyConformanceBackend()
    app = _app(loader=loader, backend=backend)
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.post(
            RUNTIME_RETRIEVE_PATH,
            content=json.dumps(_body()).encode(),
            headers=_headers(),
        )
    assert backend.retrieve_calls == 1
    assert response.status_code == 503
    assert "test_only_ccct" not in response.text
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_snapshot_mismatch_discards_retrieval_result() -> None:
    backend = ReadyConformanceBackend(snapshot="different-snapshot")
    app = _app(backend=backend)
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.post(
            RUNTIME_RETRIEVE_PATH,
            content=json.dumps(_body()).encode(),
            headers=_headers(),
        )
    assert backend.retrieve_calls == 1
    assert response.status_code == 503
    assert "different-snapshot" not in response.text


def test_no_active_or_incomplete_generation_is_readiness_503_without_secret() -> None:
    loader = FakeRuntimeLoader()
    loader.fail_acquire = RuntimeError("postgresql://user:secret@example.invalid/team-volume 3/58")
    app = _app(loader=loader)
    with TestClient(app, base_url="https://evidence.internal") as client:
        response = client.get(
            RUNTIME_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert response.status_code == 503
    assert "secret" not in response.text
    assert "3/58" not in response.text


def test_runtime_shell_has_no_legacy_retrieval_bypass() -> None:
    app = _app()
    paths = set(app.openapi()["paths"])
    assert "/internal/rag/v1/retrieve" not in paths
    assert "/internal/rag/v1/health/ready" not in paths
    assert RUNTIME_RETRIEVE_PATH in paths
    assert RUNTIME_READINESS_PATH in paths


class BlockingPreflightLoader(FakeRuntimeLoader):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def acquire(self) -> GenerationRequestLease:
        self.acquire_calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return self.current


def test_slow_identity_preflight_holds_admission_before_body_read() -> None:
    loader = BlockingPreflightLoader()
    app = _app(
        loader=loader,
        settings=_settings(max_concurrency=1),
    )
    body = json.dumps(_body()).encode()

    async def run():
        async with app.router.lifespan_context(app):
            first = asyncio.create_task(
                _invoke(
                    app,
                    method="POST",
                    path=RUNTIME_RETRIEVE_PATH,
                    raw_headers=_raw_headers(content_length=len(body)),
                    chunks=[body],
                )
            )
            assert await asyncio.to_thread(loader.started.wait, 2)
            second = await _invoke(
                app,
                method="GET",
                path=RUNTIME_READINESS_PATH,
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"authorization", f"Bearer {_TOKEN}".encode()),
                ],
            )
            loader.release.set()
            return await first, second

    first, second = asyncio.run(run())
    assert _status(first) == 200
    assert _status(second) == 503
    assert _json(second)["error"]["code"] == "SERVICE_BUSY"
    assert loader.acquire_calls == 1


def test_slow_body_is_deadline_bounded_and_releases_admission() -> None:
    app = _app(
        settings=_settings(
            max_concurrency=1,
            request_deadline_seconds=1,
        )
    )
    body_started = asyncio.Event()
    never_complete = asyncio.Event()

    async def receive_slow():
        body_started.set()
        await never_complete.wait()
        return {"type": "http.request", "body": b"", "more_body": True}

    async def run():
        async with app.router.lifespan_context(app):
            first = asyncio.create_task(
                _invoke(
                    app,
                    method="POST",
                    path=RUNTIME_RETRIEVE_PATH,
                    raw_headers=_raw_headers(),
                    receive_override=receive_slow,
                )
            )
            await asyncio.wait_for(body_started.wait(), timeout=2)
            busy = await _invoke(
                app,
                method="GET",
                path=RUNTIME_READINESS_PATH,
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"authorization", f"Bearer {_TOKEN}".encode()),
                ],
            )
            timed_out = await first
            recovered = await _invoke(
                app,
                method="GET",
                path=RUNTIME_READINESS_PATH,
                raw_headers=[
                    (b"host", b"evidence.internal"),
                    (b"authorization", f"Bearer {_TOKEN}".encode()),
                ],
            )
            return timed_out, busy, recovered

    timed_out, busy, recovered = asyncio.run(run())
    assert _status(busy) == 503
    assert _json(busy)["error"]["code"] == "SERVICE_BUSY"
    assert _status(timed_out) == 408
    assert _json(timed_out)["error"]["code"] == "REQUEST_BODY_TIMEOUT"
    assert _status(recovered) == 200


async def _invoke_without_body(
    app,
    *,
    raw_headers: list[tuple[bytes, bytes]],
) -> tuple[list[dict[str, Any]], bool]:
    messages: list[dict[str, Any]] = []
    received = False
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": RUNTIME_RETRIEVE_PATH,
        "raw_path": RUNTIME_RETRIEVE_PATH.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"evidence.internal"), *raw_headers],
        "client": ("127.0.0.1", 12345),
        "server": ("evidence.internal", 443),
        "root_path": "",
        "state": {},
    }

    async def receive():
        nonlocal received
        received = True
        raise AssertionError("binding rejection must not receive request bytes")

    async def send(message):
        messages.append(message)

    async with app.router.lifespan_context(app):
        await app(scope, receive, send)
    return messages, received


def _raw_headers(*, content_length: int | None = None) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"host", b"evidence.internal"),
        (b"authorization", f"Bearer {_TOKEN}".encode()),
        (b"content-type", b"application/json"),
        (b"x-request-id", b"req-runtime-v2"),
        *[
            (name.lower().encode(), value.encode())
            for name, value in _identity().binding_headers().items()
        ],
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return headers


async def _invoke(
    app,
    *,
    method: str,
    path: str,
    raw_headers: list[tuple[bytes, bytes]],
    chunks: list[bytes] | None = None,
    receive_override=None,
) -> list[dict[str, Any]]:
    pending = list(chunks or [b""])
    messages: list[dict[str, Any]] = []
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
        "client": ("127.0.0.1", 12345),
        "server": ("evidence.internal", 443),
        "root_path": "",
        "state": {},
    }

    async def receive_default():
        body = pending.pop(0) if pending else b""
        return {
            "type": "http.request",
            "body": body,
            "more_body": bool(pending),
        }

    async def send(message):
        messages.append(message)

    await app(scope, receive_override or receive_default, send)
    return messages


def _status(messages: list[dict[str, Any]]) -> int:
    return next(
        int(message["status"]) for message in messages if message["type"] == "http.response.start"
    )


def _json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return json.loads(payload)
