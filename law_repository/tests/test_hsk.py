"""HSK 그래프 불변식 검증 (로드맵 Phase 6 선반영).

전제: docker compose up + python -m src.ingest.hsk 적재 완료 상태.
Neo4j 미기동/미적재 시 skip.
"""

from __future__ import annotations

import pytest

from src.db.neo4j import driver as make_driver

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def session():
    drv = make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:
        drv.close()
        pytest.skip(f"Neo4j 접속 불가 (docker compose up 필요): {e}")
    with drv.session() as s:
        if s.run("MATCH (n:HSNode) RETURN count(*) AS c").single()["c"] == 0:
            pytest.skip("HSNode 미적재 (python -m src.ingest.hsk 필요)")
        yield s
    drv.close()


def test_leaf_is_exactly_level10(session):
    """불변식: HSK leaf(:HSK 라벨) = 10자리(level 10) 노드와 정확히 일치."""
    r = session.run(
        """
        MATCH (n:HSNode)
        RETURN
          count(CASE WHEN n:HSK THEN 1 END) AS hsk,
          count(CASE WHEN n.level = 10 THEN 1 END) AS lv10,
          count(CASE WHEN n:HSK AND n.level <> 10 THEN 1 END) AS mislabeled
        """
    ).single()
    assert r["mislabeled"] == 0
    assert r["hsk"] == r["lv10"] > 0


def test_codes_are_strings_with_leading_zeros(session):
    """불변식: 코드는 문자열, 1~9류 앞자리 0 보존 (제1류 '01' 존재)."""
    r = session.run(
        "MATCH (n:HSNode {code: '01'}) RETURN n.level AS level, n.name_ko AS name"
    ).single()
    assert r is not None, "code '01' 노드 없음 — 앞자리 0 유실 의심"
    assert r["level"] == 2


def test_leaf_prefixes_consistent(session):
    """leaf 의 hs6/heading4/chapter2 가 code 접두어와 일치해야 상속 조회가 성립."""
    r = session.run(
        """
        MATCH (n:HSK)
        WHERE n.hs6 <> left(n.code, 6)
           OR n.heading4 <> left(n.code, 4)
           OR n.chapter2 <> left(n.code, 2)
        RETURN count(*) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_inheritance_lookup_returns_ancestors(session):
    """상속 규칙(§4.3): leaf 에서 상위 계층명 조회(속성 매칭)가 동작해야 한다."""
    r = session.run(
        """
        MATCH (n:HSK) WITH n LIMIT 1
        MATCH (c2:HSNode {code: n.chapter2}), (h4:HSNode {code: n.heading4})
        RETURN n.code AS code, c2.name_ko AS chapter, h4.name_ko AS heading
        """
    ).single()
    assert r is not None
    assert r["chapter"] and r["heading"]
