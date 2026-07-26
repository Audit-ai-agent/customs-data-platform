"""A1 하이브리드 검색기 — 순수 로직(RRF/재질의/응답 규격) + 실데이터 스모크.

rerank 모델(2.3GB)은 테스트에서 강제 다운로드하지 않는다 — 응답 규격 테스트는
scores 를 monkeypatch 하고, 실데이터 스모크는 use_rerank=False 로 RRF 경로만 검증.
"""

from datetime import date

import pytest

import src.hybrid as hy
from src.repository import Hit


# ------------------------------------------------------------------ 순수 로직


def test_rrf_two_arm_agreement_beats_single_arm_top():
    # pk2 는 양 arm 에서 상위 → 단일 arm 1위(pk1)보다 앞서야 한다
    fused = hy.rrf_fuse([[1, 2, 3], [2, 3, 4]])
    order = sorted(fused, key=fused.get, reverse=True)
    assert order[0] == 2
    assert set(order) == {1, 2, 3, 4}


def test_rrf_rank_is_one_based():
    fused = hy.rrf_fuse([[7]])
    assert fused[7] == pytest.approx(1.0 / (hy.RRF_K + 1))


def test_rewrite_appends_measured_legal_terms():
    assert "견본품" in hy.rewrite_query("샘플 무상 반입")
    assert "경정" in hy.rewrite_query("세금을 잘못 매긴 경우")
    # 원 표현 유지(치환 아님)
    assert hy.rewrite_query("샘플 반입").startswith("샘플 반입")


def test_rewrite_noop_when_no_gap_or_already_legal():
    assert hy.rewrite_query("세관장확인대상물품") == "세관장확인대상물품"
    assert hy.rewrite_query("견본품 샘플") == "견본품 샘플"  # 법률어 이미 존재 → 중복 부착 금지


# ------------------------------------------------- 응답 규격 (스토어/모델 mock)


def _fake_hits(arm_map):
    def fake_search(query, arm="dense", limit=10, as_of=None):
        return arm_map.get(arm, [])

    return fake_search


def _fake_meta(pks):
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


def test_retrieve_shape_rerank_order_and_gap_filter(monkeypatch):
    dense = [Hit(1, 0.70, "본문1"), Hit(2, 0.65, "본문2")]
    bm25 = [Hit(2, 9.0, "본문2"), Hit(3, 5.0, "본문3")]
    monkeypatch.setattr(hy.repo, "search", _fake_hits({"dense": dense, "bm25": bm25}))
    monkeypatch.setattr(hy, "_article_meta", lambda pks: _fake_meta([p for p in pks if p != 3]))
    # reranker 가 RRF 순서를 뒤집도록: 뒤로 갈수록 높은 점수
    monkeypatch.setattr(
        hy.rerank, "scores", lambda q, ps, **kw: [0.1 * (i + 1) for i in range(len(ps))]
    )

    r = hy.retrieve("세관장확인", limit=3)
    assert r["reranked"] and not r["rewritten"] and not r["low_confidence"]
    scores = [e["score"] for e in r["evidence"]]
    assert scores == sorted(scores, reverse=True)  # rerank 점수 내림차순
    # pk=3 은 메타 없음(스토어 불일치) → 근거에서 제외
    assert all(e["article_pk"] != 3 for e in r["evidence"])
    e0 = r["evidence"][0]
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
        assert field in e0
    assert e0["evidence_id"].startswith("law-")
    assert e0["article_label"] == "제226조"


def test_retrieve_low_confidence_triggers_rewrite(monkeypatch):
    calls = []

    def fake_search(query, arm="dense", limit=10, as_of=None):
        calls.append(query)
        # 재질의(확장된 질의)면 dense top-1 이 개선되게
        top = 0.80 if "견본품" in query else 0.40
        return [Hit(1, top, "본문1")] if arm == "dense" else [Hit(1, 1.0, "본문1")]

    monkeypatch.setattr(hy.repo, "search", fake_search)
    monkeypatch.setattr(hy, "_article_meta", _fake_meta)
    monkeypatch.setattr(hy.rerank, "scores", lambda q, ps, **kw: [0.9] * len(ps))

    r = hy.retrieve("샘플 반입", limit=1)
    assert r["rewritten"] and "견본품" in r["used_query"]
    assert not r["low_confidence"]  # 재질의로 해소
    assert any("견본품" in q for q in calls)


def test_retrieve_degrades_to_rrf_when_reranker_unavailable(monkeypatch):
    dense = [Hit(1, 0.9, "본문1")]
    monkeypatch.setattr(hy.repo, "search", _fake_hits({"dense": dense, "bm25": dense}))
    monkeypatch.setattr(hy, "_article_meta", _fake_meta)
    monkeypatch.setattr(hy.rerank, "scores", lambda q, ps, **kw: None)  # 모델 불가

    r = hy.retrieve("아무 질의", limit=1)
    assert not r["reranked"]
    assert r["evidence"][0]["score"] is None
    assert r["evidence"][0]["rrf_score"] > 0


# ------------------------------------------------------------ 실데이터 스모크


@pytest.mark.live
def test_live_retrieve_anchor_hits_enforcement_decree_233():
    """골든셋 앵커(2026-07-12 실측): '세관장확인대상물품' → 시행령 제233조
    (구비조건의 확인 — 관세법 제226조의 위임 조문)가 RRF 상위에 와야 한다.
    참고: 관세법 제226조 자체는 이 질의의 양 arm 후보 40건에 없음(어휘 갭) —
    조문 도달은 expand_article(233→DELEGATES) 몫. (rerank 는 모델 다운로드
    회피를 위해 끔 — RRF 융합 경로 검증)"""
    r = hy.retrieve("세관장확인대상물품", limit=5, use_rerank=False)
    assert r["evidence"], "스토어가 비어 있음"
    tops = [(e["law_name"], e["article_label"]) for e in r["evidence"]]
    assert ("관세법 시행령", "제233조") in tops, f"시행령 제233조 미적중: {tops}"
    e = next(x for x in r["evidence"] if x["article_label"] == "제233조")
    assert e["is_current"] and e["version_mst"]
