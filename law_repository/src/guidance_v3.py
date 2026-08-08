"""Bounded general-customs-guidance HTTP contract.

This is intentionally separate from the commodity/HS requirement retrieval
contract.  A caller can select only a versioned registry topic; the user's raw
question never becomes an arbitrary Data-store search query.
"""

from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.evidence import EvidenceBackend, EvidenceError, KnowledgeSnapshotMismatch, LawArticleMatch
from src.guidance_registry import (
    GuidanceRegistryEntry,
    GuidanceTopic,
    REGISTRY_SHA256,
    REGISTRY_SOURCES_BY_ID,
    REGISTRY_VERSION,
    TradeDirection,
    active_sources_for_topic,
    guidance_topic_by_hint,
    legacy_3law_source_ids,
    resolve_guidance_topic,
    source_for_law_id,
)


NonEmpty128 = Annotated[str, Field(min_length=1, max_length=128)]
GuidanceQuery = Annotated[str, Field(min_length=1, max_length=1200)]
GuidanceUri = Annotated[str, Field(min_length=1, max_length=2000)]
VersionHash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GuidanceSourceId = Annotated[str, Field(pattern=r"^guide_[a-f0-9]{64}$")]
RegistrySourceId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GeneralGuidanceQueryRequestV3(ContractModel):
    schema_version: Literal["3.0"]
    request_id: NonEmpty128
    query: GuidanceQuery
    trade_direction: TradeDirection
    as_of: date
    # The Agent may select a known registry topic after its own bounded
    # classifier has considered context.  A hint never accepts a free-form
    # topic and prevents this Data layer from reclassifying the raw wording.
    topic_hint: GuidanceTopic | None = None
    top_k: int = Field(default=3, ge=1, le=3)


class GuidanceSourceV3(ContractModel):
    source_id: GuidanceSourceId
    # ``source_id`` identifies this concrete result record.  This separate,
    # stable field binds it to the versioned registry family that owns the
    # official URI and topic scope.  It remains useful when a snapshot derives
    # several records from one source family.
    registry_source_id: RegistrySourceId
    source_kind: Literal[
        "official_law",
        "official_law_catalog",
        "official_agency_guidance",
        "official_requirement_reference",
        "official_public_guidance",
        "development_fixture",
    ]
    title: str = Field(min_length=1, max_length=1000)
    uri: GuidanceUri
    topic: GuidanceTopic
    direction: TradeDirection
    excerpt: str = Field(min_length=1, max_length=2000)
    law_id: str | None = Field(default=None, min_length=1, max_length=64)
    law_name: str | None = Field(default=None, min_length=1, max_length=500)
    article_no: str | None = Field(default=None, min_length=1, max_length=128)
    valid_from: date | None = None
    valid_to: date | None = None
    version_hash: VersionHash
    retrieval_methods: list[Literal["registry", "dense", "bm25", "graph"]] = Field(
        min_length=1, max_length=4
    )
    score: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _unique_retrieval_methods(self) -> "GuidanceSourceV3":
        if len(self.retrieval_methods) != len(set(self.retrieval_methods)):
            raise ValueError("retrieval_methods must be unique")
        registry_source = REGISTRY_SOURCES_BY_ID.get(self.registry_source_id)
        if registry_source is None:
            raise ValueError("guidance source has an unknown registry family")
        if not registry_source.supports(topic=self.topic, direction=self.direction):
            raise ValueError("guidance source is outside its registry scope")
        # The deterministic development fixture deliberately does not claim
        # that a registry source was collected.  Its registry family is only
        # the stable topic/direction scope used to construct the fixture.
        if self.source_kind != "development_fixture" and (
            self.source_kind != registry_source.source_kind
        ):
            raise ValueError("guidance source kind does not match its registry family")
        if self.source_kind == "official_law" and (
            self.law_id is None or self.law_name is None or self.article_no is None
        ):
            raise ValueError("official guidance source requires law provenance")
        if self.source_kind == "official_law" and registry_source.law_id != self.law_id:
            raise ValueError("official law guidance must match its registry law")
        if self.source_kind != "official_law" and any(
            value is not None for value in (self.law_id, self.law_name, self.article_no)
        ):
            raise ValueError("non-law guidance source must not claim law provenance")
        return self


class GeneralGuidanceQueryResponseV3(ContractModel):
    schema_version: Literal["3.0"] = "3.0"
    request_id: NonEmpty128
    status: Literal["covered", "no_coverage", "partial", "failed"]
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=512)
    as_of: date
    coverage: Literal["covered", "no_coverage", "partial", "unavailable"]
    registry_version: Literal["1.0"] = "1.0"
    # The matching provider-owned manifest is checked into the Evidence V3
    # contract bundle.  Consumers can reject a response from another registry
    # revision before turning its URI into a user-facing official citation.
    registry_sha256: VersionHash = REGISTRY_SHA256
    topic: GuidanceTopic | None = None
    results: list[GuidanceSourceV3] = Field(default_factory=list, max_length=3)
    errors: list[EvidenceError] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def _status_matches_content(self) -> "GeneralGuidanceQueryResponseV3":
        if self.registry_sha256 != REGISTRY_SHA256:
            raise ValueError("guidance response registry hash does not match this provider")
        if self.status == "covered":
            if self.coverage != "covered" or not self.results or self.errors:
                raise ValueError("covered guidance response must contain only sources")
        elif self.status == "no_coverage":
            if self.coverage != "no_coverage" or self.results or self.errors:
                raise ValueError("no-coverage guidance response must be empty")
        elif self.status == "partial":
            if self.coverage != "partial" or not self.results or not self.errors:
                raise ValueError("partial guidance response requires sources and errors")
        elif self.status == "failed":
            if self.coverage != "unavailable" or self.results or not self.errors:
                raise ValueError("failed guidance response requires a safe error")
        if self.topic is None and self.results:
            raise ValueError("guidance sources require a topic")
        if self.topic is not None and any(source.topic != self.topic for source in self.results):
            raise ValueError("guidance source topics must match the response topic")
        return self


def _error(code: str, message: str, *, retryable: bool = False) -> EvidenceError:
    return EvidenceError(code=code, message=message, retryable=retryable)


def _source_id(*parts: str) -> str:
    return "guide_" + sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _snapshot_failure(
    request: GeneralGuidanceQueryRequestV3,
    *,
    code: str,
    message: str,
    snapshot_id: str | None = None,
    retryable: bool = False,
) -> GeneralGuidanceQueryResponseV3:
    return GeneralGuidanceQueryResponseV3(
        request_id=request.request_id,
        status="failed",
        snapshot_id=snapshot_id,
        as_of=request.as_of,
        coverage="unavailable",
        topic=None,
        errors=[_error(code, message, retryable=retryable)],
    )


def _validated_snapshot(
    request: GeneralGuidanceQueryRequestV3, backend: EvidenceBackend
) -> tuple[str, GeneralGuidanceQueryResponseV3 | None]:
    try:
        snapshot_id = backend.current_snapshot_id()
    except Exception as exc:
        code = (
            "KNOWLEDGE_SNAPSHOT_MISMATCH"
            if isinstance(exc, KnowledgeSnapshotMismatch)
            else "KNOWLEDGE_STORE_UNAVAILABLE"
        )
        return "", _snapshot_failure(
            request,
            code=code,
            message="법령 근거 저장소를 현재 사용할 수 없습니다.",
            retryable=code == "KNOWLEDGE_STORE_UNAVAILABLE",
        )
    if not snapshot_id:
        return "", _snapshot_failure(
            request,
            code="KNOWLEDGE_SNAPSHOT_MISSING",
            message="현재 법령 근거 스냅샷이 없습니다.",
        )
    try:
        supported_as_of = backend.snapshot_effective_date(snapshot_id)
    except Exception:
        return "", _snapshot_failure(
            request,
            code="KNOWLEDGE_SNAPSHOT_METADATA_UNAVAILABLE",
            message="법령 근거 스냅샷 기준일을 확인할 수 없습니다.",
            snapshot_id=snapshot_id,
            retryable=True,
        )
    if request.as_of != supported_as_of:
        return "", _snapshot_failure(
            request,
            code="UNSUPPORTED_GUIDANCE_AS_OF",
            message="현재 법령 근거 스냅샷은 요청한 기준일을 지원하지 않습니다.",
            snapshot_id=snapshot_id,
        )
    return snapshot_id, None


def _fixture_source(entry: GuidanceRegistryEntry, *, direction: TradeDirection) -> GuidanceSourceV3:
    version_hash = REGISTRY_SHA256
    registry_source = next(
        (
            REGISTRY_SOURCES_BY_ID[source_id]
            for source_id in entry.source_ids
            if REGISTRY_SOURCES_BY_ID[source_id].supports(topic=entry.topic, direction=direction)
        ),
        None,
    )
    if registry_source is None:  # pragma: no cover - registry loader prevents this
        raise ValueError("development fixture has no registry source scope")
    return GuidanceSourceV3(
        source_id=_source_id(
            "development_fixture", REGISTRY_VERSION, entry.topic, direction, version_hash
        ),
        registry_source_id=registry_source.source_id,
        source_kind="development_fixture",
        title=entry.title,
        uri=f"https://customs.local.invalid/dev-fixture/guidance/{entry.topic}",
        topic=entry.topic,
        direction=direction,
        excerpt=entry.development_excerpt,
        version_hash=version_hash,
        retrieval_methods=["registry"],
    )


def _article_label(row: dict[str, object]) -> str:
    raw_article_no = row["article_no"]
    if not isinstance(raw_article_no, int | str):
        raise ValueError("law article number is invalid")
    encoded = int(raw_article_no)
    label = f"제{encoded // 100}조"
    if encoded % 100:
        label += f"의{encoded % 100}"
    if row.get("paragraph_no") is not None:
        label += f" 제{row['paragraph_no']}항"
    return label


def _excerpt(value: object) -> str:
    text = value if isinstance(value, str) else ""
    text = " ".join(text.split())
    if not text:
        raise ValueError("law article has no text")
    return text if len(text) <= 2000 else text[:1999].rstrip() + "…"


def _official_source(
    entry: GuidanceRegistryEntry,
    *,
    direction: TradeDirection,
    active_source_ids: frozenset[str],
    match: LawArticleMatch,
) -> GuidanceSourceV3:
    row = match.row
    law_id = str(row["law_id"])
    registry_source = source_for_law_id(law_id)
    if (
        registry_source is None
        or registry_source.source_id not in active_source_ids
        or registry_source.source_id not in entry.source_ids
        or not registry_source.supports(topic=entry.topic, direction=direction)
    ):
        raise LookupError("law is outside registry topic scope")
    law_name = str(row["law_name"])
    article_label = _article_label(row)
    content_hash = str(row["content_hash"])
    if len(content_hash) != 64 or any(
        character not in "0123456789abcdef" for character in content_hash
    ):
        raise ValueError("law article version hash is invalid")
    valid_from = row.get("valid_from")
    valid_to = row.get("valid_to")
    if valid_from is not None and not isinstance(valid_from, date):
        raise ValueError("law article valid_from is invalid")
    if valid_to is not None and not isinstance(valid_to, date):
        raise ValueError("law article valid_to is invalid")
    methods = ["registry", *match.retrieval_methods]
    return GuidanceSourceV3(
        source_id=_source_id(
            "official_law", entry.topic, direction, law_id, article_label, content_hash
        ),
        registry_source_id=registry_source.source_id,
        source_kind="official_law",
        title=f"{law_name} {article_label}" + (f" ({row['title']})" if row.get("title") else ""),
        uri=f"https://www.law.go.kr/%EB%B2%95%EB%A0%B9/{quote(law_name)}/{quote(article_label)}",
        topic=entry.topic,
        direction=direction,
        excerpt=_excerpt(row.get("content")),
        law_id=law_id,
        law_name=law_name,
        article_no=article_label,
        valid_from=valid_from,
        valid_to=valid_to - timedelta(days=1) if valid_to else None,
        version_hash=content_hash,
        retrieval_methods=methods,  # type: ignore[arg-type]
        score=round(float(match.score), 6),
    )


def query_general_guidance_v3(
    request: GeneralGuidanceQueryRequestV3,
    backend: EvidenceBackend,
    *,
    profile_mode: Literal["local-development", "functional-uat"],
    active_source_ids: frozenset[str] | None = None,
) -> GeneralGuidanceQueryResponseV3:
    """Return at most one bounded registry search; never make an arbitrary search call."""

    snapshot_id, failure = _validated_snapshot(request, backend)
    if failure is not None:
        return failure
    entry = (
        guidance_topic_by_hint(request.topic_hint, direction=request.trade_direction)
        if request.topic_hint is not None
        else resolve_guidance_topic(request.query, direction=request.trade_direction)
    )
    if entry is None:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="no_coverage",
            snapshot_id=snapshot_id,
            as_of=request.as_of,
            coverage="no_coverage",
            topic=None,
        )
    if profile_mode == "local-development":
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="covered",
            snapshot_id=snapshot_id,
            as_of=request.as_of,
            coverage="covered",
            topic=entry.topic,
            results=[_fixture_source(entry, direction=request.trade_direction)],
        )
    # A functional profile can only cite source families that its own
    # validated local snapshot declares as active.  Until the separate
    # functional-uat-kr-trade builder exists, the legacy three-law profile is
    # deliberately the conservative default rather than a claim of broad
    # customs coverage.
    selected_source_ids = (
        legacy_3law_source_ids() if active_source_ids is None else active_source_ids
    )
    if not selected_source_ids <= set(REGISTRY_SOURCES_BY_ID):
        return _snapshot_failure(
            request,
            code="GUIDANCE_SNAPSHOT_SCOPE_INVALID",
            message="법령 근거 안내 스냅샷의 출처 범위를 확인할 수 없습니다.",
            snapshot_id=snapshot_id,
        )
    allowed_source_ids = active_sources_for_topic(
        entry,
        direction=request.trade_direction,
        active_source_ids=selected_source_ids,
    )
    if not allowed_source_ids:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="no_coverage",
            snapshot_id=snapshot_id,
            as_of=request.as_of,
            coverage="no_coverage",
            topic=entry.topic,
        )
    try:
        matches = backend.search_law_articles(entry.search_query, request.top_k, request.as_of)
        if not isinstance(matches, tuple):
            raise TypeError("guidance search returned an invalid collection")
    except Exception:
        return _snapshot_failure(
            request,
            code="GUIDANCE_SEARCH_FAILED",
            message="법령 근거 안내 조회를 완료하지 못했습니다.",
            snapshot_id=snapshot_id,
            retryable=True,
        )
    sources: list[GuidanceSourceV3] = []
    errors: list[EvidenceError] = []
    # The backend is asked for at most three records. Keep a defensive local
    # cap as well so an invalid backend cannot turn this API into a long scan.
    for match in matches[: request.top_k * 4]:
        if len(sources) >= request.top_k:
            break
        try:
            source = _official_source(
                entry,
                direction=request.trade_direction,
                active_source_ids=allowed_source_ids,
                match=match,
            )
        except LookupError:
            # A broad RAG hit outside the source-owned topic scope is not a
            # partial answer and must never be exposed as general guidance.
            continue
        except Exception:
            errors.append(
                _error(
                    "GUIDANCE_SOURCE_INVALID",
                    "법령 근거 안내 원문을 안전하게 구성하지 못했습니다.",
                    retryable=False,
                )
            )
            continue
        if source.source_id not in {item.source_id for item in sources}:
            sources.append(source)
    if sources and errors:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="partial",
            snapshot_id=snapshot_id,
            as_of=request.as_of,
            coverage="partial",
            topic=entry.topic,
            results=sources,
            errors=errors[:4],
        )
    if sources:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="covered",
            snapshot_id=snapshot_id,
            as_of=request.as_of,
            coverage="covered",
            topic=entry.topic,
            results=sources,
        )
    if errors:
        return _snapshot_failure(
            request,
            code="GUIDANCE_SOURCE_INVALID",
            message="법령 근거 안내 원문을 안전하게 구성하지 못했습니다.",
            snapshot_id=snapshot_id,
        )
    return GeneralGuidanceQueryResponseV3(
        request_id=request.request_id,
        status="no_coverage",
        snapshot_id=snapshot_id,
        as_of=request.as_of,
        coverage="no_coverage",
        topic=entry.topic,
    )


__all__ = [
    "GeneralGuidanceQueryRequestV3",
    "GeneralGuidanceQueryResponseV3",
    "GuidanceSourceV3",
    "query_general_guidance_v3",
]
