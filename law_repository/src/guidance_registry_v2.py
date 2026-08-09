"""Immutable page-level official-source registry for Korean trade guidance V2.

V1 remains the historical registry for the legacy three-law profile.  V2 is a
separate, page-level allowlist for ``functional-uat-kr-trade`` source builds:
the registry declares possible official pages and their bounded topic/scope/
direction use, but it never asserts that a page was collected or indexed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlparse

from src.guidance_registry import (
    GuidanceSourceAvailability,
    GuidanceSourceKind,
    GuidanceTopic,
    TradeDirection,
)


GuidanceScope = Literal[
    "import_clearance_flow",
    "export_clearance_flow",
    "import_declaration",
    "export_declaration",
    "customs_value_basics",
    "duty_payment_basics",
    "classification_principles",
    "advance_ruling_or_confirmation",
    "origin_evidence",
    "origin_verification",
    "commercial_and_declaration_documents",
    "agency_confirmation_and_supporting_documents",
    "bill_of_lading",
    "air_waybill",
    "origin_certificate",
    "preference_procedure",
    "cost_risk_delivery_terms",
]

_REGISTRY_PATH = Path(__file__).with_name("guidance_registry_v2.json")
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
_SCOPES = {
    "import_clearance_flow",
    "export_clearance_flow",
    "import_declaration",
    "export_declaration",
    "customs_value_basics",
    "duty_payment_basics",
    "classification_principles",
    "advance_ruling_or_confirmation",
    "origin_evidence",
    "origin_verification",
    "commercial_and_declaration_documents",
    "agency_confirmation_and_supporting_documents",
    "bill_of_lading",
    "air_waybill",
    "origin_certificate",
    "preference_procedure",
    "cost_risk_delivery_terms",
}
_SOURCE_KINDS = {
    "official_law",
    "official_law_catalog",
    "official_agency_guidance",
    "official_requirement_reference",
    "official_public_guidance",
}
_AVAILABILITY = {"requires_kr_trade_snapshot"}
_OFFICIAL_HOSTS_BY_AUTHORITY = {
    "국가법령정보센터": {"www.law.go.kr", "law.go.kr"},
    "관세청": {"www.customs.go.kr", "www2.customs.go.kr", "customs.go.kr"},
    "관세청 및 관계기관": {"www.customs.go.kr", "customs.go.kr"},
    "관세청 관세평가분류원": {"www.customs.go.kr", "customs.go.kr"},
    "관세청 FTA 포털": {"www.customs.go.kr", "customs.go.kr"},
    "대한무역투자진흥공사": {"dream.kotra.or.kr"},
}


@dataclass(frozen=True)
class GuidanceScopeBindingV2:
    """One exact topic/scope/direction statement made by a page-level source."""

    topic: GuidanceTopic
    scope: GuidanceScope
    directions: tuple[Literal["import", "export"], ...]

    def supports(self, direction: TradeDirection) -> bool:
        if direction == "both":
            return {"import", "export"} <= set(self.directions)
        return direction in self.directions


@dataclass(frozen=True)
class GuidanceRegistrySourceV2:
    """One official page; its bytes remain in ignored local source staging."""

    source_id: str
    source_kind: GuidanceSourceKind
    title: str
    official_uri: str
    authority: str
    jurisdiction: Literal["KR"]
    directions: tuple[Literal["import", "export"], ...]
    bindings: tuple[GuidanceScopeBindingV2, ...]
    availability: GuidanceSourceAvailability
    law_effectivity_source_id: str | None

    @property
    def topics(self) -> tuple[GuidanceTopic, ...]:
        return tuple(dict.fromkeys(binding.topic for binding in self.bindings))

    def supports(
        self,
        *,
        topic: GuidanceTopic,
        scope: GuidanceScope,
        direction: TradeDirection,
    ) -> bool:
        return any(
            binding.topic == topic and binding.scope == scope and binding.supports(direction)
            for binding in self.bindings
        )


@dataclass(frozen=True)
class GuidanceScopeEntryV2:
    """A closed answerable topic scope and the primary official pages it needs."""

    topic: GuidanceTopic
    scope: GuidanceScope
    title: str
    directions: tuple[Literal["import", "export"], ...]
    keywords: tuple[str, ...]
    development_excerpt: str
    required_source_ids: tuple[str, ...]

    def supports(self, direction: TradeDirection) -> bool:
        if direction == "both":
            return {"import", "export"} <= set(self.directions)
        return direction in self.directions


def _is_https_uri(value: object, *, authority: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2_000:
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() in _OFFICIAL_HOSTS_BY_AUTHORITY.get(authority, set())
        and not parsed.username
        and parsed.port in {None, 443}
    )


def _directions(value: object) -> tuple[Literal["import", "export"], ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(item not in {"import", "export"} for item in value)
        or len(value) != len(set(value))
    ):
        raise RuntimeError("guidance V2 registry is invalid")
    return tuple(cast(Literal["import", "export"], item) for item in value)


def _load_registry() -> tuple[
    str,
    str,
    tuple[GuidanceRegistrySourceV2, ...],
    tuple[GuidanceScopeEntryV2, ...],
    dict[GuidanceTopic, str],
    str,
]:
    raw = _REGISTRY_PATH.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover - tracked defect
        raise RuntimeError("guidance V2 registry is invalid") from exc
    if not isinstance(payload, dict):  # pragma: no cover - tracked defect
        raise RuntimeError("guidance V2 registry is invalid")
    sources = payload.get("sources")
    topics = payload.get("topics")
    if (
        payload.get("registry_version") != "2.0"
        or payload.get("jurisdiction") != "KR"
        or not isinstance(sources, list)
        or not sources
        or not isinstance(topics, list)
        or not topics
    ):
        raise RuntimeError("guidance V2 registry is invalid")

    source_entries: list[GuidanceRegistrySourceV2] = []
    for item in sources:
        if not isinstance(item, dict):
            raise RuntimeError("guidance V2 registry is invalid")
        source_id = item.get("source_id")
        source_kind = item.get("source_kind")
        title = item.get("title")
        official_uri = item.get("official_uri")
        authority = item.get("authority")
        jurisdiction = item.get("jurisdiction")
        availability = item.get("availability")
        law_effectivity_source_id = item.get("law_effectivity_source_id")
        bindings_value = item.get("bindings")
        if (
            not isinstance(source_id, str)
            or _SOURCE_ID_PATTERN.fullmatch(source_id) is None
            or not source_id.startswith("v2_")
            or not isinstance(source_kind, str)
            or source_kind not in _SOURCE_KINDS
            or not isinstance(title, str)
            or not title
            or not isinstance(authority, str)
            or authority not in _OFFICIAL_HOSTS_BY_AUTHORITY
            or not _is_https_uri(official_uri, authority=authority)
            or jurisdiction != "KR"
            or not isinstance(availability, str)
            or availability not in _AVAILABILITY
            or law_effectivity_source_id is not None
            and (
                not isinstance(law_effectivity_source_id, str)
                or _SOURCE_ID_PATTERN.fullmatch(law_effectivity_source_id) is None
            )
            or not isinstance(bindings_value, list)
            or not bindings_value
        ):
            raise RuntimeError("guidance V2 registry is invalid")
        source_directions = _directions(item.get("directions"))
        bindings: list[GuidanceScopeBindingV2] = []
        for binding in bindings_value:
            if not isinstance(binding, dict) or set(binding) != {"topic", "scope", "directions"}:
                raise RuntimeError("guidance V2 registry is invalid")
            topic = binding["topic"]
            scope = binding["scope"]
            if (
                not isinstance(topic, str)
                or topic not in _TOPICS
                or not isinstance(scope, str)
                or scope not in _SCOPES
            ):
                raise RuntimeError("guidance V2 registry is invalid")
            directions = _directions(binding["directions"])
            if not set(directions) <= set(source_directions):
                raise RuntimeError("guidance V2 registry binding is outside source direction")
            bindings.append(
                GuidanceScopeBindingV2(
                    topic=cast(GuidanceTopic, topic),
                    scope=cast(GuidanceScope, scope),
                    directions=directions,
                )
            )
        if len(bindings) != len({(item.topic, item.scope) for item in bindings}):
            raise RuntimeError("guidance V2 registry has duplicate source bindings")
        source_entries.append(
            GuidanceRegistrySourceV2(
                source_id=source_id,
                source_kind=cast(GuidanceSourceKind, source_kind),
                title=title,
                official_uri=cast(str, official_uri),
                authority=authority,
                jurisdiction="KR",
                directions=source_directions,
                bindings=tuple(bindings),
                availability=cast(GuidanceSourceAvailability, availability),
                law_effectivity_source_id=cast(str | None, law_effectivity_source_id),
            )
        )
    if len(source_entries) != len({entry.source_id for entry in source_entries}):
        raise RuntimeError("guidance V2 registry has duplicate source IDs")
    source_by_id = {entry.source_id: entry for entry in source_entries}
    for source in source_entries:
        effectivity_source_id = source.law_effectivity_source_id
        if effectivity_source_id is None:
            continue
        effectivity_source = source_by_id.get(effectivity_source_id)
        if (
            source.source_kind != "official_law"
            or effectivity_source is None
            or effectivity_source.source_kind != "official_law"
            or effectivity_source.law_effectivity_source_id is not None
        ):
            raise RuntimeError("guidance V2 registry has invalid law effectivity source")

    scope_entries: list[GuidanceScopeEntryV2] = []
    topic_titles: dict[GuidanceTopic, str] = {}
    for topic_item in topics:
        if not isinstance(topic_item, dict) or set(topic_item) != {"topic", "title", "scopes"}:
            raise RuntimeError("guidance V2 registry is invalid")
        topic = topic_item["topic"]
        title = topic_item["title"]
        scopes = topic_item["scopes"]
        if (
            not isinstance(topic, str)
            or topic not in _TOPICS
            or not isinstance(title, str)
            or not title
            or not isinstance(scopes, list)
            or not scopes
        ):
            raise RuntimeError("guidance V2 registry is invalid")
        topic_titles[cast(GuidanceTopic, topic)] = title
        for scope_item in scopes:
            required_keys = {
                "scope",
                "title",
                "directions",
                "keywords",
                "development_excerpt",
                "required_source_ids",
            }
            if not isinstance(scope_item, dict) or set(scope_item) != required_keys:
                raise RuntimeError("guidance V2 registry is invalid")
            scope = scope_item["scope"]
            scope_title = scope_item["title"]
            keywords = scope_item["keywords"]
            excerpt = scope_item["development_excerpt"]
            required_source_ids = scope_item["required_source_ids"]
            if (
                not isinstance(scope, str)
                or scope not in _SCOPES
                or not isinstance(scope_title, str)
                or not scope_title
                or not isinstance(keywords, list)
                or not keywords
                or any(not isinstance(keyword, str) or not keyword for keyword in keywords)
                or not isinstance(excerpt, str)
                or not excerpt
                or not isinstance(required_source_ids, list)
                or not required_source_ids
                or any(
                    not isinstance(source_id, str) or source_id not in source_by_id
                    for source_id in required_source_ids
                )
                or len(required_source_ids) != len(set(required_source_ids))
            ):
                raise RuntimeError("guidance V2 registry is invalid")
            directions = _directions(scope_item["directions"])
            for source_id in required_source_ids:
                source = source_by_id[cast(str, source_id)]
                if any(
                    not source.supports(
                        topic=cast(GuidanceTopic, topic),
                        scope=cast(GuidanceScope, scope),
                        direction=direction,
                    )
                    for direction in directions
                ):
                    raise RuntimeError("guidance V2 required source is outside scope")
            scope_entries.append(
                GuidanceScopeEntryV2(
                    topic=cast(GuidanceTopic, topic),
                    scope=cast(GuidanceScope, scope),
                    title=scope_title,
                    directions=directions,
                    keywords=tuple(keywords),
                    development_excerpt=excerpt,
                    required_source_ids=tuple(
                        cast(str, source_id) for source_id in required_source_ids
                    ),
                )
            )
    if len(scope_entries) != len({(entry.topic, entry.scope) for entry in scope_entries}):
        raise RuntimeError("guidance V2 registry has duplicate scopes")
    if {entry.topic for entry in scope_entries} != _TOPICS or set(topic_titles) != _TOPICS:
        raise RuntimeError("guidance V2 registry is missing a topic")
    return (
        "2.0",
        "KR",
        tuple(source_entries),
        tuple(scope_entries),
        topic_titles,
        sha256(raw).hexdigest(),
    )


(
    REGISTRY_V2_VERSION,
    REGISTRY_V2_JURISDICTION,
    REGISTRY_V2_SOURCES,
    REGISTRY_V2_SCOPES,
    REGISTRY_V2_TOPIC_TITLES,
    REGISTRY_V2_SHA256,
) = _load_registry()
REGISTRY_V2_SOURCES_BY_ID = {entry.source_id: entry for entry in REGISTRY_V2_SOURCES}
REGISTRY_V2_SCOPE_BY_KEY = {(entry.topic, entry.scope): entry for entry in REGISTRY_V2_SCOPES}


def guidance_scope_by_hint(
    topic: GuidanceTopic,
    scope: GuidanceScope,
    *,
    direction: TradeDirection,
) -> GuidanceScopeEntryV2 | None:
    """Resolve a caller-selected V2 scope without turning text into a search."""

    entry = REGISTRY_V2_SCOPE_BY_KEY.get((topic, scope))
    return entry if entry is not None and entry.supports(direction) else None


def resolve_guidance_scope(query: str, *, direction: TradeDirection) -> GuidanceScopeEntryV2 | None:
    """Resolve only one registry scope with deterministic local taxonomy matching."""

    normalized = "".join(query.casefold().split())
    candidates: list[tuple[int, int, GuidanceScopeEntryV2]] = []
    for index, entry in enumerate(REGISTRY_V2_SCOPES):
        if not entry.supports(direction):
            continue
        score = sum(
            1 for keyword in entry.keywords if "".join(keyword.casefold().split()) in normalized
        )
        if score:
            candidates.append((score, -index, entry))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def v2_source_for_id(source_id: str) -> GuidanceRegistrySourceV2 | None:
    return REGISTRY_V2_SOURCES_BY_ID.get(source_id)


def kr_trade_v2_source_ids() -> frozenset[str]:
    return frozenset(source.source_id for source in REGISTRY_V2_SOURCES)


def registry_manifest_v2() -> dict[str, object]:
    """Return the Git-safe, consumer-copyable V2 provenance manifest."""

    return {
        "schema_version": REGISTRY_V2_VERSION,
        "jurisdiction": REGISTRY_V2_JURISDICTION,
        "registry_sha256": REGISTRY_V2_SHA256,
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
                "scopes": [
                    {
                        "topic": binding.topic,
                        "scope": binding.scope,
                        "directions": list(binding.directions),
                    }
                    for binding in source.bindings
                ],
                "availability": source.availability,
            }
            for source in REGISTRY_V2_SOURCES
        ],
        "topics": [
            {
                "topic": topic,
                "title": REGISTRY_V2_TOPIC_TITLES[cast(GuidanceTopic, topic)],
                "scopes": [
                    {
                        "scope": entry.scope,
                        "title": entry.title,
                        "directions": list(entry.directions),
                        "required_source_ids": list(entry.required_source_ids),
                    }
                    for entry in REGISTRY_V2_SCOPES
                    if entry.topic == topic
                ],
            }
            for topic in (
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
            )
        ],
        "coverage_profiles": {
            "functional-uat-kr-trade-v2": {
                "status": "source_build_required",
                "source_ids": sorted(kr_trade_v2_source_ids()),
            }
        },
    }


__all__ = [
    "GuidanceRegistrySourceV2",
    "GuidanceScope",
    "GuidanceScopeBindingV2",
    "GuidanceScopeEntryV2",
    "REGISTRY_V2_JURISDICTION",
    "REGISTRY_V2_SCOPE_BY_KEY",
    "REGISTRY_V2_SCOPES",
    "REGISTRY_V2_SHA256",
    "REGISTRY_V2_SOURCES",
    "REGISTRY_V2_SOURCES_BY_ID",
    "REGISTRY_V2_TOPIC_TITLES",
    "REGISTRY_V2_VERSION",
    "guidance_scope_by_hint",
    "kr_trade_v2_source_ids",
    "registry_manifest_v2",
    "resolve_guidance_scope",
    "v2_source_for_id",
]
