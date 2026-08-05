"""Small loopback Evidence V3 applications for development and functional UAT.

These applications preserve the V3 JSON request/response shapes so an Agent
can exercise the real HTTP boundary, while mode, bearer token, and loopback
binding keep the two local profiles separate from each other.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from datetime import date
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence import ReadinessBackend
from src.evidence_v3 import (
    RetrieveRequestV3,
    RetrieveResponseV3,
    retrieve_evidence_v3,
)
from src.profile_wire import EvidenceRuntimeReadinessV3

LOCAL_DEVELOPMENT_MODE: Literal["local-development"] = "local-development"
FUNCTIONAL_UAT_MODE: Literal["functional-uat"] = "functional-uat"
SERVICE_MODE_HEADER = "X-Customs-Service-Mode"
LITERAL_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,512}$")


class ProfileEvidenceError(RuntimeError):
    """A local Evidence profile cannot safely start or serve."""


def _single_header(request: Request, name: str) -> str | None:
    values = tuple(request.headers.getlist(name))
    if len(values) > 1:
        raise HTTPException(status_code=400, detail="request headers are malformed")
    return values[0] if values else None


def _authenticate_and_check_mode(request: Request, *, token: str, mode: str) -> None:
    authorization = _single_header(request, "authorization")
    expected = f"Bearer {token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=401,
            detail="service authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )
    received_mode = _single_header(request, SERVICE_MODE_HEADER)
    if received_mode != mode:
        raise HTTPException(status_code=412, detail="service mode does not match")


def create_profile_evidence_application(
    *,
    mode: Literal["local-development", "functional-uat"],
    service_token: str,
    backend: ReadinessBackend,
    runtime_state_validator: Callable[[], object] | None = None,
) -> FastAPI:
    """Create one local, authenticated V3 application.

    The profile launcher validates the bind host; the app's own service mode
    check additionally prevents an Agent configured for one profile from
    accepting a different profile on the same workstation.  A functional
    profile may supply a small local-state validator for each authenticated
    readiness probe.  It deliberately does not run a full cross-store scan.
    """

    if _TOKEN_PATTERN.fullmatch(service_token) is None:
        raise ProfileEvidenceError("service token has an invalid format")
    app = FastAPI(
        title=f"Customs Evidence API ({mode})",
        version="3.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.profile_mode = mode
    app.state.automatic_completion_eligible = False
    app.state.evidence_backend = backend

    @app.middleware("http")
    async def mode_response_header(request: Request, call_next):
        response = await call_next(request)
        response.headers[SERVICE_MODE_HEADER] = mode
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ProfileEvidenceError)
    async def profile_error(_request: Request, _error: ProfileEvidenceError) -> JSONResponse:
        return JSONResponse({"detail": "local Evidence profile is unavailable"}, status_code=503)

    @app.get("/internal/rag/v3/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/internal/rag/v3/health/ready", response_model=EvidenceRuntimeReadinessV3)
    def ready(request: Request) -> EvidenceRuntimeReadinessV3:
        _authenticate_and_check_mode(request, token=service_token, mode=mode)
        try:
            if runtime_state_validator is not None:
                runtime_state_validator()
            # The backend performs the substantive local-store check. The
            # response intentionally carries no release, provenance, policy,
            # or approval assertion; mode is conveyed in the response header.
            backend.readiness()
            return EvidenceRuntimeReadinessV3(status="ready")
        except HTTPException:
            raise
        except Exception as exc:
            # Store failures can contain paths/connection strings.  The HTTP
            # body remains a stable classification only.
            raise ProfileEvidenceError(type(exc).__name__) from None

    @app.post("/internal/rag/v3/retrieve", response_model=RetrieveResponseV3)
    def retrieve(
        payload: RetrieveRequestV3,
        request: Request,
        response: Response,
    ) -> RetrieveResponseV3:
        _authenticate_and_check_mode(request, token=service_token, mode=mode)
        result = retrieve_evidence_v3(
            payload,
            backend,
        )
        response.headers["X-Request-ID"] = payload.request_id
        return result

    return app


def create_dev_fixture_application(*, service_token: str) -> FastAPI:
    """Create deterministic synthetic Evidence V3 with no Docker or model."""

    fixture = TestOnlyConformanceEvidenceBackend(effective_date=date(2026, 8, 5))
    return create_profile_evidence_application(
        mode=LOCAL_DEVELOPMENT_MODE,
        service_token=service_token,
        backend=fixture,
    )


__all__ = [
    "FUNCTIONAL_UAT_MODE",
    "LITERAL_LOOPBACK_HOSTS",
    "LOCAL_DEVELOPMENT_MODE",
    "ProfileEvidenceError",
    "SERVICE_MODE_HEADER",
    "create_dev_fixture_application",
    "create_profile_evidence_application",
]
