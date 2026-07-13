"""Phase 5 — 조회 repository (에이전트 인터페이스). 설계 §5·§6, 로드맵 Phase 5.

에이전트가 호출하는 유일한 조회 계약. 세 저장소(PG 시간원장 / Qdrant 현행 인덱스 /
Neo4j 그래프)를 잇지만 융합·rerank 는 하지 않는다 — 설계 §4.2 대로 arm 별
{article_pk, score, text} 만 반환하고, 최종 정규화·RRF 융합·Reranker 는 에이전트 몫.

■ 유일 불변식(설계 §6): 모든 PG 시점 조회는 `_temporal_predicate()` 한 곳을 반드시
  거친다. 호출부가 temporal predicate 를 빼먹으면 구법이 현행에 혼입되므로, 생성 지점을
  1곳으로 가둔다. 반열림 [valid_from, valid_to), '현행(effective now)' ≠ '최신(valid_to NULL)'.

공개 API
- search(query, arm, limit, as_of):
    의미검색. arm ∈ {dense, bm25}. as_of=None → 현행(Qdrant 가 이미 현행만 담음).
    as_of=D → Qdrant 로 후보 회수 후 PG 에서 D 시점 버전으로 본문 확정(설계 §6).
- resolve_as_of(law_id, article_no, paragraph_no, as_of):
    반열림 시점 필터로 그 시점에 유효했던 딱 1버전.
- hsk_requirements(code, trade_type, as_of):
    HSK10 exact 또는 HS6의 단일 leaf만 해소해 Requirement 그래프를 조회.
    HS6의 leaf가 여러 개면 ambiguous로 반환하고 임의 0 패딩을 하지 않는다.
- expand_article(article_pk):
    Neo4j 조문 위임/인용(+확인법령 DETAILED_IN) 확장. 조문 그래프 미구축이면 [] (graceful).

실행(스모크): law_repository/ 에서  python -m src.repository
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Literal

from psycopg.rows import dict_row

from src.db.neo4j import driver as make_driver
from src.db.postgres import connect
from src.db.qdrant import BM25_VECTOR, COLLECTION, DENSE_VECTOR, client as qdrant_client
from src.embed import bm25

ARMS = (DENSE_VECTOR, BM25_VECTOR)


@dataclass
class Hit:
    """arm 반환 규격(설계 §4.2). 융합·rerank 없이 이 셋만 노출."""
    article_pk: int
    score: float
    text: str


# ------------------------------------------------------- temporal (유일 생성 지점)

def _temporal_predicate(as_of: date | None) -> tuple[str, dict]:
    """모든 PG 시점 조회가 거치는 유일한 temporal predicate 생성 지점 (설계 §6).

    반열림 [valid_from, valid_to). as_of=None → 현행(effective today).
    '현행(effective now)' ≠ '최신(valid_to IS NULL)' — 공포됐지만 미시행인
    미래 버전은 valid_from > today 라 여기서 제외된다.
    """
    d = as_of or date.today()
    return (
        "valid_from <= %(as_of)s AND (valid_to > %(as_of)s OR valid_to IS NULL)",
        {"as_of": d},
    )


def resolve_as_of(
    law_id: str, article_no: int, paragraph_no: int | None, as_of: date | None = None
) -> dict | None:
    """(law_id, article_no, paragraph_no) 의 as_of 시점 유효 버전 1건. EXCLUDE 제약이
    구간 겹침을 막으므로 최대 1건. 그 시점에 조문이 없었으면 None."""
    pred, params = _temporal_predicate(as_of)
    with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT id AS article_pk, law_id, article_no, paragraph_no, title,
                   content, version_mst, valid_from, valid_to, is_current
            FROM law_articles
            WHERE law_id = %(law_id)s AND article_no = %(article_no)s
              AND paragraph_no IS NOT DISTINCT FROM %(paragraph_no)s
              AND {pred}
            """,
            {"law_id": law_id, "article_no": article_no,
             "paragraph_no": paragraph_no, **params},
        )
        return cur.fetchone()


# ------------------------------------------------------------------- arm 검색

def _arm_query(qc, arm: str, query: str, limit: int):
    """단일 arm(dense|bm25) Qdrant 조회. 인코더는 적재와 동일 모듈 공유."""
    if arm == DENSE_VECTOR:
        from src.embed import dense

        dv = dense.encode([query])[0]
        return qc.query_points(
            COLLECTION, query=dv.tolist(), using=DENSE_VECTOR, limit=limit
        ).points
    if arm == BM25_VECTOR:
        from qdrant_client import models

        sidx, sval = bm25.encode_query(query)
        return qc.query_points(
            COLLECTION,
            query=models.SparseVector(indices=sidx, values=sval),
            using=BM25_VECTOR,
            limit=limit,
        ).points
    raise ValueError(f"알 수 없는 arm: {arm!r} (dense|bm25)")


def search(
    query: str, arm: str = DENSE_VECTOR, limit: int = 10, as_of: date | None = None
) -> list[Hit]:
    """의미검색 → arm 별 {article_pk, score, text}.

    현행(as_of=None): Qdrant 가 현행만 담으므로 payload 를 그대로 반환.
    특정일 감사(as_of=D): Qdrant 로 후보를 넉넉히 회수한 뒤, 각 후보를 PG 에서
    D 시점 버전으로 재확정해 그 시점 본문을 반환한다(설계 §6). D 에 존재하지
    않던 조문은 탈락.
    """
    qc = qdrant_client()
    recall = limit if as_of is None else max(limit * 3, limit)
    points = _arm_query(qc, arm, query, recall)
    if as_of is None:
        return [Hit(p.payload["article_pk"], p.score, p.payload["text"]) for p in points][:limit]
    out: list[Hit] = []
    for p in points:
        row = resolve_as_of(
            p.payload["law_id"], p.payload["article_no"],
            p.payload.get("paragraph_no"), as_of,
        )
        if row:
            out.append(Hit(row["article_pk"], p.score, row["content"]))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------- HSK traverse

HsResolutionStatus = Literal["exact", "unique_descendant", "ambiguous", "invalid", "missing"]


@dataclass(frozen=True)
class HskResolution:
    status: HsResolutionStatus
    normalized_input: str | None
    resolved_hsk10: str | None
    candidate_hsk10s: tuple[str, ...] = ()


def normalize_hsk(code: str | None) -> str | None:
    """HS 표기를 6자리 또는 10자리로만 정규화한다.

    공백·점·하이픈은 표기 구분자로 허용하지만 기타 문자, 자리 절단,
    우측 0 패딩은 허용하지 않는다. 6자리는 HSK10이 아닌 HS6 입력이다.
    """
    if code is None or not str(code).strip():
        return None
    raw = str(code).strip()
    if re.search(r"[^0-9.\-\s]", raw):
        raise ValueError("HS Code는 숫자와 공백/./- 구분자만 허용")
    digits = re.sub(r"[.\-\s]", "", raw)
    if len(digits) not in (6, 10) or not digits.isdigit():
        raise ValueError("HS Code는 숫자 6자리 또는 HSK 10자리여야 함")
    return digits


def resolve_hsk_candidates(code: str | None, leaf_codes: Iterable[str]) -> HskResolution:
    """DB 없이 검증 가능한 HS6/HSK10 resolution 정책."""
    try:
        normalized = normalize_hsk(code)
    except ValueError:
        return HskResolution("invalid", None, None)
    if normalized is None:
        return HskResolution("missing", None, None)
    leaves = tuple(sorted({c for c in leaf_codes if re.fullmatch(r"\d{10}", c or "")}))
    if len(normalized) == 10:
        if normalized in leaves:
            return HskResolution("exact", normalized, normalized, (normalized,))
        return HskResolution("invalid", normalized, None)
    candidates = tuple(c for c in leaves if c.startswith(normalized))
    if len(candidates) == 1:
        return HskResolution("unique_descendant", normalized, candidates[0], candidates)
    if len(candidates) > 1:
        return HskResolution("ambiguous", normalized, None, candidates)
    return HskResolution("invalid", normalized, None)


def resolve_hsk(code: str | None) -> HskResolution:
    """Neo4j HSK leaf 마스터를 기준으로 품목번호를 해소."""
    try:
        normalized = normalize_hsk(code)
    except ValueError:
        return HskResolution("invalid", None, None)
    if normalized is None:
        return HskResolution("missing", None, None)
    prefix = normalized if len(normalized) == 6 else None
    drv = make_driver()
    try:
        with drv.session() as s:
            if prefix:
                rows = s.run(
                    "MATCH (h:HSK) WHERE h.code STARTS WITH $prefix "
                    "RETURN h.code AS code ORDER BY code",
                    prefix=prefix,
                )
            else:
                rows = s.run(
                    "MATCH (h:HSK {code: $code}) RETURN h.code AS code",
                    code=normalized,
                )
            leaves = [r["code"] for r in rows]
    finally:
        drv.close()
    return resolve_hsk_candidates(normalized, leaves)


# 직접(10자리) + 상속(상위 자리) 요건. 요건은 leaf 뿐 아니라 상위 레벨 노드에 붙을 수
# 있어(설계 §4.3 자릿수 상속) src.code 로 속성 매칭. 기관 다중이면 collect 로 묶는다.
_REQ_CYPHER = """
OPTIONAL MATCH (src:HSK {code: $code10})-[:HAS_REQUIREMENT]->(req:Requirement)
OPTIONAL MATCH (req)-[:BASED_ON]->(law:Law)
OPTIONAL MATCH (req)-[:APPROVED_BY]->(ag:Agency)
OPTIONAL MATCH (req)-[:REQUIRES_DOCUMENT]->(cert:Certificate)
RETURN req.id AS requirement_id, req.trade_type AS trade_type,
       req.valid_from AS valid_from, req.valid_to AS valid_to,
       req.version_hash AS version_hash, req.snapshot_id AS snapshot_id,
       law.code AS law_code, law.name AS law_name,
       ag.code AS agency_code, ag.name AS agency_name,
       cert.code AS document_code, cert.name AS document
"""

# 조상 계층명(감사 설명용) — 접두어로 레벨 노드 조회. 모두 OPTIONAL 이라 1행 보장.
_ID_CYPHER = """
OPTIONAL MATCH (h:HSK {code: $code10})
OPTIONAL MATCH (a2:HSNode {code: $chapter2})
OPTIONAL MATCH (a4:HSNode {code: $heading4})
OPTIONAL MATCH (a6:HSNode {code: $hs6})
RETURN h.name_ko AS name_ko, h.name_en AS name_en,
       a2.name_ko AS chapter2_name, a4.name_ko AS heading4_name, a6.name_ko AS hs6_name
"""


def hsk_requirements(
    code: str, trade_type: str | None = None, as_of: date | None = None
) -> dict:
    """HSK 필요 서류: HSK → Requirement → 법령/기관/서류.

    요건은 valid_from/valid_to 기준일에 유효한 것만 반환하고, trade_type은
    wire(import/export)와 기존 표시값(수입/수출)을 모두 받는다.
    """
    resolution = resolve_hsk(code)
    code10 = resolution.resolved_hsk10
    if code10 is None:
        return {
            "hs_resolution": resolution.status,
            "resolved_hsk10": None,
            "candidate_hsk10s": list(resolution.candidate_hsk10s),
            "hsk10": None,
            "name_ko": None,
            "name_en": None,
            "ancestors": {},
            "requirements": [],
        }
    params = {
        "code10": code10,
        "hs6": code10[:6],
        "heading4": code10[:4],
        "chapter2": code10[:2],
    }
    as_of_str = (as_of or date.today()).isoformat()
    wire_trade_type = {"수입": "import", "수출": "export"}.get(trade_type, trade_type)

    drv = make_driver()
    try:
        with drv.session() as s:
            ident = s.run(_ID_CYPHER, **params).single()
            rows = list(s.run(_REQ_CYPHER, **params))
    finally:
        drv.close()

    requirements = []
    for r in rows:
        if not r["law_name"]:  # OPTIONAL MATCH 무매칭 행
            continue
        if wire_trade_type and r["trade_type"] != wire_trade_type:
            continue
        if r["valid_from"] and r["valid_from"] > as_of_str:
            continue  # 아직 시행 전 요건 제외 (요건도 temporal)
        if r["valid_to"] and r["valid_to"] < as_of_str:
            continue
        requirements.append({
            "trade_type": r["trade_type"],
            "requirement_id": r["requirement_id"],
            "law_code": r["law_code"],
            "law_name": r["law_name"],
            "document": r["document"],
            "document_code": r["document_code"],
            "agencies": [r["agency_name"]] if r["agency_name"] else [],
            "agency_codes": [r["agency_code"]] if r["agency_code"] else [],
            "valid_from": r["valid_from"],
            "valid_to": r["valid_to"],
            "version_hash": r["version_hash"],
            "knowledge_snapshot_id": r["snapshot_id"],
            "source": "direct",
            "level": 10,
        })

    return {
        "hs_resolution": resolution.status,
        "resolved_hsk10": code10,
        "candidate_hsk10s": list(resolution.candidate_hsk10s),
        "hsk10": code10,
        "name_ko": ident["name_ko"] if ident else None,
        "name_en": ident["name_en"] if ident else None,
        "ancestors": {
            "chapter2": {"code": params["chapter2"],
                         "name": ident["chapter2_name"] if ident else None},
            "heading4": {"code": params["heading4"],
                         "name": ident["heading4_name"] if ident else None},
            "hs6": {"code": params["hs6"],
                    "name": ident["hs6_name"] if ident else None},
        },
        "requirements": requirements,
    }


# ------------------------------------------------------ Neo4j 위임/인용 확장

_EXPAND_CYPHER = """
MATCH (a:Article {article_pk: $pk})-[rel]->(t)
RETURN type(rel) AS rel, properties(rel) AS props,
       labels(t) AS labels, t.article_pk AS article_pk,
       t.law_id AS law_id, t.article_no AS article_no,
       coalesce(t.title, t.name) AS title
"""


def expand_article(article_pk: int) -> list[dict]:
    """조문 그래프 확장 — 위임(DELEGATES)/인용(CITES)/확인법령(DETAILED_IN).

    설계 §4.3 의 조문 그래프는 별도 적재 단계에서 구축된다. 아직 미구축이면
    매칭이 없어 [] 를 반환(graceful) — 그래프가 생기면 코드 변경 없이 동작한다.
    """
    drv = make_driver()
    try:
        with drv.session() as s:
            return [dict(r) for r in s.run(_EXPAND_CYPHER, pk=article_pk)]
    finally:
        drv.close()


# ------------------------------------------------------------------ 스모크

def _demo() -> int:
    print("[repository/dense] '수입신고 시 세관장 확인이 필요한 물품'")
    for h in search("수입신고 시 세관장 확인이 필요한 물품", arm=DENSE_VECTOR, limit=3):
        head = h.text.splitlines()[0]
        print(f"  pk={h.article_pk} score={h.score:.4f} :: {head}")
    print("[repository/bm25] '세관장확인대상물품'")
    for h in search("세관장확인대상물품", arm=BM25_VECTOR, limit=3):
        head = h.text.splitlines()[0]
        print(f"  pk={h.article_pk} score={h.score:.4f} :: {head}")

    drv = make_driver()
    with drv.session() as s:
        rec = s.run(
            "MATCH (h:HSK)-[:HAS_REQUIREMENT]->(:Requirement) "
            "RETURN h.code AS c LIMIT 1"
        ).single()
    drv.close()
    if rec:
        req = hsk_requirements(rec["c"])
        anc = req["ancestors"]
        print(f"[repository/hsk] {req['hsk10']} ({req['name_ko']}) — 요건 {len(req['requirements'])}건")
        for q in req["requirements"][:3]:
            print(f"  [{q['trade_type']}/{q['source']}] {q['law_name']} / {q['document']} / {q['agencies']}")
        print(f"  조상: {anc['chapter2']['name']} > {anc['heading4']['name']} > {anc['hs6']['name']}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_demo())
