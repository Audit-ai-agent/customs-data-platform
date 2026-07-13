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
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from src import repository as repo
from src.cli import LAW_ALIAS, LAW_NAME, _parse_article_no
from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR
from src.evidence import (
    DatabaseEvidenceBackend,
    RetrieveRequest,
    RetrieveResponse,
    retrieve_evidence,
)

app = FastAPI(title="관세법령 저장소 데모")
_encode_lock = threading.Lock()
_INDEX_HTML = (Path(__file__).parent / "webui.html").read_text(encoding="utf-8")
_DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")


@app.middleware("http")
async def _token_guard(request: Request, call_next):
    if _DEMO_TOKEN and request.url.path.startswith(("/api", "/internal")):
        supplied = request.query_params.get("token") or request.headers.get("x-demo-token")
        if supplied != _DEMO_TOKEN:
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "token 필요 (?token=... 링크로 접속)"}, status_code=401)
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
                {"pk": h.article_pk, "score": round(h.score, 4),
                 "head": h.text.splitlines()[0].strip(), "text": h.text}
                for h in hits
            ]
    return out


@app.get("/api/hsk")
def api_hsk(code: str, trade: str | None = None, as_of: str | None = None) -> dict:
    return repo.hsk_requirements(code, trade_type=trade or None, as_of=_as_of(as_of))


@app.post("/internal/rag/v1/retrieve", response_model=RetrieveResponse)
def internal_retrieve(payload: RetrieveRequest) -> RetrieveResponse:
    """AI Agent와 Data/RAG 경계의 버전 고정 Evidence API."""
    return retrieve_evidence(payload)


@app.get("/internal/rag/v1/health/live")
def internal_liveness() -> dict:
    return {"status": "alive"}


@app.get("/internal/rag/v1/health/ready")
def internal_readiness() -> dict:
    try:
        return DatabaseEvidenceBackend().readiness()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/article")
def api_article(law: str, no: str, as_of: str | None = None) -> dict:
    law_id = LAW_ALIAS.get(law, law)
    if law_id not in LAW_NAME:
        raise HTTPException(400, "law 는 법/영/규칙 또는 법령ID")
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
        label = (f"{LAW_NAME.get(e['law_id'], e['law_id'])} 제{t_no // 100}조"
                 + (f"의{t_no % 100}" if t_no % 100 else "")) if t_no else (e["title"] or "")
        edges.append({"rel": e["rel"], "pk": e["article_pk"],
                      "label": label, "title": e["title"]})
    return {
        "found": True,
        "pk": row["article_pk"],
        "law": LAW_NAME[law_id],
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
    from src.embed import dense

    dense.encode(["예열"])
    print(f"[webapp] 준비 완료 — 팀원 접속: http://<이 PC의 IP>:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
