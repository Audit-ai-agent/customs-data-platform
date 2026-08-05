"""Pure and mocked hybrid retrieval tests; no store or model download is needed."""

from __future__ import annotations

from datetime import date

import pytest

import src.hybrid as hy
from src.embed import rerank
from src.repository import Hit


def test_rrf_two_arm_agreement_beats_single_arm_top() -> None:
    fused = hy.rrf_fuse([[1, 2, 3], [2, 3, 4]])
    order = sorted(fused, key=fused.get, reverse=True)
    assert order[0] == 2
    assert set(order) == {1, 2, 3, 4}


def test_rrf_rank_is_one_based() -> None:
    fused = hy.rrf_fuse([[7]])
    assert fused[7] == pytest.approx(1.0 / (hy.RRF_K + 1))


def test_rewrite_appends_measured_legal_terms() -> None:
    assert "견본품" in hy.rewrite_query("샘플 무상 반입")
    assert "경정" in hy.rewrite_query("세금을 잘못 매긴 경우")
    assert hy.rewrite_query("샘플 반입").startswith("샘플 반입")


def test_rewrite_noop_when_no_gap_or_already_legal() -> None:
    assert hy.rewrite_query("세관장확인대상물품") == "세관장확인대상물품"
    assert hy.rewrite_query("견본품 샘플") == "견본품 샘플"


def _fake_hits(arm_map: dict[str, list[Hit]]):
    def fake_search(_query: str, arm: str = "dense", **_kwargs: object) -> list[Hit]:
        return arm_map.get(arm, [])

    return fake_search


def _fake_meta(pks: list[int]) -> dict[int, dict[str, object]]:
    return {
        pk: {
            "article_pk": pk,
            "law_id": "001556",
            "law_name": "관세법",
            "hierarchy": "법",
            "article_no": 22600,
            "paragraph_no": None,
            "title": "허가ㆍ승인 등의 증명 및 확인",
            "valid_from": date(2025, 1, 1),
            "valid_to": None,
            "is_current": True,
            "version_mst": 999999,
        }
        for pk in pks
    }


def test_retrieve_shape_rerank_order_and_gap_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    dense = [Hit(1, 0.70, "본문1"), Hit(2, 0.65, "본문2")]
    bm25 = [Hit(2, 9.0, "본문2"), Hit(3, 5.0, "본문3")]
    monkeypatch.setattr(hy.repo, "search", _fake_hits({"dense": dense, "bm25": bm25}))
    monkeypatch.setattr(hy, "_article_meta", lambda pks: _fake_meta([pk for pk in pks if pk != 3]))
    monkeypatch.setattr(
        hy.rerank,
        "scores",
        lambda _query, passages, **_kwargs: [0.1 * (i + 1) for i in range(len(passages))],
    )

    response = hy.retrieve("세관장확인", limit=3)
    assert response["reranked"] and not response["rewritten"] and not response["low_confidence"]
    scores = [e["score"] for e in response["evidence"]]
    assert scores == sorted(scores, reverse=True)
    assert all(e["article_pk"] != 3 for e in response["evidence"])
    first = response["evidence"][0]
    for field in (
        "evidence_id",
        "law_name",
        "article_label",
        "score",
        "rrf_score",
        "valid_from",
        "version_mst",
        "source_uri",
        "text",
    ):
        assert field in first
    assert first["evidence_id"].startswith("law-")
    assert first["article_label"] == "제226조"


def test_retrieve_low_confidence_triggers_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_search(query: str, arm: str = "dense", **_kwargs: object) -> list[Hit]:
        calls.append(query)
        top = 0.80 if "견본품" in query else 0.40
        return [Hit(1, top, "본문1")] if arm == "dense" else [Hit(1, 1.0, "본문1")]

    monkeypatch.setattr(hy.repo, "search", fake_search)
    monkeypatch.setattr(hy, "_article_meta", _fake_meta)
    monkeypatch.setattr(
        hy.rerank, "scores", lambda _query, passages, **_kwargs: [0.9] * len(passages)
    )

    response = hy.retrieve("샘플 반입", limit=1)
    assert response["rewritten"] and "견본품" in response["used_query"]
    assert not response["low_confidence"]
    assert any("견본품" in query for query in calls)


def test_retrieve_degrades_to_rrf_when_reranker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dense = [Hit(1, 0.9, "본문1")]
    monkeypatch.setattr(hy.repo, "search", _fake_hits({"dense": dense, "bm25": dense}))
    monkeypatch.setattr(hy, "_article_meta", _fake_meta)
    monkeypatch.setattr(hy.rerank, "scores", lambda *_args, **_kwargs: None)

    response = hy.retrieve("아무 질의", limit=1)
    assert not response["reranked"]
    assert response["evidence"][0]["score"] is None
    assert response["evidence"][0]["rrf_score"] > 0


def test_required_rerank_boundary_never_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rerank, "scores", lambda *_args, **_kwargs: None)

    with pytest.raises(rerank.RerankUnavailable, match="unavailable"):
        rerank.required_scores("query", ["passage"])
