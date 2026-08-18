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
    # The retained import default is a V3 wire-compatibility detail.  Runtime
    # semantics normalize that untouched legacy value from ``trade_type`` so
    # an export request means ``required_export_documents`` rather than an
    # import request with a misleading label.
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


class TemporalContext(ContractModel):
    """The independently versioned inputs used for one Evidence result.

    Law text can be retrieved at the review date in the law-history profile,
    while the retained HSK and CCCT requirement snapshots have one verified
    baseline date.  Keeping all three dates explicit prevents a consumer from
    presenting that mixed coverage as one time-complete result.
    """

    law_as_of: date
    hsk_snapshot_as_of: date
    requirements_snapshot_as_of: date


class TemporalCoverage(ContractModel):
    """Whether each independently dated source is verified for this request.

    Dates alone are not a coverage claim.  In particular an unavailable
    historical HSK/CCCT archive keeps the retained baseline date visible in
    ``temporal_context`` while this object prevents consumers from presenting
    it as an answer for the requested date.
    """

    law: Literal["covered", "unavailable"]
    hsk: Literal["covered", "unavailable"]
    requirements: Literal["covered", "unavailable"]


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
    retrieval_methods: list[Literal["exact_hs", "dense", "bm25", "graph", "rerank"]] = Field(
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
    temporal_context: TemporalContext | None = None
    temporal_coverage: TemporalCoverage | None = None
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
    retrieval_methods: tuple[Literal["dense", "bm25", "graph", "rerank"], ...]
    score: float
    raw_scores: dict[str, float]


@dataclass(frozen=True)
class TemporalEvidenceSupport:
    """Profile-owned temporal availability returned without raw source data."""

    context: TemporalContext
    law_coverage_available: bool
    requirements_lookup_as_of: date
    # ``None`` preserves the retained 3-law and prior law-history test
    # backends.  Archive-aware law history always supplies the explicit
    # three-component public coverage object.
    temporal_coverage: TemporalCoverage | None = None
    hsk_coverage_available: bool = True
    requirements_coverage_available: bool = True
    hsk_snapshot_id: str | None = None
    requirements_snapshot_id: str | None = None


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

    def readiness(self) -> dict:
        """Check only live store connectivity and minimal snapshot state.

        This is intentionally safe to call from ``/health/ready``.  Full
        PostgreSQL/Qdrant/Neo4j article-ID and content comparison is exposed
        separately through the explicit ``dev_store.py verify`` or
        ``functional_uat_3law.py verify-store`` maintenance command.
        """

        snapshot_id = self.current_snapshot_id()
        if not snapshot_id:
            raise RuntimeError("current CCCT snapshot is missing")
        return {
            "status": "ready",
            "knowledge_snapshot_id": snapshot_id,
            "supported_review_as_of": self.snapshot_effective_date(snapshot_id),
        }

    def list_laws(self, as_of: date) -> list[dict]:
        """The statutes this store can search, with article counts valid at as_of.

        A read-only reference catalog for the Browser's law-search screen; it
        carries no requirement, release, or approval meaning.
        """

        with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT l.law_id, l.law_name, l.hierarchy, count(a.id) AS article_count
                FROM laws l JOIN law_articles a USING (law_id)
                WHERE a.valid_from <= %s
                  AND (a.valid_to IS NULL OR a.valid_to > %s)
                GROUP BY l.law_id, l.law_name, l.hierarchy
                ORDER BY l.law_name, l.law_id
                """,
                (as_of, as_of),
            )
            return [dict(row) for row in cur.fetchall()]

    def search_law_articles(
        self, query: str, top_k: int, as_of: date
    ) -> tuple[LawArticleMatch, ...]:
        """Dense+BM25 RRF, lexical rerank, then one-hop graph expansion."""
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
                tuple[Literal["dense", "bm25", "graph", "rerank"], ...],
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
    # The history backend joins a receipt for the exact version MST.  Its
    # official URI is the only permitted citation target for that branch;
    # never synthesize a generic current-law URL for an historical article.
    verified_uri = row.get("official_uri")
    if verified_uri is not None:
        if not isinstance(verified_uri, str) or not verified_uri.startswith(
            "https://www.law.go.kr/"
        ):
            raise RuntimeError("LAW_HISTORY_OFFICIAL_URI_INVALID")
        source_uri = verified_uri
    else:
        # Retained three-law Evidence does not have the law-history receipt
        # join and preserves its legacy, human-readable article URL.
        source_uri = f"https://www.law.go.kr/%EB%B2%95%EB%A0%B9/{quote(row['law_name'])}/{quote(article_label)}"
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


def _effective_query_intent(request: RetrieveRequest) -> str:
    """Normalize only the old direction-specific default without changing V3 wire.

    Existing import clients keep their exact input and behavior.  Older
    callers that rely on the historical default for an export request are
    interpreted as asking for Korean export documents.  This does not extend
    Data coverage to a foreign destination country's import requirements.
    """

    if request.trade_type == "export":
        if request.query_intent == "required_import_documents":
            return "required_export_documents"
        if request.query_intent == "required_import_documents_and_law_support":
            return "required_export_documents_and_law_support"
    if request.trade_type == "import":
        if request.query_intent == "required_export_documents":
            return "required_import_documents"
        if request.query_intent == "required_export_documents_and_law_support":
            return "required_import_documents_and_law_support"
    return request.query_intent


def _destination_coverage(
    request: RetrieveRequest,
    item: RetrieveItem,
) -> tuple[EvidenceError | None, bool]:
    """Return a fixed limitation and whether Korean-side lookup may continue.

    The current jurisdiction is Korea.  A Korean import must therefore have
    ``destination_country=KR``.  A Korean export can return Korean export
    process evidence for a foreign destination, but the same CCCT snapshot
    cannot establish that destination country's import restrictions.
    """

    if request.trade_type == "import":
        if item.destination_country != "KR":
            return (
                _error(
                    "IMPORT_DESTINATION_COUNTRY_UNSUPPORTED",
                    "현재 Data 범위는 대한민국으로 수입되는 건의 요건만 확인할 수 있습니다.",
                    retryable=False,
                ),
                False,
            )
        return None, True
    if item.destination_country == "KR":
        return (
            _error(
                "EXPORT_DESTINATION_COUNTRY_INVALID",
                "수출 심사는 목적국 코드가 필요하며 KR은 수출 목적국으로 사용할 수 없습니다.",
                retryable=False,
            ),
            False,
        )
    return (
        _error(
            "FOREIGN_DESTINATION_REQUIREMENTS_UNAVAILABLE",
            "현재 Data 스냅샷은 대한민국 수출 절차 근거만 제공하며 목적국 수입요건은 포함하지 않습니다.",
            retryable=False,
        ),
        True,
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


def _law_history_temporal_support(
    backend: EvidenceBackend,
    snapshot_id: str | None,
    review_as_of: date,
) -> TemporalEvidenceSupport | None:
    """Read an optional profile-owned temporal capability without changing V1.

    The retained three-law backend intentionally has no such method.  It
    keeps its exact one-date behaviour.  The law-history backend opts in and
    must return a fully populated, typed context; malformed values are a
    server-side availability failure rather than a silent legacy fallback.
    """

    provider = getattr(backend, "temporal_evidence_support", None)
    if provider is None:
        return None
    support = provider(snapshot_id, review_as_of)
    if not isinstance(support, TemporalEvidenceSupport):
        raise RuntimeError("LAW_HISTORY_TEMPORAL_SUPPORT_INVALID")
    if support.context.law_as_of != review_as_of:
        raise RuntimeError("LAW_HISTORY_TEMPORAL_CONTEXT_INVALID")
    if support.temporal_coverage is not None and (
        support.temporal_coverage.law
        != ("covered" if support.law_coverage_available else "unavailable")
        or support.temporal_coverage.hsk
        != ("covered" if support.hsk_coverage_available else "unavailable")
        or support.temporal_coverage.requirements
        != ("covered" if support.requirements_coverage_available else "unavailable")
    ):
        raise RuntimeError("LAW_HISTORY_TEMPORAL_COVERAGE_INVALID")
    if support.requirements_snapshot_id is not None and not support.requirements_snapshot_id:
        raise RuntimeError("LAW_HISTORY_TEMPORAL_SUPPORT_INVALID")
    return support


def _resolve_hsk_for_review_date(
    backend: EvidenceBackend,
    code: str | None,
    review_as_of: date,
) -> repository.HskResolution:
    """Use a profile's archived HSK snapshot only when it explicitly opts in."""

    resolver = getattr(backend, "resolve_hsk_as_of", None)
    if callable(resolver):
        value = resolver(code, review_as_of)
        if not isinstance(value, repository.HskResolution):
            raise RuntimeError("HSK_HISTORY_RESOLUTION_INVALID")
        return value
    return backend.resolve_hsk(code)


def retrieve_evidence(
    request: RetrieveRequest, backend: EvidenceBackend | None = None
) -> RetrieveResponse:
    """Evidence v1 orchestration. backend 주입으로 단위/계약 테스트를 분리한다."""
    backend = backend or DatabaseEvidenceBackend()
    # A law-history profile owns its three-date context independently from
    # the mutable CCCT snapshot ID.  Resolve it before any snapshot lookup so
    # even a controlled availability failure cannot silently erase the date
    # boundary from a law-history response.  Legacy backends simply return
    # ``None`` here and retain their old nullable wire behaviour.
    try:
        history_support = _law_history_temporal_support(backend, None, request.review_as_of)
    except Exception:
        err = _backend_error("KNOWLEDGE_SNAPSHOT_METADATA_UNAVAILABLE")
        return RetrieveResponse(
            schema_version=SCHEMA_VERSION,
            request_id=request.request_id,
            status="failed",
            knowledge_snapshot_id=None,
            results=_unavailable_results(request, err),
            errors=[err],
        )
    temporal_context = history_support.context if history_support is not None else None
    temporal_coverage = history_support.temporal_coverage if history_support is not None else None
    # A missing historical CCCT archive is a known, non-retryable coverage
    # boundary.  It must not depend on today's mutable current snapshot just
    # to report that boundary: the caller can still receive independently
    # covered law evidence (when HSK is also archived) with a ``partial``
    # result.  For every covered-requirements path, retain the established
    # snapshot lookup and consistency checks.
    requirements_coverage_available_at_start = (
        history_support.requirements_coverage_available if history_support is not None else True
    )
    if history_support is not None and not requirements_coverage_available_at_start:
        snapshot_id: str | None = None
    else:
        try:
            # An active historical archive has a verified PostgreSQL binding
            # and deliberately has no Neo4j current-graph projection.  Do
            # not make a historical archive lookup depend on the mutable
            # current graph.
            snapshot_id = (
                history_support.requirements_snapshot_id
                if history_support is not None
                and history_support.requirements_snapshot_id is not None
                else backend.current_snapshot_id()
            )
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
                temporal_context=temporal_context,
                temporal_coverage=temporal_coverage,
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
                temporal_context=temporal_context,
                temporal_coverage=temporal_coverage,
                results=_unavailable_results(request, err),
                errors=[err],
            )

    if history_support is None:
        assert snapshot_id is not None
        try:
            supported_as_of = backend.snapshot_effective_date(snapshot_id)
        except Exception:
            err = _backend_error("KNOWLEDGE_SNAPSHOT_METADATA_UNAVAILABLE")
            return RetrieveResponse(
                schema_version=SCHEMA_VERSION,
                request_id=request.request_id,
                status="failed",
                knowledge_snapshot_id=snapshot_id,
                temporal_context=temporal_context,
                temporal_coverage=temporal_coverage,
                results=_unavailable_results(request, err),
                errors=[err],
            )
    else:
        supported_as_of = history_support.requirements_lookup_as_of
    # The legacy profile remains deliberately one-date only.  A law-history
    # profile owns a verified legal date range and keeps the retained HSK /
    # CCCT snapshot date visible through ``temporal_context`` instead.
    if history_support is None and request.review_as_of != supported_as_of:
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
            temporal_context=temporal_context,
            temporal_coverage=temporal_coverage,
            results=_unsupported_as_of_results(request, backend, err),
            errors=[err],
        )

    query_intent = _effective_query_intent(request)
    results: list[ItemEvidenceResult] = []
    requirements_as_of = (
        history_support.requirements_lookup_as_of
        if history_support is not None
        else request.review_as_of
    )
    law_coverage_available = (
        history_support.law_coverage_available if history_support is not None else True
    )
    hsk_coverage_available = (
        history_support.hsk_coverage_available if history_support is not None else True
    )
    requirements_coverage_available = (
        history_support.requirements_coverage_available if history_support is not None else True
    )
    temporal_item_errors: list[EvidenceError] = []
    temporal_top_errors: list[EvidenceError] = []
    partial = False
    if history_support is not None:
        if requirements_coverage_available and requirements_as_of != request.review_as_of:
            mismatch = _error(
                "TEMPORAL_REQUIREMENT_SNAPSHOT_MISMATCH",
                (
                    "법령 기준일과 HSK·요건 스냅샷 기준일이 다릅니다: "
                    f"law_as_of={request.review_as_of.isoformat()}, "
                    f"snapshot_as_of={requirements_as_of.isoformat()}"
                ),
                retryable=False,
            )
            temporal_item_errors.append(mismatch)
            temporal_top_errors.append(mismatch)
            partial = True
        if not law_coverage_available:
            unavailable = _error(
                "LAW_HISTORY_COVERAGE_MISSING",
                (
                    "요청 법령 기준일의 검증된 이력 스냅샷이 없습니다: "
                    f"law_as_of={request.review_as_of.isoformat()}"
                ),
                retryable=False,
            )
            temporal_item_errors.append(unavailable)
            temporal_top_errors.append(unavailable)
            partial = True
        if not hsk_coverage_available:
            unavailable = _error(
                "HSK_HISTORY_COVERAGE_MISSING",
                (
                    "요청 HSK 기준일의 검증된 공식 archive가 없습니다: "
                    f"hsk_as_of={request.review_as_of.isoformat()}"
                ),
                retryable=False,
            )
            temporal_item_errors.append(unavailable)
            temporal_top_errors.append(unavailable)
            partial = True
        if not requirements_coverage_available:
            unavailable = _error(
                "CCCT_HISTORY_COVERAGE_MISSING",
                (
                    "요청 요건 기준일의 검증된 공식 archive가 없습니다: "
                    f"requirements_as_of={request.review_as_of.isoformat()}"
                ),
                retryable=False,
            )
            temporal_item_errors.append(unavailable)
            temporal_top_errors.append(unavailable)
            partial = True
    for item in request.items:
        if not hsk_coverage_available:
            # Never resolve against today's Neo4j master and present it as an
            # archive-date answer.  HSK candidates and CCCT requirements are
            # absent in this state.  Independently covered law support may
            # still be returned because the procedural law query does not
            # rely on an HSK resolution.  Do not label a caller-provided code
            # ``invalid``: that conclusion requires the unavailable archive.
            # ``missing`` here means that this result has no resolvable HSK
            # value, while the stable coverage error carries the reason.
            archive_item_errors = list(temporal_item_errors)
            hsk_unavailable_law_evidence: list[Evidence] = []
            destination_error, may_lookup_korean_requirements = _destination_coverage(request, item)
            if destination_error is not None:
                archive_item_errors.append(destination_error)
            if (
                may_lookup_korean_requirements
                and "law_support" in query_intent
                and law_coverage_available
            ):
                try:
                    matches = backend.search_law_articles(
                        _law_query(request, item, ""), request.top_k, request.review_as_of
                    )
                    hsk_unavailable_law_evidence = [
                        _law_match_to_evidence(item.item_id, request.trade_type, match)
                        for match in matches
                    ]
                except Exception:
                    archive_item_errors.append(_backend_error("LAW_RETRIEVAL_FAILED"))
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution="missing",
                    resolved_hsk10=None,
                    candidate_hsk10s=[],
                    coverage="partial",
                    requirements_found=False,
                    evidence=hsk_unavailable_law_evidence,
                    errors=archive_item_errors,
                )
            )
            continue
        try:
            resolution = _resolve_hsk_for_review_date(backend, item.hs_code, request.review_as_of)
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
                    errors=[*temporal_item_errors, err],
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
                "invalid": "HS Code가 선택된 HSK leaf로 해소되지 않음",
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
                    errors=[*temporal_item_errors, _error(code, message)],
                )
            )
            continue

        destination_error, may_lookup_korean_requirements = _destination_coverage(request, item)
        if not may_lookup_korean_requirements:
            partial = True
            assert destination_error is not None
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution=resolution.status,
                    resolved_hsk10=resolution.resolved_hsk10,
                    candidate_hsk10s=list(resolution.candidate_hsk10s),
                    coverage="partial",
                    requirements_found=False,
                    evidence=[],
                    errors=[*temporal_item_errors, destination_error],
                )
            )
            continue

        if not requirements_coverage_available:
            # HSK itself may be archived while CCCT is not.  Law support can
            # still be returned only from the independently covered law
            # history; no current/baseline CCCT row is substituted.
            archived_law_evidence: list[Evidence] = []
            archive_item_errors = list(temporal_item_errors)
            if "law_support" in query_intent and law_coverage_available:
                try:
                    matches = backend.search_law_articles(
                        _law_query(request, item, resolution.resolved_hsk10),
                        request.top_k,
                        request.review_as_of,
                    )
                    archived_law_evidence = [
                        _law_match_to_evidence(item.item_id, request.trade_type, match)
                        for match in matches
                    ]
                except Exception:
                    archive_item_errors.append(_backend_error("LAW_RETRIEVAL_FAILED"))
            results.append(
                ItemEvidenceResult(
                    item_id=item.item_id,
                    hs_resolution=resolution.status,
                    resolved_hsk10=resolution.resolved_hsk10,
                    candidate_hsk10s=list(resolution.candidate_hsk10s),
                    coverage="partial",
                    requirements_found=False,
                    evidence=archived_law_evidence,
                    errors=archive_item_errors,
                )
            )
            continue

        try:
            assert snapshot_id is not None
            lookup = backend.lookup_requirements(
                snapshot_id, resolution.resolved_hsk10, request.trade_type, requirements_as_of
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
                    errors=[*temporal_item_errors, err],
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
                    errors=[*temporal_item_errors, err],
                )
            )
            continue

        requirement_evidence = [_row_to_evidence(item.item_id, row) for row in lookup.rows]
        law_evidence: list[Evidence] = []
        item_errors: list[EvidenceError] = list(temporal_item_errors)
        item_coverage: Literal["complete", "partial"] = (
            "partial" if temporal_item_errors else "complete"
        )
        if destination_error is not None:
            # A foreign destination is valid for Korean export processing, but
            # no Korean-only local dataset can certify the destination's own
            # import restrictions.  Preserve Korean evidence and make the
            # boundary explicit instead of silently treating it as complete.
            partial = True
            item_coverage = "partial"
            item_errors.append(destination_error)
        if "law_support" in query_intent and not law_coverage_available:
            # Do not query a current vector collection and then pretend it is
            # a historical answer.  The explicit coverage error above remains
            # the only public explanation.
            item_coverage = "partial"
        elif "law_support" in query_intent:
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

    top_errors = [*temporal_top_errors]
    if partial and not top_errors:
        top_errors = [
            _error(
                "PARTIAL_RESULTS",
                "일부 품목의 지식 조회를 완료하지 못함",
                retryable=any(error.retryable for result in results for error in result.errors),
            )
        ]
    return RetrieveResponse(
        schema_version=SCHEMA_VERSION,
        request_id=request.request_id,
        status="partial" if partial else "ok",
        knowledge_snapshot_id=snapshot_id,
        temporal_context=temporal_context,
        temporal_coverage=temporal_coverage,
        results=results,
        errors=top_errors,
    )
