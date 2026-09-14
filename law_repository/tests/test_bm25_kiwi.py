"""Kiwi-based BM25 encoder rules (v2). No store is needed; Kiwi ships its model."""

from __future__ import annotations

import pytest

from src.embed import bm25


def test_encoder_version_is_the_kiwi_revision() -> None:
    assert bm25.ENCODER_VERSION == "ko-kiwi-md5-bm25/v2"
    assert bm25.AVG_LEN == 141.0
    assert len(bm25.encoder_fingerprint()) == 64
    assert bm25.encoder_fingerprint() == bm25.encoder_fingerprint()


def test_particles_and_endings_are_dropped_but_content_words_stay() -> None:
    tokens = bm25.tokenize("수입물품의 통관은 세관장에게 신고하여야 한다")
    assert "수입" in tokens and "물품" in tokens and "통관" in tokens and "신고" in tokens
    assert "의" not in tokens and "은" not in tokens and "에게" not in tokens
    assert "하" not in tokens and "한다" not in tokens


def test_adjacent_nouns_emit_joined_compounds_for_similar_regime_discrimination() -> None:
    reimport = bm25.tokenize("재수입면세")
    reexport = bm25.tokenize("재수출면세")
    assert "재수입면세" in reimport and "재수입" in reimport
    assert "재수출면세" in reexport and "재수출" in reexport
    # The joined forms are what tell the two regimes apart; the shared noun
    # "면세" alone must not be the only overlap that the compound step adds.
    assert "재수입면세" not in reexport and "재수출면세" not in reimport
    assert "세관장" in bm25.tokenize("세관장 확인")
    assert "원산지증명서" in bm25.tokenize("원산지증명서 발급")


def test_article_numbers_are_one_exact_token_including_branch_numbers() -> None:
    tokens = bm25.tokenize("관세법 제241조의2에 따른 수입신고")
    assert "제241조" in tokens and "제241조의2" in tokens
    assert tokens.count("제241조") == 1
    spaced = bm25.tokenize("제 226 조")
    assert "제226조" in spaced


def test_latin_and_digit_runs_are_lowercased() -> None:
    tokens = bm25.tokenize("FTA 협정관세 HS 8471.30")
    assert "fta" in tokens and "hs" in tokens
    assert "FTA" not in tokens


def test_legal_boilerplate_is_not_a_token() -> None:
    tokens = bm25.tokenize("다음 각 호의 어느 하나에 해당하는 경우에는 그러하지 아니하다")
    assert "경우" not in tokens and "해당" not in tokens and "다음" not in tokens


def test_index_and_query_share_the_same_token_space() -> None:
    doc_ids, _ = bm25.encode_doc("여행자 휴대품의 면세 범위는 대통령령으로 정한다")
    query_ids, query_values = bm25.encode_query("휴대품 면세")
    assert set(query_ids) <= set(doc_ids)
    assert query_values == [1.0] * len(query_ids)
    assert query_ids == sorted(query_ids)


def test_document_length_uses_the_measured_average_in_tf_saturation() -> None:
    _, short_values = bm25.encode_doc("관세")
    assert short_values[0] == pytest.approx(
        1 * (bm25.K1 + 1) / (1 + bm25.K1 * (1 - bm25.B + bm25.B * 1 / bm25.AVG_LEN))
    )


def test_empty_and_symbol_only_text_encode_to_nothing() -> None:
    assert bm25.tokenize("") == []
    assert bm25.tokenize("   ") == []
    assert bm25.encode_doc("...") == ([], [])
