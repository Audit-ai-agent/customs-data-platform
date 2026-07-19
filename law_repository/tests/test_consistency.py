"""Phase 6 — 정합성 검증 (로드맵: EXCLUDE 겹침·시점쿼리 정확성·delta 멱등·HSK 상속 누락).

실데이터(적재 완료된 3-스토어)를 대상으로 저장소 간 불변식을 전수 검사한다.
합성 데이터 단위검증은 test_schema.py(EXCLUDE 거부)·test_delta.py(델타 시나리오)가
담당하고, 여기는 "지금 들어있는 데이터"가 불변식을 만족하는지를 본다.

전제: docker compose 기동 + Phase 0~5 적재 완료. 미기동/미적재 시 skip.
delta 멱등 재실행(네트워크 잡)은 pytest 가 아니라 운영 절차로 검증:
  python -m src.sync.delta  두 번 → 두 번째가 "신규 버전 없음"이어야 한다.
여기서는 promote() 의 실데이터 멱등만 트랜잭션 롤백 안에서 검사한다.
"""

from __future__ import annotations

from datetime import date

import pytest

from src import repository as repo
from src.db.qdrant import COLLECTION
from src.ingest.graph import HIER_RANK

pytestmark = pytest.mark.live

# 조문 identity (설계 §4.1 EXCLUDE 키와 동일)
_KEY = "law_id, article_no, paragraph_no, item_no"


# ------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def pg():
    import psycopg

    from src.config import settings

    try:
        conn = psycopg.connect(settings.postgres_dsn, connect_timeout=3)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres 접속 불가 (docker compose up 필요): {e}")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM law_articles")
        if cur.fetchone()[0] == 0:
            conn.close()
            pytest.skip("law_articles 비어있음 (python -m src.ingest.laws 필요)")
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def qc():
    from src.db.qdrant import client

    try:
        c = client()
        if not c.collection_exists(COLLECTION) or c.count(COLLECTION).count == 0:
            pytest.skip("Qdrant 현행 인덱스 비어있음")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Qdrant 접속 불가: {e}")
    return c


@pytest.fixture(scope="module")
def neo():
    drv = repo.make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:  # noqa: BLE001
        drv.close()
        pytest.skip(f"Neo4j 접속 불가: {e}")
    yield drv
    drv.close()


@pytest.fixture(scope="module")
def qdrant_points(qc):
    """전 포인트 1회 scroll — (point_id, payload.article_pk) 목록."""
    out, offset = [], None
    while True:
        points, offset = qc.scroll(
            COLLECTION,
            limit=1024,
            offset=offset,
            with_payload=["article_pk"],
            with_vectors=False,
        )
        out.extend((str(p.id), p.payload["article_pk"]) for p in points)
        if offset is None:
            return out


def _indexable_rows(pg) -> list[tuple[int, str | None]]:
    """Qdrant 적재 대상 = 현행이면서 분할 자식이 없는 행 (laws.index_qdrant 와 동일 기준)."""
    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.qdrant_point_id::text FROM law_articles a
            WHERE a.is_current
              AND NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk = a.id)
            """
        )
        return cur.fetchall()


# ------------------------------------------------- A. PG 시간원장 불변식


def test_no_overlapping_intervals_in_real_data(pg):
    """유효구간 겹침 0건 — EXCLUDE 제약이 지켜온 것을 데이터 단에서 재확인."""
    with pg.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) FROM law_articles a JOIN law_articles b
              ON a.id < b.id
             AND a.law_id = b.law_id AND a.article_no = b.article_no
             AND a.paragraph_no IS NOT DISTINCT FROM b.paragraph_no
             AND a.item_no IS NOT DISTINCT FROM b.item_no
             AND daterange(a.valid_from, a.valid_to, '[)')
                 && daterange(b.valid_from, b.valid_to, '[)')
            """
        )
        assert cur.fetchone()[0] == 0


def test_is_current_iff_effective_today(pg):
    """is_current ⇔ 오늘 ∈ [valid_from, valid_to) — 전 행. 위반은 promote 잡 미실행 신호."""
    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM law_articles
            WHERE is_current <> (valid_from <= current_date
                                 AND (valid_to > current_date OR valid_to IS NULL))
            """
        )
        assert cur.fetchone()[0] == 0, (
            "is_current 와 유효구간 불일치 (python -m src.sync.delta 필요)"
        )


def test_qdrant_point_id_iff_indexable(pg):
    """qdrant_point_id NOT NULL ⇔ (현행 ∧ 분할 자식 없음). 구법·분할부모에 포인트가 남으면 위반."""
    with pg.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM law_articles a
            WHERE (a.qdrant_point_id IS NOT NULL)
              <> (a.is_current AND NOT EXISTS
                    (SELECT 1 FROM law_articles c WHERE c.parent_article_pk = a.id))
            """
        )
        assert cur.fetchone()[0] == 0


def test_as_of_boundaries_exactly_one_version(pg):
    """시점쿼리 정확성 — 다버전 조문의 모든 경계일에서 정확히 1버전, 반열림 준수.

    각 버전 v 에 대해 as_of=v.valid_from 은 v 만 매칭해야 하고(직전 버전은 그날 닫힘),
    as_of=v.valid_to 는 v 를 제외해야 한다(반열림 [from,to)).
    """
    with pg.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_KEY} FROM law_articles
            GROUP BY {_KEY} HAVING count(*) >= 2
            """
        )
        multi = cur.fetchall()
        if not multi:
            pytest.skip("다버전 조문 없음 (델타 반영 전)")
        for law_id, art_no, para_no, item_no in multi:
            cur.execute(
                """
                SELECT id, valid_from, valid_to FROM law_articles
                WHERE law_id=%s AND article_no=%s
                  AND paragraph_no IS NOT DISTINCT FROM %s
                  AND item_no IS NOT DISTINCT FROM %s
                ORDER BY valid_from
                """,
                (law_id, art_no, para_no, item_no),
            )
            versions = cur.fetchall()
            for pk, vfrom, vto in versions:
                for as_of, expect_self in ((vfrom, True), (vto, False)):
                    if as_of is None:
                        continue
                    pred, params = repo._temporal_predicate(as_of)
                    cur.execute(
                        f"""
                        SELECT id FROM law_articles
                        WHERE law_id=%(law_id)s AND article_no=%(art_no)s
                          AND paragraph_no IS NOT DISTINCT FROM %(para_no)s
                          AND item_no IS NOT DISTINCT FROM %(item_no)s
                          AND {pred}
                        """,
                        {
                            "law_id": law_id,
                            "art_no": art_no,
                            "para_no": para_no,
                            "item_no": item_no,
                            **params,
                        },
                    )
                    hits = [r[0] for r in cur.fetchall()]
                    assert len(hits) <= 1, (
                        f"조문 {law_id}/{art_no} as_of={as_of} 에 {len(hits)}버전"
                    )
                    if expect_self:
                        assert hits == [pk], f"as_of=valid_from({as_of}) 이 자기 버전을 못 찾음"
                    else:
                        assert pk not in hits, (
                            f"as_of=valid_to({as_of}) 에 닫힌 버전 혼입(반열림 위반)"
                        )


def test_promote_idempotent_on_real_data(pg):
    """promote() 재실행 멱등 — 1회 정렬 후 2회째는 반드시 no-op. 롤백으로 미영속."""
    from src.sync.delta import promote

    with pg.cursor() as cur:
        promote(cur, qc=None)  # 상태 정렬 (Qdrant 는 건드리지 않음)
        second = promote(cur, qc=None)
        assert second == {"demoted": 0, "promoted": 0}
    pg.rollback()


# ------------------------------------------------- B. PG ↔ Qdrant


def test_qdrant_count_matches_pg_indexable(pg, qc):
    assert qc.count(COLLECTION).count == len(_indexable_rows(pg))


def test_qdrant_point_ids_match_pg(pg, qdrant_points):
    """포인트 id 집합이 PG 의 qdrant_point_id 집합과 정확히 일치 (고아·누락 0)."""
    pg_ids = {pid for _, pid in _indexable_rows(pg) if pid}
    qd_ids = {pid for pid, _ in qdrant_points}
    assert qd_ids - pg_ids == set(), "Qdrant 에만 있는 고아 포인트"
    assert pg_ids - qd_ids == set(), "PG 는 포인트가 있다는데 Qdrant 에 없음"


def test_qdrant_payload_pks_match_pg(pg, qdrant_points):
    """payload.article_pk 집합 == PG 인덱싱 대상 pk 집합 (구법 pk 혼입 0)."""
    pg_pks = {pk for pk, _ in _indexable_rows(pg)}
    qd_pks = {pk for _, pk in qdrant_points}
    assert qd_pks == pg_pks


# ------------------------------------------------- C. PG ↔ Neo4j 조문 그래프


def _current_whole_articles(pg) -> dict[int, str]:
    """그래프 노드 대상 = 현행 whole-article (graph._fetch_current_articles 와 동일 기준)."""
    with pg.cursor() as cur:
        cur.execute("SELECT id, law_id FROM law_articles WHERE is_current AND paragraph_no IS NULL")
        return dict(cur.fetchall())


def test_graph_nodes_match_pg_current(pg, neo):
    expected = _current_whole_articles(pg)
    with neo.session() as s:
        got = {
            r["pk"]: r["law_id"]
            for r in s.run("MATCH (a:Article) RETURN a.article_pk AS pk, a.law_id AS law_id")
        }
    assert set(got) == set(expected), "Article 노드 ≠ PG 현행 whole-article (그래프 재구축 필요)"
    assert all(got[pk] == expected[pk] for pk in got), "노드 law_id 가 PG 와 불일치"


def test_graph_edge_invariants(neo):
    """self-loop 0 · CITES 동일법령 · DELEGATES 상위→하위(hierarchy) ·
    REFERS_TO 는 타 법령 간 — 전 엣지 (2026-07-12 전 법령 일반화)."""
    with neo.session() as s:
        loops = s.run(
            "MATCH (a:Article)-[r:CITES|DELEGATES|REFERS_TO]->(a) RETURN count(r) AS c"
        ).single()["c"]
        assert loops == 0

        bad_cites = s.run(
            "MATCH (s:Article)-[:CITES]->(t:Article) "
            "WHERE s.law_id <> t.law_id RETURN count(*) AS c"
        ).single()["c"]
        assert bad_cites == 0, "CITES 가 법령 경계를 넘음 (DELEGATES/REFERS_TO 여야 함)"

        bad_refers = s.run(
            "MATCH (s:Article)-[:REFERS_TO]->(t:Article) "
            "WHERE s.law_id = t.law_id RETURN count(*) AS c"
        ).single()["c"]
        assert bad_refers == 0, "REFERS_TO 가 동일 법령 내부에 있음 (CITES 여야 함)"

        pairs = list(
            s.run(
                "MATCH (s:Article)-[:DELEGATES]->(t:Article) "
                "RETURN DISTINCT s.hierarchy AS sh, t.hierarchy AS th"
            )
        )
    for r in pairs:
        assert HIER_RANK[r["sh"]] < HIER_RANK[r["th"]], (
            f"DELEGATES 방향 위반: {r['sh']} → {r['th']}"
        )


# ------------------------------------------------- D. HSK 상속 누락 체크
#
# 실측(2026-07-06): 소스 명칭 파일은 세분화되지 않은 HS6(leaf 가 hs6+'0000' 뿐인
# 코드, 예 010130)과 HS 체계 밖 행정코드(2424000000 이사화물)의 상위 명칭 행을
# 아예 갖지 않는다 — leaf 3,395건의 hs6, 1건의 heading4 조상 노드 부재는 소스
# 고유 특성이지 적재 누락이 아니다. 따라서 "모든 접두어 존재"가 아니라
# ① 소스 무손실 적재  ② HAS_REQUIREMENT가 repository 매칭으로 도달 가능을 불변식으로 삼는다.


def test_hsnode_codes_lossless_vs_source(neo):
    """HSNode 코드 집합 == 소스 xlsx 코드 집합 — 적재 손실·유령 노드 0.

    이 등식이 성립하면 '조상 노드 부재 = 소스에도 없음'이 자동으로 보장된다
    (상속 누락이 ingest 에서 생길 수 없음)."""
    from src.ingest.hsk import load_level_names

    source = set(load_level_names()["code"])
    with neo.session() as s:
        db = {r["c"] for r in s.run("MATCH (n:HSNode) RETURN n.code AS c")}
    assert db - source == set(), f"소스에 없는 유령 노드 {len(db - source)}건"
    assert source - db == set(), f"적재 누락 {len(source - db)}건"


def test_hsk_label_iff_leaf_level10(neo):
    """:HSK 라벨 ⇔ level 10 (leaf = 10자리만, 불변식 §2)."""
    q = """
    MATCH (n:HSNode)
    WHERE (n:HSK) <> (n.level = 10)
    RETURN count(n) AS bad
    """
    with neo.session() as s:
        assert s.run(q).single()["bad"] == 0


def test_requirement_sources_reachable_by_repository(neo):
    """HAS_REQUIREMENT 출발은 HSK10 leaf로만 한정된다."""
    q = """
    MATCH (s)-[:HAS_REQUIREMENT]->(:Requirement)
    WHERE NOT s:HSK OR s.level <> 10
    RETURN count(*) AS bad
    """
    with neo.session() as s:
        assert s.run(q).single()["bad"] == 0


def test_requirement_has_law_agency_document(neo):
    """모든 Requirement는 법령·승인기관·서류와 직접 연결된다."""
    q = """
    MATCH (req:Requirement {source_type: 'ccct'})
    WHERE NOT (req)-[:BASED_ON]->(:Law)
       OR NOT (req)-[:APPROVED_BY]->(:Agency)
       OR NOT (req)-[:REQUIRES_DOCUMENT]->(:Certificate)
    RETURN count(req) AS orphan
    """
    with neo.session() as s:
        assert s.run(q).single()["orphan"] == 0


def test_hsk_inherited_traverse_functional(neo):
    """요건 걸린 leaf 하나로 end-to-end traverse — 직접 요건 + 조상 계층명 반환."""
    with neo.session() as s:
        rec = s.run(
            "MATCH (h:HSK)-[:HAS_REQUIREMENT]->(:Requirement) RETURN h.code AS c LIMIT 1"
        ).single()
    if not rec:
        pytest.skip("Requirement 미적재")
    req = repo.hsk_requirements(rec["c"])
    assert any(q["source"] == "direct" for q in req["requirements"])
    assert req["ancestors"]["chapter2"]["name"], "조상 계층명 누락 (상속 경로 구멍)"
