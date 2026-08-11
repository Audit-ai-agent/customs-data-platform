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


__all__ = ["RetrieveRequestV3", "RetrieveResponseV3", "retrieve_evidence_v3"]
