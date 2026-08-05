"""AI Agent가 사용하는 내부 Evidence Service v1 계약과 조합 로직.

결정 근거는 현행 CCCT PostgreSQL 스냅샷에서 읽고, HS 해소는 Neo4j HSK
leaf 마스터를 사용한다. 정확한 HSK10 또는 단일 leaf로 해소된 HS6만
finding 자격 evidence를 반환한다. 모호·장애·커버리지 누락은 빈 결과가
아니라 명시적 상태와 오류로 반환한다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import quote

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, model_validator

from src import repository

SCHEMA_VERSION = "1.0"
_QDRANT_SCROLL_PAGE_SIZE = 256
_QDRANT_READINESS_MAX_POINTS = 1_000_000
_QDRANT_COLLECTION = "customs_law_current"


def connect():
    """Open the optional local PostgreSQL connection only when a store path runs."""

    from src.db.postgres import connect as open_connection

    return open_connection()


def make_driver():
    """Open the optional local Neo4j driver only when a store path runs."""

    from src.db.neo4j import driver as open_driver

    return open_driver()


def dict_row(*args: object, **kwargs: object):
    """Defer the psycopg row-factory import until a real cursor consumes it."""

    from psycopg.rows import dict_row as row_factory

    return row_factory(*args, **kwargs)


def qdrant_client():
    """Open the optional local Qdrant client only when a store path runs."""

    from src.db.qdrant import client as open_client

    return open_client()


NonEmpty128 = Annotated[str, Field(min_length=1, max_length=128)]
NonEmpty500 = Annotated[str, Field(min_length=1, max_length=500)]
NullableNonEmpty = Annotated[str, Field(min_length=1)] | None
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
VersionHash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
EvidenceId = Annotated[str, Field(pattern=r"^ev_[a-f0-9]{64}$")]
Hsk10 = Annotated[str, Field(pattern=r"^[0-9]{10}$")]
Hs6 = Annotated[str, Field(pattern=r"^[0-9]{6}$")]
MatchedHsCode = Hs6 | Hsk10


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrieveItem(ContractModel):
    item_id: NonEmpty128
    description: str = Field(min_length=1, max_length=2000)
    hs_code: Annotated[str, Field(min_length=1, max_length=64)] | None
    origin_country: CountryCode | None
    destination_country: CountryCode


class RetrieveRequest(ContractModel):
    schema_version: Literal["1.0"]
    request_id: NonEmpty128
    case_id: NonEmpty128
    review_run_id: NonEmpty128
    review_as_of: date
    jurisdiction: Literal["KR"]
    trade_type: Literal["import", "export"]
    top_k: int = Field(ge=1, le=100)
    items: list[RetrieveItem] = Field(min_length=1, max_length=100)
    uploaded_requirement_document_codes: list[NonEmpty128] = Field(
        default_factory=list,
        json_schema_extra={"default": [], "uniqueItems": True},
    )
    validation_issue_codes: list[NonEmpty128] = Field(
        default_factory=list,
        json_schema_extra={"default": [], "uniqueItems": True},
    )
    query_intent: str = Field(default="required_import_documents", min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_item_ids(self):
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("items.item_id는 request 내에서 유일해야 함")
        if len(self.uploaded_requirement_document_codes) != len(
            set(self.uploaded_requirement_document_codes)
        ):
            raise ValueError("uploaded_requirement_document_codes는 중복될 수 없음")
        if len(self.validation_issue_codes) != len(set(self.validation_issue_codes)):
            raise ValueError("validation_issue_codes는 중복될 수 없음")
        if any(not code or len(code) > 128 for code in self.uploaded_requirement_document_codes):
            raise ValueError("uploaded_requirement_document_codes 항목은 1..128자여야 함")
        if any(not code or len(code) > 128 for code in self.validation_issue_codes):
            raise ValueError("validation_issue_codes 항목은 1..128자여야 함")
        return self


class EvidenceError(ContractModel):
    code: NonEmpty128
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool


class Evidence(ContractModel):
    evidence_id: EvidenceId
    source_record_id: str = Field(min_length=1, max_length=512)
    source_type: Literal["law_article", "customs_requirement", "hsk_entry", "notice"]
    item_id: NonEmpty128
    title: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1)
    law_id: NullableNonEmpty
    law_name: NullableNonEmpty
    article_no: NullableNonEmpty
    source_uri: Annotated[AnyUrl, Field(min_length=1)]
    valid_from: date | None
    valid_to: date | None
    version_hash: VersionHash
    matched_hs_code: MatchedHsCode | None
    trade_type: Literal["import", "export"]
    required_document_code: NullableNonEmpty
    required_document_name: NullableNonEmpty
    agencies: list[NonEmpty500] = Field(json_schema_extra={"uniqueItems": True})
    retrieval_methods: list[Literal["exact_hs", "dense", "bm25", "graph"]] = Field(
        min_length=1, json_schema_extra={"uniqueItems": True}
    )
    eligibility: Literal["finding", "supporting", "blocked"]
    score: float | None = Field(ge=0, le=1)
    raw_scores: dict[str, float]

    @model_validator(mode="after")
    def unique_list_values(self):
        if len(self.agencies) != len(set(self.agencies)):
            raise ValueError("agencies는 중복될 수 없음")
        if len(self.retrieval_methods) != len(set(self.retrieval_methods)):
            raise ValueError("retrieval_methods는 중복될 수 없음")
        return self


class ItemEvidenceResult(ContractModel):
    item_id: NonEmpty128
    hs_resolution: Literal["exact", "unique_descendant", "ambiguous", "invalid", "missing"]
    resolved_hsk10: Hsk10 | None
    candidate_hsk10s: list[Hsk10] = Field(json_schema_extra={"uniqueItems": True})
    coverage: Literal["complete", "partial", "unknown"]
    requirements_found: bool
    evidence: list[Evidence]
    errors: list[EvidenceError]

    @model_validator(mode="after")
    def unique_candidate_hsk10s(self):
        if len(self.candidate_hsk10s) != len(set(self.candidate_hsk10s)):
            raise ValueError("candidate_hsk10s는 중복될 수 없음")
        return self


class RetrieveResponse(ContractModel):
    schema_version: Literal["1.0"]
    request_id: NonEmpty128
    status: Literal["ok", "partial", "failed"]
    knowledge_snapshot_id: NullableNonEmpty
    results: list[ItemEvidenceResult] = Field(max_length=100)
    errors: list[EvidenceError] = Field(
        default_factory=list,
        json_schema_extra={"default": []},
    )


@dataclass(frozen=True)
class RequirementLookup:
    covered: bool
    rows: tuple[dict, ...]


@dataclass(frozen=True)
class LawArticleMatch:
    row: dict
    retrieval_methods: tuple[Literal["dense", "bm25", "graph"], ...]
    score: float
    raw_scores: dict[str, float]


class EvidenceBackend(Protocol):
    def current_snapshot_id(self) -> str | None: ...

    def snapshot_effective_date(self, snapshot_id: str) -> date: ...

    def resolve_hsk(self, code: str | None) -> repository.HskResolution: ...

    def lookup_requirements(
        self, snapshot_id: str, hsk10: str, trade_type: str, as_of: date
    ) -> RequirementLookup: ...

    def search_law_articles(
        self, query: str, top_k: int, as_of: date
    ) -> tuple[LawArticleMatch, ...]: ...


class ReadinessBackend(EvidenceBackend, Protocol):
    def readiness(self) -> dict: ...


class KnowledgeSnapshotMismatch(RuntimeError):
    """PostgreSQL 원장과 Neo4j projection의 현행 스냅샷이 다를 때."""


class DatabaseEvidenceBackend:
    """Optional local-store backend backed by PostgreSQL and the HSK graph."""

    def __init__(self) -> None:
        self._law_index_verified = False
        self._law_index_state: dict[str, int] | None = None

    def current_snapshot_id(self) -> str | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT snapshot_id FROM knowledge_snapshots "
                "WHERE source_type='ccct' AND is_current"
            )
            row = cur.fetchone()
            snapshot_id = row[0] if row else None
        if snapshot_id is None:
            return None
        drv = make_driver()
        try:
            with drv.session() as session:
                graph_snapshots = {
                    row["snapshot_id"]
                    for row in session.run(
                        "MATCH (r:Requirement {source_type: 'ccct'}) "
                        "RETURN DISTINCT r.snapshot_id AS snapshot_id"
                    )
                    if row["snapshot_id"]
                }
        finally:
            drv.close()
        if graph_snapshots != {snapshot_id}:
            raise KnowledgeSnapshotMismatch(
                "CCCT knowledge snapshot mismatch: "
                f"postgres={snapshot_id}, neo4j={sorted(graph_snapshots)}"
            )
        return snapshot_id

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        """Return the one review date for which a CCCT snapshot is complete.

        The current CCCT sweep records query coverage, but it does not record a
        separate historical query universe.  Treating that coverage as proof
        for an arbitrary past or future date would turn "not found" into an
        unsafe false negative.  V1 is therefore deliberately snapshot-date
        only until time-versioned coverage is ingested.
        """
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT (generated_at AT TIME ZONE 'Asia/Seoul')::date
                FROM knowledge_snapshots
                WHERE snapshot_id=%s AND source_type='ccct'
                """,
                (snapshot_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"CCCT knowledge snapshot not found: {snapshot_id}")
        return row[0]

    def resolve_hsk(self, code: str | None) -> repository.HskResolution:
        return repository.resolve_hsk(code)

    def lookup_requirements(
        self, snapshot_id: str, hsk10: str, trade_type: str, as_of: date
    ) -> RequirementLookup:
        with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM ccct_queries
                  WHERE snapshot_id=%s AND hsk10=%s AND trade_type=%s
                ) AS covered
                """,
                (snapshot_id, hsk10, trade_type),
            )
            covered = bool(cur.fetchone()["covered"])
            if not covered:
                return RequirementLookup(False, ())
            cur.execute(
                """
                SELECT r.requirement_id, r.requirement_version_hash, r.hsk10,
                       r.trade_type, r.law_code, r.law_name,
                       r.agency_code, r.agency_name,
                       r.document_code, r.document_name,
                       r.valid_from, r.valid_to,
                       src.source_record_id, src.source_uri
                FROM ccct_requirements r
                JOIN LATERAL (
                  SELECT s.source_record_id, s.source_uri
                  FROM ccct_source_records s
                  WHERE s.snapshot_id=r.snapshot_id
                    AND s.requirement_id=r.requirement_id
                  ORDER BY s.source_record_id
                  LIMIT 1
                ) src ON true
                WHERE r.snapshot_id=%s AND r.hsk10=%s AND r.trade_type=%s
                  AND (r.valid_from IS NULL OR r.valid_from <= %s)
                  AND (r.valid_to IS NULL OR r.valid_to >= %s)
                ORDER BY r.law_code, r.document_code, r.agency_code, r.requirement_id
                """,
                (snapshot_id, hsk10, trade_type, as_of, as_of),
            )
            return RequirementLookup(True, tuple(dict(row) for row in cur.fetchall()))

    def _verify_target_law_coverage(self) -> dict[str, int]:
        """Require a complete and byte-attested current target-law corpus."""
        from src.functional_uat_spec import FUNCTIONAL_UAT_TARGET_LAWS

        expected_ids = {law["law_id"] for law in FUNCTIONAL_UAT_TARGET_LAWS}
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.law_id, a.id, a.content, a.content_hash, a.version_mst,
                       EXISTS (
                         SELECT 1
                         FROM law_versions v
                         WHERE v.law_id=a.law_id AND v.mst=a.version_mst
                       ) AS has_law_version
                FROM laws l
                LEFT JOIN law_articles a
                  ON a.law_id=l.law_id AND a.is_current
                WHERE l.law_id = ANY(%s)
                ORDER BY l.law_id, a.id
                """,
                (sorted(expected_ids),),
            )
            rows = tuple(cur.fetchall())
        loaded_ids = {row[0] for row in rows}
        article_backed_ids = {row[0] for row in rows if row[1] is not None}
        invalid_current_articles = 0
        for _law_id, article_id, content, content_hash, version_mst, has_version in rows:
            if article_id is None:
                continue
            actual_hash = (
                hashlib.sha256(content.encode("utf-8")).hexdigest()
                if isinstance(content, str) and content.strip()
                else None
            )
            if (
                actual_hash is None
                or content_hash != actual_hash
                or not isinstance(version_mst, str)
                or not version_mst.strip()
                or not has_version
            ):
                invalid_current_articles += 1
        missing = expected_ids - loaded_ids
        without_articles = loaded_ids - article_backed_ids
        if missing or without_articles or invalid_current_articles:
            raise RuntimeError(
                "law corpus incomplete: "
                f"expected={len(expected_ids)}, loaded={len(loaded_ids)}, "
                f"article_backed={len(article_backed_ids)}, "
                f"missing={len(missing)}, without_current_articles={len(without_articles)}, "
                f"invalid_current_articles={invalid_current_articles}"
            )
        return {
            "expected": len(expected_ids),
            "loaded": len(loaded_ids),
            "article_backed": len(article_backed_ids),
        }

    @staticmethod
    def _scroll_qdrant_point_ids(qdrant, collection: str, exact_count: int) -> list[str]:
        """Read the complete point-id set with finite pagination guards."""
        if exact_count < 0 or exact_count > _QDRANT_READINESS_MAX_POINTS:
            raise RuntimeError(
                "law index unreadable: "
                f"qdrant_count={exact_count}, "
                f"readiness_limit={_QDRANT_READINESS_MAX_POINTS}"
            )
        point_ids: list[str] = []
        offset = None
        seen_offsets: set[str] = set()
        max_pages = exact_count + 1
        for _ in range(max_pages):
            records, next_offset = qdrant.scroll(
                collection_name=collection,
                limit=_QDRANT_SCROLL_PAGE_SIZE,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            if len(records) > _QDRANT_SCROLL_PAGE_SIZE:
                raise RuntimeError("law index unreadable: oversized qdrant scroll page")
            point_ids.extend(str(record.id) for record in records)
            if len(point_ids) > exact_count:
                raise RuntimeError("law index incomplete: qdrant scroll exceeded exact count")
            if next_offset is None:
                return point_ids
            offset_marker = str(next_offset)
            if offset_marker in seen_offsets:
                raise RuntimeError("law index unreadable: qdrant pagination loop")
            seen_offsets.add(offset_marker)
            offset = next_offset
        raise RuntimeError("law index unreadable: qdrant pagination exceeded exact count")

    def _verify_law_index(self) -> dict[str, int]:
        """Require complete Qdrant retrieval and Neo4j article projections."""
        if self._law_index_verified:
            return dict(self._law_index_state or {})
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  count(*) FILTER (
                    WHERE a.is_current AND NOT EXISTS (
                      SELECT 1 FROM law_articles c WHERE c.parent_article_pk=a.id
                    )
                  ),
                  count(*) FILTER (
                    WHERE a.is_current
                      AND a.qdrant_point_id IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM law_articles c WHERE c.parent_article_pk=a.id
                      )
                  ),
                  count(*) FILTER (
                    WHERE a.is_current AND a.paragraph_no IS NULL
                  )
                FROM law_articles a
                """
            )
            expected, indexed, graph_articles = cur.fetchone()
            cur.execute(
                """
                SELECT a.qdrant_point_id::text
                FROM law_articles a
                WHERE a.is_current
                  AND a.qdrant_point_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM law_articles c WHERE c.parent_article_pk=a.id
                  )
                """
            )
            postgres_qdrant_point_ids = [str(row[0]) for row in cur.fetchall()]
            cur.execute(
                """
                SELECT id
                FROM law_articles
                WHERE is_current AND paragraph_no IS NULL
                """
            )
            postgres_graph_ids = {int(row[0]) for row in cur.fetchall()}
        qdrant = qdrant_client()
        qdrant_count = int(qdrant.count(_QDRANT_COLLECTION, exact=True).count)
        qdrant_point_ids = self._scroll_qdrant_point_ids(qdrant, _QDRANT_COLLECTION, qdrant_count)
        postgres_qdrant_ids = set(postgres_qdrant_point_ids)
        actual_qdrant_ids = set(qdrant_point_ids)
        postgres_duplicate_ids = len(postgres_qdrant_point_ids) - len(postgres_qdrant_ids)
        qdrant_duplicate_ids = len(qdrant_point_ids) - len(actual_qdrant_ids)
        qdrant_missing_ids = postgres_qdrant_ids - actual_qdrant_ids
        qdrant_orphaned_ids = actual_qdrant_ids - postgres_qdrant_ids
        drv = make_driver()
        try:
            with drv.session() as session:
                graph_rows = list(
                    session.run("MATCH (a:Article) RETURN a.article_pk AS article_pk")
                )
        finally:
            drv.close()
        neo4j_ids = {int(row["article_pk"]) for row in graph_rows if row["article_pk"] is not None}
        neo4j_count = len(graph_rows)
        missing_ids = postgres_graph_ids - neo4j_ids
        orphaned_count = len(neo4j_ids - postgres_graph_ids) + (neo4j_count - len(neo4j_ids))
        if (
            expected != indexed
            or expected != qdrant_count
            or qdrant_count != len(qdrant_point_ids)
            or postgres_duplicate_ids
            or qdrant_duplicate_ids
            or qdrant_missing_ids
            or qdrant_orphaned_ids
        ):
            raise RuntimeError(
                "law index incomplete: "
                f"expected={expected}, postgres_indexed={indexed}, "
                f"qdrant={qdrant_count}, qdrant_scrolled={len(qdrant_point_ids)}, "
                f"missing={len(qdrant_missing_ids)}, "
                f"orphaned={len(qdrant_orphaned_ids)}, "
                f"postgres_duplicates={postgres_duplicate_ids}, "
                f"qdrant_duplicates={qdrant_duplicate_ids}"
            )
        if (
            graph_articles != len(postgres_graph_ids)
            or graph_articles != neo4j_count
            or missing_ids
            or orphaned_count
        ):
            raise RuntimeError(
                "law graph incomplete: "
                f"postgres_graph_articles={graph_articles}, neo4j={neo4j_count}, "
                f"missing={len(missing_ids)}, orphaned={orphaned_count}"
            )
        self._law_index_state = {
            "expected": int(expected),
            "postgres_indexed": int(indexed),
            "qdrant": int(qdrant_count),
            "postgres_graph_articles": int(graph_articles),
            "neo4j": int(neo4j_count),
            "neo4j_missing": 0,
            "neo4j_orphaned": 0,
        }
        self._law_index_verified = True
        return dict(self._law_index_state)

    def readiness(self) -> dict:
        snapshot_id = self.current_snapshot_id()
        if not snapshot_id:
            raise RuntimeError("current CCCT snapshot is missing")
        return {
            "status": "ready",
            "knowledge_snapshot_id": snapshot_id,
            "supported_review_as_of": self.snapshot_effective_date(snapshot_id),
            "law_corpus": self._verify_target_law_coverage(),
            "law_index": self._verify_law_index(),
        }

    def search_law_articles(
        self, query: str, top_k: int, as_of: date
    ) -> tuple[LawArticleMatch, ...]:
        """Dense+BM25 RRF, lexical rerank, then one-hop graph expansion."""
        self._verify_target_law_coverage()
        self._verify_law_index()
        recall = min(max(top_k * 2, 8), 40)
        dense_hits = repository.search(query, arm="dense", limit=recall, as_of=as_of)
        bm25_hits = repository.search(query, arm="bm25", limit=recall, as_of=as_of)
        rrf_k = 60
        candidates: dict[int, dict] = {}
        for method, hits in (("dense", dense_hits), ("bm25", bm25_hits)):
            for rank, hit in enumerate(hits, start=1):
                entry = candidates.setdefault(
                    hit.article_pk,
                    {"methods": set(), "raw": {}, "rrf": 0.0},
                )
                entry["methods"].add(method)
                entry["raw"][method] = float(hit.score)
                entry["rrf"] += 1.0 / (rrf_k + rank)

        # Add related articles without allowing graph-only records to outrank
        # their retrieved seed.  Graph lookup failure is a real requested-arm
        # failure and is surfaced by the caller as partial coverage.
        seeds = sorted(candidates, key=lambda pk: candidates[pk]["rrf"], reverse=True)[:3]
        for seed_pk in seeds:
            for related in repository.expand_article(seed_pk):
                related_pk = related.get("article_pk")
                if not related_pk or related_pk == seed_pk:
                    continue
                entry = candidates.setdefault(
                    int(related_pk),
                    {"methods": set(), "raw": {}, "rrf": candidates[seed_pk]["rrf"] * 0.65},
                )
                entry["methods"].add("graph")
                entry["raw"]["graph"] = max(
                    float(entry["raw"].get("graph", 0.0)),
                    float(candidates[seed_pk]["rrf"]),
                )

        if not candidates:
            return ()
        pks = list(candidates)
        with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT a.id AS article_pk, a.law_id, l.law_name, l.hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.title, a.content,
                       a.content_hash, a.version_mst, a.valid_from, a.valid_to
                FROM law_articles a JOIN laws l USING (law_id)
                WHERE a.id = ANY(%s)
                  AND a.valid_from <= %s
                  AND (a.valid_to IS NULL OR a.valid_to > %s)
                """,
                (pks, as_of, as_of),
            )
            rows = {row["article_pk"]: dict(row) for row in cur.fetchall()}

        query_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", query.lower()))
        theoretical_rrf_max = 2.0 / (rrf_k + 1)
        matches: list[LawArticleMatch] = []
        for pk, candidate in candidates.items():
            row = rows.get(pk)
            if row is None:
                continue
            text_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", row["content"].lower()))
            lexical = len(query_tokens & text_tokens) / len(query_tokens) if query_tokens else 0.0
            rrf_normalized = min(1.0, candidate["rrf"] / theoretical_rrf_max)
            score = min(1.0, 0.85 * rrf_normalized + 0.15 * lexical)
            raw_scores = {
                **candidate["raw"],
                "rrf": float(candidate["rrf"]),
                "lexical_overlap": float(lexical),
            }
            method_order = cast(
                tuple[Literal["dense", "bm25", "graph"], ...],
                tuple(
                    method
                    for method in ("dense", "bm25", "graph")
                    if method in candidate["methods"]
                ),
            )
            matches.append(LawArticleMatch(row, method_order, score, raw_scores))
        matches.sort(key=lambda match: (-match.score, match.row["article_pk"]))
        return tuple(matches[:top_k])


def _evidence_id(source_record_id: str, version_hash: str) -> str:
    # Agent/Data 공유 계약: ev_ + sha256(source_record_id + US + version_hash) hex.
    raw = f"{source_record_id}\x1f{version_hash}".encode("utf-8")
    return f"ev_{hashlib.sha256(raw).hexdigest()}"


def _error(code: str, message: str, *, retryable: bool = False) -> EvidenceError:
    return EvidenceError(code=code, message=message, retryable=retryable)


_BACKEND_ERROR_MESSAGES = {
    "KNOWLEDGE_SNAPSHOT_MISMATCH": "Knowledge snapshot consistency check failed",
    "KNOWLEDGE_STORE_UNAVAILABLE": "Knowledge store is unavailable",
    "KNOWLEDGE_SNAPSHOT_METADATA_UNAVAILABLE": ("Knowledge snapshot metadata is unavailable"),
    "HS_MASTER_UNAVAILABLE": "HS master is unavailable",
    "REQUIREMENT_LOOKUP_FAILED": "Requirement lookup failed",
    "LAW_RETRIEVAL_FAILED": "Law retrieval failed",
}

_VALIDATION_ISSUE_LAW_TERMS = {
    "INVOICE_NO_MISMATCH": "송장번호 불일치 신고사항 보완 상업송장 대조",
    "INV_LINE_AMOUNT_MISMATCH": "송장 품목금액 불일치 과세가격 신고사항 보완",
    "QTY_MISMATCH_INVOICE_PACKING": "송장 포장명세서 수량 불일치 신고사항 보완",
    "ORIGIN_MISMATCH": "원산지증명서 원산지 불일치 신고사항 보완",
    "COUNTRY_OF_ORIGIN_MISMATCH": "원산지 국가 불일치 신고사항 보완",
}


def _backend_error(code: str) -> EvidenceError:
    """Return a fixed public message; backend exception text stays server-side."""
    return _error(code, _BACKEND_ERROR_MESSAGES[code], retryable=True)


def _row_to_evidence(item_id: str, row: dict) -> Evidence:
    agencies = [row["agency_name"]] if row.get("agency_name") else []
    agency_text = ", ".join(agencies) or "기관 미상"
    title = f"{row['law_name']} — {row['document_name']}"
    text = (
        f"HSK {row['hsk10']} {row['trade_type']} 요건: {row['law_name']}; "
        f"필요 확인서류 {row['document_name']}; 요건승인기관 {agency_text}."
    )
    return Evidence(
        evidence_id=_evidence_id(row["source_record_id"], row["requirement_version_hash"]),
        source_record_id=row["source_record_id"],
        source_type="customs_requirement",
        item_id=item_id,
        title=title,
        text=text,
        law_id=row.get("law_code"),
        law_name=row.get("law_name"),
        article_no=None,
        source_uri=row["source_uri"],
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        version_hash=row["requirement_version_hash"],
        matched_hs_code=row.get("hsk10"),
        trade_type=row["trade_type"],
        required_document_code=row.get("document_code"),
        required_document_name=row.get("document_name"),
        agencies=agencies,
        retrieval_methods=["exact_hs", "graph"],
        eligibility="finding",
        score=None,
        raw_scores={},
    )


def _law_match_to_evidence(
    item_id: str,
    trade_type: Literal["import", "export"],
    match: LawArticleMatch,
) -> Evidence:
    row = match.row
    source_record_id = f"law_article:{row['article_pk']}"
    encoded_article_no = int(row["article_no"])
    article_label = f"제{encoded_article_no // 100}조"
    if encoded_article_no % 100:
        article_label += f"의{encoded_article_no % 100}"
    if row.get("paragraph_no") is not None:
        article_label += f" 제{row['paragraph_no']}항"
    title_suffix = f"({row['title']})" if row.get("title") else ""
    source_uri = (
        f"https://www.law.go.kr/%EB%B2%95%EB%A0%B9/{quote(row['law_name'])}/{quote(article_label)}"
    )
    # law_articles uses a half-open valid_to.  The Evidence wire exposes the
    # last applicable calendar date so every consumer uses one convention.
    wire_valid_to = row["valid_to"] - timedelta(days=1) if row.get("valid_to") else None
    return Evidence(
        evidence_id=_evidence_id(source_record_id, row["content_hash"]),
        source_record_id=source_record_id,
        source_type="law_article",
        item_id=item_id,
        title=f"{row['law_name']} {article_label}{title_suffix}",
        text=row["content"],
        law_id=row["law_id"],
        law_name=row["law_name"],
        article_no=article_label,
        source_uri=source_uri,
        valid_from=row["valid_from"],
        valid_to=wire_valid_to,
        version_hash=row["content_hash"],
        matched_hs_code=None,
        trade_type=trade_type,
        required_document_code=None,
        required_document_name=None,
        agencies=[],
        retrieval_methods=list(match.retrieval_methods),
        eligibility="supporting",
        score=round(match.score, 6),
        raw_scores={key: float(value) for key, value in match.raw_scores.items()},
    )


def _law_query(request: RetrieveRequest, _item: RetrieveItem, _hsk10: str) -> str:
    # HSK-specific legal requirements are resolved authoritatively by the
    # structured lookup above.  Law-article retrieval supports explanation,
    # so goods descriptions and HSK digits must not drown out the procedural
    # customs-law intent (the former ranking put an unrelated exemption rule
    # above declaration-document provisions for ordinary steel fasteners).
    direction = "수입" if request.trade_type == "import" else "수출"
    issue_terms = " ".join(
        _VALIDATION_ISSUE_LAW_TERMS.get(code, "") for code in request.validation_issue_codes
    )
    return " ".join(
        part
        for part in (
            f"{direction} 신고 제출서류 세관장 확인 허가 승인 증명 관세법",
            issue_terms,
        )
        if part
    )


def _unavailable_results(
    request: RetrieveRequest, error: EvidenceError
) -> list[ItemEvidenceResult]:
    return [
        ItemEvidenceResult(
            item_id=item.item_id,
            hs_resolution="missing" if not item.hs_code else "invalid",
            resolved_hsk10=None,
            candidate_hsk10s=[],
            coverage="unknown",
            requirements_found=False,
            evidence=[],
            errors=[error],
        )
        for item in request.items
    ]


def _unsupported_as_of_results(
    request: RetrieveRequest,
    backend: EvidenceBackend,
    error: EvidenceError,
) -> list[ItemEvidenceResult]:
    """Preserve current HSK resolution, but never claim temporal coverage."""
    results: list[ItemEvidenceResult] = []
    for item in request.items:
        try:
            resolution = backend.resolve_hsk(item.hs_code)
        except Exception:  # the temporal error remains the primary blocker
            resolution = repository.HskResolution(
                "missing" if not item.hs_code else "invalid",
                item.hs_code,
                None,
                (),
            )
        results.append(
            ItemEvidenceResult(
                item_id=item.item_id,
                hs_resolution=resolution.status,
                resolved_hsk10=resolution.resolved_hsk10,
                candidate_hsk10s=list(resolution.candidate_hsk10s),
                coverage="unknown",
                requirements_found=False,
                evidence=[],
                errors=[error],
            )
        )
    return results


def retrieve_evidence(
    request: RetrieveRequest, backend: EvidenceBackend | None = None
) -> RetrieveResponse:
    """Evidence v1 orchestration. backend 주입으로 단위/계약 테스트를 분리한다."""
    backend = backend or DatabaseEvidenceBackend()
    try:
        snapshot_id = backend.current_snapshot_id()
    except Exception as exc:  # DB 장애를 근거 0건으로 오해하지 않도록 계약 응답화
        code = (
            "KNOWLEDGE_SNAPSHOT_MISMATCH"
            if isinstance(exc, KnowledgeSnapshotMismatch)
            else "KNOWLEDGE_STORE_UNAVAILABLE"
        )
        err = _backend_error(code)
        return RetrieveResponse(
            schema_version=SCHEMA_VERSION,
            request_id=request.request_id,
            status="failed",
            knowledge_snapshot_id=None,
            results=_unavailable_results(request, err),
            errors=[err],
        )
    if not snapshot_id:
        err = _error("KNOWLEDGE_SNAPSHOT_MISSING", "현행 CCCT knowledge snapshot이 없음")
        return RetrieveResponse(
            schema_version=SCHEMA_VERSION,
            request_id=request.request_id,
            status="failed",
            knowledge_snapshot_id=None,
            results=_unavailable_results(request, err),
            errors=[err],
        )

    try:
        supported_as_of = backend.snapshot_effective_date(snapshot_id)
    except Exception:
        err = _backend_error("KNOWLEDGE_SNAPSHOT_METADATA_UNAVAILABLE")
        return RetrieveResponse(
            schema_version=SCHEMA_VERSION,
            request_id=request.request_id,
            status="failed",
            knowledge_snapshot_id=snapshot_id,
            results=_unavailable_results(request, err),
            errors=[err],
        )
    if request.review_as_of != supported_as_of:
        err = _error(
            "UNSUPPORTED_REVIEW_AS_OF",
            (
                f"CCCT snapshot {snapshot_id} supports review_as_of "
                f"{supported_as_of.isoformat()} only; got {request.review_as_of.isoformat()}"
            ),
            retryable=False,
        )
        return RetrieveResponse(
            schema_version=SCHEMA_VERSION,
            request_id=request.request_id,
            status="failed",
            knowledge_snapshot_id=snapshot_id,
            results=_unsupported_as_of_results(request, backend, err),
            errors=[err],
        )

    results: list[ItemEvidenceResult] = []
    partial = False
    for item in request.items:
        try:
            resolution = backend.resolve_hsk(item.hs_code)
        except Exception:
            partial = True
            err = _backend_error("HS_MASTER_UNAVAILABLE")
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution="missing" if not item.hs_code else "invalid",
                    resolved_hsk10=None,
                    candidate_hsk10s=[],
                    coverage="unknown",
                    requirements_found=False,
                    evidence=[],
                    errors=[err],
                )
            )
            continue

        if resolution.resolved_hsk10 is None:
            partial = True
            code = {
                "missing": "HS_CODE_MISSING",
                "invalid": "HS_CODE_INVALID",
                "ambiguous": "HS_CODE_AMBIGUOUS",
            }[resolution.status]
            message = {
                "missing": "HS Code가 없어 요건 커버리지를 확정할 수 없음",
                "invalid": "HS Code가 현행 HSK leaf로 해소되지 않음",
                "ambiguous": "HS6의 HSK10 하위 leaf가 여러 개라 자동 확정할 수 없음",
            }[resolution.status]
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution=resolution.status,
                    resolved_hsk10=None,
                    candidate_hsk10s=list(resolution.candidate_hsk10s),
                    coverage="unknown",
                    requirements_found=False,
                    evidence=[],
                    errors=[_error(code, message)],
                )
            )
            continue

        try:
            lookup = backend.lookup_requirements(
                snapshot_id, resolution.resolved_hsk10, request.trade_type, request.review_as_of
            )
        except Exception:
            partial = True
            err = _backend_error("REQUIREMENT_LOOKUP_FAILED")
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution=resolution.status,
                    resolved_hsk10=resolution.resolved_hsk10,
                    candidate_hsk10s=list(resolution.candidate_hsk10s),
                    coverage="partial",
                    requirements_found=False,
                    evidence=[],
                    errors=[err],
                )
            )
            continue

        if not lookup.covered:
            partial = True
            err = _error(
                "CCCT_COVERAGE_MISSING",
                f"{resolution.resolved_hsk10}/{request.trade_type} 공식 API 조회 커버리지가 없음",
                retryable=True,
            )
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution=resolution.status,
                    resolved_hsk10=resolution.resolved_hsk10,
                    candidate_hsk10s=list(resolution.candidate_hsk10s),
                    coverage="partial",
                    requirements_found=False,
                    evidence=[],
                    errors=[err],
                )
            )
            continue

        requirement_evidence = [_row_to_evidence(item.item_id, row) for row in lookup.rows]
        law_evidence: list[Evidence] = []
        item_errors: list[EvidenceError] = []
        item_coverage: Literal["complete", "partial"] = "complete"
        if "law_support" in request.query_intent:
            try:
                matches = backend.search_law_articles(
                    _law_query(request, item, resolution.resolved_hsk10),
                    request.top_k,
                    request.review_as_of,
                )
                law_evidence = [
                    _law_match_to_evidence(item.item_id, request.trade_type, match)
                    for match in matches
                ]
            except Exception:
                partial = True
                item_coverage = "partial"
                item_errors.append(_backend_error("LAW_RETRIEVAL_FAILED"))
        evidence = [*requirement_evidence, *law_evidence]
        results.append(
            ItemEvidenceResult(
                item_id=item.item_id,
                hs_resolution=resolution.status,
                resolved_hsk10=resolution.resolved_hsk10,
                candidate_hsk10s=list(resolution.candidate_hsk10s),
                coverage=item_coverage,
                requirements_found=bool(requirement_evidence),
                evidence=evidence,
                errors=item_errors,
            )
        )

    top_errors = (
        [
            _error(
                "PARTIAL_RESULTS",
                "일부 품목의 지식 조회를 완료하지 못함",
                retryable=any(error.retryable for result in results for error in result.errors),
            )
        ]
        if partial
        else []
    )
    return RetrieveResponse(
        schema_version=SCHEMA_VERSION,
        request_id=request.request_id,
        status="partial" if partial else "ok",
        knowledge_snapshot_id=snapshot_id,
        results=results,
        errors=top_errors,
    )
