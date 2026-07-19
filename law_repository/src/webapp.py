"""팀 공유용 웹 데모 서버 — 브라우저만으로 저장소를 써보게 한다.

호스트(서버 띄우는 사람) 한 명만 파이썬 환경·도커가 필요하고, 팀원은
http://<호스트IP>:8010 을 열면 검색/품목요건/조문조회를 바로 쓸 수 있다.

실행: law_repository/ 에서  python -m src.webapp   [--port 8010]
- 기동 시 bge-m3 를 미리 로딩(첫 검색 지연 제거).
- 임베딩 encode 는 스레드 안전하지 않아 lock 으로 직렬화.
- 포트는 --port 또는 PORT 환경변수 (HF Spaces 등 PaaS 는 PORT 로 지정).
- 접근 보호: DEMO_TOKEN 환경변수를 설정하면 모든 /api 가 토큰을 요구한다.
  팀원에겐 http://<주소>/?token=<값> 링크를 공유하면 UI 가 알아서 붙인다.
  (LAN 데모는 비워도 되지만, 공개 인터넷에 올릴 땐 반드시 설정하거나 Space 를 private 으로.)
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from src import hybrid
from src import repository as repo
from src.cli import LAW_ALIAS, _parse_article_no
from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR
from src.evidence import (
    DatabaseEvidenceBackend,
    ReadinessBackend,
    RetrieveRequest,
    RetrieveResponse,
    retrieve_evidence,
)
from src.provider_identity import (
    ProviderBuildIdentity,
    ReadinessResponse,
    binding_header_mismatches,
    load_provider_build_identity,
)

app = FastAPI(title="관세법령 저장소 데모")
_internal_router = APIRouter()
_encode_lock = threading.Lock()
_INDEX_HTML = (Path(__file__).parent / "webui.html").read_text(encoding="utf-8")
_DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")
_EVIDENCE_RETRIEVE_PATH = "/internal/rag/v1/retrieve"
_BOUND_IDENTITY_STATE_KEY = "_evidence_provider_build_identity"


def _binding_preflight(
    *,
    provider_app: FastAPI,
    headers: Mapping[str, str],
) -> tuple[ProviderBuildIdentity | None, JSONResponse | None]:
    """Validate provider identity using headers only, before body consumption."""
    try:
        identity = provider_app.state.provider_identity_loader()
    except Exception as exc:
        # Response/logs deliberately omit the raw exception: it can contain a
        # deployment path or secret and is not useful to an Agent.
        print(
            f"[evidence] provider identity unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        return None, JSONResponse(
            {"detail": "Evidence provider build identity is unavailable"},
            status_code=503,
        )
    if binding_header_mismatches(headers, identity):
        return None, JSONResponse(
            {"detail": "Evidence provider build binding does not match"},
            status_code=412,
        )
    return identity, None


class _EvidenceBindingMiddleware:
    """Fail closed at the raw ASGI boundary without receiving a request body."""

    def __init__(self, app: ASGIApp, *, provider_app: FastAPI) -> None:
        self.app = app
        self.provider_app = provider_app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == _EVIDENCE_RETRIEVE_PATH
        ):
            identity, rejection = _binding_preflight(
                provider_app=self.provider_app,
                headers=Headers(scope=scope),
            )
            if rejection is not None:
                await rejection(scope, receive, send)
                return
            scope.setdefault("state", {})[_BOUND_IDENTITY_STATE_KEY] = identity
        await self.app(scope, receive, send)


@app.middleware("http")
async def _token_guard(request: Request, call_next):
    if _DEMO_TOKEN and request.url.path.startswith(("/api", "/internal")):
        supplied = request.query_params.get("token") or request.headers.get("x-demo-token")
        if supplied != _DEMO_TOKEN:
            return JSONResponse({"detail": "token 필요 (?token=... 링크로 접속)"}, status_code=401)
    # The shared demo app already owns this outer token middleware.  Perform
    # the Evidence preflight here, after authentication, so the demo's access
    # behavior stays unchanged while invalid bindings still precede body reads.
    if request.method == "POST" and request.url.path == _EVIDENCE_RETRIEVE_PATH:
        identity, rejection = _binding_preflight(
            provider_app=app,
            headers=request.headers,
        )
        if rejection is not None:
            return rejection
        setattr(request.state, _BOUND_IDENTITY_STATE_KEY, identity)
    return await call_next(request)


def _as_of(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "as_of 형식은 YYYY-MM-DD")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML


@app.get("/api/search")
def api_search(q: str, limit: int = 5, as_of: str | None = None) -> dict:
    d = _as_of(as_of)
    out = {}
    with _encode_lock:  # dense encode 직렬화 (bm25 도 같은 락이면 충분히 가볍다)
        for arm in (DENSE_VECTOR, BM25_VECTOR):
            hits = repo.search(q, arm=arm, limit=min(limit, 20), as_of=d)
            out[arm] = [
                {
                    "pk": h.article_pk,
                    "score": round(h.score, 4),
                    "head": h.text.splitlines()[0].strip(),
                    "text": h.text,
                }
                for h in hits
            ]
    return out


@app.get("/api/retrieve")
def api_retrieve(q: str, limit: int = 5, as_of: str | None = None, rerank: bool = True) -> dict:
    """A1 하이브리드 검색 — 에이전트(tool use)용 단일 랭킹 RagEvidence.

    /api/search 는 arm 별 결과를 나란히 보여주는 데모/디버그용이고,
    에이전트 연동은 이 엔드포인트를 쓴다 (docs/API계약_에이전트연동.md).
    """
    with _encode_lock:
        return hybrid.retrieve(q, limit=min(limit, 10), as_of=_as_of(as_of), use_rerank=rerank)


@app.get("/api/hsk")
def api_hsk(code: str, trade: str | None = None, as_of: str | None = None) -> dict:
    return repo.hsk_requirements(code, trade_type=trade or None, as_of=_as_of(as_of))


_law_names: dict[str, str] = {}  # law_id → 법령명 (laws 테이블 캐시)


def _load_law_names() -> dict[str, str]:
    from src.db.postgres import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT law_id, law_name FROM laws")
        return dict(cur.fetchall())


def _resolve_law(law: str) -> str | None:
    """법/영/규칙 별칭, 법령ID, 법령명(공백 무시) → law_id. 미적재 법령이면 None."""
    global _law_names
    if not _law_names:
        _law_names = _load_law_names()
    lid = LAW_ALIAS.get(law, law)
    if lid in _law_names:
        return lid
    squashed = law.replace(" ", "")
    for law_id, name in _law_names.items():
        if name.replace(" ", "") == squashed:
            return law_id
    # 캐시가 낡았을 수 있음(델타로 법령 추가) — 1회 갱신 후 재시도
    _law_names = _load_law_names()
    if lid in _law_names:
        return lid
    return next((i for i, n in _law_names.items() if n.replace(" ", "") == squashed), None)


def _request_identity(request: Request) -> ProviderBuildIdentity:
    bound_identity: Any = getattr(
        request.state,
        _BOUND_IDENTITY_STATE_KEY,
        None,
    )
    if isinstance(bound_identity, ProviderBuildIdentity):
        return bound_identity
    try:
        return request.app.state.provider_identity_loader()
    except Exception as exc:
        # Response/logs deliberately omit the raw exception: it can contain a
        # deployment path and is not needed by an Agent to decide fail-closed.
        print(
            f"[evidence] provider identity unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail="Evidence provider build identity is unavailable",
        ) from None


@_internal_router.post("/internal/rag/v1/retrieve", response_model=RetrieveResponse)
def internal_retrieve(
    payload: RetrieveRequest,
    request: Request,
    response: Response,
) -> RetrieveResponse:
    """AI Agent와 Data/RAG 경계의 버전 고정 Evidence API."""
    identity = _request_identity(request)
    if binding_header_mismatches(request.headers, identity):
        raise HTTPException(
            status_code=412,
            detail="Evidence provider build binding does not match",
        )
    backend = request.app.state.evidence_backend_factory()
    result = retrieve_evidence(payload, backend)
    for name, value in identity.binding_headers().items():
        response.headers[name] = value
    return result


@_internal_router.get("/internal/rag/v1/health/live")
def internal_liveness() -> dict:
    return {"status": "alive"}


@_internal_router.get(
    "/internal/rag/v1/health/ready",
    response_model=ReadinessResponse,
)
def internal_readiness(request: Request) -> ReadinessResponse:
    identity = _request_identity(request)
    try:
        readiness = request.app.state.evidence_backend_factory().readiness()
        return ReadinessResponse.model_validate(
            {**readiness, "evidence_contract": identity.model_dump()}
        )
    except Exception as exc:
        print(
            f"[evidence] readiness failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        raise HTTPException(
            status_code=503,
            detail="Evidence provider is not ready",
        ) from None


def _configure_evidence_application(
    target: FastAPI,
    *,
    backend_factory: Callable[[], ReadinessBackend],
    identity_loader: Callable[[], ProviderBuildIdentity],
    test_only_conformance: bool,
    install_binding_middleware: bool = True,
) -> FastAPI:
    target.state.evidence_backend_factory = backend_factory
    target.state.provider_identity_loader = identity_loader
    target.state.test_only_conformance = test_only_conformance
    target.include_router(_internal_router)
    if target is not app and install_binding_middleware:
        target.add_middleware(
            _EvidenceBindingMiddleware,
            provider_app=target,
        )
    return target


def create_evidence_application() -> FastAPI:
    """Create the bare contract app with no fixture activation path.

    This factory intentionally preserves the v1.1 contract test boundary.  It
    does not provide service authentication, transport enforcement, admission,
    or lifecycle controls and therefore must not be deployed directly.  Use
    ``src.production.create_production_evidence_application`` for deployment.
    """
    target = FastAPI(title="Customs Evidence API")
    return _configure_evidence_application(
        target,
        backend_factory=DatabaseEvidenceBackend,
        identity_loader=load_provider_build_identity,
        test_only_conformance=False,
    )


def create_test_conformance_application(
    *,
    identity: ProviderBuildIdentity,
    backend: ReadinessBackend | None = None,
) -> FastAPI:
    """Create an isolated test-only app; production never calls this factory."""
    from src.conformance import TestOnlyConformanceEvidenceBackend

    selected_backend = backend or TestOnlyConformanceEvidenceBackend()
    target = FastAPI(title="TEST ONLY — Evidence API conformance")
    return _configure_evidence_application(
        target,
        backend_factory=lambda: selected_backend,
        identity_loader=lambda: identity,
        test_only_conformance=True,
    )


_configure_evidence_application(
    app,
    backend_factory=DatabaseEvidenceBackend,
    identity_loader=load_provider_build_identity,
    test_only_conformance=False,
)


@app.get("/api/article")
def api_article(law: str, no: str, as_of: str | None = None) -> dict:
    law_id = _resolve_law(law)
    if law_id is None:
        raise HTTPException(400, "law 는 법/영/규칙 별칭, 법령ID, 또는 적재된 법령명")
    try:
        art_no = _parse_article_no(no)
    except ValueError:
        raise HTTPException(400, f"조번호 해석 불가: {no}")
    row = repo.resolve_as_of(law_id, art_no, None, as_of=_as_of(as_of))
    if row is None:
        return {"found": False}
    edges = []
    for e in repo.expand_article(row["article_pk"]):
        t_no = e["article_no"]
        label = (
            (
                f"{_law_names.get(e['law_id'], e['law_id'])} 제{t_no // 100}조"
                + (f"의{t_no % 100}" if t_no % 100 else "")
            )
            if t_no
            else (e["title"] or "")
        )
        edges.append({"rel": e["rel"], "pk": e["article_pk"], "label": label, "title": e["title"]})
    return {
        "found": True,
        "pk": row["article_pk"],
        "law": _law_names[law_id],
        "title": row["title"],
        "valid_from": str(row["valid_from"]),
        "valid_to": str(row["valid_to"]) if row["valid_to"] else None,
        "is_current": row["is_current"],
        "content": row["content"],
        "edges": edges,
    }


def main() -> int:
    import uvicorn

    # cp949 콘솔에서 유니코드 출력 크래시 방어 (ccct 988664b 와 동일)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    port = int(os.environ.get("PORT", "8010"))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print("[webapp] bge-m3 예열 중… (수십 초)")
    from src.embed import dense, rerank

    dense.encode(["예열"])
    print("[webapp] reranker 예열 중… (최초 1회는 모델 다운로드 ≈2.3GB)")
    if not rerank.available():
        print("[webapp] reranker 불가 — /api/retrieve 는 RRF 순위로 동작 (reranked=false)")
    print(f"[webapp] 준비 완료 — 팀원 접속: http://<이 PC의 IP>:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
