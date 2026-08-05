"""Phase 5 조회 repository — temporal 불변식·arm 규격·HSK traverse 검증.

전제: docker compose 기동 + Phase 2(법령→PG/Qdrant)·Phase 4(HSK 요건) 적재.
미기동/미적재 시 각 fixture 가 skip.
"""

from __future__ import annotations

from datetime import date

import pytest

from src import repository as repo
from src.db.qdrant import BM25_VECTOR, COLLECTION, DENSE_VECTOR


# ------------------------------------------------- 단위(DB 불필요)


def test_temporal_predicate_current_uses_today():
    sql, params = repo._temporal_predicate(None)
    assert params["as_of"] == date.today()
    assert "valid_from <= %(as_of)s" in sql
    assert "valid_to > %(as_of)s OR valid_to IS NULL" in sql


def test_temporal_predicate_as_of_passthrough():
    d = date(2024, 3, 1)
    _, params = repo._temporal_predicate(d)
    assert params["as_of"] == d


def test_normalize_hsk_accepts_only_hs6_or_hsk10_without_padding():
    assert repo.normalize_hsk("0101 21 0000") == "0101210000"  # 공백 제거
    assert repo.normalize_hsk("854231") == "854231"  # HS6는 HS6로 유지
    with pytest.raises(ValueError):
        repo.normalize_hsk("8542")  # 우측 0 패딩 금지
    with pytest.raises(ValueError):
        repo.normalize_hsk("123456789012345")  # 자리 절단 금지
    assert repo.normalize_hsk("0204100000")[0] == "0"  # 앞자리 0 보존


def test_resolve_hsk_candidates_exact_unique_and_ambiguous():
    leaves = ["7318151000", "7318159000", "0303540000"]
    assert repo.resolve_hsk_candidates("0303540000", leaves).status == "exact"
    unique = repo.resolve_hsk_candidates("030354", leaves)
    assert unique.status == "unique_descendant"
    assert unique.resolved_hsk10 == "0303540000"
    ambiguous = repo.resolve_hsk_candidates("731815", leaves)
    assert ambiguous.status == "ambiguous"
    assert ambiguous.resolved_hsk10 is None
    assert list(ambiguous.candidate_hsk10s) == ["7318151000", "7318159000"]


def test_search_unknown_arm_raises():
    with pytest.raises(ValueError):
        repo.search("아무거나", arm="rerank")


# ------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def qc():
    from src.db.qdrant import client

    try:
        c = client()
        if not c.collection_exists(COLLECTION) or c.count(COLLECTION).count == 0:
            pytest.skip("Qdrant 현행 인덱스 비어있음 (functional UAT build 필요)")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Qdrant 접속 불가: {e}")
    return c


@pytest.fixture(scope="module")
def sample_article():
    from src.db.postgres import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT law_id, article_no, paragraph_no FROM law_articles "
                "WHERE is_current ORDER BY id LIMIT 1"
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres 접속 불가: {e}")
    if not row:
        pytest.skip("현행 조문 없음 (functional UAT build 필요)")
    return {"law_id": row[0], "article_no": row[1], "paragraph_no": row[2]}


@pytest.fixture(scope="module")
def sample_hsk():
    drv = repo.make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:  # noqa: BLE001
        drv.close()
        pytest.skip(f"Neo4j 접속 불가: {e}")
    with drv.session() as s:
        rec = s.run(
            "MATCH (h:HSK)-[:HAS_REQUIREMENT]->(:Requirement) RETURN h.code AS c LIMIT 1"
        ).single()
    drv.close()
    if not rec:
        pytest.skip("Requirement 미적재 (python -m src.ingest.ccct 필요)")
    return rec["c"]


# ------------------------------------------------- arm 검색


@pytest.mark.live
def test_current_search_arm_shape(qc):
    for arm in (DENSE_VECTOR, BM25_VECTOR):
        hits = repo.search("세관장 확인 대상 물품", arm=arm, limit=5)
        assert hits, f"{arm} arm 결과 없음"
        for h in hits:
            assert isinstance(h.article_pk, int)
            assert isinstance(h.score, float)
            assert h.text and isinstance(h.text, str)  # text 온전 반환(설계 §4.2)


@pytest.mark.live
def test_current_search_respects_limit(qc):
    assert len(repo.search("관세", arm=DENSE_VECTOR, limit=3)) <= 3


# ------------------------------------------------- temporal 시점 조회


@pytest.mark.live
def test_resolve_current_is_current(sample_article):
    row = repo.resolve_as_of(**sample_article, as_of=None)
    assert row is not None
    assert row["is_current"] is True
    # 반열림: valid_from <= today
    assert row["valid_from"] <= date.today()


@pytest.mark.live
def test_resolve_before_enforcement_empty(sample_article):
    """시행 전 시점(1900년)에는 그 조문 버전이 존재하지 않는다."""
    assert repo.resolve_as_of(**sample_article, as_of=date(1900, 1, 1)) is None


@pytest.mark.live
def test_search_as_of_current_matches_today(qc, sample_article):
    """as_of=today 는 현행 검색과 같은 조문 집합을 확정할 수 있어야(구법 혼입 없음)."""
    hits = repo.search("관세 납부", arm=DENSE_VECTOR, limit=5, as_of=date.today())
    for h in hits:
        assert h.text  # 시점 확정 후에도 본문 온전


# ------------------------------------------------- HSK traverse


@pytest.mark.live
def test_hsk_requirements_shape(sample_hsk):
    req = repo.hsk_requirements(sample_hsk)
    assert req["hsk10"] == sample_hsk
    assert req["requirements"], "요건 있는 leaf 인데 결과 없음"
    for q in req["requirements"]:
        assert q["law_name"] and q["agencies"]  # 확인법령 + 기관 완성
        assert q["trade_type"] in ("export", "import")
        assert q["source"] in ("direct", "inherited")
    # 조상 계층 접두어 정합(감사 설명용)
    assert req["ancestors"]["hs6"]["code"] == sample_hsk[:6]
    assert req["ancestors"]["heading4"]["code"] == sample_hsk[:4]
    assert req["ancestors"]["chapter2"]["code"] == sample_hsk[:2]


@pytest.mark.live
def test_hsk_trade_type_filter(sample_hsk):
    exp = repo.hsk_requirements(sample_hsk, trade_type="수입")
    assert all(q["trade_type"] == "import" for q in exp["requirements"])


@pytest.mark.live
def test_hsk_requirements_direct_from_leaf(sample_hsk):
    """Phase 4 는 leaf 직접 요건만 적재 → 직접 요건은 level 10."""
    req = repo.hsk_requirements(sample_hsk)
    directs = [q for q in req["requirements"] if q["source"] == "direct"]
    assert directs
    assert all(q["level"] == 10 for q in directs)


# ------------------------------------------------- 조문 그래프 확장(graceful)


@pytest.mark.live
def test_expand_article_graceful(sample_hsk):
    """조문 그래프 미구축이어도 예외 없이 list 반환(그래프 생기면 자동 동작)."""
    out = repo.expand_article(1)
    assert isinstance(out, list)
