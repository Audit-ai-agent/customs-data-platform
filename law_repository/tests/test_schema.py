"""불변식 검증 — law_articles 의 유효구간 겹침이 DB 단에서 거부되는지 (설계 §5).

전제: docker compose up + python scripts/migrate.py 로 스키마 적용된 상태.
Docker 미기동 시 접속 실패로 skip 됨.
"""

from __future__ import annotations

import psycopg
import pytest

from src.config import settings

_SEED_LAW = ("TEST_LAW", "테스트법", "법률", "테스트부")
pytestmark = pytest.mark.live


@pytest.fixture()
def conn():
    try:
        c = psycopg.connect(settings.postgres_dsn, connect_timeout=3)
    except Exception as e:  # DB 미기동 등
        pytest.skip(f"Postgres 접속 불가 (docker compose up 필요): {e}")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _insert_article(cur, valid_from, valid_to):
    cur.execute(
        """
        INSERT INTO law_articles
          (law_id, article_no, content, content_hash, version_mst, valid_from, valid_to)
        VALUES (%s, 1, 'x', 'h', 'mst', %s, %s)
        """,
        (_SEED_LAW[0], valid_from, valid_to),
    )


def test_overlapping_intervals_rejected(conn):
    """같은 조문의 겹치는 유효구간 insert 는 EXCLUDE 제약으로 실패해야 한다."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO laws (law_id, law_name, hierarchy, ministry) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (law_id) DO NOTHING",
            _SEED_LAW,
        )
        _insert_article(cur, "2024-01-01", "2025-01-01")
        with pytest.raises(psycopg.errors.ExclusionViolation):
            # [2024-06-01, 2025-06-01) 은 위 구간과 겹침 → 거부
            _insert_article(cur, "2024-06-01", "2025-06-01")
    # 롤백은 fixture 가 처리 (테스트 데이터 미영속)


def test_adjacent_intervals_allowed(conn):
    """반열림 [from,to) 이므로 끝점이 맞닿는 인접 구간은 허용돼야 한다."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO laws (law_id, law_name, hierarchy, ministry) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (law_id) DO NOTHING",
            _SEED_LAW,
        )
        _insert_article(cur, "2024-01-01", "2025-01-01")
        _insert_article(cur, "2025-01-01", None)  # 맞닿음 → 겹치지 않음
