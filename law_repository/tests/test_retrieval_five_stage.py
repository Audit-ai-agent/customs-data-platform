"""Pure tests for the five-stage law-history ranking; no store or model is used."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pytest

from src import retrieval_five_stage as five
from src.retrieval_five_stage import ArmHit

AS_OF = date(2026, 8, 5)


# ---------------------------------------------------------------- rewrite


def test_rewrite_variants_puts_the_original_first_and_weights_lexicon_variants() -> None:
    variants = five.rewrite_variants("샘플 반입 시 세금 면제 되나요")
    assert variants[0] == ("샘플 반입 시 세금 면제 되나요", 1.0)
    weights = {text: weight for text, weight in variants[1:]}
    assert any("견본품 면세" in text for text in weights)
    assert all(weight == five.VARIANT_WEIGHT for weight in weights.values())
    assert len(variants) - 1 <= five.MAX_TOTAL


def test_requirement_and_shopping_anchors_weigh_like_the_original_query() -> None:
    requirement = five.rewrite_variants("이 물품 수입 가능한가요 필요한 서류는")
    anchors = [(text, weight) for text, weight in requirement[1:] if "제226조" in text]
    assert anchors and anchors[0][1] == five.ANCHOR_WEIGHT
    shopping = five.rewrite_variants("해외 직구로 산 물건")
    assert any(weight == five.ANCHOR_WEIGHT and "소액물품" in text for text, weight in shopping)


def test_rewrite_is_deterministic_and_never_calls_out() -> None:
    query = "이사 올 때 가져온 물품 관세 얼마"
    assert five.rewrite_tagged(query) == five.rewrite_tagged(query)
    assert len(five.rewrite_tagged(query)) <= five.MAX_TOTAL
    assert five.rewrite_tagged("세관장확인대상물품") == []


# ---------------------------------------------------------------- RRF


def test_weighted_rrf_sums_weight_over_k_plus_rank_per_variant_and_arm() -> None:
    pool = five.weighted_rrf(
        {
            "dense:0": [ArmHit(1, 0.9), ArmHit(2, 0.8)],
            "bm25:0": [ArmHit(2, 5.0)],
            "dense:1": [ArmHit(3, 0.7)],
        },
        {"dense:0": 1.0, "bm25:0": 1.0, "dense:1": five.VARIANT_WEIGHT},
    )
    k = five.RRF_K
    assert pool[1].rrf == pytest.approx(1.0 / (k + 1))
    assert pool[2].rrf == pytest.approx(1.0 / (k + 2) + 1.0 / (k + 1))
    assert pool[3].rrf == pytest.approx(five.VARIANT_WEIGHT / (k + 1))
    assert pool[2].methods == {"dense", "bm25"}
    assert pool[2].raw == {"dense": 0.8, "bm25": 5.0}
    assert five.ordered_pool(pool) == [2, 1, 3]


def test_ordered_pool_is_bounded_and_tie_breaks_on_article_pk() -> None:
    pool = five.weighted_rrf({"dense:0": [ArmHit(pk, 1.0) for pk in range(40, 0, -1)]})
    ordered = five.ordered_pool(pool, depth=five.POOL_DEPTH)
    assert len(ordered) == five.POOL_DEPTH
    tie = five.weighted_rrf({"dense:0": [ArmHit(9, 1.0)], "bm25:0": [ArmHit(4, 1.0)]})
    assert five.ordered_pool(tie) == [4, 9]


# ---------------------------------------------------------------- graph


def _row(pk: int, law_id: str, article_no: int, paragraph_no: int | None = None) -> dict:
    return {
        "article_pk": pk,
        "law_id": law_id,
        "article_no": article_no,
        "paragraph_no": paragraph_no,
        "content": f"본문 {pk}",
    }


def test_graph_neighbours_are_resolved_to_leaf_rows_valid_at_as_of() -> None:
    seeds = [_row(101, "law", 22600), _row(102, "decree", 24500)]
    seen_pairs: list[Sequence[tuple[str, int]]] = []
    resolved_as_of: list[date] = []

    def neighbor_pairs(pairs: Sequence[tuple[str, int]]) -> list[tuple[str, int, str, int]]:
        seen_pairs.append(list(pairs))
        return [
            ("law", 22600, "decree", 24500),  # already a seed → skipped
            ("law", 22600, "rule", 700),
            ("law", 22600, "decree", 24600),
            ("decree", 24500, "rule", 700),  # duplicate target under another seed
        ]

    def resolve(pairs: Sequence[tuple[str, int]], as_of: date) -> list[Mapping[str, object]]:
        resolved_as_of.append(as_of)
        assert set(pairs) == {("rule", 700), ("decree", 24600)}
        return [
            _row(301, "rule", 700, paragraph_no=2),
            _row(300, "rule", 700, paragraph_no=1),
            _row(310, "decree", 24600),
        ]

    rows, status = five.delegates_neighbors_as_of(
        seeds,
        as_of=AS_OF,
        neighbor_pairs=neighbor_pairs,
        resolve_leaf_rows=resolve,
        exclude=frozenset({310}),
    )
    assert status == "ok"
    assert seen_pairs == [[("law", 22600), ("decree", 24500)]]
    assert resolved_as_of == [AS_OF]
    # Seed order first, then (article_no, paragraph_no, pk); the excluded pool
    # member never re-enters, and each neighbour remembers its seed.
    assert [(row["article_pk"], row["seed_article_pk"]) for row in rows] == [
        (300, 101),
        (301, 101),
    ]


def test_graph_neighbours_respect_the_add_cap_and_report_absent_or_unavailable() -> None:
    seeds = [_row(1, "law", 100)]
    targets = [("law", 100, "decree", number) for number in range(200, 240)]

    def resolve(pairs: Sequence[tuple[str, int]], _as_of: date) -> list[Mapping[str, object]]:
        return [_row(1000 + article_no, law_id, article_no) for law_id, article_no in pairs]

    rows, status = five.delegates_neighbors_as_of(
        seeds, as_of=AS_OF, neighbor_pairs=lambda _pairs: targets, resolve_leaf_rows=resolve
    )
    assert status == "ok" and len(rows) == five.GRAPH_MAX_ADD

    assert five.delegates_neighbors_as_of(
        seeds, as_of=AS_OF, neighbor_pairs=lambda _pairs: [], resolve_leaf_rows=resolve
    ) == ([], "absent")

    def broken(_pairs: Sequence[tuple[str, int]]) -> list[tuple[str, int, str, int]]:
        raise ConnectionError("neo4j down")

    assert five.delegates_neighbors_as_of(
        seeds, as_of=AS_OF, neighbor_pairs=broken, resolve_leaf_rows=resolve
    ) == ([], "unavailable")
    assert five.delegates_neighbors_as_of(
        [], as_of=AS_OF, neighbor_pairs=broken, resolve_leaf_rows=resolve
    ) == ([], "absent")


# ---------------------------------------------------------------- blend


def test_blend_is_z_rerank_plus_weighted_z_rrf_projected_into_unit_interval() -> None:
    rrf = {1: 0.030, 2: 0.020, 3: 0.010}
    rerank = {1: 0.10, 2: 0.95, 3: 0.50}
    blended = five.blend_scores(rrf, rerank)
    z_rrf = dict(zip([1, 2, 3], five.zscores([0.030, 0.020, 0.010])))
    z_rerank = dict(zip([1, 2, 3], five.zscores([0.10, 0.95, 0.50])))
    for pk in (1, 2, 3):
        assert blended[pk][0] == pytest.approx(z_rerank[pk] + five.RERANK_BLEND * z_rrf[pk])
    scores = {pk: value[1] for pk, value in blended.items()}
    assert max(scores.values()) == 1.0 and min(scores.values()) == 0.0
    assert all(0.0 <= value <= 1.0 for value in scores.values())
    # The RRF vote (w=2.0) keeps the fusion leader on top although the
    # reranker preferred article 2 on its own.
    assert max(scores, key=lambda pk: (scores[pk], -pk)) == 1


def test_blend_without_rerank_keeps_the_rrf_order_and_single_candidate_scores_one() -> None:
    blended = five.blend_scores({7: 0.01, 3: 0.03, 5: 0.02}, None)
    ordered = sorted(blended, key=lambda pk: (-blended[pk][1], pk))
    assert ordered == [3, 5, 7]
    assert five.blend_scores({42: 0.5}, {42: 0.9}) == {42: (0.0, 1.0)}
    assert five.blend_scores({}, None) == {}
    assert five.zscores([1.0]) == [0.0]
    assert five.zscores([2.0, 2.0]) == [0.0, 0.0]


# ---------------------------------------------------------------- pipeline


class _Stores:
    """In-memory stand-ins for the arm search, hydration, graph, and reranker."""

    def __init__(
        self,
        arms: Mapping[str, Sequence[ArmHit]],
        rows: Mapping[int, dict],
        *,
        neighbours: Sequence[dict] = (),
        graph_status: five.GraphStatus = "ok",
        rerank: Mapping[int, float] | None = None,
    ) -> None:
        self._arms = arms
        self._rows = rows
        self._neighbours = list(neighbours)
        self._graph_status = graph_status
        self._rerank = rerank
        self.queries: list[tuple[str, str, int, date]] = []
        self.rerank_calls: list[tuple[str, tuple[str, ...]]] = []

    def search_arm(self, query: str, arm: str, limit: int, as_of: date) -> Sequence[ArmHit]:
        self.queries.append((query, arm, limit, as_of))
        return self._arms.get(f"{arm}:{query}", ())

    def hydrate(self, pks: Sequence[int], _as_of: date) -> Mapping[int, dict]:
        return {pk: self._rows[pk] for pk in pks if pk in self._rows}

    def neighbours(
        self, seeds: Sequence[Mapping[str, object]], _as_of: date, exclude: frozenset[int]
    ) -> tuple[Sequence[dict], five.GraphStatus]:
        assert all(int(seed["article_pk"]) in exclude for seed in seeds)
        return [row for row in self._neighbours if row["article_pk"] not in exclude], (
            self._graph_status
        )

    def rerank_scores(self, query: str, passages: Sequence[str]) -> list[float] | None:
        self.rerank_calls.append((query, tuple(passages)))
        if self._rerank is None:
            return None
        by_text = {str(row["content"])[: five.RERANK_CHARS]: pk for pk, row in self._rows.items()}
        return [self._rerank[by_text[passage]] for passage in passages]


def _rows(*pks: int) -> dict[int, dict]:
    return {pk: _row(pk, "law", pk) for pk in pks}


def _run(stores: _Stores, query: str = "세관장확인대상물품", top_k: int = 5, **kwargs):
    return five.search_five_stage(
        query,
        top_k,
        AS_OF,
        search_arm=stores.search_arm,
        hydrate=stores.hydrate,
        neighbors=stores.neighbours,
        rerank_scores=stores.rerank_scores,
        **kwargs,
    )


def test_pipeline_claims_only_the_stages_that_really_contributed() -> None:
    rows = _rows(1, 2, 3)
    rows[9] = {**_row(9, "decree", 9), "seed_article_pk": 1}
    stores = _Stores(
        {"dense:q": [ArmHit(1, 0.9), ArmHit(2, 0.8)], "bm25:q": [ArmHit(2, 3.0), ArmHit(3, 1.0)]},
        rows,
        neighbours=[rows[9]],
        rerank={1: 0.8, 2: 0.9, 3: 0.2, 9: 0.95},
    )
    matches = _run(stores, "q", top_k=4, use_rewrite=False)
    by_pk = {int(match.row["article_pk"]): match for match in matches}
    assert set(by_pk) == {1, 2, 3, 9}
    assert by_pk[2].retrieval_methods == ("dense", "bm25", "rerank")
    assert by_pk[1].retrieval_methods == ("dense", "rerank")
    assert by_pk[9].retrieval_methods == ("graph", "rerank")
    assert by_pk[9].raw_scores["graph"] == pytest.approx(by_pk[1].raw_scores["rrf"])
    assert by_pk[9].raw_scores["rrf"] == 0.0
    for match in matches:
        assert 0.0 <= match.score <= 1.0
        assert {"rrf", "blend", "rerank"} <= set(match.raw_scores)
    assert [m.score for m in matches] == sorted((m.score for m in matches), reverse=True)
    assert stores.rerank_calls and stores.rerank_calls[0][0] == "q"
    # Rerank is always scored against the original query, never a variant.
    assert len(stores.rerank_calls) == 1


def test_pipeline_searches_each_variant_on_both_arms_to_pool_depth() -> None:
    stores = _Stores({"dense:샘플 반입": [ArmHit(1, 0.5)]}, _rows(1))
    matches = _run(stores, "샘플 반입", top_k=3)
    assert [int(m.row["article_pk"]) for m in matches] == [1]
    variants = [text for text, _weight in five.rewrite_variants("샘플 반입")]
    assert len(variants) >= 2
    assert {(query, arm) for query, arm, _limit, _as_of in stores.queries} == {
        (variant, arm) for variant in variants for arm in five.ARMS
    }
    assert all(
        limit == five.POOL_DEPTH and as_of == AS_OF for _q, _a, limit, as_of in stores.queries
    )


def test_pipeline_degrades_to_rrf_order_without_rerank_and_without_graph() -> None:
    stores = _Stores(
        {"dense:q": [ArmHit(1, 0.9), ArmHit(2, 0.8)], "bm25:q": [ArmHit(2, 3.0)]},
        _rows(1, 2),
        graph_status="unavailable",
        rerank=None,
    )
    matches = _run(stores, "q", top_k=5, use_rewrite=False)
    assert [int(m.row["article_pk"]) for m in matches] == [2, 1]
    assert matches[0].retrieval_methods == ("dense", "bm25")
    assert matches[1].retrieval_methods == ("dense",)
    assert all("rerank" not in m.raw_scores and "graph" not in m.raw_scores for m in matches)
    assert matches[0].score == 1.0 and matches[1].score == 0.0


def test_pipeline_rejects_malformed_rerank_batches_instead_of_trusting_them() -> None:
    rows = _rows(1, 2)

    class _BadRerank(_Stores):
        def rerank_scores(self, query: str, passages: Sequence[str]) -> list[float] | None:
            return [1.5] * len(passages)

    stores = _BadRerank({"dense:q": [ArmHit(1, 0.9), ArmHit(2, 0.8)]}, rows)
    matches = _run(stores, "q", top_k=5, use_rewrite=False)
    assert all("rerank" not in m.retrieval_methods for m in matches)

    class _ShortRerank(_Stores):
        def rerank_scores(self, query: str, passages: Sequence[str]) -> list[float] | None:
            return [0.5]

    stores = _ShortRerank({"dense:q": [ArmHit(1, 0.9), ArmHit(2, 0.8)]}, rows)
    assert all("rerank" not in m.retrieval_methods for m in _run(stores, "q", use_rewrite=False))


def test_pipeline_drops_candidates_that_are_not_valid_at_as_of_and_is_deterministic() -> None:
    stores = _Stores(
        {"dense:q": [ArmHit(1, 0.9), ArmHit(77, 0.85), ArmHit(2, 0.8)]},
        _rows(1, 2),
        rerank={1: 0.4, 2: 0.6},
    )
    first = _run(stores, "q", top_k=5, use_rewrite=False)
    second = _run(stores, "q", top_k=5, use_rewrite=False)
    assert {int(m.row["article_pk"]) for m in first} == {1, 2}
    assert first == second
    assert _run(stores, "q", top_k=0, use_rewrite=False) == ()
    assert _run(_Stores({}, {}), "q", use_rewrite=False) == ()


def test_pipeline_truncates_to_top_k_after_blending_over_the_whole_pool() -> None:
    stores = _Stores(
        {"dense:q": [ArmHit(pk, 1.0 - pk / 100) for pk in range(1, 8)]},
        _rows(*range(1, 8)),
        rerank={pk: pk / 10 for pk in range(1, 8)},
    )
    matches = _run(stores, "q", top_k=3, use_rewrite=False)
    assert len(matches) == 3
    assert matches[0].score == 1.0
