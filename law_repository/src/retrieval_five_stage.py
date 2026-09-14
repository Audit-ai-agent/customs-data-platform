"""Five-stage law-history retrieval: rewrite → weighted RRF → graph → rerank → blend.

This is the product port of the research ranking pipeline.  Every stage is a
pure function over injected store callables so the ordering rules can be unit
tested without PostgreSQL, Qdrant, Neo4j, or the reranker model:

1. ``rewrite_variants`` — rule-based legal-term variants of the query.  No LLM
   call and no file cache: the lexicon is the only source of variants, so the
   same query always produces the same variant set.
2. ``weighted_rrf`` — reciprocal rank fusion (k=60) over every (variant, arm)
   rank list; variants weigh 0.6, anchors and the original query 1.0.
3. ``expand_with_graph`` — DELEGATES neighbours of the top fused seeds join the
   candidate pool without a fusion score.  The neighbour lookup is temporal:
   the current-law graph is only used to find (law_id, article_no) pairs,
   which are then resolved to the article versions valid at ``as_of``.
4. cross-encoder rerank over the bounded pool (CPU).
5. ``blend_scores`` — final order ``z(rerank) + 2.0·z(RRF)`` so the reranker is
   one voter, not the sole judge; the public ``score`` is a min-max projection
   of that blend into ``[0, 1]`` as the Evidence contract requires.

Every degradation is honest: when the reranker is unavailable the RRF order
stands and ``rerank`` is not claimed; when the graph is unavailable or has no
edges ``graph`` is not claimed.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast

from src.evidence import LawArticleMatch

RRF_K = 60
VARIANT_WEIGHT = 0.6
ANCHOR_WEIGHT = 1.0
GRAPH_SEEDS = 10
GRAPH_MAX_ADD = 15
POOL_DEPTH = 30
RERANK_BLEND = 2.0
RERANK_CHARS = 2000
MAX_VARIANTS = 3
MAX_TOTAL = 4
STRATEGY = "five_stage_cpu"
ARMS = ("dense", "bm25")

RetrievalMethod = Literal["dense", "bm25", "graph", "rerank"]
GraphStatus = Literal["ok", "unavailable", "absent"]
_METHOD_ORDER: tuple[RetrievalMethod, ...] = ("dense", "bm25", "graph", "rerank")

# ---------------------------------------------------------------- stage 1

# 일상어 → 법률 용어 확장.  (트리거 정규식, 추가 검색어) — 치환이 아니라 변형 쿼리 생성.
# 통관 도메인 일반 지식 기반 (골드셋 정답 참조 금지).
_LEXICON: tuple[tuple[str, str], ...] = (
    (r"돌려받|환불|되돌려", "관세 환급"),
    (r"세금.{0,6}(면제|안 ?내|깎)|면제", "관세 면세 감면"),
    (r"고장|불량|하자|파손|상해|상했|망가|계약.{0,4}(다르|위반)", "위약물품 환급"),
    (r"수리|고치", "재수출 재수입 면세"),
    (r"여행|휴대품|출장|입국", "여행자 휴대품 면세"),
    (r"이사|이주", "이사물품 감면"),
    (r"샘플|견본", "견본품 면세"),
    (r"연구|실험|학술", "학술연구용품 감면"),
    (r"종교|자선|기부", "종교용품 자선용품 면세"),
    (r"가격|값|금액", "과세가격 결정"),
    (r"세율|관세율|얼마", "세율 적용"),
    (r"늦|기한|지연", "신고기한 가산세"),
    (r"잘못 ?(신고|냈)|정정|고쳐", "수정신고 경정청구"),
    (r"FTA|자유무역|협정", "자유무역협정 협정관세 원산지"),
    (r"원산지", "원산지증명서"),
    (r"보세", "보세구역 보세운송"),
    (r"몰수|압수|유치", "몰수 유치 통관보류"),
    (r"밀수|처벌|벌금", "벌칙 몰수 추징"),
)
_LEXICON_COMPILED = tuple((re.compile(pattern), expansion) for pattern, expansion in _LEXICON)

# 요건성 질문(수입 가능 여부·필요 서류·검사) → 세관장확인 앵커.
_REQUIREMENT_TRIGGER = re.compile(
    r"수입.{0,10}(가능|되나|해도|할 수|허가|승인|요건|절차|서류|검사|검역|확인|신고해야)"
    r"|필요한 ?서류|허가.{0,4}받|검역|승인.{0,4}받"
)
_REQUIREMENT_ANCHOR = "관세법 제226조 세관장확인대상물품 허가 승인 요건 구비 확인"

# 개인 해외구매(직구) 질문 → 소액·자가사용 면세 앵커.  원 문장을 붙이면
# "직구/싸게" 표층이 가격결정 조문으로 어그로를 끌어 독립 쿼리로 둔다.
_SHOPPING_TRIGGER = re.compile(r"직구|구매대행|배송대행|해외.{0,4}(쇼핑|주문)")
_SHOPPING_ANCHOR = "소액물품 면세 자가사용물품 관세 면제 한도"


def rewrite_tagged(query: str) -> list[tuple[str, bool]]:
    """(변형, 앵커 여부) 목록.  앵커 = 원 문장 없이 법률 용어만으로 만든 독립 쿼리."""

    variants: list[tuple[str, bool]] = []
    for pattern, expansion in _LEXICON_COMPILED:
        if pattern.search(query):
            variants.append((f"{query} {expansion}", False))
        if len(variants) >= MAX_VARIANTS - 1:
            break
    if _REQUIREMENT_TRIGGER.search(query):
        variants.append((_REQUIREMENT_ANCHOR, True))
    if _SHOPPING_TRIGGER.search(query):
        variants.append((_SHOPPING_ANCHOR, True))
    variants = variants[:MAX_VARIANTS]
    # 앵커 먼저 — 총 상한에 걸릴 때 덧붙임 변형이 앵커를 밀어내지 않게.
    ordered = sorted(variants, key=lambda item: not item[1])
    out: list[tuple[str, bool]] = []
    seen = {query}
    for variant, anchor in ordered:
        if variant not in seen:
            out.append((variant, anchor))
            seen.add(variant)
    return out[:MAX_TOTAL]


def rewrite_variants(query: str) -> list[tuple[str, float]]:
    """The original query first (weight 1.0), then weighted lexicon variants."""

    return [(query, 1.0)] + [
        (variant, ANCHOR_WEIGHT if anchor else VARIANT_WEIGHT)
        for variant, anchor in rewrite_tagged(query)
    ]


# ---------------------------------------------------------------- stage 2


@dataclass(frozen=True)
class ArmHit:
    article_pk: int
    score: float


@dataclass
class PoolEntry:
    methods: set[str] = field(default_factory=set)
    raw: dict[str, float] = field(default_factory=dict)
    rrf: float = 0.0


def weighted_rrf(
    ranklists: Mapping[str, Sequence[ArmHit]],
    weights: Mapping[str, float] | None = None,
    *,
    k: int = RRF_K,
) -> dict[int, PoolEntry]:
    """``Σ w / (k + rank)`` per article over every ``"{arm}:{variant}"`` rank list."""

    pool: dict[int, PoolEntry] = {}
    for key, hits in ranklists.items():
        arm = key.split(":", 1)[0]
        weight = float((weights or {}).get(key, 1.0))
        for rank, hit in enumerate(hits, start=1):
            entry = pool.setdefault(int(hit.article_pk), PoolEntry())
            entry.methods.add(arm)
            previous = entry.raw.get(arm)
            entry.raw[arm] = (
                float(hit.score) if previous is None else max(previous, float(hit.score))
            )
            entry.rrf += weight / (k + rank)
    return pool


def ordered_pool(pool: Mapping[int, PoolEntry], depth: int = POOL_DEPTH) -> list[int]:
    return sorted(pool, key=lambda pk: (-pool[pk].rrf, pk))[:depth]


# ---------------------------------------------------------------- stage 3

NeighborPairs = Callable[[Sequence[tuple[str, int]]], Sequence[tuple[str, int, str, int]]]
"""(seed law_id, seed article_no) pairs → rows of (seed_law_id, seed_article_no, law_id, article_no)."""
ResolveLeafRows = Callable[[Sequence[tuple[str, int]], date], Sequence[Mapping[str, object]]]
"""(law_id, article_no) pairs → leaf article rows valid at as_of (row dicts with article_pk)."""


def delegates_neighbors_as_of(
    seed_rows: Sequence[Mapping[str, object]],
    *,
    as_of: date,
    neighbor_pairs: NeighborPairs,
    resolve_leaf_rows: ResolveLeafRows,
    exclude: frozenset[int] = frozenset(),
    max_add: int = GRAPH_MAX_ADD,
) -> tuple[list[dict[str, object]], GraphStatus]:
    """Temporal-aware DELEGATES expansion.

    The Neo4j article graph holds current whole-article nodes only, so seeds
    are addressed by ``(law_id, article_no)`` and each neighbour pair is
    resolved back to the leaf rows that were valid at ``as_of``.  Neighbours are
    returned in seed order, then ``(law_id, article_no, paragraph_no, pk)``, and
    each carries ``seed_article_pk`` so the caller can attribute the graph raw
    score.  A driver failure yields ``unavailable``; no edges yield ``absent``.
    """

    if not seed_rows or max_add <= 0:
        return [], "absent"
    seed_pairs: list[tuple[str, int]] = []
    seed_pk_by_pair: dict[tuple[str, int], int] = {}
    for row in seed_rows:
        pair = (str(row["law_id"]), int(cast(int, row["article_no"])))
        if pair not in seed_pk_by_pair:
            seed_pairs.append(pair)
            seed_pk_by_pair[pair] = int(cast(int, row["article_pk"]))
    try:
        edges = list(neighbor_pairs(seed_pairs))
    except Exception:  # noqa: BLE001 — graph is optional; never fail retrieval
        return [], "unavailable"
    if not edges:
        return [], "absent"
    neighbors_by_seed: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for seed_law_id, seed_article_no, law_id, article_no in edges:
        seed = (str(seed_law_id), int(seed_article_no))
        target = (str(law_id), int(article_no))
        if seed not in seed_pk_by_pair or target in seed_pk_by_pair:
            continue
        bucket = neighbors_by_seed.setdefault(seed, [])
        if target not in bucket:
            bucket.append(target)
    targets: list[tuple[str, int]] = []
    for seed in seed_pairs:
        for target in sorted(neighbors_by_seed.get(seed, ())):
            if target not in targets:
                targets.append(target)
    if not targets:
        return [], "absent"
    try:
        rows = list(resolve_leaf_rows(targets, as_of))
    except Exception:  # noqa: BLE001
        return [], "unavailable"
    rows_by_pair: dict[tuple[str, int], list[Mapping[str, object]]] = {}
    for row in rows:
        pair = (str(row["law_id"]), int(cast(int, row["article_no"])))
        rows_by_pair.setdefault(pair, []).append(row)
    added: list[dict[str, object]] = []
    seen: set[int] = set(exclude)
    for seed in seed_pairs:
        for target in sorted(neighbors_by_seed.get(seed, ())):
            for row in sorted(
                rows_by_pair.get(target, ()),
                key=lambda value: (
                    int(cast(int, value["article_no"])),
                    -1
                    if value.get("paragraph_no") is None
                    else int(cast(int, value["paragraph_no"])),
                    int(cast(int, value["article_pk"])),
                ),
            ):
                article_pk = int(cast(int, row["article_pk"]))
                if article_pk in seen:
                    continue
                seen.add(article_pk)
                added.append({**dict(row), "seed_article_pk": seed_pk_by_pair[seed]})
                if len(added) >= max_add:
                    return added, "ok"
    return added, "ok"


# ---------------------------------------------------------------- stage 5


def zscores(values: Sequence[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mean = statistics.fmean(values)
    deviation = statistics.pstdev(values) or 1.0
    return [(value - mean) / deviation for value in values]


def blend_scores(
    rrf: Mapping[int, float],
    rerank: Mapping[int, float] | None,
    *,
    weight: float = RERANK_BLEND,
) -> dict[int, tuple[float, float]]:
    """``pk → (blend, score)`` where ``score`` is the min-max projection into [0, 1].

    Without rerank scores the blend is ``z(rrf)`` alone so the RRF order is
    preserved exactly.  A single candidate scores 1.0.
    """

    pks = sorted(rrf, key=lambda pk: (-rrf[pk], pk))
    if not pks:
        return {}
    z_rrf = dict(zip(pks, zscores([rrf[pk] for pk in pks])))
    if rerank is not None and set(rerank) >= set(pks):
        z_rerank = dict(zip(pks, zscores([float(rerank[pk]) for pk in pks])))
        blend = {pk: z_rerank[pk] + weight * z_rrf[pk] for pk in pks}
    else:
        blend = {pk: z_rrf[pk] for pk in pks}
    low = min(blend.values())
    high = max(blend.values())
    span = high - low
    out: dict[int, tuple[float, float]] = {}
    for pk in pks:
        score = 1.0 if span <= 0.0 else (blend[pk] - low) / span
        out[pk] = (blend[pk], min(1.0, max(0.0, score)))
    return out


# ---------------------------------------------------------------- pipeline

SearchArm = Callable[[str, str, int, date], Sequence[ArmHit]]
"""(query, arm, limit, as_of) → arm hits, temporal-filtered before ranking."""
Hydrate = Callable[[Sequence[int], date], Mapping[int, Mapping[str, object]]]
"""article_pks, as_of → rows valid at as_of (missing pks are dropped)."""
Neighbors = Callable[
    [Sequence[Mapping[str, object]], date, frozenset[int]],
    tuple[Sequence[Mapping[str, object]], GraphStatus],
]
"""seed rows, as_of, excluded pks → (neighbour rows with seed_article_pk, status)."""
RerankScores = Callable[[str, Sequence[str]], Sequence[float] | None]
"""query, passages → sigmoid scores in [0, 1] or None when the model is unavailable."""


def search_five_stage(
    query: str,
    top_k: int,
    as_of: date,
    *,
    search_arm: SearchArm,
    hydrate: Hydrate,
    neighbors: Neighbors | None,
    rerank_scores: RerankScores | None,
    use_rewrite: bool = True,
    depth: int = POOL_DEPTH,
) -> tuple[LawArticleMatch, ...]:
    if top_k <= 0:
        return ()
    variants = rewrite_variants(query) if use_rewrite else [(query, 1.0)]
    ranklists: dict[str, Sequence[ArmHit]] = {}
    weights: dict[str, float] = {}
    for index, (variant, weight) in enumerate(variants):
        for arm in ARMS:
            key = f"{arm}:{index}"
            ranklists[key] = tuple(search_arm(variant, arm, depth, as_of))
            weights[key] = weight
    pool = weighted_rrf(ranklists, weights)
    if not pool:
        return ()
    pool_order = ordered_pool(pool, depth)
    rows = dict(hydrate(pool_order, as_of))
    pool_order = [pk for pk in pool_order if pk in rows]
    if not pool_order:
        return ()
    if neighbors is not None:
        seeds = [rows[pk] for pk in pool_order[:GRAPH_SEEDS]]
        # The status is diagnostic only: the honest ``retrieval_methods`` of
        # each match already say whether a graph neighbour actually joined.
        neighbor_rows, _graph_status = neighbors(seeds, as_of, frozenset(pool_order))
        for row in neighbor_rows:
            article_pk = int(cast(int, row["article_pk"]))
            if article_pk in rows:
                continue
            seed_pk = int(cast(int, row.get("seed_article_pk", pool_order[0])))
            entry = PoolEntry(methods={"graph"}, raw={"graph": pool[seed_pk].rrf}, rrf=0.0)
            pool[article_pk] = entry
            rows[article_pk] = row
            pool_order.append(article_pk)
    rerank_by_pk: dict[int, float] | None = None
    if rerank_scores is not None:
        passages = [str(rows[pk]["content"])[:RERANK_CHARS] for pk in pool_order]
        scored = rerank_scores(query, passages)
        if scored is not None and len(scored) == len(pool_order):
            candidate = {pk: float(value) for pk, value in zip(pool_order, scored)}
            if all(0.0 <= value <= 1.0 for value in candidate.values()):
                rerank_by_pk = candidate
    blended = blend_scores({pk: pool[pk].rrf for pk in pool_order}, rerank_by_pk)
    matches: list[LawArticleMatch] = []
    for article_pk in pool_order:
        entry = pool[article_pk]
        blend, score = blended[article_pk]
        methods = set(entry.methods)
        raw: dict[str, float] = {**entry.raw, "rrf": float(entry.rrf), "blend": float(blend)}
        if rerank_by_pk is not None:
            methods.add("rerank")
            raw["rerank"] = rerank_by_pk[article_pk]
        matches.append(
            LawArticleMatch(
                row=dict(rows[article_pk]),
                retrieval_methods=cast(
                    tuple[RetrievalMethod, ...],
                    tuple(method for method in _METHOD_ORDER if method in methods),
                ),
                score=float(score),
                raw_scores=raw,
            )
        )
    matches.sort(key=lambda value: (-value.score, int(value.row["article_pk"])))
    return tuple(matches[:top_k])


__all__ = [
    "ANCHOR_WEIGHT",
    "ARMS",
    "GRAPH_MAX_ADD",
    "GRAPH_SEEDS",
    "MAX_TOTAL",
    "MAX_VARIANTS",
    "POOL_DEPTH",
    "RERANK_BLEND",
    "RERANK_CHARS",
    "RRF_K",
    "STRATEGY",
    "VARIANT_WEIGHT",
    "ArmHit",
    "GraphStatus",
    "PoolEntry",
    "blend_scores",
    "delegates_neighbors_as_of",
    "ordered_pool",
    "rewrite_tagged",
    "rewrite_variants",
    "search_five_stage",
    "weighted_rrf",
    "zscores",
]
