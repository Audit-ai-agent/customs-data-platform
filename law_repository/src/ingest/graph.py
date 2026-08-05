"""Phase 5+ — 조문 위임/인용 그래프 (설계 §4.3(1), 로드맵 Phase 5 'Neo4j 위임/인용 확장').

현행 조문을 :Article 노드로 만들고, 조문 본문에서 상호참조를 추출해 두 종류의 엣지를 건다.
Phase 5 `repository.expand_article()` 가 이 그래프 위에서 동작한다.

노드
  (:Article {article_pk, law_id, law_name, hierarchy, article_no, title})
    article_pk = PG law_articles.id (현행 whole-article 행). PG·Qdrant 와 동일 키.

엣지 (설계 §4.3(1))
  (:Article)-[:CITES     {basis, count}]->(:Article)   -- 동일 법령 내 조문 상호참조
  (:Article)-[:DELEGATES {basis, count}]->(:Article)   -- 법령 위계를 가로지르는 참조
      방향은 항상 상위법령→하위법령(법률→시행령→시행규칙). 인용 방향과 무관하게
      구조적 위임/시행 관계로 본다. 상위 조문이 하위 조문으로 세부를 위임한 링크.

참조 추출 (실측 기반, 2026-07-12 전 법령 일반화)
  - 헤딩 라인 `[법령명 라벨(제목)]` 은 자기 라벨이라 제거(관세'법 제1조' 꼬리 오탐 방지).
  - 「」 낫표 제거. 코퍼스에 적재된 모든 법령명(공백 유무 변형 포함)을 §법령ID§
    토큰으로 치환 — 긴 이름 우선이라 '관세법 시행령'이 '관세법'보다 먼저 잡힌다.
  - 한정어 해석은 출발 법령의 패밀리(본법·시행령·시행규칙, 이름 접미사로 판정) 상대적:
    · 이 법·같은 법(영·규칙 동형) → 출발 조문의 법령 (기존 규칙 유지).
    · bare 법/영/규칙·시행령·시행규칙 → 출발 법령 패밀리의 해당 위계 (미적재면 스킵).
    · 법령명 토큰 → 그 법령. 코퍼스 밖 법령(외국환거래법 등) → 스킵. self 참조 스킵.
  - 분류: 동일 법령=CITES · 동일 패밀리 위계교차=DELEGATES(항상 상위→하위) ·
    타 패밀리=REFERS_TO(인용 방향 그대로 — 예: 수입식품법→식품위생법).

DETAILED_IN(확인법령↔조문)은 확인법령 조문 매핑 근거가 아직 없어 만들지 않는다.
(특수물자 개별법령 자체는 2026-07-12 부터 코퍼스에 있음 — REFERS_TO 로 연결됨.)

실행: law_repository/ 에서  python -m src.ingest.graph  [--nodes-only|--edges-only|--keep]
"""

from __future__ import annotations

import re
import sys

from src.lawgo import jo_code

# 위계 rank (작을수록 상위). DELEGATES 는 상위→하위.
HIER_RANK = {"법률": 1, "시행령": 2, "시행규칙": 3}

_FAMILY_SUFFIX = re.compile(r"\s*시행(령|규칙)$")


def connect():
    """Import the optional PostgreSQL driver only for store work."""

    from src.db.postgres import connect as open_connection

    return open_connection()


def make_driver():
    """Import the optional Neo4j driver only for store work."""

    from src.db.neo4j import driver as open_driver

    return open_driver()


class LawRegistry:
    """코퍼스에 적재된 법령 명부 — 한정어·법령명 해석의 단일 소스.

    laws: {law_id: (law_name, hierarchy)}. hierarchy ∈ HIER_RANK 키.
    """

    def __init__(self, laws: dict[str, tuple[str, str]]):
        self.name_of = {lid: n for lid, (n, _h) in laws.items()}
        self.hier_of = {lid: h for lid, (_n, h) in laws.items()}
        self.base_of = {lid: _FAMILY_SUFFIX.sub("", n) for lid, (n, _h) in laws.items()}
        self.family: dict[str, dict[str, str]] = {}
        for lid, (_n, h) in laws.items():
            self.family.setdefault(self.base_of[lid], {})[h] = lid
        # 공백제거 명칭 → law_id (한정어로 붙은 '관세법시행령'·'약사법' 해석용)
        self.by_squashed = {re.sub(r"\s+", "", n): lid for lid, (n, _h) in laws.items()}
        # 본문 내 법령명 토큰화 — 공백 유무 변형 포함, 긴 이름 우선
        variants: dict[str, str] = {}
        for lid, (n, _h) in laws.items():
            variants[n] = lid
            variants[re.sub(r"\s+", "", n)] = lid
        alts = sorted(variants, key=len, reverse=True)
        self._token_pat = re.compile("(?<![가-힣])(" + "|".join(re.escape(a) for a in alts) + ")")
        self._variants = variants

    def tokenize(self, body: str) -> str:
        return self._token_pat.sub(lambda m: f"§{self._variants[m.group(1)]}§", body)

    def family_member(self, source_law_id: str, hierarchy: str) -> str | None:
        return self.family.get(self.base_of[source_law_id], {}).get(hierarchy)


def _fetch_registry() -> LawRegistry:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT law_id, law_name, hierarchy FROM laws")
        return LawRegistry({lid: (n, h) for lid, n, h in cur.fetchall()})


# 한정어(law qualifier) 후보. 긴/구체적인 것 먼저. 법령명은 사전 토큰화(§ID§)됨.
_Q = (
    r"이\s?법|같은\s?법|이\s?영|같은\s?영|이\s?규칙|같은\s?규칙|"
    r"시행규칙|시행령|[가-힣]{2,12}법|법|영|규칙"
)
_REF = re.compile(rf"(?:§(?P<lid>\d{{6}})§\s*|(?P<q>{_Q})\s*)?제(?P<no>\d+)조(?:의(?P<b>\d+))?")


def _resolve_qualifier(q: str | None, source_law_id: str, reg: LawRegistry) -> str | None:
    """한정어 → 대상 법령ID. 코퍼스 밖이면 None(스킵). None 한정어 = 동일 법령."""
    if q is None:
        return source_law_id
    qn = q.replace(" ", "")
    if qn in ("이법", "같은법", "이영", "같은영", "이규칙", "같은규칙"):
        return source_law_id  # 기존 규칙 유지: 출발 조문의 법령
    if qn in ("시행규칙", "규칙"):
        return reg.family_member(source_law_id, "시행규칙")
    if qn in ("시행령", "영"):
        return reg.family_member(source_law_id, "시행령")
    if qn == "법":
        return reg.family_member(source_law_id, "법률")
    return reg.by_squashed.get(qn)  # '관세법시행령'·'약사법' 등 명칭 직접 / 외부 법령 None


def _strip_heading(content: str) -> str:
    body = content
    if body.startswith("["):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    return body.replace("「", "").replace("」", "")


def extract_refs(content: str, source_law_id: str, source_art_no: int, reg: LawRegistry):
    """조문 본문 → {(대상 law_id, 대상 article_no): basis} 참조 집합. self/외부는 제외."""
    body = reg.tokenize(_strip_heading(content))
    out: dict[tuple[str, int], str] = {}
    for m in _REF.finditer(body):
        if m.group("lid"):
            t_law = m.group("lid") if m.group("lid") in reg.name_of else None
        else:
            t_law = _resolve_qualifier(m.group("q"), source_law_id, reg)
        if t_law is None:
            continue
        t_art = jo_code(m.group("no"), m.group("b") or 0)
        if (t_law, t_art) == (source_law_id, source_art_no):
            continue  # self
        basis = re.sub(r"§(\d{6})§", lambda mm: reg.name_of[mm.group(1)] + " ", m.group())
        out.setdefault((t_law, t_art), basis.strip())
    return out


# ------------------------------------------------------------ Neo4j 적재

_NODE_CYPHER = """
UNWIND $rows AS r
MERGE (a:Article {article_pk: r.article_pk})
SET a.law_id = r.law_id, a.law_name = r.law_name, a.hierarchy = r.hierarchy,
    a.article_no = r.article_no, a.title = r.title
"""

_EDGE_CYPHER = """
UNWIND $rows AS r
MATCH (s:Article {article_pk: r.s}), (t:Article {article_pk: r.t})
MERGE (s)-[e:%s]->(t)
SET e.basis = r.basis, e.count = r.count
"""


def _fetch_current_articles():
    """현행 whole-article 행(분할 자식 제외). 반환: node dict + (law_id,art_no)->pk 맵 + 본문."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.law_id, l.law_name, l.hierarchy, a.article_no, a.title, a.content
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.is_current AND a.paragraph_no IS NULL
            ORDER BY a.id
            """
        )
        return cur.fetchall()


def build(keep: bool = False, nodes: bool = True, edges: bool = True) -> None:
    reg = _fetch_registry()
    rows = _fetch_current_articles()
    node_rows = [
        {
            "article_pk": r[0],
            "law_id": r[1],
            "law_name": r[2],
            "hierarchy": r[3],
            "article_no": r[4],
            "title": r[5],
        }
        for r in rows
    ]
    pk_by_key = {(r[1], r[4]): r[0] for r in rows}

    drv = make_driver()
    try:
        with drv.session() as s:
            if not keep and nodes:
                s.run("MATCH (a:Article) DETACH DELETE a")  # 결정적 재구축
            s.run(
                "CREATE CONSTRAINT article_pk IF NOT EXISTS "
                "FOR (a:Article) REQUIRE a.article_pk IS UNIQUE"
            )
            if nodes:
                for i in range(0, len(node_rows), 500):
                    s.run(_NODE_CYPHER, rows=node_rows[i : i + 500])
                print(f"[graph] :Article 노드 {len(node_rows)}건")

            if edges:
                cites, delegates, refers = [], [], []
                for r in rows:
                    pk, law_id, _, _, art_no, _, content = r
                    for (t_law, t_art), basis in extract_refs(content, law_id, art_no, reg).items():
                        t_pk = pk_by_key.get((t_law, t_art))
                        if t_pk is None:  # 대상 조문이 현행 노드에 없음(폐지/미적재)
                            continue
                        if t_law == law_id:
                            cites.append({"s": pk, "t": t_pk, "basis": basis})
                        elif reg.base_of[law_id] == reg.base_of[t_law]:
                            # 동일 패밀리 위계 교차 → 상위→하위 위임
                            hi, lo = (
                                (pk, t_pk)
                                if HIER_RANK[reg.hier_of[law_id]] < HIER_RANK[reg.hier_of[t_law]]
                                else (t_pk, pk)
                            )
                            delegates.append({"s": hi, "t": lo, "basis": basis})
                        else:  # 타 패밀리 법령 인용 — 방향 그대로
                            refers.append({"s": pk, "t": t_pk, "basis": basis})
                _write_edges(s, "CITES", _dedupe(cites))
                _write_edges(s, "DELEGATES", _dedupe(delegates))
                _write_edges(s, "REFERS_TO", _dedupe(refers))
                print(
                    f"[graph] CITES {len(_dedupe(cites))} · DELEGATES {len(_dedupe(delegates))}"
                    f" · REFERS_TO {len(_dedupe(refers))}"
                )
    finally:
        drv.close()


def _dedupe(edges: list[dict]) -> list[dict]:
    """(s,t) 별 중복 병합 — count 누적, basis 는 첫 표현."""
    agg: dict[tuple[int, int], dict] = {}
    for e in edges:
        key = (e["s"], e["t"])
        if key in agg:
            agg[key]["count"] += 1
        else:
            agg[key] = {**e, "count": 1}
    return list(agg.values())


def _write_edges(s, rel: str, rows: list[dict]) -> None:
    for i in range(0, len(rows), 500):
        s.run(_EDGE_CYPHER % rel, rows=rows[i : i + 500])


# ------------------------------------------------------------ 검증


def verify() -> None:
    drv = make_driver()
    try:
        with drv.session() as s:
            n = s.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"]
            c = s.run("MATCH ()-[e:CITES]->() RETURN count(e) AS c").single()["c"]
            d = s.run("MATCH ()-[e:DELEGATES]->() RETURN count(e) AS c").single()["c"]
            rf = s.run("MATCH ()-[e:REFERS_TO]->() RETURN count(e) AS c").single()["c"]
            print(f"[verify] :Article {n} | CITES {c} | DELEGATES {d} | REFERS_TO {rf}")
            # 위임 방향 불변식: 시작이 끝보다 상위 위계여야 (hierarchy 기반 — 전 법령)
            bad = s.run(
                """
                MATCH (hi:Article)-[:DELEGATES]->(lo:Article)
                WITH hi, lo,
                  CASE hi.hierarchy WHEN '법률' THEN 1 WHEN '시행령' THEN 2 ELSE 3 END AS rh,
                  CASE lo.hierarchy WHEN '법률' THEN 1 WHEN '시행령' THEN 2 ELSE 3 END AS rl
                WHERE rh >= rl RETURN count(*) AS bad
                """
            ).single()["bad"]
            print(f"[verify] DELEGATES 방향 위반(상위→하위 아님): {bad}")
            print("[sample] 타 법령 인용(REFERS_TO) 상위 5:")
            for r in s.run(
                """
                MATCH (s:Article)-[e:REFERS_TO]->(t:Article)
                RETURN s.law_name AS sl, s.article_no AS sa,
                       t.law_name AS tl, t.article_no AS ta LIMIT 5
                """
            ):
                print(f"  {r['sl']} 제{r['sa'] // 100}조 → {r['tl']} 제{r['ta'] // 100}조")
            print("\n[sample] 관세법 제226조가 위임한 하위 조문:")
            for r in s.run(
                """
                MATCH (:Article {law_id:'001556', article_no:22600})-[:DELEGATES]->(t:Article)
                RETURN t.law_name AS ln, t.article_no AS ano, t.title AS ti LIMIT 5
                """
            ):
                print(f"  → {r['ln']} 제{r['ano'] // 100}조({r['ti']})")
            print("[sample] 관세법 제226조가 인용한 동일법령 조문:")
            for r in s.run(
                """
                MATCH (:Article {law_id:'001556', article_no:22600})-[:CITES]->(t:Article)
                RETURN t.article_no AS ano, t.title AS ti LIMIT 5
                """
            ):
                print(f"  → 제{r['ano'] // 100}조({r['ti']})")
    finally:
        drv.close()


def main(argv: list[str]) -> int:
    for _stream in (sys.stdout, sys.stderr):  # cp949 콘솔에서 em-dash 등 크래시 방지
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    keep = "--keep" in argv
    nodes = "--edges-only" not in argv
    edges = "--nodes-only" not in argv
    build(keep=keep, nodes=nodes, edges=edges)
    verify()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
