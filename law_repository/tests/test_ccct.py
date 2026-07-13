"""Phase 4 HSK 요건 그래프 불변식 검증.

전제: python -m src.ingest.ccct 적재 완료(부분이라도). 미적재/미기동 시 skip.
"""
from __future__ import annotations

import pytest

from src.db.neo4j import driver as make_driver


@pytest.fixture(scope="module")
def session():
    drv = make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:
        drv.close()
        pytest.skip(f"Neo4j 접속 불가: {e}")
    with drv.session() as s:
        if s.run("MATCH (r:Requirement {source_type: 'ccct'}) RETURN count(r) AS c").single()["c"] == 0:
            pytest.skip("Requirement 미적재 (python -m src.ingest.ccct 필요)")
        yield s
    drv.close()


def test_has_requirement_only_from_leaf(session):
    """불변식: 요건 엣지는 leaf(:HSK, 10자리)에서만 출발."""
    r = session.run(
        """
        MATCH (h)-[:HAS_REQUIREMENT]->(:Requirement)
        WHERE NOT h:HSK OR h.level <> 10
        RETURN count(*) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_requirement_shape(session):
    """Requirement 속성: wire trade_type과 ISO 유효일."""
    r = session.run(
        """
        MATCH (r:Requirement {source_type: 'ccct'})
        WHERE NOT r.trade_type IN ['export', 'import']
           OR (r.valid_from IS NOT NULL AND NOT r.valid_from =~ '\\d{4}-\\d{2}-\\d{2}')
           OR r.id IS NULL OR r.version_hash IS NULL OR r.snapshot_id IS NULL
        RETURN count(*) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_requirement_has_law_agency_and_certificate(session):
    """각 Requirement에 법령·승인기관·확인서류가 직접 연결되어야 한다."""
    r = session.run(
        """
        MATCH (req:Requirement {source_type: 'ccct'})
        WHERE NOT (req)-[:BASED_ON]->(:Law)
           OR NOT (req)-[:APPROVED_BY]->(:Agency)
           OR NOT (req)-[:REQUIRES_DOCUMENT]->(:Certificate)
        RETURN count(req) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_requirement_traverse(session):
    """설계 §4.3 요건 조회: HSK → 확인법령 → 기관 + 서류가 한 번에 나와야 한다."""
    rec = session.run(
        """
        MATCH (h:HSK)-[:HAS_REQUIREMENT]->(req:Requirement)-[:BASED_ON]->(law:Law),
              (req)-[:APPROVED_BY]->(ag:Agency),
              (req)-[:REQUIRES_DOCUMENT]->(cert:Certificate)
        RETURN h.code AS hs, req.trade_type AS tt, law.name AS law,
               cert.name AS doc, ag.name AS agency
        LIMIT 1
        """
    ).single()
    assert rec and rec["law"] and rec["agency"]
    assert len(rec["hs"]) == 10
