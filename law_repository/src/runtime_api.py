"""Production-only Evidence runtime v2 routes."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

from src.evidence import EvidenceBackend, RetrieveRequest, RetrieveResponse, retrieve_evidence
from src.runtime_release import (
    EvidenceRuntimeReadiness,
    GenerationRequestLease,
    RuntimeLeaseLoader,
    runtime_binding_header_mismatches,
)

RUNTIME_RETRIEVE_PATH = "/internal/rag/v2/retrieve"
RUNTIME_LIVENESS_PATH = "/internal/rag/v2/health/live"
RUNTIME_READINESS_PATH = "/internal/rag/v2/health/ready"
RUNTIME_METRICS_PATH = "/internal/rag/v2/metrics"
RUNTIME_OPENAPI_PATH = "/internal/rag/v2/openapi.json"
BOUND_RUNTIME_LEASE_STATE_KEY = "_evidence_runtime_release_lease"


class ReadyEvidenceBackend(EvidenceBackend, Protocol):
    def validate_ready(self) -> None: ...


RuntimeBackendFactory = Callable[[GenerationRequestLease], ReadyEvidenceBackend]

runtime_router = APIRouter()


def _request_lease(request: Request) -> GenerationRequestLease:
    bound: Any = getattr(request.state, BOUND_RUNTIME_LEASE_STATE_KEY, None)
    if isinstance(bound, GenerationRequestLease):
        return bound
    try:
        return request.app.state.runtime_release_loader.acquire()
    except Exception as exc:
        print(
            f"[evidence] runtime release unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail="Evidence runtime release is unavailable",
        ) from None


@runtime_router.get(RUNTIME_LIVENESS_PATH)
def runtime_liveness() -> dict[str, str]:
    return {"status": "alive"}


@runtime_router.get(
    RUNTIME_READINESS_PATH,
    response_model=EvidenceRuntimeReadiness,
)
def runtime_readiness(request: Request) -> EvidenceRuntimeReadiness:
    lease = _request_lease(request)
    try:
        backend = request.app.state.runtime_backend_factory(lease)
        backend.validate_ready()
        request.app.state.runtime_release_loader.assert_unchanged(lease)
    except Exception as exc:
        print(
            f"[evidence] runtime readiness failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail="Evidence runtime is not ready",
        ) from None
    return EvidenceRuntimeReadiness(
        status="ready",
        evidence_runtime_release=lease.identity,
    )


@runtime_router.post(
    RUNTIME_RETRIEVE_PATH,
    response_model=RetrieveResponse,
)
def runtime_retrieve(
    payload: RetrieveRequest,
    request: Request,
    response: Response,
) -> RetrieveResponse:
    lease = _request_lease(request)
    if runtime_binding_header_mismatches(request.headers, lease.identity):
        raise HTTPException(
            status_code=412,
            detail="Evidence runtime release binding does not match",
        )
    try:
        backend = request.app.state.runtime_backend_factory(lease)
        result = retrieve_evidence(payload, backend)
        if (
            result.knowledge_snapshot_id is not None
            and result.knowledge_snapshot_id != lease.identity.knowledge_snapshot_id
        ):
            raise RuntimeError("retrieval snapshot does not match runtime release")
        # Re-load code + generation identity after all store reads. A changed
        # epoch, seal, generation, provider build, or release discards result.
        request.app.state.runtime_release_loader.assert_unchanged(lease)
    except Exception as exc:
        print(
            f"[evidence] runtime retrieval failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail="Evidence runtime changed or became unavailable",
        ) from None
    for name, value in lease.identity.binding_headers().items():
        response.headers[name] = value
    return result


def configure_runtime_v2_application(
    target: FastAPI,
    *,
    runtime_release_loader: RuntimeLeaseLoader,
    backend_factory: RuntimeBackendFactory,
    test_only_conformance: bool,
) -> FastAPI:
    target.state.runtime_release_loader = runtime_release_loader
    target.state.runtime_backend_factory = backend_factory
    target.state.test_only_runtime_conformance = test_only_conformance
    target.include_router(runtime_router)
    return target


__all__ = [
    "BOUND_RUNTIME_LEASE_STATE_KEY",
    "RUNTIME_LIVENESS_PATH",
    "RUNTIME_METRICS_PATH",
    "RUNTIME_OPENAPI_PATH",
    "RUNTIME_READINESS_PATH",
    "RUNTIME_RETRIEVE_PATH",
    "ReadyEvidenceBackend",
    "RuntimeBackendFactory",
    "configure_runtime_v2_application",
]
