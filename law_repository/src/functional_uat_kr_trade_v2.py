"""Fail-closed V2 source builds for page-level Korean trade guidance.

This module is intentionally separate from ``functional_uat_kr_trade``.  The
older V1 pointer and its three-law state remain readable without migration;
V2 uses its own active pointer and immutable local manifest.  No URL is
downloaded here.  An operator stages official source bytes under the ignored
profile and attests the exact page URL and source hash in a V2 build manifest.
Binary PDF sources also require a separately hash-bound local text projection;
the raw PDF remains the source of record.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal, cast

from src.evidence import EvidenceError
from src.functional_uat_kr_trade import FunctionalUatKrTradeError
from src.guidance_registry import GuidanceTopic, TradeDirection
from src.guidance_registry_v2 import (
    GuidanceScope,
    GuidanceScopeEntryV2,
    REGISTRY_V2_SHA256,
    REGISTRY_V2_SOURCES_BY_ID,
    guidance_scope_by_hint,
)
from src.guidance_v3 import (
    GeneralGuidanceQueryRequestV3,
    GeneralGuidanceQueryResponseV3,
    GuidanceSourceV3,
)
from src.local_profiles import (
    FUNCTIONAL_UAT_KR_TRADE_PROFILE,
    ensure_functional_uat_kr_trade_profile,
)


KR_TRADE_V2_LOCAL_STATE_VERSION = "functional-uat-kr-trade-v2-local/v1"
KR_TRADE_V2_BUILD_STATE_VERSION = "functional-uat-kr-trade-v2-local/v1"
_ACTIVE_SNAPSHOT_FILENAME = "active-guidance-v2-snapshot.json"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
_EXCERPT_MINIMUM_LENGTH = 20
_PDF_TEXT_PROJECTION_METHOD = "operator_pdf_text_projection/v1"
_KR_TRADE_PROFILE_LITERAL = cast(
    Literal["functional-uat-kr-trade"], FUNCTIONAL_UAT_KR_TRADE_PROFILE
)


@dataclass(frozen=True)
class KrTradeV2SnapshotStatus:
    profile: Literal["functional-uat-kr-trade"]
    registry_version: Literal["2.0"]
    registry_sha256: str
    status: Literal["source_build_required", "ready"]
    source_count: int
    as_of: date | None
    snapshot_id: str | None = None
    record_count: int = 0
    coverage_count: int = 0
    covered_bundle_count: int = 0


@dataclass(frozen=True)
class KrTradeV2ReceiptBinding:
    topic: GuidanceTopic
    scope: GuidanceScope
    directions: tuple[Literal["import", "export"], ...]


@dataclass(frozen=True)
class KrTradeV2SourceReceipt:
    source_id: str
    source_uri: str
    relative_path: str
    source_sha256: str
    collected_at: str
    bindings: tuple[KrTradeV2ReceiptBinding, ...]
    legal_effective_from: date | None
    legal_effective_to: date | None
    legal_effective_date_excerpt: str | None
    text_relative_path: str | None
    text_sha256: str | None
    text_extraction_method: str | None


@dataclass(frozen=True)
class KrTradeV2GuidanceRecord:
    record_id: str
    source_id: str
    topic: GuidanceTopic
    scope: GuidanceScope
    direction: TradeDirection
    excerpt: str
    locator: str | None
    source_sha256: str
    legal_effective_from: date | None


@dataclass(frozen=True)
class KrTradeV2GuidanceSnapshot:
    snapshot_id: str
    as_of: date
    source_receipts: tuple[KrTradeV2SourceReceipt, ...]
    records: tuple[KrTradeV2GuidanceRecord, ...]
    coverage: tuple[
        tuple[GuidanceTopic, GuidanceScope, Literal["import", "export"], tuple[str, ...]], ...
    ]


def _active_snapshot_path(profile: Path) -> Path:
    return profile / "state" / _ACTIVE_SNAPSHOT_FILENAME


def _snapshot_manifest_path(profile: Path, snapshot_id: str) -> Path:
    if _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    directory = profile / "state" / "guidance-v2-snapshots" / snapshot_id
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    return directory / "manifest.json"


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _write_json_new(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_EXISTS")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(_canonical_json_bytes(payload).decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _replace_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(_canonical_json_bytes(payload).decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _safe_profile_file(profile: Path, relative: str) -> Path:
    value = Path(relative)
    if (
        not relative
        or value.is_absolute()
        or value.drive
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
        or value.parts[0] != "data"
        or len(value.parts) < 3
        or value.parts[1] != "guidance-sources"
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_PATH_INVALID")
    candidate = profile.joinpath(*value.parts)
    current = profile
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_PATH_INVALID")
    try:
        candidate.resolve(strict=False).relative_to(profile.resolve())
    except ValueError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_PATH_INVALID") from exc
    return candidate


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


class _VisibleTextParser(HTMLParser):
    """Extract visible text without accepting scripts as citation evidence."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _visible_text(raw: bytes) -> str:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_TEXT_UNAVAILABLE") from exc
    parser = _VisibleTextParser()
    try:
        parser.feed(decoded)
        parser.close()
    except ValueError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_TEXT_UNAVAILABLE") from exc
    return _normalized_text(" ".join(parser.parts))


def _parse_aware_datetime(value: object) -> str:
    if not isinstance(value, str):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    return parsed.isoformat()


def _directions(value: object) -> tuple[Literal["import", "export"], ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(item not in {"import", "export"} for item in value)
        or len(value) != len(set(value))
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    return tuple(cast(Literal["import", "export"], item) for item in value)


def _record_direction(value: object) -> TradeDirection:
    if value not in {"import", "export", "both"}:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    return cast(TradeDirection, value)


def _record_supports_direction(
    record_direction: TradeDirection, direction: Literal["import", "export"]
) -> bool:
    return record_direction in {direction, "both"}


def _read_and_hash_source(profile: Path, relative_path: str, expected_hash: str) -> bytes:
    if _SHA256.fullmatch(expected_hash) is None:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    source_path = _safe_profile_file(profile, relative_path)
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_UNAVAILABLE") from exc
    if sha256(raw).hexdigest() != expected_hash:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_HASH_MISMATCH")
    return raw


def _date_value(value: object) -> date:
    if not isinstance(value, str):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID") from exc


def _statute_effectivity(
    *, source_id: str, receipt: dict[str, object], raw: bytes
) -> tuple[date | None, date | None]:
    """Validate statute timing from an operator-selected source-text excerpt.

    The source builder does not infer legal effectivity from an URL or from the
    snapshot date.  A staged statute has to carry an explicit effective-date
    excerpt that is actually present in the hash-verified official source.
    """

    source = REGISTRY_V2_SOURCES_BY_ID[source_id]
    if source.source_kind != "official_law" or source.law_effectivity_source_id is not None:
        return None, None
    effective_from = _date_value(receipt["effective_from"])
    effective_to_value = receipt["effective_to"]
    effective_to = None if effective_to_value is None else _date_value(effective_to_value)
    if effective_to is not None and effective_to < effective_from:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    excerpt = receipt["effective_date_excerpt"]
    if not isinstance(excerpt, str) or len(_normalized_text(excerpt)) < _EXCERPT_MINIMUM_LENGTH:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    source_text = _visible_text(raw)
    normalized_excerpt = _normalized_text(excerpt)
    if normalized_excerpt not in source_text:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_EFFECTIVE_DATE_NOT_IN_SOURCE")
    if not _date_is_written_in_excerpt(effective_from, normalized_excerpt) or (
        effective_to is not None
        and not _date_is_written_in_excerpt(effective_to, normalized_excerpt)
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_EFFECTIVE_DATE_NOT_IN_SOURCE")
    return effective_from, effective_to


def _assert_statutes_applicable(
    receipts: tuple[KrTradeV2SourceReceipt, ...], *, as_of: date
) -> None:
    for receipt in receipts:
        source = REGISTRY_V2_SOURCES_BY_ID[receipt.source_id]
        if source.source_kind != "official_law":
            continue
        if receipt.legal_effective_from is None or receipt.legal_effective_from > as_of:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_STATUTE_NOT_EFFECTIVE_AT_AS_OF")
        if receipt.legal_effective_to is not None and receipt.legal_effective_to < as_of:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_STATUTE_NOT_EFFECTIVE_AT_AS_OF")


def _date_is_written_in_excerpt(value: date, excerpt: str) -> bool:
    return (
        re.search(rf"{value.year}\D+0?{value.month}\D+0?{value.day}(?:\D|$)", excerpt) is not None
    )


def _parse_receipt_bindings(
    value: object, *, source_id: str
) -> tuple[KrTradeV2ReceiptBinding, ...]:
    if not isinstance(value, list) or not value:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    source = REGISTRY_V2_SOURCES_BY_ID[source_id]
    parsed: list[KrTradeV2ReceiptBinding] = []
    for binding in value:
        if not isinstance(binding, dict) or set(binding) != {"topic", "scope", "directions"}:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        topic = binding["topic"]
        scope = binding["scope"]
        if not isinstance(topic, str) or not isinstance(scope, str):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        directions = _directions(binding["directions"])
        if any(
            not source.supports(
                topic=cast(GuidanceTopic, topic),
                scope=cast(GuidanceScope, scope),
                direction=direction,
            )
            for direction in directions
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        parsed.append(
            KrTradeV2ReceiptBinding(
                topic=cast(GuidanceTopic, topic),
                scope=cast(GuidanceScope, scope),
                directions=directions,
            )
        )
    if len(parsed) != len({(item.topic, item.scope) for item in parsed}):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    return tuple(parsed)


def _validated_receipts(profile: Path, value: object) -> tuple[KrTradeV2SourceReceipt, ...]:
    if not isinstance(value, list) or not value:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    parsed: list[KrTradeV2SourceReceipt] = []
    seen: set[str] = set()
    base_keys = {"source_id", "source_uri", "relative_path", "sha256", "collected_at", "bindings"}
    statute_keys = {*base_keys, "effective_from", "effective_to", "effective_date_excerpt"}
    pdf_projection_keys = {"text_relative_path", "text_sha256", "text_extraction_method"}
    for receipt in value:
        if not isinstance(receipt, dict):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        source_id = receipt.get("source_id")
        if not isinstance(source_id, str) or source_id not in REGISTRY_V2_SOURCES_BY_ID:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        source = REGISTRY_V2_SOURCES_BY_ID[source_id]
        if not base_keys <= set(receipt):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        source_uri = receipt["source_uri"]
        relative_path = receipt["relative_path"]
        source_hash = receipt["sha256"]
        if (
            source_id in seen
            or not isinstance(source_uri, str)
            or not isinstance(relative_path, str)
            or not isinstance(source_hash, str)
            or _SHA256.fullmatch(source_hash) is None
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        if source_uri != source.official_uri:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        bindings = _parse_receipt_bindings(receipt["bindings"], source_id=source_id)
        raw = _read_and_hash_source(profile, relative_path, source_hash)
        is_pdf = raw.startswith(b"%PDF-")
        requires_own_law_effectivity = (
            source.source_kind == "official_law" and source.law_effectivity_source_id is None
        )
        expected_keys = statute_keys if requires_own_law_effectivity else base_keys
        if is_pdf:
            expected_keys = {*expected_keys, *pdf_projection_keys}
        if set(receipt) != expected_keys:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        text_relative_path: str | None = None
        text_sha256: str | None = None
        text_extraction_method: str | None = None
        if is_pdf:
            candidate_text_path = receipt["text_relative_path"]
            candidate_text_sha256 = receipt["text_sha256"]
            candidate_method = receipt["text_extraction_method"]
            if (
                not isinstance(candidate_text_path, str)
                or candidate_text_path == relative_path
                or not isinstance(candidate_text_sha256, str)
                or _SHA256.fullmatch(candidate_text_sha256) is None
                or candidate_method != _PDF_TEXT_PROJECTION_METHOD
            ):
                raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
            projection = _read_and_hash_source(profile, candidate_text_path, candidate_text_sha256)
            if projection.startswith(b"%PDF-"):
                raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_TEXT_UNAVAILABLE")
            _visible_text(projection)
            text_relative_path = candidate_text_path
            text_sha256 = candidate_text_sha256
            text_extraction_method = candidate_method
        legal_effective_from, legal_effective_to = _statute_effectivity(
            source_id=source_id, receipt=receipt, raw=raw
        )
        parsed.append(
            KrTradeV2SourceReceipt(
                source_id=source_id,
                source_uri=source_uri,
                relative_path=relative_path,
                source_sha256=source_hash,
                collected_at=_parse_aware_datetime(receipt["collected_at"]),
                bindings=bindings,
                legal_effective_from=legal_effective_from,
                legal_effective_to=legal_effective_to,
                legal_effective_date_excerpt=(
                    cast(str, receipt["effective_date_excerpt"])
                    if legal_effective_from is not None
                    else None
                ),
                text_relative_path=text_relative_path,
                text_sha256=text_sha256,
                text_extraction_method=text_extraction_method,
            )
        )
        seen.add(source_id)
    return _resolve_law_effectivity(tuple(parsed))


def _resolve_law_effectivity(
    receipts: tuple[KrTradeV2SourceReceipt, ...],
) -> tuple[KrTradeV2SourceReceipt, ...]:
    """Inherit a body page's effective date only from its explicit law page."""

    receipts_by_id = {receipt.source_id: receipt for receipt in receipts}
    resolved: list[KrTradeV2SourceReceipt] = []
    for receipt in receipts:
        source = REGISTRY_V2_SOURCES_BY_ID[receipt.source_id]
        effectivity_source_id = source.law_effectivity_source_id
        if effectivity_source_id is None:
            resolved.append(receipt)
            continue
        effectivity_receipt = receipts_by_id.get(effectivity_source_id)
        if (
            effectivity_receipt is None
            or effectivity_receipt.legal_effective_from is None
            or effectivity_receipt.legal_effective_date_excerpt is None
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_LAW_EFFECTIVITY_SOURCE_UNAVAILABLE")
        resolved.append(
            replace(
                receipt,
                legal_effective_from=effectivity_receipt.legal_effective_from,
                legal_effective_to=effectivity_receipt.legal_effective_to,
                legal_effective_date_excerpt=effectivity_receipt.legal_effective_date_excerpt,
            )
        )
    return tuple(resolved)


def _source_text(profile: Path, receipt: KrTradeV2SourceReceipt) -> str:
    raw = _read_and_hash_source(profile, receipt.relative_path, receipt.source_sha256)
    if raw.startswith(b"%PDF-"):
        if receipt.text_relative_path is None or receipt.text_sha256 is None:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_TEXT_UNAVAILABLE")
        projection = _read_and_hash_source(profile, receipt.text_relative_path, receipt.text_sha256)
        if projection.startswith(b"%PDF-"):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_TEXT_UNAVAILABLE")
        return _visible_text(projection)
    return _visible_text(raw)


def _validated_records(
    profile: Path,
    receipts: tuple[KrTradeV2SourceReceipt, ...],
    value: object,
) -> tuple[KrTradeV2GuidanceRecord, ...]:
    if not isinstance(value, list) or not value:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    by_source = {receipt.source_id: receipt for receipt in receipts}
    text_by_source = {
        source_id: _normalized_text(_source_text(profile, receipt))
        for source_id, receipt in by_source.items()
    }
    parsed: list[KrTradeV2GuidanceRecord] = []
    seen: set[str] = set()
    valid_keys = {
        frozenset({"source_id", "topic", "scope", "direction", "excerpt"}),
        frozenset({"source_id", "topic", "scope", "direction", "excerpt", "locator"}),
    }
    for record in value:
        if not isinstance(record, dict) or frozenset(record) not in valid_keys:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        source_id = record["source_id"]
        topic = record["topic"]
        scope = record["scope"]
        excerpt = record["excerpt"]
        locator = record.get("locator")
        if (
            not isinstance(source_id, str)
            or source_id not in by_source
            or not isinstance(topic, str)
            or not isinstance(scope, str)
            or not isinstance(excerpt, str)
            or len(_normalized_text(excerpt)) < _EXCERPT_MINIMUM_LENGTH
            or locator is not None
            and (not isinstance(locator, str) or not locator or len(locator) > 500)
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        direction = _record_direction(record["direction"])
        source = REGISTRY_V2_SOURCES_BY_ID[source_id]
        if not source.supports(
            topic=cast(GuidanceTopic, topic),
            scope=cast(GuidanceScope, scope),
            direction=direction,
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        receipt = by_source[source_id]
        receipt_binding = next(
            (
                binding
                for binding in receipt.bindings
                if binding.topic == topic and binding.scope == scope
            ),
            None,
        )
        if receipt_binding is None or (
            direction == "both"
            and {"import", "export"} - set(receipt_binding.directions)
            or direction != "both"
            and direction not in receipt_binding.directions
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        normalized_excerpt = _normalized_text(excerpt)
        if normalized_excerpt not in text_by_source[source_id]:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_EXCERPT_NOT_IN_SOURCE")
        record_id = sha256(
            "\x1f".join(
                [source_id, topic, scope, direction, normalized_excerpt, locator or ""]
            ).encode("utf-8")
        ).hexdigest()
        if record_id in seen:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        parsed.append(
            KrTradeV2GuidanceRecord(
                record_id=record_id,
                source_id=source_id,
                topic=cast(GuidanceTopic, topic),
                scope=cast(GuidanceScope, scope),
                direction=direction,
                excerpt=normalized_excerpt,
                locator=cast(str | None, locator),
                source_sha256=receipt.source_sha256,
                legal_effective_from=receipt.legal_effective_from,
            )
        )
        seen.add(record_id)
    return tuple(parsed)


def _coverage(
    records: tuple[KrTradeV2GuidanceRecord, ...],
) -> tuple[tuple[GuidanceTopic, GuidanceScope, Literal["import", "export"], tuple[str, ...]], ...]:
    covered: list[
        tuple[GuidanceTopic, GuidanceScope, Literal["import", "export"], tuple[str, ...]]
    ] = []
    for entry in _scope_entries():
        for direction in entry.directions:
            source_ids = tuple(
                source_id
                for source_id in entry.required_source_ids
                if any(
                    record.source_id == source_id
                    and record.topic == entry.topic
                    and record.scope == entry.scope
                    and _record_supports_direction(record.direction, direction)
                    for record in records
                )
            )
            covered.append((entry.topic, entry.scope, direction, source_ids))
    return tuple(covered)


def _scope_entries() -> tuple[GuidanceScopeEntryV2, ...]:
    # This import-free indirection keeps the coverage routine easy to audit in
    # tests while avoiding a second copy of the registry's scope matrix.
    from src.guidance_registry_v2 import REGISTRY_V2_SCOPES

    return REGISTRY_V2_SCOPES


def _receipt_payload(receipt: KrTradeV2SourceReceipt) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_id": receipt.source_id,
        "source_uri": receipt.source_uri,
        "relative_path": receipt.relative_path,
        "sha256": receipt.source_sha256,
        "collected_at": receipt.collected_at,
        "bindings": [
            {"topic": binding.topic, "scope": binding.scope, "directions": list(binding.directions)}
            for binding in receipt.bindings
        ],
    }
    source = REGISTRY_V2_SOURCES_BY_ID[receipt.source_id]
    if source.source_kind == "official_law" and source.law_effectivity_source_id is None:
        if receipt.legal_effective_from is None or receipt.legal_effective_date_excerpt is None:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        payload["effective_from"] = receipt.legal_effective_from.isoformat()
        payload["effective_to"] = (
            receipt.legal_effective_to.isoformat()
            if receipt.legal_effective_to is not None
            else None
        )
        # Local V2 state remains under ignored ``_local`` and may retain this
        # short provenance excerpt so readiness can revalidate it against the
        # source hash.  It is never added to a Git contract or HTTP response.
        payload["effective_date_excerpt"] = receipt.legal_effective_date_excerpt
    if receipt.text_relative_path is not None:
        payload["text_relative_path"] = receipt.text_relative_path
        payload["text_sha256"] = receipt.text_sha256
        payload["text_extraction_method"] = receipt.text_extraction_method
    return payload


def _record_payload(record: KrTradeV2GuidanceRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "source_id": record.source_id,
        "topic": record.topic,
        "scope": record.scope,
        "direction": record.direction,
        "excerpt": record.excerpt,
        "locator": record.locator,
        "source_sha256": record.source_sha256,
        "legal_effective_from": (
            record.legal_effective_from.isoformat()
            if record.legal_effective_from is not None
            else None
        ),
    }


def _coverage_payload(
    coverage: tuple[
        tuple[GuidanceTopic, GuidanceScope, Literal["import", "export"], tuple[str, ...]], ...
    ],
) -> list[dict[str, object]]:
    return [
        {"topic": topic, "scope": scope, "direction": direction, "source_ids": list(source_ids)}
        for topic, scope, direction, source_ids in coverage
    ]


def _build_manifest(
    *,
    snapshot_id: str,
    as_of: date,
    receipts: tuple[KrTradeV2SourceReceipt, ...],
    records: tuple[KrTradeV2GuidanceRecord, ...],
    coverage: tuple[
        tuple[GuidanceTopic, GuidanceScope, Literal["import", "export"], tuple[str, ...]], ...
    ],
) -> dict[str, object]:
    return {
        "local_state_version": KR_TRADE_V2_BUILD_STATE_VERSION,
        "profile": FUNCTIONAL_UAT_KR_TRADE_PROFILE,
        "registry_version": "2.0",
        "registry_sha256": REGISTRY_V2_SHA256,
        "snapshot_id": snapshot_id,
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_receipts": [_receipt_payload(receipt) for receipt in receipts],
        "guidance_records": [_record_payload(record) for record in records],
        "coverage": _coverage_payload(coverage),
    }


def _parse_ready_manifest(profile: Path, payload: object) -> KrTradeV2GuidanceSnapshot:
    required_keys = {
        "local_state_version",
        "profile",
        "registry_version",
        "registry_sha256",
        "snapshot_id",
        "as_of",
        "built_at",
        "source_receipts",
        "guidance_records",
        "coverage",
    }
    if not isinstance(payload, dict) or set(payload) != required_keys:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    if (
        payload["local_state_version"] != KR_TRADE_V2_BUILD_STATE_VERSION
        or payload["profile"] != FUNCTIONAL_UAT_KR_TRADE_PROFILE
        or payload["registry_version"] != "2.0"
        or payload["registry_sha256"] != REGISTRY_V2_SHA256
        or not isinstance(payload["snapshot_id"], str)
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    snapshot_id = payload["snapshot_id"]
    if _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    try:
        as_of = date.fromisoformat(cast(str, payload["as_of"]))
    except (TypeError, ValueError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID") from exc
    _parse_aware_datetime(payload["built_at"])
    receipts = _validated_receipts(profile, payload["source_receipts"])
    _assert_statutes_applicable(receipts, as_of=as_of)
    raw_records = payload["guidance_records"]
    if not isinstance(raw_records, list):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    # Ready manifests add local-only record IDs and source hashes. Convert them
    # to the strict build-input form, then recompute every derived value.
    input_records: list[dict[str, object]] = []
    for record in raw_records:
        if not isinstance(record, dict):
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        expected = {
            "record_id",
            "source_id",
            "topic",
            "scope",
            "direction",
            "excerpt",
            "locator",
            "source_sha256",
            "legal_effective_from",
        }
        if set(record) != expected:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        item = {
            key: record[key]
            for key in ("source_id", "topic", "scope", "direction", "excerpt", "locator")
        }
        input_records.append(item)
    records = _validated_records(profile, receipts, input_records)
    if len(records) != len(raw_records) or any(
        record.record_id != raw["record_id"]
        or record.source_sha256 != raw["source_sha256"]
        or (
            record.legal_effective_from.isoformat()
            if record.legal_effective_from is not None
            else None
        )
        != raw["legal_effective_from"]
        for record, raw in zip(records, raw_records, strict=True)
        if isinstance(raw, dict)
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    expected_coverage = _coverage(records)
    if payload["coverage"] != _coverage_payload(expected_coverage):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    return KrTradeV2GuidanceSnapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        source_receipts=receipts,
        records=records,
        coverage=expected_coverage,
    )


def _read_active_snapshot(profile: Path) -> KrTradeV2GuidanceSnapshot:
    pointer_path = _active_snapshot_path(profile)
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SOURCE_BUILD_REQUIRED")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID") from exc
    if (
        not isinstance(pointer, dict)
        or set(pointer)
        != {"local_state_version", "profile", "snapshot_id", "manifest_relative_path"}
        or pointer["local_state_version"] != KR_TRADE_V2_BUILD_STATE_VERSION
        or pointer["profile"] != FUNCTIONAL_UAT_KR_TRADE_PROFILE
        or not isinstance(pointer["snapshot_id"], str)
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    snapshot_id = pointer["snapshot_id"]
    expected_relative = f"state/guidance-v2-snapshots/{snapshot_id}/manifest.json"
    if pointer["manifest_relative_path"] != expected_relative:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    manifest_path = _snapshot_manifest_path(profile, snapshot_id)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID") from exc
    return _parse_ready_manifest(profile, payload)


def _status_from_snapshot(snapshot: KrTradeV2GuidanceSnapshot) -> KrTradeV2SnapshotStatus:
    covered = 0
    for topic, scope, direction, source_ids in snapshot.coverage:
        entry = guidance_scope_by_hint(topic, scope, direction=direction)
        if entry is None:
            raise FunctionalUatKrTradeError("KR_TRADE_V2_SNAPSHOT_INVALID")
        if set(source_ids) == set(entry.required_source_ids):
            covered += 1
    return KrTradeV2SnapshotStatus(
        profile=_KR_TRADE_PROFILE_LITERAL,
        registry_version="2.0",
        registry_sha256=REGISTRY_V2_SHA256,
        status="ready",
        source_count=len(snapshot.source_receipts),
        as_of=snapshot.as_of,
        snapshot_id=snapshot.snapshot_id,
        record_count=len(snapshot.records),
        coverage_count=len(snapshot.coverage),
        covered_bundle_count=covered,
    )


def inspect_kr_trade_v2_snapshot(
    *, local_root_override: Path | None = None
) -> KrTradeV2SnapshotStatus:
    profile = ensure_functional_uat_kr_trade_profile(root=local_root_override)
    if not _active_snapshot_path(profile).exists():
        return KrTradeV2SnapshotStatus(
            profile=_KR_TRADE_PROFILE_LITERAL,
            registry_version="2.0",
            registry_sha256=REGISTRY_V2_SHA256,
            status="source_build_required",
            source_count=0,
            as_of=None,
        )
    return _status_from_snapshot(_read_active_snapshot(profile))


def require_kr_trade_v2_retrieval_snapshot(
    *, local_root_override: Path | None = None
) -> KrTradeV2GuidanceSnapshot:
    profile = ensure_functional_uat_kr_trade_profile(root=local_root_override)
    return _read_active_snapshot(profile)


def build_kr_trade_v2_guidance_snapshot(
    *,
    staged_manifest_relative: str,
    local_root_override: Path | None = None,
) -> KrTradeV2SnapshotStatus:
    """Build one immutable V2 projection from manually staged official bytes."""

    profile = ensure_functional_uat_kr_trade_profile(root=local_root_override)
    manifest_path = _safe_profile_file(profile, staged_manifest_relative)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_V2_STAGED_MANIFEST_UNAVAILABLE")
    try:
        staged = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_STAGED_MANIFEST_INVALID") from exc
    keys = {"schema_version", "snapshot_id", "as_of", "source_receipts", "guidance_records"}
    if (
        not isinstance(staged, dict)
        or set(staged) != keys
        or staged["schema_version"] != "functional-uat-kr-trade-source-build-input/v2"
        or not isinstance(staged["snapshot_id"], str)
        or _SNAPSHOT_ID.fullmatch(staged["snapshot_id"]) is None
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_V2_STAGED_MANIFEST_INVALID")
    try:
        as_of = date.fromisoformat(cast(str, staged["as_of"]))
    except (TypeError, ValueError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_V2_STAGED_MANIFEST_INVALID") from exc
    receipts = _validated_receipts(profile, staged["source_receipts"])
    _assert_statutes_applicable(receipts, as_of=as_of)
    records = _validated_records(profile, receipts, staged["guidance_records"])
    coverage = _coverage(records)
    snapshot_id = staged["snapshot_id"]
    output_path = _snapshot_manifest_path(profile, snapshot_id)
    _write_json_new(
        output_path,
        _build_manifest(
            snapshot_id=snapshot_id,
            as_of=as_of,
            receipts=receipts,
            records=records,
            coverage=coverage,
        ),
    )
    _replace_json(
        _active_snapshot_path(profile),
        {
            "local_state_version": KR_TRADE_V2_BUILD_STATE_VERSION,
            "profile": FUNCTIONAL_UAT_KR_TRADE_PROFILE,
            "snapshot_id": snapshot_id,
            "manifest_relative_path": f"state/guidance-v2-snapshots/{snapshot_id}/manifest.json",
        },
    )
    return _status_from_snapshot(_read_active_snapshot(profile))


def _v2_error(code: str, message: str, *, retryable: bool = False) -> EvidenceError:
    return EvidenceError(code=code, message=message, retryable=retryable)


def _v2_failure(
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
        registry_version="2.0",
        registry_sha256=REGISTRY_V2_SHA256,
        topic=request.topic_hint,
        guidance_scope=request.scope_hint,
        errors=[_v2_error(code, message, retryable=retryable)],
    )


def _entry_for_request(request: GeneralGuidanceQueryRequestV3) -> GuidanceScopeEntryV2 | None:
    # The V2 provider accepts a code-selected, closed scope only.  It never
    # classifies the caller's free-form query or forwards it to a search.
    if request.topic_hint is None or request.scope_hint is None:
        return None
    return guidance_scope_by_hint(
        request.topic_hint,
        request.scope_hint,
        direction=request.trade_direction,
    )


def _source_id(snapshot_id: str, record: KrTradeV2GuidanceRecord) -> str:
    return (
        "guide_"
        + sha256(
            "\x1f".join(["v2", snapshot_id, record.record_id, record.source_sha256]).encode("utf-8")
        ).hexdigest()
    )


def _coverage_is_complete(
    snapshot: KrTradeV2GuidanceSnapshot,
    entry: GuidanceScopeEntryV2,
    requested_direction: TradeDirection,
) -> bool:
    by_key = {
        (topic, scope, direction): set(source_ids)
        for topic, scope, direction, source_ids in snapshot.coverage
    }
    directions: tuple[Literal["import", "export"], ...] = (
        ("import", "export") if requested_direction == "both" else (requested_direction,)
    )
    return all(
        by_key.get((entry.topic, entry.scope, direction), set()) == set(entry.required_source_ids)
        for direction in directions
    )


def query_kr_trade_v2_guidance_v3(
    request: GeneralGuidanceQueryRequestV3,
    *,
    local_root_override: Path | None = None,
) -> GeneralGuidanceQueryResponseV3:
    """Return only V2 registry-bound excerpts from a rehashed local snapshot."""

    try:
        snapshot = require_kr_trade_v2_retrieval_snapshot(local_root_override=local_root_override)
    except FunctionalUatKrTradeError:
        return _v2_failure(
            request,
            code="GUIDANCE_SNAPSHOT_UNAVAILABLE",
            message="현재 공식 지식 스냅샷을 확인할 수 없습니다.",
            retryable=True,
        )
    if request.as_of != snapshot.as_of:
        return _v2_failure(
            request,
            code="UNSUPPORTED_GUIDANCE_AS_OF",
            message="현재 공식 지식 스냅샷은 요청한 기준일을 지원하지 않습니다.",
            snapshot_id=snapshot.snapshot_id,
        )
    entry = _entry_for_request(request)
    if entry is None:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="no_coverage",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="no_coverage",
            registry_version="2.0",
            registry_sha256=REGISTRY_V2_SHA256,
            topic=request.topic_hint,
            guidance_scope=request.scope_hint,
        )
    records = tuple(
        record
        for record in snapshot.records
        if record.topic == entry.topic
        and record.scope == entry.scope
        and (
            record.direction == "both"
            or request.trade_direction == "both"
            or record.direction == request.trade_direction
        )
    )
    if not records:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="no_coverage",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="no_coverage",
            registry_version="2.0",
            registry_sha256=REGISTRY_V2_SHA256,
            topic=entry.topic,
            guidance_scope=entry.scope,
        )
    results = [
        GuidanceSourceV3(
            source_id=_source_id(snapshot.snapshot_id, record),
            registry_source_id=record.source_id,
            source_kind=REGISTRY_V2_SOURCES_BY_ID[record.source_id].source_kind,
            title=REGISTRY_V2_SOURCES_BY_ID[record.source_id].title,
            uri=REGISTRY_V2_SOURCES_BY_ID[record.source_id].official_uri,
            topic=record.topic,
            direction=record.direction,
            excerpt=record.excerpt,
            valid_from=record.legal_effective_from,
            version_hash=record.source_sha256,
            retrieval_methods=["registry"],
        )
        for record in sorted(records, key=lambda item: (item.source_id, item.record_id))[
            : request.top_k
        ]
    ]
    if _coverage_is_complete(snapshot, entry, request.trade_direction):
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="covered",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="covered",
            registry_version="2.0",
            registry_sha256=REGISTRY_V2_SHA256,
            topic=entry.topic,
            guidance_scope=entry.scope,
            results=results,
        )
    return GeneralGuidanceQueryResponseV3(
        request_id=request.request_id,
        status="partial",
        snapshot_id=snapshot.snapshot_id,
        as_of=request.as_of,
        coverage="partial",
        registry_version="2.0",
        registry_sha256=REGISTRY_V2_SHA256,
        topic=entry.topic,
        guidance_scope=entry.scope,
        results=results,
        errors=[
            _v2_error(
                "GUIDANCE_SNAPSHOT_PARTIAL_COVERAGE",
                "해당 세부범위의 필수 공식 출처가 현재 로컬 스냅샷에 모두 포함되어 있지 않습니다.",
            )
        ],
    )


__all__ = [
    "KR_TRADE_V2_BUILD_STATE_VERSION",
    "KR_TRADE_V2_LOCAL_STATE_VERSION",
    "KrTradeV2GuidanceRecord",
    "KrTradeV2GuidanceSnapshot",
    "KrTradeV2SnapshotStatus",
    "build_kr_trade_v2_guidance_snapshot",
    "inspect_kr_trade_v2_snapshot",
    "query_kr_trade_v2_guidance_v3",
    "require_kr_trade_v2_retrieval_snapshot",
]
