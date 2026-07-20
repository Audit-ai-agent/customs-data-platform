"""Phase 3 델타/승격 로직 검증 — 합성 법령으로 트랜잭션 안에서 (롤백, 실데이터 불침범).

시나리오: 개정(닫기+새버전) → 공포≠시행 pre-load → 승격 → 시점쿼리 → 멱등 재실행.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from src.config import settings
from src.sync.delta import _reconcile_article, promote

LAW = "TEST_DELTA"
pytestmark = pytest.mark.live


@pytest.fixture()
def cur():
    try:
        conn = psycopg.connect(settings.postgres_dsn, connect_timeout=3)
    except Exception as e:
        pytest.skip(f"Postgres 접속 불가: {e}")
    with conn.cursor() as c:
        c.execute(
            "INSERT INTO laws (law_id, law_name, hierarchy) VALUES (%s, '테스트법', '법률') "
            "ON CONFLICT (law_id) DO NOTHING",
            (LAW,),
        )
        yield c
    conn.rollback()
    conn.close()


def _row(content: str, valid_from: date, article_no: int = 100, paragraph_no=None) -> dict:
    return {
        "article_no": article_no,
        "paragraph_no": paragraph_no,
        "title": "테스트",
        "content": content,
        "valid_from": valid_from,
        "children": [],
    }


def _seed_current(cur, content="v1", valid_from=date(2024, 1, 1)) -> int:
    pk, status = _reconcile_article(cur, LAW, "m1", _row(content, valid_from))
    assert status == "added"
    cur.execute("UPDATE law_articles SET is_current=true WHERE id=%s", (pk,))
    return pk


def test_amend_closes_open_row_and_preloads(cur):
    """개정: 열린 행 valid_to 닫힘 + 새 행 [시행일, NULL) is_current=false(pre-load)."""
    old_pk = _seed_current(cur)
    new_pk, status = _reconcile_article(cur, LAW, "m2", _row("v2", date(2099, 1, 1)))
    assert status == "amended"
    cur.execute("SELECT valid_to, is_current FROM law_articles WHERE id=%s", (old_pk,))
    assert cur.fetchone() == (date(2099, 1, 1), True)  # 닫혔지만 아직 현행
    cur.execute("SELECT valid_from, valid_to, is_current FROM law_articles WHERE id=%s", (new_pk,))
    assert cur.fetchone() == (date(2099, 1, 1), None, False)  # 최신이지만 비현행


def test_promote_flips_on_enforcement_day(cur):
    """승격: 시행일 도달 시 구버전 demote + 신버전 promote. 재실행 멱등."""
    old_pk = _seed_current(cur)
    new_pk, _ = _reconcile_article(cur, LAW, "m2", _row("v2", date(2099, 1, 1)))

    # 2098-12-31 시점엔 합성 조문이 아직 안 뒤집혀야 한다. (promote 전체 카운트 0
    # 등식은 실데이터에 진짜 미래 preload 가 생긴 2026-07-12 부터 무효 — 합성 행만 검증.
    # cur 픽스처가 롤백 격리하므로 실데이터 flip 도 커밋되지 않는다.)
    promote(cur, qc=None, as_of=date(2098, 12, 31))
    cur.execute("SELECT is_current FROM law_articles WHERE id=%s", (old_pk,))
    assert cur.fetchone()[0] is True
    cur.execute("SELECT is_current FROM law_articles WHERE id=%s", (new_pk,))
    assert cur.fetchone()[0] is False

    stats = promote(cur, qc=None, as_of=date(2099, 1, 1))
    assert stats["demoted"] >= 1 and stats["promoted"] >= 1
    cur.execute("SELECT is_current FROM law_articles WHERE id=%s", (old_pk,))
    assert cur.fetchone()[0] is False
    cur.execute("SELECT is_current FROM law_articles WHERE id=%s", (new_pk,))
    assert cur.fetchone()[0] is True
    # 멱등: 같은 as_of 재실행 → 변화 없음
    assert promote(cur, qc=None, as_of=date(2099, 1, 1)) == {"demoted": 0, "promoted": 0}


def test_point_in_time_exactly_one_version(cur):
    """시점쿼리: 임의 날짜에 유효한 버전이 정확히 1개 (반열림 [from,to))."""
    _seed_current(cur)
    _reconcile_article(cur, LAW, "m2", _row("v2", date(2099, 1, 1)))
    for probe, expect in [
        (date(2050, 6, 15), "v1"),
        (date(2098, 12, 31), "v1"),
        (date(2099, 1, 1), "v2"),  # 시행일 당일부터 신버전
        (date(2120, 1, 1), "v2"),
    ]:
        cur.execute(
            """
            SELECT content FROM law_articles
            WHERE law_id=%s AND article_no=100
              AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)
            """,
            (LAW, probe, probe),
        )
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0][0] == expect, f"{probe}: {rows}"


def test_same_content_skips(cur):
    """content_hash 동일 → 아무 변화 없음 (재임베딩 skip 불변식)."""
    _seed_current(cur, content="v1")
    pk, status = _reconcile_article(cur, LAW, "m2", _row("v1", date(2099, 1, 1)))
    assert (pk, status) == (None, "same")


def test_stale_version_ignored(cur):
    """열린 행보다 과거 시행일의 늦은 도착 → skip (현행 추적 원칙)."""
    _seed_current(cur, valid_from=date(2024, 1, 1))
    pk, status = _reconcile_article(cur, LAW, "m0", _row("v0", date(2020, 1, 1)))
    assert (pk, status) == (None, "stale")


def test_correction_replaces_in_place(cur):
    """동일 시행일 재공포(정정) → 열린 행 교체 + 재임베딩 대상(point NULL)."""
    pk = _seed_current(cur, content="v1", valid_from=date(2024, 1, 1))
    pk2, status = _reconcile_article(cur, LAW, "m2", _row("v1-fixed", date(2024, 1, 1)))
    assert status == "corrected" and pk2 == pk
    cur.execute("SELECT content, qdrant_point_id FROM law_articles WHERE id=%s", (pk,))
    content, point = cur.fetchone()
    assert content == "v1-fixed" and point is None
