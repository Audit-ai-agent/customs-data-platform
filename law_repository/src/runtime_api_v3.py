"""Production-only Evidence runtime v3 routes."""

from __future__ import annotations

import sys
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

from src.deadline import REQUEST_DEADLINE_STATE_KEY, RequestDeadline
from src.evidence_v3 import (
    MappingRequirementPolicyResolver,
    RetrieveRequestV3,
    RetrieveResponseV3,
    retrieve_evidence_v3,
)
from src.runtime_api import RuntimeBackendFactory
from src.runtime_release_v3 import (
    EvidenceRuntimeReadinessV3,
    RuntimeV3Lease,
    RuntimeV3LeaseLoaderProtocol,
    runtime_v3_binding_header_mismatches,
)

RUNTIME_V3_RETRIEVE_PATH = "/internal/rag/v3/retrieve"
RUNTIME_V3_LIVENESS_PATH = "/internal/rag/v3/health/live"
RUNTIME_V3_READINESS_PATH = "/internal/rag/v3/health/ready"
RUNTIME_V3_METRICS_PATH = "/internal/rag/v3/metrics"
RUNTIME_V3_OPENAPI_PATH = "/internal/rag/v3/openapi.json"
BOUND_RUNTIME_V3_LEASE_STATE_KEY = "_evidence_runtime_release_v3_lease"

runtime_v3_router = APIRouter()


def _deadline(request: Request) -> RequestDeadline:
    value: Any = getattr(request.state, REQUEST_DEADLINE_STATE_KEY, None)
    if not isinstance(value, RequestDeadline):
        raise HTTPException(503, "Evidence request deadline is unavailable")
    return value


def _lease(request: Request) -> RuntimeV3Lease:
    bound: Any = getattr(request.state, BOUND_RUNTIME_V3_LEASE_STATE_KEY, None)
    if isinstance(bound, RuntimeV3Lease):
        return bound
    try:
        return request.app.state.runtime_v3_release_loader.acquire(_deadline(request))
    except Exception as exc:
        print(
            f"[evidence] runtime v3 release unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(503, "Evidence runtime v3 release is unavailable") from None


@runtime_v3_router.get(RUNTIME_V3_LIVENESS_PATH)
def runtime_v3_liveness() -> dict[str, str]:
    return {"status": "alive"}


@runtime_v3_router.get(
    RUNTIME_V3_READINESS_PATH,
    response_model=EvidenceRuntimeReadinessV3,
)
def runtime_v3_readiness(request: Request) -> EvidenceRuntimeReadinessV3:
    lease = _lease(request)
    deadline = _deadline(request)
    try:
        backend = request.app.state.runtime_backend_factory(lease.base_lease, deadline)
        backend.validate_ready()
        request.app.state.runtime_v3_release_loader.assert_unchanged(lease, deadline)
    except Exception as exc:
        print(
            f"[evidence] runtime v3 readiness failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(503, "Evidence runtime v3 is not ready") from None
    return EvidenceRuntimeReadinessV3(status="ready", evidence_runtime_release=lease.identity)


@runtime_v3_router.post(
    RUNTIME_V3_RETRIEVE_PATH,
    response_model=RetrieveResponseV3,
)
def runtime_v3_retrieve(
    payload: RetrieveRequestV3,
    request: Request,
    response: Response,
) -> RetrieveResponseV3:
    lease = _lease(request)
    deadline = _deadline(request)
    if runtime_v3_binding_header_mismatches(request.headers, lease.identity):
        raise HTTPException(412, "Evidence runtime v3 release binding does not match")
    try:
        backend = request.app.state.runtime_backend_factory(lease.base_lease, deadline)
        policy_resolver = MappingRequirementPolicyResolver(lease.policy_release.bindings())
        result = retrieve_evidence_v3(payload, backend, policy_resolver)
        if (
            result.knowledge_snapshot_id is not None
            and result.knowledge_snapshot_id
            != lease.identity.base_runtime_release.knowledge_snapshot_id
        ):
            raise RuntimeError("retrieval snapshot does not match runtime release")
        request.app.state.runtime_v3_release_loader.assert_unchanged(lease, deadline)
    except Exception as exc:
        print(
            f"[evidence] runtime v3 retrieval failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(503, "Evidence runtime v3 changed or became unavailable") from None
    for name, value in lease.identity.binding_headers().items():
        response.headers[name] = value
    # Echo the caller-owned request identity separately from the service's
    # own X-Service-Request-ID correlation value.  Consumers bind the body,
    # response headers, and original request before accepting Evidence.
    response.headers["X-Request-ID"] = payload.request_id
    return result


def configure_runtime_v3_application(
    target: FastAPI,
    *,
    runtime_release_loader: RuntimeV3LeaseLoaderProtocol,
    backend_factory: RuntimeBackendFactory,
    test_only_conformance: bool,
) -> FastAPI:
    target.state.runtime_v3_release_loader = runtime_release_loader
    target.state.runtime_backend_factory = backend_factory
    target.state.test_only_runtime_v3_conformance = test_only_conformance
    target.include_router(runtime_v3_router)
    return target


__all__ = [
    "BOUND_RUNTIME_V3_LEASE_STATE_KEY",
    "RUNTIME_V3_LIVENESS_PATH",
    "RUNTIME_V3_METRICS_PATH",
    "RUNTIME_V3_OPENAPI_PATH",
    "RUNTIME_V3_READINESS_PATH",
    "RUNTIME_V3_RETRIEVE_PATH",
    "configure_runtime_v3_application",
]
