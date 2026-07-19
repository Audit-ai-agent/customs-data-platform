"""조문 위임/인용 그래프 — 참조 추출 규칙·엣지 불변식·Phase 5 연동 검증.

전제(live): Neo4j 기동 + `python -m src.ingest.graph` 적재. 미기동/미구축 시 skip.
"""

from __future__ import annotations

import pytest

from src import repository as repo
from src.db.postgres import connect
from src.ingest import graph


# ------------------------------------------------- 단위(DB 불필요)

# 코퍼스 축소판: 관세 패밀리 3법 + 특수물자 법령 2개 (레지스트리 단위검증용)
REG = graph.LawRegistry(
    {
        "001556": ("관세법", "법률"),
        "002421": ("관세법 시행령", "시행령"),
        "006392": ("관세법 시행규칙", "시행규칙"),
        "001783": ("약사법", "법률"),
        "001805": ("식품위생법", "법률"),
    }
)


def test_resolve_qualifier():
    assert graph._resolve_qualifier(None, "002421", REG) == "002421"  # bare → 동일 법령
    assert graph._resolve_qualifier("법", "002421", REG) == "001556"  # 법 → 패밀리 본법
    assert graph._resolve_qualifier("영", "006392", REG) == "002421"  # 영 → 패밀리 시행령
    assert graph._resolve_qualifier("규칙", "001556", REG) == "006392"  # 규칙 → 패밀리 시행규칙
    assert graph._resolve_qualifier("관세법시행령", "001556", REG) == "002421"
    assert graph._resolve_qualifier("이 법", "002421", REG) == "002421"  # 이 법 → 출발 법령
    assert graph._resolve_qualifier("외국환거래법", "002421", REG) is None  # 외부 → 스킵
    # 패밀리에 없는 위계 shorthand → None (약사법엔 시행령 미적재)
    assert graph._resolve_qualifier("영", "001783", REG) is None
    # bare '법'이 관세법으로 오폭되면 안 됨 — 약사법 출발이면 약사법
    assert graph._resolve_qualifier("법", "001783", REG) == "001783"


def test_extract_refs_resolves_and_filters():
    content = (
        "[관세법 시행령 제233조(구비조건의 확인)]\n"
        "제233조(구비조건의 확인) 법 제226조에 따른 허가ㆍ승인의 증명은 "
        "제234조를 준용하며 「외국환거래법」 제5조는 적용하지 아니한다."
    )
    refs = graph.extract_refs(content, "002421", 23300, REG)
    assert ("001556", 22600) in refs  # 법 제226조 → 관세법
    assert ("002421", 23400) in refs  # bare 제234조 → 동일 시행령
    assert ("002421", 23300) not in refs  # self(제233조) 제외
    assert all(art != 500 for _, art in refs)  # 외국환거래법 제5조 → 외부 스킵


def test_extract_refs_heading_not_selfcited():
    """헤딩의 '관세법 제226조' 라벨이 참조로 새지 않아야(오탐 방지)."""
    content = "[관세법 제226조(허가ㆍ승인 등의 증명 및 확인)]\n제226조(허가) 제245조를 준용한다."
    refs = graph.extract_refs(content, "001556", 22600, REG)
    assert ("001556", 22600) not in refs  # 자기 라벨 제외
    assert ("001556", 24500) in refs  # 제245조만 남음


def test_extract_refs_cross_law_by_name():
    """코퍼스 내 타 법령을 공식 명칭으로 인용 → 그 법령으로 해석 (REFERS_TO 재료)."""
    content = (
        "[수입식품안전관리 특별법 제20조(수입신고)]\n"
        "제20조(수입신고) 식품위생법 제7조에 따른 기준에 적합하여야 하며, "
        "약사법 제42조는 별도로 적용한다."
    )
    refs = graph.extract_refs(content, "012247", 2000, REG)
    assert ("001805", 700) in refs  # 식품위생법 제7조
    assert ("001783", 4200) in refs  # 약사법 제42조


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
