"""조문 위임/인용 그래프 — 참조 추출 규칙·엣지 불변식·Phase 5 연동 검증.

전제(live): Neo4j 기동 + `python -m src.ingest.graph` 적재. 미기동/미구축 시 skip.
"""

from __future__ import annotations

import pytest

from src import repository as repo
from src.db.postgres import connect
from src.ingest import graph


# ------------------------------------------------- live fixture


@pytest.fixture(scope="module")
def gsession():
    drv = graph.make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:  # noqa: BLE001
        drv.close()
        pytest.skip(f"Neo4j 접속 불가: {e}")
    with drv.session() as s:
        if s.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"] == 0:
            pytest.skip("조문 그래프 미구축 (python -m src.ingest.graph 필요)")
        yield s
    drv.close()


@pytest.mark.live
def test_article_count_matches_current_pg(gsession):
    n = gsession.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"]
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM law_articles WHERE is_current AND paragraph_no IS NULL")
        pg = cur.fetchone()[0]
    assert n == pg


@pytest.mark.live
def test_no_self_loops(gsession):
    bad = gsession.run("MATCH (a:Article)-[r]->(a) RETURN count(r) AS c").single()["c"]
    assert bad == 0


@pytest.mark.live
def test_delegates_direction_hi_to_lo(gsession):
    """DELEGATES 는 항상 상위법령→하위법령(법률→시행령→시행규칙) — hierarchy 기반."""
    bad = gsession.run(
        """
        MATCH (hi:Article)-[:DELEGATES]->(lo:Article)
        WITH CASE hi.hierarchy WHEN '법률' THEN 1 WHEN '시행령' THEN 2 ELSE 3 END AS rh,
             CASE lo.hierarchy WHEN '법률' THEN 1 WHEN '시행령' THEN 2 ELSE 3 END AS rl
        WHERE rh >= rl RETURN count(*) AS bad
        """
    ).single()["bad"]
    assert bad == 0


@pytest.mark.live
def test_refers_to_crosses_family(gsession):
    """REFERS_TO 는 서로 다른 법령 간(동일 법령 금지). 패밀리 내부는 CITES/DELEGATES 몫."""
    bad = gsession.run(
        "MATCH (s:Article)-[:REFERS_TO]->(t:Article) WHERE s.law_id = t.law_id "
        "RETURN count(*) AS bad"
    ).single()["bad"]
    assert bad == 0


@pytest.mark.live
def test_cites_within_same_law(gsession):
    bad = gsession.run(
        "MATCH (s:Article)-[:CITES]->(t:Article) WHERE s.law_id <> t.law_id RETURN count(*) AS bad"
    ).single()["bad"]
    assert bad == 0


@pytest.mark.live
def test_known_citation_226_to_245(gsession):
    """관세법 제226조 본문의 '제245조제2항을 준용' → CITES(226→245)."""
    c = gsession.run(
        "MATCH (:Article {law_id:'001556', article_no:22600})"
        "-[:CITES]->(:Article {article_no:24500}) RETURN count(*) AS c"
    ).single()["c"]
    assert c >= 1


@pytest.mark.live
def test_expand_article_integration(gsession):
    """Phase 5 repository.expand_article 가 이 그래프 위에서 실제 확장을 반환."""
    rec = gsession.run("MATCH (a:Article)-[]->() RETURN a.article_pk AS pk LIMIT 1").single()
    out = repo.expand_article(rec["pk"])
    assert out
    for e in out:
        assert e["rel"] in ("CITES", "DELEGATES", "REFERS_TO")
        assert isinstance(e["article_pk"], int)
