"""Live hybrid-search smoke test; pure tests are in test_hybrid_unit.py."""

import pytest

import src.hybrid as hy


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
