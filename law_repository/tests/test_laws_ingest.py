"""Phase 2 적재 결과 정합성 검증.

전제: python -m src.ingest.laws 완료 상태. 미적재/미기동 시 skip.
"""

from __future__ import annotations

import psycopg
import pytest

from src.config import settings
from src.db.qdrant import COLLECTION, client as qdrant_client

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def cur():
    try:
        conn = psycopg.connect(settings.postgres_dsn, connect_timeout=3)
    except Exception as e:
        pytest.skip(f"Postgres 접속 불가: {e}")
    with conn.cursor() as c:
        c.execute("SELECT count(*) FROM law_articles")
        if c.fetchone()[0] == 0:
            pytest.skip("law_articles 미적재 (python -m src.ingest.laws 필요)")
        yield c
    conn.close()


def test_target_laws_loaded(cur):
    """laws 테이블 ⊇ TARGET_LAWS (관세 3법 + 특수물자 개별법령 55, 2026-07-12 확장)."""
    from src.ingest.laws import TARGET_LAWS

    cur.execute("SELECT law_id, law_name, hierarchy FROM laws")
    rows = cur.fetchall()
    loaded_ids = {r[0] for r in rows}
    assert {t["law_id"] for t in TARGET_LAWS} <= loaded_ids
    core = {r[1] for r in rows if r[0] in ("001556", "002421", "006392")}
    assert core == {"관세법", "관세법 시행령", "관세법 시행규칙"}
    assert {r[2] for r in rows} <= {"법률", "시행령", "시행규칙"}


def test_temporal_current_invariant(cur):
    """불변식: is_current ⇔ 오늘이 유효구간 [valid_from, valid_to) 안.

    (초기적재 직후엔 전부 열린 행이지만, 델타 반영 후엔 닫힌 이력 행이 정상 존재 —
    승격 잡이 매일 이 동치를 유지한다.)
    """
    cur.execute(
        """
        SELECT count(*) FROM law_articles
        WHERE is_current <> (
          valid_from <= CURRENT_DATE
          AND (valid_to IS NULL OR valid_to > CURRENT_DATE)
        )
        """
    )
    assert cur.fetchone()[0] == 0, "is_current 가 유효구간과 불일치 (승격 잡 누락?)"


def test_content_hash_present(cur):
    cur.execute(
        "SELECT count(*) FROM law_articles WHERE content_hash IS NULL OR length(content_hash) <> 64"
    )
    assert cur.fetchone()[0] == 0


def test_version_mst_matches_law_versions(cur):
    """조문의 version_mst 는 law_versions 에 등록된 MST 여야 한다 (Phase 3 조인 전제)."""
    cur.execute(
        """
        SELECT count(*) FROM law_articles a
        WHERE NOT EXISTS (
          SELECT 1 FROM law_versions v
          WHERE v.law_id = a.law_id AND v.mst = a.version_mst
        )
        """
    )
    assert cur.fetchone()[0] == 0


def test_qdrant_point_count_matches_pg(cur):
    """Qdrant 포인트 수 = 인덱싱된 조문 수 (현행·분할부모 제외 전부)."""
    cur.execute(
        """
        SELECT
          count(*) FILTER (WHERE qdrant_point_id IS NOT NULL) AS indexed,
          count(*) FILTER (
            WHERE is_current
              AND NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk = law_articles.id)
          ) AS should_index
        FROM law_articles
        """
    )
    indexed, should_index = cur.fetchone()
    if indexed == 0:
        pytest.skip("Qdrant 미인덱싱 (임베딩 단계 미실행)")
    assert indexed == should_index
    try:
        qc = qdrant_client()
        cnt = qc.count(COLLECTION).count
    except Exception as e:
        pytest.skip(f"Qdrant 접속 불가: {e}")
    assert cnt == indexed


def test_qdrant_payload_schema(cur):
    """payload 가 설계 §4.2 필수 필드를 갖고 article_pk 가 PG 와 조인돼야 한다."""
    try:
        qc = qdrant_client()
        pts, _ = qc.scroll(COLLECTION, limit=5, with_payload=True)
    except Exception as e:
        pytest.skip(f"Qdrant 접속 불가: {e}")
    if not pts:
        pytest.skip("Qdrant 미인덱싱")
    required = {
        "article_pk",
        "law_id",
        "law_name",
        "hierarchy",
        "article_no",
        "enforcement_date",
        "text",
    }
    for p in pts:
        assert required <= set(p.payload), f"payload 필드 누락: {required - set(p.payload)}"
        cur.execute("SELECT is_current FROM law_articles WHERE id = %s", (p.payload["article_pk"],))
        row = cur.fetchone()
        assert row is not None and row[0], "Qdrant 포인트가 비현행/미존재 조문을 가리킴"
