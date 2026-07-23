"""BM25 sparse 인코더 단위 테스트 (DB 불필요)."""

from __future__ import annotations

from src.embed import bm25


def test_doc_and_query_share_token_space():
    """조사 붙은 표층형도 2-gram 겹침으로 매칭돼야 한다."""
    d_idx, _ = bm25.encode_doc("수입물품의 통관")
    q_idx, _ = bm25.encode_query("물품")
    assert set(q_idx) & set(d_idx), "쿼리 '물품' 토큰이 문서와 겹치지 않음"


def test_deterministic():
    assert bm25.encode_doc("관세법 제226조") == bm25.encode_doc("관세법 제226조")
    assert bm25.token_id("세관장") == bm25.token_id("세관장")


def test_tf_saturation():
    """같은 토큰 반복 시 값이 tf 에 따라 증가하되 포화(k1+1 상한)해야 한다."""
    _, v1 = bm25.encode_doc("관세")
    _, v5 = bm25.encode_doc("관세 " * 5)
    assert max(v5) > max(v1)
    assert max(v5) < bm25.K1 + 1


def test_empty_text():
    assert bm25.encode_doc("") == ([], [])
    assert bm25.encode_query("...") == ([], [])
