"""Versioned, bounded source registry for Korean trade guidance.

The registry is deliberately *not* a copy of official material.  It declares
which official source families a local snapshot may use, their permitted topic
and direction, and whether they are loaded by the legacy three-law profile or
require the separate ``functional-uat-kr-trade`` source build.  A source listed
as ``requires_kr_trade_snapshot`` is never evidence that the material has been
collected, indexed, or is available to a caller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse


GuidanceTopic = Literal[
    "customs_clearance",
    "customs_declaration",
    "customs_duty",
    "hs_classification",
    "origin_verification",
    "trade_documents",
    "import_requirements",
    "export_requirements",
    "transport_documents",
    "fta_procedure",
    "trade_terms",
]
TradeDirection = Literal["import", "export", "both"]
GuidanceSourceKind = Literal[
    "official_law",
    "official_law_catalog",
    "official_agency_guidance",
    "official_requirement_reference",
    "official_public_guidance",
]
GuidanceSourceAvailability = Literal[
    "available_in_legacy_3law",
    "requires_kr_trade_snapshot",
]

_REGISTRY_PATH = Path(__file__).with_name("guidance_registry_v1.json")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_TOPICS = {
    "customs_clearance",
    "customs_declaration",
    "customs_duty",
    "hs_classification",
    "origin_verification",
    "trade_documents",
    "import_requirements",
    "export_requirements",
    "transport_documents",
    "fta_procedure",
    "trade_terms",
}
_SOURCE_KINDS = {
    "official_law",
    "official_law_catalog",
    "official_agency_guidance",
    "official_requirement_reference",
    "official_public_guidance",
}
_AVAILABILITY = {"available_in_legacy_3law", "requires_kr_trade_snapshot"}


@dataclass(frozen=True)
class GuidanceRegistrySource:
    """One declared official source family, not a collected source receipt."""

    source_id: str
    source_kind: GuidanceSourceKind
    title: str
    official_uri: str
    authority: str
    jurisdiction: Literal["KR"]
    directions: tuple[Literal["import", "export"], ...]
    topics: tuple[GuidanceTopic, ...]
    availability: GuidanceSourceAvailability
    law_id: str | None

    def supports(self, *, topic: GuidanceTopic, direction: TradeDirection) -> bool:
        if topic not in self.topics:
            return False
        # A cross-direction question is allowed to cite the bounded union of
        # its separately declared import and export sources.  Consumers still
        # receive each source's own direction rather than a fabricated
        # ``both`` provenance claim.
        return direction == "both" or direction in self.directions


@dataclass(frozen=True)
class GuidanceRegistryEntry:
    topic: GuidanceTopic
    title: str
    directions: tuple[Literal["import", "export"], ...]
    keywords: tuple[str, ...]
    search_query: str
    source_ids: tuple[str, ...]
    development_excerpt: str

    def supports(self, direction: TradeDirection) -> bool:
        return direction == "both" or direction in self.directions


def _is_https_uri(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2_000:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username


def _load_registry() -> tuple[
    str,
    str,
    tuple[GuidanceRegistrySource, ...],
    tuple[GuidanceRegistryEntry, ...],
    str,
]:
    raw = _REGISTRY_PATH.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - source defect
        raise RuntimeError("guidance registry is invalid") from exc
    if not isinstance(payload, dict):  # pragma: no cover - source defect
        raise RuntimeError("guidance registry is invalid")
    version = payload.get("registry_version")
    jurisdiction = payload.get("jurisdiction")
    sources = payload.get("sources")
    topics = payload.get("topics")
    if (
        version != "1.0"
        or jurisdiction != "KR"
        or not isinstance(sources, list)
        or not sources
        or not isinstance(topics, list)
        or not topics
    ):  # pragma: no cover - source defect
        raise RuntimeError("guidance registry is invalid")

    source_entries: list[GuidanceRegistrySource] = []
    law_ids: set[str] = set()
    for item in sources:
        if not isinstance(item, dict):  # pragma: no cover - source defect
            raise RuntimeError("guidance registry is invalid")
        source_id = item.get("source_id")
        source_kind = item.get("source_kind")
        title = item.get("title")
        official_uri = item.get("official_uri")
        authority = item.get("authority")
        source_jurisdiction = item.get("jurisdiction")
        directions = item.get("directions")
        source_topics = item.get("topics")
        availability = item.get("availability")
        law_id = item.get("law_id")
        if (
            not isinstance(source_id, str)
            or _SOURCE_ID_PATTERN.fullmatch(source_id) is None
            or not isinstance(source_kind, str)
            or source_kind not in _SOURCE_KINDS
            or not isinstance(title, str)
            or not title
            or not _is_https_uri(official_uri)
            or not isinstance(authority, str)
            or not authority
            or source_jurisdiction != "KR"
            or not isinstance(directions, list)
            or not directions
            or any(direction not in {"import", "export"} for direction in directions)
            or len(directions) != len(set(directions))
            or not isinstance(source_topics, list)
            or not source_topics
            or any(topic not in _TOPICS for topic in source_topics)
            or len(source_topics) != len(set(source_topics))
            or not isinstance(availability, str)
            or availability not in _AVAILABILITY
            or law_id is not None
            and (not isinstance(law_id, str) or not law_id)
            or source_kind == "official_law"
            and not isinstance(law_id, str)
            or source_kind != "official_law"
            and law_id is not None
        ):  # pragma: no cover - source defect
            raise RuntimeError("guidance registry is invalid")
        if law_id is not None:
            if law_id in law_ids:  # pragma: no cover - source defect
                raise RuntimeError("guidance registry has duplicate law IDs")
            law_ids.add(law_id)
        source_entries.append(
            GuidanceRegistrySource(
                source_id=source_id,
                source_kind=source_kind,  # type: ignore[arg-type]
                title=title,
                official_uri=cast(str, official_uri),
                authority=authority,
                jurisdiction="KR",
                directions=tuple(directions),  # type: ignore[arg-type]
                topics=tuple(source_topics),  # type: ignore[arg-type]
                availability=availability,  # type: ignore[arg-type]
                law_id=law_id,
            )
        )
    if len(source_entries) != len(
        {entry.source_id for entry in source_entries}
    ):  # pragma: no cover
        raise RuntimeError("guidance registry has duplicate source IDs")
    sources_by_id = {entry.source_id: entry for entry in source_entries}

    entries: list[GuidanceRegistryEntry] = []
    for item in topics:
        if not isinstance(item, dict):  # pragma: no cover - source defect
            raise RuntimeError("guidance registry is invalid")
        topic = item.get("topic")
        title = item.get("title")
        directions = item.get("directions")
        keywords = item.get("keywords")
        search_query = item.get("search_query")
        source_ids = item.get("source_ids")
        development_excerpt = item.get("development_excerpt")
        if (
            not isinstance(topic, str)
            or topic not in _TOPICS
            or not isinstance(title, str)
            or not title
            or not isinstance(directions, list)
            or not directions
            or any(direction not in {"import", "export"} for direction in directions)
            or len(directions) != len(set(directions))
            or not isinstance(keywords, list)
            or not keywords
            or any(not isinstance(value, str) or not value for value in keywords)
            or not isinstance(search_query, str)
            or not search_query
            or not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(value, str) or value not in sources_by_id for value in source_ids)
            or len(source_ids) != len(set(source_ids))
            or not isinstance(development_excerpt, str)
            or not development_excerpt
        ):  # pragma: no cover - source defect
            raise RuntimeError("guidance registry is invalid")
        direction_set = set(directions)
        if not any(
            source.supports(topic=topic, direction=direction)  # type: ignore[arg-type]
            for source_id in source_ids
            for source in (sources_by_id[source_id],)
            for direction in direction_set
        ):  # pragma: no cover - source defect
            raise RuntimeError("guidance registry topic has no compatible source")
        entries.append(
            GuidanceRegistryEntry(
                topic=topic,  # type: ignore[arg-type]
                title=title,
                directions=tuple(directions),  # type: ignore[arg-type]
                keywords=tuple(keywords),
                search_query=search_query,
                source_ids=tuple(source_ids),
                development_excerpt=development_excerpt,
            )
        )
    if len(entries) != len({entry.topic for entry in entries}):  # pragma: no cover - source defect
        raise RuntimeError("guidance registry has duplicate topics")
    return (
        version,
        jurisdiction,
        tuple(source_entries),
        tuple(entries),
        sha256(raw).hexdigest(),
    )


(
    REGISTRY_VERSION,
    REGISTRY_JURISDICTION,
    REGISTRY_SOURCES,
    REGISTRY_ENTRIES,
    REGISTRY_SHA256,
) = _load_registry()
REGISTRY_SOURCES_BY_ID = {entry.source_id: entry for entry in REGISTRY_SOURCES}
REGISTRY_SOURCES_BY_LAW_ID = {
    entry.law_id: entry for entry in REGISTRY_SOURCES if entry.law_id is not None
}


def resolve_guidance_topic(
    query: str, *, direction: TradeDirection
) -> GuidanceRegistryEntry | None:
    """Return one deterministic topic; a raw question is never a store query."""

    normalized = "".join(query.casefold().split())
    candidates: list[tuple[int, int, GuidanceRegistryEntry]] = []
    for index, entry in enumerate(REGISTRY_ENTRIES):
        if not entry.supports(direction):
            continue
        score = sum(
            1 for keyword in entry.keywords if "".join(keyword.casefold().split()) in normalized
        )
        if score:
            candidates.append((score, -index, entry))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def guidance_topic_by_hint(
    topic: GuidanceTopic, *, direction: TradeDirection
) -> GuidanceRegistryEntry | None:
    """Resolve a caller-selected registry topic without reclassifying text."""

    for entry in REGISTRY_ENTRIES:
        if entry.topic == topic and entry.supports(direction):
            return entry
    return None


def legacy_3law_source_ids() -> frozenset[str]:
    """The only registry sources that legacy functional UAT may claim to load."""

    return frozenset(
        source.source_id
        for source in REGISTRY_SOURCES
        if source.availability == "available_in_legacy_3law"
    )


def kr_trade_source_ids() -> frozenset[str]:
    """Declared sources that need an explicit future KR-trade snapshot build."""

    return frozenset(
        source.source_id
        for source in REGISTRY_SOURCES
        if source.availability == "requires_kr_trade_snapshot"
    )


def source_for_law_id(law_id: str) -> GuidanceRegistrySource | None:
    return REGISTRY_SOURCES_BY_LAW_ID.get(law_id)


def active_sources_for_topic(
    entry: GuidanceRegistryEntry,
    *,
    direction: TradeDirection,
    active_source_ids: frozenset[str],
) -> frozenset[str]:
    """Return source IDs actually available for one local snapshot scope."""

    return frozenset(
        source_id
        for source_id in entry.source_ids
        if source_id in active_source_ids
        and REGISTRY_SOURCES_BY_ID[source_id].supports(topic=entry.topic, direction=direction)
    )


def registry_manifest() -> dict[str, object]:
    """Return the safe provider-owned registry projection for consumers.

    ``availability`` is a declaration of local build state, not a statement
    that a source has been fetched.  The active runtime snapshot is the only
    place that may claim a collected source hash.
    """

    return {
        "schema_version": REGISTRY_VERSION,
        "jurisdiction": REGISTRY_JURISDICTION,
        "registry_sha256": REGISTRY_SHA256,
        "sources": [
            {
                "source_id": source.source_id,
                "source_kind": source.source_kind,
                "title": source.title,
                "official_uri": source.official_uri,
                "authority": source.authority,
                "jurisdiction": source.jurisdiction,
                "directions": list(source.directions),
                "topics": list(source.topics),
                "availability": source.availability,
                **({"law_id": source.law_id} if source.law_id is not None else {}),
            }
            for source in REGISTRY_SOURCES
        ],
        "topics": [
            {
                "topic": entry.topic,
                "title": entry.title,
                "directions": list(entry.directions),
                "source_ids": list(entry.source_ids),
            }
            for entry in REGISTRY_ENTRIES
        ],
        "coverage_profiles": {
            "functional-uat-3law": {
                "status": "limited",
                "source_ids": sorted(legacy_3law_source_ids()),
            },
            "functional-uat-kr-trade": {
                "status": "source_build_required",
                "source_ids": sorted(kr_trade_source_ids()),
            },
        },
    }


__all__ = [
    "GuidanceRegistryEntry",
    "GuidanceRegistrySource",
    "GuidanceSourceAvailability",
    "GuidanceSourceKind",
    "GuidanceTopic",
    "REGISTRY_ENTRIES",
    "REGISTRY_JURISDICTION",
    "REGISTRY_SHA256",
    "REGISTRY_SOURCES",
    "REGISTRY_SOURCES_BY_ID",
    "REGISTRY_VERSION",
    "TradeDirection",
    "active_sources_for_topic",
    "guidance_topic_by_hint",
    "kr_trade_source_ids",
    "legacy_3law_source_ids",
    "registry_manifest",
    "resolve_guidance_topic",
    "source_for_law_id",
]
