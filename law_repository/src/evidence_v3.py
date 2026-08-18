"""The small Evidence V3 HTTP contract used by the two local profiles.

V3 preserves the Data retrieval body while giving the loopback HTTP boundary a
separate schema version. It has no policy approval, signature, release, or
automatic-completion meaning; Agent always sends its result to human review.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.evidence import (
    EvidenceBackend,
    EvidenceError,
    ItemEvidenceResult,
    RetrieveItem,
    RetrieveRequest,
    TemporalCoverage,
    TemporalContext,
    retrieve_evidence,
)

NonEmpty128 = Annotated[str, Field(min_length=1, max_length=128)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrieveRequestV3(ContractModel):
    schema_version: Literal["3.0"]
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
    # Keep the historical V3 default byte-for-byte compatible.  Data derives
    # the effective import/export document intent from ``trade_type``.
    query_intent: str = Field(default="required_import_documents", min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_values(self) -> "RetrieveRequestV3":
        item_ids = [item.item_id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("items.item_id must be unique")
        for name in ("uploaded_requirement_document_codes", "validation_issue_codes"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")
        return self

    def as_v1(self) -> RetrieveRequest:
        payload = self.model_dump(mode="json")
        payload["schema_version"] = "1.0"
        return RetrieveRequest.model_validate(payload)


class RetrieveResponseV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    status: Literal["ok", "partial", "failed"]
    knowledge_snapshot_id: str | None
    temporal_context: TemporalContext | None = None
    temporal_coverage: TemporalCoverage | None = None
    results: list[ItemEvidenceResult] = Field(max_length=100)
    errors: list[EvidenceError] = Field(default_factory=list)


def retrieve_evidence_v3(
    request: RetrieveRequestV3,
    backend: EvidenceBackend,
) -> RetrieveResponseV3:
    """Run Data retrieval without any approval or release-policy projection."""

    response = retrieve_evidence(request.as_v1(), backend)
    payload = response.model_dump(mode="json")
    payload["schema_version"] = "3.0"
    return RetrieveResponseV3.model_validate(payload)


class LawSearchRequestV3(ContractModel):
    """One free-text search over the loaded statute store.

    This is a read-only reference lookup for a human reviewer.  It carries no
    case, item, or HS meaning and never contributes to a review result.
    """

    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(ge=1, le=20)
    as_of: date


class LawSearchMatchV3(ContractModel):
    law_id: str | None = Field(default=None, max_length=128)
    law_name: str = Field(min_length=1, max_length=400)
    article_no: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=1000)
    text: str = Field(min_length=1)
    source_uri: str = Field(min_length=1, max_length=2000)
    valid_from: date | None
    valid_to: date | None
    score: float = Field(ge=0.0, le=1.0)


class LawSearchResponseV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    status: Literal["ok"]
    as_of: date
    results: list[LawSearchMatchV3] = Field(max_length=20)


class LawCatalogRequestV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    as_of: date


class LawCatalogEntryV3(ContractModel):
    law_id: NonEmpty128
    law_name: str = Field(min_length=1, max_length=400)
    hierarchy: str | None = Field(default=None, max_length=200)
    article_count: int = Field(ge=0)


class LawCatalogResponseV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    status: Literal["ok"]
    as_of: date
    laws: list[LawCatalogEntryV3] = Field(max_length=500)


def search_laws_v3(request: LawSearchRequestV3, backend: EvidenceBackend) -> LawSearchResponseV3:
    """Free-text statute search reusing the retrieval branch's own conversion."""

    from src.evidence import _law_match_to_evidence

    matches = backend.search_law_articles(request.query, request.top_k, request.as_of)
    results: list[LawSearchMatchV3] = []
    for match in matches[: request.top_k]:
        # The converter owns article labelling, official-URI verification, and
        # the closed-interval valid_to convention.  The item/trade fields it
        # requires have no meaning for a reference search and are not copied.
        evidence = _law_match_to_evidence("law-search", "import", match)
        results.append(
            LawSearchMatchV3(
                law_id=evidence.law_id,
                law_name=evidence.law_name or evidence.title,
                article_no=evidence.article_no,
                title=evidence.title,
                text=evidence.text,
                source_uri=str(evidence.source_uri),
                valid_from=evidence.valid_from,
                valid_to=evidence.valid_to,
                score=float(evidence.score or 0.0),
            )
        )
    return LawSearchResponseV3(
        schema_version="3.0",
        request_id=request.request_id,
        status="ok",
        as_of=request.as_of,
        results=results,
    )


def law_catalog_v3(request: LawCatalogRequestV3, backend: EvidenceBackend) -> LawCatalogResponseV3:
    """List the statutes the loaded store can search, with live article counts."""

    lister = getattr(backend, "list_laws", None)
    if not callable(lister):
        raise RuntimeError("law catalog is unavailable on this Evidence backend")
    laws = [
        LawCatalogEntryV3(
            law_id=str(row["law_id"]),
            law_name=str(row["law_name"]),
            hierarchy=(str(row["hierarchy"]) if row.get("hierarchy") else None),
            article_count=int(row["article_count"]),
        )
        for row in lister(request.as_of)
    ]
    return LawCatalogResponseV3(
        schema_version="3.0",
        request_id=request.request_id,
        status="ok",
        as_of=request.as_of,
        laws=laws,
    )


__all__ = [
    "LawCatalogEntryV3",
    "LawCatalogRequestV3",
    "LawCatalogResponseV3",
    "LawSearchMatchV3",
    "LawSearchRequestV3",
    "LawSearchResponseV3",
    "RetrieveRequestV3",
    "RetrieveResponseV3",
    "law_catalog_v3",
    "retrieve_evidence_v3",
    "search_laws_v3",
]
