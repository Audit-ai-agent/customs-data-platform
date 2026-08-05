"""A1 — 하이브리드 검색기 (에이전트 레이어 1단, LLM 불필요).

repository 의 두 arm(dense/bm25)을 RRF 로 융합하고 bge-reranker-v2-m3 로
재정렬해, 에이전트(Claude tool use)가 그대로 심사 근거로 쓰는 단일 랭킹
(RagEvidence 형태)을 만든다.

설계 불변식 '융합·rerank 는 저장소가 안 한다'에 따라 repository.py 밖(이 모듈)에
둔다 — repository 는 계속 arm 별 {article_pk, score, text} 만 반환하고,
이 모듈이 그 위에서 융합·재정렬·메타데이터 부착을 담당한다.

재질의 규칙(골든셋 실측): dense top-1 < LOW_CONF 면 저신뢰 — 일상어→법률어
사전으로 질의를 확장(치환 아님)해 한 번 더 검색하고, dense top-1 이 더 높은
쪽을 취한다. 사전에는 블라인드 테스트에서 실측된 어휘갭만 등재한다.

점수 의미(계약): evidence.score 는 reranker 의 sigmoid 정규화 점수 [0,1].
reranker 불가 시(reranked=false) score 는 None 이고 순위는 RRF. dense/bm25/rrf
점수는 스케일이 서로 달라 단일 임계값을 적용하면 안 된다.

실행(스모크): law_repository/ 에서  python -m src.hybrid "질의문"
"""

from __future__ import annotations

from datetime import date

from src import repository as repo
from src.embed import rerank

BM25_VECTOR = repo.BM25_VECTOR
DENSE_VECTOR = repo.DENSE_VECTOR

RRF_K = 60  # RRF 상수 (관행값)
RECALL = 20  # arm 당 후보 회수량
RERANK_POOL = 10  # rerank 에 넣을 RRF 상위 후보 수 (CPU cross-encoder 비용 상한)
RERANK_CHARS = 2000  # rerank 입력 지문 절단 길이 (max_length 1024 토큰과 짝)
LOW_CONF = 0.62  # dense top-1 이 이 미만이면 저신뢰 (골든셋 실측 경계)

# 일상어 → 법률어 (2026-07-06 골든셋 블라인드 테스트 실측 어휘갭만 등재)
LEXICON = {
    "샘플": "견본품",
    "잘못 매": "경정",  # "세금 잘못 매김/매겨졌다" 류
}


def rewrite_query(query: str) -> str:
    """질의에 법률어를 덧붙이는 확장. 원 표현도 검색 신호라 치환하지 않는다."""
    extra = [t for k, t in LEXICON.items() if k in query and t not in query]
    return f"{query} {' '.join(extra)}" if extra else query


def rrf_fuse(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion — pk → Σ 1/(k + rank). rank 는 1부터."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for i, pk in enumerate(ranking):
            fused[pk] = fused.get(pk, 0.0) + 1.0 / (k + i + 1)
    return fused


# ------------------------------------------------------------- 후보 수집


class _Candidates:
    """한 질의의 arm 결과 + RRF 융합 상태."""

    def __init__(self, query: str, as_of: date | None):
        dense_hits = repo.search(query, arm=DENSE_VECTOR, limit=RECALL, as_of=as_of)
        bm25_hits = repo.search(query, arm=BM25_VECTOR, limit=RECALL, as_of=as_of)
        self.fused = rrf_fuse(
            [[h.article_pk for h in dense_hits], [h.article_pk for h in bm25_hits]]
        )
        self.order = sorted(self.fused, key=self.fused.get, reverse=True)
        self.texts = {h.article_pk: h.text for h in bm25_hits}
        self.texts.update({h.article_pk: h.text for h in dense_hits})
        self.dense_score = {h.article_pk: h.score for h in dense_hits}
        self.bm25_score = {h.article_pk: h.score for h in bm25_hits}
        self.dense_top1 = dense_hits[0].score if dense_hits else 0.0


def _article_meta(pks: list[int]) -> dict[int, dict]:
    """evidence 메타데이터(법령명·조문라벨·유효구간·버전). pk=버전 행이라
    as_of 검색이 돌려준 과거 버전 pk 도 그대로 조회된다."""
    if not pks:
        return {}
    from psycopg.rows import dict_row

    from src.db.postgres import connect

    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.id AS article_pk, a.law_id, l.law_name, l.hierarchy,
                   a.article_no, a.paragraph_no, a.title,
                   a.valid_from, a.valid_to, a.is_current, a.version_mst
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.id = ANY(%(pks)s)
            """,
            {"pks": pks},
        )
        return {r["article_pk"]: r for r in cur.fetchall()}


def _label(article_no: int, paragraph_no: int | None) -> str:
    lab = f"제{article_no // 100}조"
    if article_no % 100:
        lab += f"의{article_no % 100}"
    if paragraph_no:
        lab += f" 제{paragraph_no}항"
    return lab


# ------------------------------------------------------------------ 본체


def retrieve(
    query: str, limit: int = 5, as_of: date | None = None, use_rerank: bool = True
) -> dict:
    """하이브리드 검색 → 단일 랭킹 RagEvidence.

    반환: {query, used_query, rewritten, low_confidence, reranked, as_of, evidence[]}
    evidence 항목: evidence_id, article_pk, law_name, article_label, title,
    score(reranker [0,1] | None), rrf/dense/bm25 점수, 유효구간·version_mst(버전 인용),
    source_uri, text.
    """
    cand = _Candidates(query, as_of)
    used, rewritten = query, False
    low_confidence = cand.dense_top1 < LOW_CONF
    if low_confidence:
        rq = rewrite_query(query)
        if rq != query:
            cand2 = _Candidates(rq, as_of)
            if cand2.dense_top1 > cand.dense_top1:
                cand, used, rewritten = cand2, rq, True
                low_confidence = cand.dense_top1 < LOW_CONF

    pool = cand.order[: max(RERANK_POOL, limit)]
    rr_scores = (
        rerank.scores(used, [cand.texts[pk][:RERANK_CHARS] for pk in pool]) if use_rerank else None
    )
    reranked = rr_scores is not None
    score_of: dict[int, float] = {}
    if reranked:
        score_of = dict(zip(pool, rr_scores))
        pool = sorted(pool, key=lambda pk: score_of[pk], reverse=True)

    final = pool[:limit]
    meta = _article_meta(final)
    evidence = []
    for pk in final:
        m = meta.get(pk)
        if not m:  # PG 에 없는 pk (스토어 불일치) — 근거로 내보내지 않는다
            continue
        lab = _label(m["article_no"], m["paragraph_no"])
        evidence.append(
            {
                "evidence_id": f"law-{pk}",
                "source_type": "law",
                "article_pk": pk,
                "law_id": m["law_id"],
                "law_name": m["law_name"],
                "hierarchy": m["hierarchy"],
                "article_label": lab,
                "title": m["title"],
                "score": round(score_of[pk], 4) if reranked else None,
                "rrf_score": round(cand.fused[pk], 6),
                "dense_score": round(cand.dense_score[pk], 4) if pk in cand.dense_score else None,
                "bm25_score": round(cand.bm25_score[pk], 4) if pk in cand.bm25_score else None,
                "valid_from": str(m["valid_from"]),
                "valid_to": str(m["valid_to"]) if m["valid_to"] else None,
                "is_current": m["is_current"],
                "version_mst": m["version_mst"],
                "source_uri": f"https://www.law.go.kr/법령/{m['law_name']}/{lab.split(' ')[0]}",
                "text": cand.texts[pk],
            }
        )

    return {
        "query": query,
        "used_query": used,
        "rewritten": rewritten,
        "low_confidence": low_confidence,
        "reranked": reranked,
        "as_of": str(as_of) if as_of else None,
        "evidence": evidence,
    }


# ------------------------------------------------------------------ 스모크


def _demo(argv: list[str]) -> int:
    queries = argv or ["세관장확인대상물품", "샘플 무상 반입 관세"]
    for q in queries:
        r = retrieve(q, limit=5)
        flags = []
        if r["rewritten"]:
            flags.append(f"재질의→'{r['used_query']}'")
        if r["low_confidence"]:
            flags.append("저신뢰")
        if not r["reranked"]:
            flags.append("rerank 없음(RRF 순)")
        print(f"\n[hybrid] '{q}'" + (f"  ({', '.join(flags)})" if flags else ""))
        for e in r["evidence"]:
            s = f"{e['score']:.4f}" if e["score"] is not None else f"rrf {e['rrf_score']:.4f}"
            print(
                f"  {s}  {e['law_name']} {e['article_label']} ({e['title']})  [{e['evidence_id']}]"
            )
    return 0


if __name__ == "__main__":
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    sys.exit(_demo(sys.argv[1:]))
