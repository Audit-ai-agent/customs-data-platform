"""Hardened production ASGI shell for the versioned Evidence API.

This module intentionally does not expose the team demo routes.  It wraps the
existing Evidence router with service authentication, transport assertions,
request framing and JSON limits, bounded admission, deadlines, redacted
telemetry, and graceful draining.

Run behind a TLS-terminating ingress or service mesh:

    python -m src.production

The entrypoint never generates certificates and never enables a fixture
backend.  The explicit test factory below exists only for offline shell tests.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.config import settings as data_store_settings
from src.evidence import DatabaseEvidenceBackend, ReadinessBackend
from src.production_settings import (
    ProductionSettings,
    RuntimeSecrets,
    apply_runtime_data_store_secrets,
    validate_production_data_store_settings,
)
from src.provider_identity import (
    ProviderBuildIdentity,
    binding_header_mismatches,
    load_provider_build_identity,
)
from src.webapp import _configure_evidence_application

_LOGGER = logging.getLogger("customs_evidence.production")
_RETRIEVE_PATH = "/internal/rag/v1/retrieve"
_LIVENESS_PATH = "/internal/rag/v1/health/live"
_READINESS_PATH = "/internal/rag/v1/health/ready"
_METRICS_PATH = "/internal/rag/v1/metrics"
_OPENAPI_PATH = "/internal/rag/v1/openapi.json"
_INTERNAL_PREFIX = "/internal/rag/"
_BOUND_IDENTITY_STATE_KEY = "_evidence_provider_build_identity"
_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BEARER_PATTERN = re.compile(rb"^Bearer ([A-Za-z0-9._~+/=-]{32,512})$", re.IGNORECASE)


class _RequestRejected(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


class _ResponseTooLarge(RuntimeError):
    """The route attempted to exceed the production response byte budget."""


def _safe_error(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    response_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Service-Request-ID": request_id,
        **dict(headers or {}),
    }
    return JSONResponse(
        {
            "error": {
                "code": code,
                "message": message,
            },
            "request_id": request_id,
        },
        status_code=status_code,
        headers=response_headers,
    )


def _raw_header_values(scope: Scope, name: bytes) -> tuple[bytes, ...]:
    target = name.lower()
    return tuple(value for key, value in scope.get("headers", []) if key.lower() == target)


def _single_header(
    scope: Scope,
    name: bytes,
    *,
    required: bool = False,
) -> bytes | None:
    values = _raw_header_values(scope, name)
    if len(values) > 1 or (required and len(values) != 1):
        raise _RequestRejected(
            400,
            "INVALID_HEADERS",
            "Request headers are malformed",
        )
    return values[0] if values else None


def _client_request_id(scope: Scope) -> str | None:
    value = _single_header(scope, b"x-request-id")
    if value is None:
        return None
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise _RequestRejected(
            400,
            "INVALID_REQUEST_ID",
            "X-Request-ID is invalid",
        ) from exc
    if not _REQUEST_ID_PATTERN.fullmatch(decoded):
        raise _RequestRejected(
            400,
            "INVALID_REQUEST_ID",
            "X-Request-ID is invalid",
        )
    return decoded


def _route_label(path: str) -> str:
    return {
        _RETRIEVE_PATH: "retrieve",
        _LIVENESS_PATH: "live",
        _READINESS_PATH: "ready",
        _METRICS_PATH: "metrics",
        _OPENAPI_PATH: "openapi",
    }.get(path, "other")


class _Metrics:
    """Small bounded-cardinality metrics registry with no request content."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str, str]] = Counter()
        self._rejections: Counter[str] = Counter()
        self._timeouts: Counter[str] = Counter()
        self._duration_ms_sum: Counter[str] = Counter()
        self._duration_count: Counter[str] = Counter()
        self._inflight = 0

    def admitted(self) -> None:
        with self._lock:
            self._inflight += 1

    def released(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    def request(
        self,
        *,
        route: str,
        method: str,
        status_code: int,
        duration_ms: int,
    ) -> None:
        status_class = f"{status_code // 100}xx"
        with self._lock:
            self._requests[(route, method, status_class)] += 1
            self._duration_ms_sum[route] += max(0, duration_ms)
            self._duration_count[route] += 1

    def rejected(self, reason: str) -> None:
        with self._lock:
            self._rejections[reason] += 1

    def timed_out(self, route: str) -> None:
        with self._lock:
            self._timeouts[route] += 1

    def render(self) -> str:
        with self._lock:
            requests = sorted(self._requests.items())
            rejections = sorted(self._rejections.items())
            timeouts = sorted(self._timeouts.items())
            duration_sum = sorted(self._duration_ms_sum.items())
            duration_count = sorted(self._duration_count.items())
            inflight = self._inflight
        lines = [
            "# HELP evidence_service_inflight Current admitted Evidence requests.",
            "# TYPE evidence_service_inflight gauge",
            f"evidence_service_inflight {inflight}",
            "# HELP evidence_http_requests_total Completed HTTP requests.",
            "# TYPE evidence_http_requests_total counter",
        ]
        lines.extend(
            (
                "evidence_http_requests_total"
                f'{{route="{route}",method="{method}",status_class="{status}"}} {count}'
            )
            for (route, method, status), count in requests
        )
        lines.extend(
            [
                "# HELP evidence_request_rejections_total Requests rejected before routing.",
                "# TYPE evidence_request_rejections_total counter",
            ]
        )
        lines.extend(
            f'evidence_request_rejections_total{{reason="{reason}"}} {count}'
            for reason, count in rejections
        )
        lines.extend(
            [
                "# HELP evidence_request_timeouts_total Requests exceeding the service deadline.",
                "# TYPE evidence_request_timeouts_total counter",
            ]
        )
        lines.extend(
            f'evidence_request_timeouts_total{{route="{route}"}} {count}'
            for route, count in timeouts
        )
        lines.extend(
            [
                "# HELP evidence_request_duration_milliseconds_sum "
                "Sum of completed request latency.",
                "# TYPE evidence_request_duration_milliseconds_sum counter",
            ]
        )
        lines.extend(
            f'evidence_request_duration_milliseconds_sum{{route="{route}"}} {value}'
            for route, value in duration_sum
        )
        lines.extend(
            [
                "# HELP evidence_request_duration_milliseconds_count "
                "Count of completed request latency observations.",
                "# TYPE evidence_request_duration_milliseconds_count counter",
            ]
        )
        lines.extend(
            f'evidence_request_duration_milliseconds_count{{route="{route}"}} {value}'
            for route, value in duration_count
        )
        return "\n".join(lines) + "\n"


class _AdmissionController:
    """Immediate bounded admission with graceful drain tracking."""

    def __init__(self, maximum: int, metrics: _Metrics) -> None:
        self.maximum = maximum
        self.metrics = metrics
        self._active = 0
        self._accepting = False
        self._lock = asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()

    async def start(self) -> None:
        async with self._lock:
            self._accepting = True

    async def try_acquire(self) -> bool:
        async with self._lock:
            if not self._accepting or self._active >= self.maximum:
                return False
            self._active += 1
            self._idle.clear()
            self.metrics.admitted()
            return True

    async def release(self) -> None:
        async with self._lock:
            if self._active:
                self._active -= 1
                self.metrics.released()
            if self._active == 0:
                self._idle.set()

    async def stop_and_drain(self, timeout_seconds: float) -> bool:
        async with self._lock:
            self._accepting = False
            if self._active == 0:
                return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout_seconds)
        except TimeoutError:
            return False
        return True


@dataclass
class _ProductionState:
    settings: ProductionSettings
    secrets: RuntimeSecrets
    metrics: _Metrics = field(default_factory=_Metrics)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.admission = _AdmissionController(self.settings.max_concurrency, self.metrics)

    def track(self, task: asyncio.Task[Any]) -> None:
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)


class _ResponseBuffer:
    def __init__(self, maximum_bytes: int) -> None:
        self.maximum_bytes = maximum_bytes
        self.messages: list[Message] = []
        self.status_code = 500
        self.total_body_bytes = 0
        self.discard = False
        self.overflowed = False

    async def send(self, message: Message) -> None:
        if self.discard:
            return
        if message["type"] == "http.response.start":
            self.status_code = int(message["status"])
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            self.total_body_bytes += len(body)
            if self.total_body_bytes > self.maximum_bytes:
                self.overflowed = True
                self.discard = True
                self.messages.clear()
                raise _ResponseTooLarge
        self.messages.append(message)

    async def flush(self, send: Send, request_id: str) -> None:
        for message in self.messages:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-service-request-id", request_id.encode("ascii")),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)


def _transport_is_authorized(scope: Scope, settings: ProductionSettings) -> bool:
    if settings.transport_mode == "direct_tls":
        return scope.get("scheme") == "https"
    client = scope.get("client")
    client_host = client[0] if client else ""
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if settings.transport_mode == "loopback_dev":
        return client_ip.is_loopback
    if not any(client_ip in network for network in settings.proxy_networks()):
        return False
    forwarded_proto = _single_header(scope, b"x-forwarded-proto", required=True)
    return forwarded_proto is not None and hmac.compare_digest(
        forwarded_proto.lower(),
        b"https",
    )


def _authenticate(scope: Scope, expected_token: bytes) -> None:
    values = _raw_header_values(scope, b"authorization")
    if len(values) != 1:
        raise _RequestRejected(
            401,
            "UNAUTHORIZED",
            "Service authentication is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    match = _BEARER_PATTERN.fullmatch(values[0])
    supplied = match.group(1) if match else b""
    if not match or not hmac.compare_digest(supplied, expected_token):
        raise _RequestRejected(
            401,
            "UNAUTHORIZED",
            "Service authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _validate_request_framing(scope: Scope, maximum_bytes: int) -> None:
    if _raw_header_values(scope, b"content-encoding"):
        raise _RequestRejected(
            415,
            "UNSUPPORTED_CONTENT_ENCODING",
            "Compressed request bodies are not accepted",
        )
    content_types = _raw_header_values(scope, b"content-type")
    if len(content_types) != 1:
        raise _RequestRejected(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "Content-Type must be application/json",
        )
    try:
        media_type, *parameters = content_types[0].decode("ascii").lower().split(";")
    except UnicodeDecodeError as exc:
        raise _RequestRejected(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "Content-Type must be application/json",
        ) from exc
    if media_type.strip() != "application/json":
        raise _RequestRejected(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "Content-Type must be application/json",
        )
    normalized_parameters = {part.strip() for part in parameters if part.strip()}
    if normalized_parameters - {"charset=utf-8"}:
        raise _RequestRejected(
            415,
            "UNSUPPORTED_MEDIA_TYPE",
            "Only UTF-8 JSON is accepted",
        )

    content_lengths = _raw_header_values(scope, b"content-length")
    transfer_encodings = _raw_header_values(scope, b"transfer-encoding")
    if len(content_lengths) > 1 or len(transfer_encodings) > 1:
        raise _RequestRejected(
            400,
            "INVALID_HEADERS",
            "Request framing headers are malformed",
        )
    if content_lengths and transfer_encodings:
        raise _RequestRejected(
            400,
            "AMBIGUOUS_REQUEST_FRAMING",
            "Ambiguous request framing is not accepted",
        )
    if content_lengths:
        try:
            decoded = content_lengths[0].decode("ascii")
            if not decoded.isdigit():
                raise ValueError
            declared = int(decoded)
        except (UnicodeDecodeError, ValueError) as exc:
            raise _RequestRejected(
                400,
                "INVALID_CONTENT_LENGTH",
                "Content-Length is invalid",
            ) from exc
        if declared > maximum_bytes:
            raise _RequestRejected(
                413,
                "REQUEST_TOO_LARGE",
                "Request body exceeds the configured limit",
            )


async def _read_limited_body(receive: Receive, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _RequestRejected(
                400,
                "CLIENT_DISCONNECTED",
                "Client disconnected before sending a complete request",
            )
        if message["type"] != "http.request":
            raise _RequestRejected(
                400,
                "INVALID_REQUEST_BODY",
                "Request body framing is invalid",
            )
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > maximum_bytes:
            raise _RequestRejected(
                413,
                "REQUEST_TOO_LARGE",
                "Request body exceeds the configured limit",
            )
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_strict_json(
    body: bytes,
    header_request_id: str,
    *,
    enforce_request_id_match: bool,
) -> None:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _RequestRejected(
            400,
            "INVALID_JSON",
            "Request body must be strict UTF-8 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise _RequestRejected(
            400,
            "INVALID_JSON",
            "Request body must be a JSON object",
        )
    body_request_id = payload.get("request_id")
    if (
        enforce_request_id_match
        and body_request_id is not None
        and body_request_id != header_request_id
    ):
        raise _RequestRejected(
            400,
            "REQUEST_ID_MISMATCH",
            "Header and body request identifiers do not match",
        )


def _replay_body(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _bind_provider_identity(scope: Scope, provider_app: FastAPI) -> None:
    try:
        identity = provider_app.state.provider_identity_loader()
    except Exception as exc:
        _LOGGER.error(
            json.dumps(
                {
                    "event": "provider_identity_unavailable",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        raise _RequestRejected(
            503,
            "PROVIDER_IDENTITY_UNAVAILABLE",
            "Evidence provider identity is unavailable",
        ) from None
    if binding_header_mismatches(
        _RawHeaders(scope.get("headers", [])),
        identity,
    ):
        raise _RequestRejected(
            412,
            "PROVIDER_BINDING_MISMATCH",
            "Evidence provider build binding does not match",
        )
    scope.setdefault("state", {})[_BOUND_IDENTITY_STATE_KEY] = identity


class _RawHeaders(Mapping[str, str]):
    """Minimal case-insensitive raw header view preserving exact cardinality."""

    def __init__(self, raw: list[tuple[bytes, bytes]]) -> None:
        self.raw = tuple(raw)

    def getlist(self, name: str) -> list[str]:
        target = name.lower().encode("ascii")
        values: list[str] = []
        for key, value in self.raw:
            if key.lower() == target:
                try:
                    values.append(value.decode("ascii"))
                except UnicodeDecodeError:
                    values.append("")
        return values

    def __iter__(self):
        return iter({key.decode("latin-1").lower() for key, _ in self.raw})

    def __len__(self) -> int:
        return len(set(iter(self)))

    def __getitem__(self, key: str) -> str:
        values = self.getlist(key)
        if not values:
            raise KeyError(key)
        return values[-1]


class ProductionBoundaryMiddleware:
    """Raw ASGI security, resource, and telemetry boundary."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        provider_app: FastAPI,
        service_state: _ProductionState,
    ) -> None:
        self.app = app
        self.provider_app = provider_app
        self.service_state = service_state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        method = scope.get("method", "")
        path = scope.get("path", "")
        route = _route_label(path)
        request_id = f"svc-{uuid.uuid4().hex}"
        admitted = False
        try:
            client_request_id = _client_request_id(scope)
            scope.setdefault("state", {})["service_request_id"] = request_id
            _single_header(scope, b"host", required=True)
            if scope.get("query_string", b""):
                raise _RequestRejected(
                    400,
                    "QUERY_PARAMETERS_FORBIDDEN",
                    "Query parameters are not accepted",
                )
            if not _transport_is_authorized(scope, self.service_state.settings):
                raise _RequestRejected(
                    426,
                    "TLS_REQUIRED",
                    "A trusted TLS transport is required",
                )
            if path.startswith(_INTERNAL_PREFIX) and path != _LIVENESS_PATH:
                _authenticate(scope, self.service_state.secrets.service_token)
            if method == "POST" and path == _RETRIEVE_PATH:
                _validate_request_framing(
                    scope,
                    self.service_state.settings.max_request_body_bytes,
                )
                _bind_provider_identity(scope, self.provider_app)
                body = await _read_limited_body(
                    receive,
                    self.service_state.settings.max_request_body_bytes,
                )
                _validate_strict_json(
                    body,
                    client_request_id or "",
                    enforce_request_id_match=client_request_id is not None,
                )
                receive = _replay_body(body)

            if path == _LIVENESS_PATH:
                buffer = _ResponseBuffer(
                    self.service_state.settings.max_response_body_bytes
                )
                await self.app(scope, receive, buffer.send)
                await buffer.flush(send, request_id)
                self._record(
                    route=route,
                    method=method,
                    status_code=buffer.status_code,
                    started=started,
                    request_id=request_id,
                )
                return

            admitted = await self.service_state.admission.try_acquire()
            if not admitted:
                raise _RequestRejected(
                    503,
                    "SERVICE_BUSY",
                    "Evidence service is not accepting more work",
                    headers={
                        "Retry-After": str(
                            self.service_state.settings.retry_after_seconds
                        )
                    },
                )
            buffer = _ResponseBuffer(
                self.service_state.settings.max_response_body_bytes
            )
            inner = asyncio.create_task(self.app(scope, receive, buffer.send))
            done, _ = await asyncio.wait(
                {inner},
                timeout=self.service_state.settings.request_deadline_seconds,
            )
            if not done:
                buffer.discard = True
                self.service_state.metrics.timed_out(route)
                finalizer = asyncio.create_task(
                    self._finish_after_timeout(inner)
                )
                self.service_state.track(finalizer)
                admitted = False
                response = _safe_error(
                    504,
                    "DEADLINE_EXCEEDED",
                    "Evidence request exceeded its service deadline",
                    request_id=request_id,
                )
                await response(scope, receive, send)
                self._record(
                    route=route,
                    method=method,
                    status_code=504,
                    started=started,
                    request_id=request_id,
                )
                return
            try:
                await inner
            except _ResponseTooLarge:
                buffer.overflowed = True
            if buffer.overflowed:
                response = _safe_error(
                    502,
                    "RESPONSE_TOO_LARGE",
                    "Evidence response exceeded its configured limit",
                    request_id=request_id,
                )
                await response(scope, receive, send)
                self._record(
                    route=route,
                    method=method,
                    status_code=502,
                    started=started,
                    request_id=request_id,
                )
                return
            await buffer.flush(send, request_id)
            self._record(
                route=route,
                method=method,
                status_code=buffer.status_code,
                started=started,
                request_id=request_id,
            )
        except _RequestRejected as exc:
            self.service_state.metrics.rejected(exc.code.lower())
            response = _safe_error(
                exc.status_code,
                exc.code,
                exc.message,
                request_id=request_id,
                headers=exc.headers,
            )
            await response(scope, receive, send)
            self._record(
                route=route,
                method=method,
                status_code=exc.status_code,
                started=started,
                request_id=request_id,
            )
        except Exception as exc:
            _LOGGER.error(
                json.dumps(
                    {
                        "event": "unhandled_request_error",
                        "exception_type": type(exc).__name__,
                        "request_id": request_id,
                        "route": route,
                    },
                    sort_keys=True,
                )
            )
            response = _safe_error(
                500,
                "INTERNAL_ERROR",
                "Evidence service could not process the request",
                request_id=request_id,
            )
            await response(scope, receive, send)
            self._record(
                route=route,
                method=method,
                status_code=500,
                started=started,
                request_id=request_id,
            )
        finally:
            if admitted:
                await self.service_state.admission.release()

    async def _finish_after_timeout(self, task: asyncio.Task[Any]) -> None:
        try:
            await task
        except BaseException as exc:
            _LOGGER.warning(
                json.dumps(
                    {
                        "event": "timed_out_work_finished_with_error",
                        "exception_type": type(exc).__name__,
                    },
                    sort_keys=True,
                )
            )
        finally:
            await self.service_state.admission.release()

    def _record(
        self,
        *,
        route: str,
        method: str,
        status_code: int,
        started: float,
        request_id: str,
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1_000)
        self.service_state.metrics.request(
            route=route,
            method=method,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        _LOGGER.info(
            json.dumps(
                {
                    "duration_ms": duration_ms,
                    "event": "evidence_http_request",
                    "method": method,
                    "request_id": request_id,
                    "route": route,
                    "status_code": status_code,
                },
                sort_keys=True,
            )
        )


def _install_safe_exception_handlers(target: FastAPI) -> None:
    @target.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        request_id = getattr(_request.state, "service_request_id", "unavailable")
        return _safe_error(
            422,
            "SCHEMA_VALIDATION_FAILED",
            "Request does not satisfy the Evidence schema",
            request_id=request_id,
        )

    @target.exception_handler(HTTPException)
    async def expected_http_error(request: Request, exc: HTTPException):
        request_id = getattr(request.state, "service_request_id", "unavailable")
        codes = {
            404: ("NOT_FOUND", "Resource was not found"),
            405: ("METHOD_NOT_ALLOWED", "HTTP method is not allowed"),
            412: (
                "PROVIDER_BINDING_MISMATCH",
                "Evidence provider build binding does not match",
            ),
            503: ("SERVICE_UNAVAILABLE", "Evidence service is unavailable"),
        }
        code, message = codes.get(
            exc.status_code,
            ("REQUEST_REJECTED", "Evidence request was rejected"),
        )
        return _safe_error(
            exc.status_code,
            code,
            message,
            request_id=request_id,
        )

    @target.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        request_id = getattr(request.state, "service_request_id", "unavailable")
        _LOGGER.error(
            json.dumps(
                {
                    "event": "route_exception",
                    "exception_type": type(exc).__name__,
                    "request_id": request_id,
                },
                sort_keys=True,
            )
        )
        return _safe_error(
            500,
            "INTERNAL_ERROR",
            "Evidence service could not process the request",
            request_id=request_id,
        )


def _install_openapi_security(target: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if target.openapi_schema:
            return target.openapi_schema
        schema = get_openapi(
            title=target.title,
            version=target.version,
            routes=target.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["ServiceBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Short-lived service credential; never use a query token.",
        }
        for path, operations in schema.get("paths", {}).items():
            if path.startswith(_INTERNAL_PREFIX) and path != _LIVENESS_PATH:
                for operation in operations.values():
                    if isinstance(operation, dict):
                        operation["security"] = [{"ServiceBearer": []}]
                        operation.setdefault("responses", {}).setdefault(
                            "401",
                            {"description": "Service authentication failed"},
                        )
        target.openapi_schema = schema
        return schema

    target.openapi = custom_openapi  # type: ignore[method-assign]


def _create_application(
    *,
    service_settings: ProductionSettings,
    backend_factory: Callable[[], ReadinessBackend],
    identity_loader: Callable[[], ProviderBuildIdentity],
    test_only: bool,
) -> FastAPI:
    secrets = service_settings.resolve_secrets()
    state = _ProductionState(settings=service_settings, secrets=secrets)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await state.admission.start()
        _LOGGER.info(
            json.dumps(
                {
                    "event": "evidence_service_started",
                    "max_concurrency": service_settings.max_concurrency,
                    "transport_mode": service_settings.transport_mode,
                },
                sort_keys=True,
            )
        )
        try:
            yield
        finally:
            drained = await state.admission.stop_and_drain(
                service_settings.shutdown_grace_seconds
            )
            _LOGGER.info(
                json.dumps(
                    {
                        "drained": drained,
                        "event": "evidence_service_stopped",
                    },
                    sort_keys=True,
                )
            )

    target = FastAPI(
        title="Customs Evidence API — Production",
        version="1.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=_OPENAPI_PATH if service_settings.enable_openapi else None,
        lifespan=lifespan,
    )
    target.state.production_service = state
    target.state.test_only_production_shell = test_only

    @target.get(_METRICS_PATH, include_in_schema=False)
    async def production_metrics() -> PlainTextResponse:
        return PlainTextResponse(
            state.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    _install_safe_exception_handlers(target)
    _configure_evidence_application(
        target,
        backend_factory=backend_factory,
        identity_loader=identity_loader,
        test_only_conformance=test_only,
        install_binding_middleware=False,
    )
    _install_openapi_security(target)
    target.add_middleware(
        ProductionBoundaryMiddleware,
        provider_app=target,
        service_state=state,
    )
    return target


def create_production_evidence_application(
    service_settings: ProductionSettings | None = None,
) -> FastAPI:
    """Create the real Database-backed service; fixture env switches do not exist."""

    selected = service_settings or ProductionSettings()
    apply_runtime_data_store_secrets(selected, data_store_settings)
    validate_production_data_store_settings(selected, data_store_settings)
    return _create_application(
        service_settings=selected,
        backend_factory=DatabaseEvidenceBackend,
        identity_loader=load_provider_build_identity,
        test_only=False,
    )


def create_test_production_shell_application(
    *,
    service_settings: ProductionSettings,
    identity: ProviderBuildIdentity,
    backend: ReadinessBackend,
) -> FastAPI:
    """Build the same shell around an explicit offline backend for tests only."""

    return _create_application(
        service_settings=service_settings,
        backend_factory=lambda: backend,
        identity_loader=lambda: identity,
        test_only=True,
    )


def main() -> int:
    """Run one hardened worker; horizontal concurrency belongs at the orchestrator."""

    try:
        selected = ProductionSettings()
        target = create_production_evidence_application(selected)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "evidence_service_configuration_failed",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78

    if selected.transport_mode == "direct_tls":
        print(
            json.dumps(
                {
                    "event": "evidence_service_configuration_failed",
                    "reason": (
                        "direct_tls requires an operator-managed TLS ASGI server; "
                        "the bundled entrypoint is ingress-only"
                    ),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78

    logging.basicConfig(
        level=getattr(logging, selected.log_level.upper()),
        format="%(message)s",
    )
    import uvicorn

    uvicorn.run(
        target,
        host=selected.bind_host,
        port=selected.port,
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        workers=1,
        limit_concurrency=selected.max_concurrency + 8,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=int(selected.shutdown_grace_seconds),
        log_level=selected.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
