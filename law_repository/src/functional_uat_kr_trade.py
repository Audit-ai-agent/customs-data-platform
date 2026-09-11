"""Fail-closed local source builds for Korean trade guidance.

``functional-uat-kr-trade`` is isolated from the legacy three-law profile.  It
does not fetch from the internet or adopt an existing Data volume.  An operator
first stages bytes from a registry-approved official source below the ignored
profile, then explicitly builds a small, immutable local guidance projection.

The projection is deliberately *not* a hidden general web search or a full
three-store ingestion path.  It is sufficient only for the bounded
``/guidance/query`` surface: every returned excerpt must be present in a
locally hash-verified source file and every source/topic/direction must be in
the tracked registry.  Full HS Evidence retrieval remains owned by the
existing Data store profiles.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

from src.guidance_registry import (
    GuidanceTopic,
    REGISTRY_SHA256,
    REGISTRY_SOURCES_BY_ID,
    TradeDirection,
    active_sources_for_topic,
    guidance_topic_by_hint,
    resolve_guidance_topic,
)
from src.guidance_v3 import (
    GeneralGuidanceQueryRequestV3,
    GeneralGuidanceQueryResponseV3,
    GuidanceSourceV3,
)
from src.local_profiles import (
    FUNCTIONAL_UAT_KR_TRADE_PROFILE,
    ensure_functional_uat_kr_trade_profile,
    local_root,
)


KR_TRADE_LOCAL_STATE_VERSION = "functional-uat-kr-trade-local/v1"
KR_TRADE_BUILD_STATE_VERSION = "functional-uat-kr-trade-local/v2"
_ACTIVE_SNAPSHOT_FILENAME = "active-guidance-snapshot.json"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
_EXCERPT_MINIMUM_LENGTH = 20


class FunctionalUatKrTradeError(RuntimeError):
    """The broad local guidance snapshot is unavailable or unsafe."""


@dataclass(frozen=True)
class KrTradeSnapshotStatus:
    profile: Literal["functional-uat-kr-trade"]
    status: Literal["source_build_required", "registered_not_indexed", "ready"]
    registry_sha256: str
    source_count: int
    as_of: date | None
    snapshot_id: str | None = None
    record_count: int = 0
    coverage_count: int = 0


@dataclass(frozen=True)
class KrTradeSourceReceipt:
    """A local raw source bound to its registry URL by operator attestation."""

    source_id: str
    source_uri: str
    relative_path: str
    sha256: str
    collected_at: str
    topics: tuple[GuidanceTopic, ...]
    directions: tuple[Literal["import", "export"], ...]


@dataclass(frozen=True)
class KrTradeGuidanceRecord:
    """One bounded locally-derived excerpt that a guidance query may cite."""

    record_id: str
    source_id: str
    topic: GuidanceTopic
    direction: TradeDirection
    excerpt: str
    locator: str | None
    source_sha256: str


@dataclass(frozen=True)
class KrTradeGuidanceSnapshot:
    """Validated immutable local projection for the guidance-only endpoint."""

    snapshot_id: str
    as_of: date
    source_receipts: tuple[KrTradeSourceReceipt, ...]
    records: tuple[KrTradeGuidanceRecord, ...]
    coverage: tuple[tuple[GuidanceTopic, TradeDirection, tuple[str, ...]], ...]


def _active_snapshot_path(profile: Path) -> Path:
    return profile / "state" / _ACTIVE_SNAPSHOT_FILENAME


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    if path.exists() or path.is_symlink():
        raise FunctionalUatKrTradeError("KR_TRADE_ACTIVE_SNAPSHOT_EXISTS")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _replace_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically update the active pointer after an immutable build exists."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _snapshot_manifest_path(profile: Path, snapshot_id: str) -> Path:
    if _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    directory = profile / "state" / "guidance-snapshots" / snapshot_id
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return directory / "manifest.json"


def _active_pointer_payload(snapshot_id: str) -> dict[str, object]:
    return {
        "local_state_version": KR_TRADE_BUILD_STATE_VERSION,
        "profile": FUNCTIONAL_UAT_KR_TRADE_PROFILE,
        "snapshot_id": snapshot_id,
        "manifest_relative_path": f"state/guidance-snapshots/{snapshot_id}/manifest.json",
    }


def _safe_profile_file(profile: Path, relative: str) -> Path:
    # Receipts written on Windows carry backslash separators; resolve them
    # identically on POSIX so a snapshot built on one platform verifies on another.
    value = Path(relative.replace("\\", "/"))
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
        raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_PATH_INVALID")
    candidate = profile.joinpath(*value.parts)
    current = profile
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_PATH_INVALID")
    try:
        candidate.resolve(strict=False).relative_to(profile.resolve())
    except ValueError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_PATH_INVALID") from exc
    return candidate


def _validated_receipts(profile: Path, receipts: object) -> int:
    if not isinstance(receipts, list) or not receipts:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    seen: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {
            "source_id",
            "source_uri",
            "relative_path",
            "sha256",
            "collected_at",
        }:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        source_id = receipt["source_id"]
        source_uri = receipt["source_uri"]
        relative_path = receipt["relative_path"]
        expected_hash = receipt["sha256"]
        collected_at = receipt["collected_at"]
        if (
            not isinstance(source_id, str)
            or source_id not in REGISTRY_SOURCES_BY_ID
            or source_id in seen
            or REGISTRY_SOURCES_BY_ID[source_id].availability != "requires_kr_trade_snapshot"
            or not isinstance(source_uri, str)
            or source_uri != REGISTRY_SOURCES_BY_ID[source_id].official_uri
            or not isinstance(relative_path, str)
            or not isinstance(expected_hash, str)
            or _SHA256.fullmatch(expected_hash) is None
            or not isinstance(collected_at, str)
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        try:
            collected = datetime.fromisoformat(collected_at)
        except ValueError as exc:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
        if collected.tzinfo is None or collected.utcoffset() is None:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        source_path = _safe_profile_file(profile, relative_path)
        try:
            actual_hash = sha256(source_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_UNAVAILABLE") from exc
        if actual_hash != expected_hash:
            raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_HASH_MISMATCH")
        seen.add(source_id)
    return len(seen)


def _parse_aware_datetime(value: object) -> str:
    if not isinstance(value, str):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return parsed.isoformat()


def _validated_topic_values(value: object) -> tuple[GuidanceTopic, ...]:
    if not isinstance(value, list) or not value:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    allowed = {
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
    if any(not isinstance(item, str) or item not in allowed for item in value):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    if len(value) != len(set(value)):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return tuple(cast(GuidanceTopic, item) for item in value)


def _validated_source_directions(
    value: object,
) -> tuple[Literal["import", "export"], ...]:
    if not isinstance(value, list) or not value:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    if any(item not in {"import", "export"} for item in value) or len(value) != len(set(value)):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return tuple(cast(Literal["import", "export"], item) for item in value)


def _read_and_hash_source(profile: Path, relative_path: str, expected_hash: str) -> bytes:
    if _SHA256.fullmatch(expected_hash) is None:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    source_path = _safe_profile_file(profile, relative_path)
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_UNAVAILABLE") from exc
    if sha256(raw).hexdigest() != expected_hash:
        raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_HASH_MISMATCH")
    return raw


def _validated_build_receipts(profile: Path, receipts: object) -> tuple[KrTradeSourceReceipt, ...]:
    """Validate hash-bound source receipts for the queryable v2 projection."""

    if not isinstance(receipts, list) or not receipts:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    parsed: list[KrTradeSourceReceipt] = []
    seen: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {
            "source_id",
            "source_uri",
            "relative_path",
            "sha256",
            "collected_at",
            "topics",
            "directions",
        }:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        source_id = receipt["source_id"]
        source_uri = receipt["source_uri"]
        relative_path = receipt["relative_path"]
        expected_hash = receipt["sha256"]
        if (
            not isinstance(source_id, str)
            or source_id not in REGISTRY_SOURCES_BY_ID
            or source_id in seen
            or REGISTRY_SOURCES_BY_ID[source_id].availability != "requires_kr_trade_snapshot"
            or not isinstance(source_uri, str)
            or not isinstance(relative_path, str)
            or not isinstance(expected_hash, str)
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        topics = _validated_topic_values(receipt["topics"])
        directions = _validated_source_directions(receipt["directions"])
        source = REGISTRY_SOURCES_BY_ID[source_id]
        if (
            source_uri != source.official_uri
            or not set(topics) <= set(source.topics)
            or not set(directions) <= set(source.directions)
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        _read_and_hash_source(profile, relative_path, expected_hash)
        parsed.append(
            KrTradeSourceReceipt(
                source_id=source_id,
                source_uri=source_uri,
                relative_path=relative_path,
                sha256=expected_hash,
                collected_at=_parse_aware_datetime(receipt["collected_at"]),
                topics=topics,
                directions=directions,
            )
        )
        seen.add(source_id)
    return tuple(parsed)


def _record_direction(value: object) -> TradeDirection:
    if value not in {"import", "export", "both"}:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return cast(TradeDirection, value)


def _direction_supported(declared: TradeDirection, requested: TradeDirection) -> bool:
    if requested == "both":
        return declared in {"import", "export", "both"}
    return declared in {requested, "both"}


def _source_text_for_receipt(profile: Path, receipt: KrTradeSourceReceipt) -> str:
    raw = _read_and_hash_source(profile, receipt.relative_path, receipt.sha256)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_TEXT_UNAVAILABLE") from exc


def _validated_records(
    profile: Path,
    receipts: tuple[KrTradeSourceReceipt, ...],
    records: object,
) -> tuple[KrTradeGuidanceRecord, ...]:
    """Validate snippets against locally staged raw source bytes.

    A source build may derive excerpts externally, but it cannot add an
    untraceable statement: after whitespace normalisation every excerpt must
    occur in the corresponding raw text.  This keeps the API bounded to
    content the operator actually staged and hash-checked.
    """

    if not isinstance(records, list) or not records:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    receipts_by_source = {receipt.source_id: receipt for receipt in receipts}
    source_text = {
        source_id: _normalized_text(_source_text_for_receipt(profile, receipt))
        for source_id, receipt in receipts_by_source.items()
    }
    parsed: list[KrTradeGuidanceRecord] = []
    seen: set[str] = set()
    expected_keys = {
        frozenset({"source_id", "topic", "direction", "excerpt"}),
        frozenset({"source_id", "topic", "direction", "excerpt", "locator"}),
    }
    for record in records:
        if not isinstance(record, dict) or frozenset(record) not in expected_keys:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        source_id = record["source_id"]
        topic = record["topic"]
        direction = _record_direction(record["direction"])
        excerpt = record["excerpt"]
        locator = record.get("locator")
        if (
            not isinstance(source_id, str)
            or source_id not in receipts_by_source
            or not isinstance(topic, str)
            or not isinstance(excerpt, str)
            or not isinstance(locator, str | None)
            or len(excerpt) > 2_000
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        normalized_excerpt = _normalized_text(excerpt)
        if len(normalized_excerpt) < _EXCERPT_MINIMUM_LENGTH:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        receipt = receipts_by_source[source_id]
        if (
            topic not in receipt.topics
            or (direction == "both" and set(receipt.directions) != {"import", "export"})
            or (direction != "both" and direction not in receipt.directions)
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        source = REGISTRY_SOURCES_BY_ID[source_id]
        if topic not in source.topics or (
            direction == "both" and set(receipt.directions) != {"import", "export"}
        ):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        if normalized_excerpt not in source_text[source_id]:
            raise FunctionalUatKrTradeError("KR_TRADE_EXCERPT_NOT_IN_SOURCE")
        identity = sha256(
            "\x1f".join((source_id, topic, direction, receipt.sha256, normalized_excerpt)).encode(
                "utf-8"
            )
        ).hexdigest()
        if identity in seen:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        parsed.append(
            KrTradeGuidanceRecord(
                record_id="record_" + identity,
                source_id=source_id,
                topic=cast(GuidanceTopic, topic),
                direction=direction,
                excerpt=normalized_excerpt,
                locator=locator,
                source_sha256=receipt.sha256,
            )
        )
        seen.add(identity)
    return tuple(parsed)


def _coverage_from_records(
    records: tuple[KrTradeGuidanceRecord, ...],
) -> tuple[tuple[GuidanceTopic, TradeDirection, tuple[str, ...]], ...]:
    grouped: dict[tuple[GuidanceTopic, TradeDirection], set[str]] = {}
    for record in records:
        directions: tuple[TradeDirection, ...] = (
            ("import", "export", "both") if record.direction == "both" else (record.direction,)
        )
        for direction in directions:
            grouped.setdefault((record.topic, direction), set()).add(record.source_id)
    return tuple(
        (topic, direction, tuple(sorted(source_ids)))
        for (topic, direction), source_ids in sorted(grouped.items())
    )


def _receipt_payload(receipt: KrTradeSourceReceipt) -> dict[str, object]:
    return {
        "source_id": receipt.source_id,
        "source_uri": receipt.source_uri,
        "relative_path": receipt.relative_path,
        "sha256": receipt.sha256,
        "collected_at": receipt.collected_at,
        "topics": list(receipt.topics),
        "directions": list(receipt.directions),
    }


def _record_payload(record: KrTradeGuidanceRecord) -> dict[str, object]:
    value: dict[str, object] = {
        "record_id": record.record_id,
        "source_id": record.source_id,
        "topic": record.topic,
        "direction": record.direction,
        "excerpt": record.excerpt,
        "source_sha256": record.source_sha256,
    }
    if record.locator is not None:
        value["locator"] = record.locator
    return value


def _coverage_payload(
    coverage: tuple[tuple[GuidanceTopic, TradeDirection, tuple[str, ...]], ...],
) -> list[dict[str, object]]:
    return [
        {"topic": topic, "direction": direction, "source_ids": list(source_ids)}
        for topic, direction, source_ids in coverage
    ]


def _build_manifest_payload(
    *,
    snapshot_id: str,
    as_of: date,
    receipts: tuple[KrTradeSourceReceipt, ...],
    records: tuple[KrTradeGuidanceRecord, ...],
    built_at: str,
) -> dict[str, object]:
    coverage = _coverage_from_records(records)
    return {
        "local_state_version": KR_TRADE_BUILD_STATE_VERSION,
        "profile": FUNCTIONAL_UAT_KR_TRADE_PROFILE,
        "snapshot_id": snapshot_id,
        "registry_sha256": REGISTRY_SHA256,
        "as_of": as_of.isoformat(),
        "built_at": built_at,
        "source_receipts": [_receipt_payload(receipt) for receipt in receipts],
        "guidance_records": [_record_payload(record) for record in records],
        "coverage": _coverage_payload(coverage),
        "retrieval_ready": True,
    }


def _parse_ready_manifest(profile: Path, payload: object) -> KrTradeGuidanceSnapshot:
    if not isinstance(payload, dict) or set(payload) != {
        "local_state_version",
        "profile",
        "snapshot_id",
        "registry_sha256",
        "as_of",
        "built_at",
        "source_receipts",
        "guidance_records",
        "coverage",
        "retrieval_ready",
    }:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    snapshot_id = payload["snapshot_id"]
    if (
        payload["local_state_version"] != KR_TRADE_BUILD_STATE_VERSION
        or payload["profile"] != FUNCTIONAL_UAT_KR_TRADE_PROFILE
        or not isinstance(snapshot_id, str)
        or _SNAPSHOT_ID.fullmatch(snapshot_id) is None
        or payload["registry_sha256"] != REGISTRY_SHA256
        or payload["retrieval_ready"] is not True
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        as_of = date.fromisoformat(str(payload["as_of"]))
    except (TypeError, ValueError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    _parse_aware_datetime(payload["built_at"])
    receipts = _validated_build_receipts(profile, payload["source_receipts"])
    raw_records = payload["guidance_records"]
    if not isinstance(raw_records, list):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    # Rebuild the input form so the same traceability checks are used for both
    # initial build and every serve/query preflight.  The record ID and source
    # hash are derived values, never operator-controlled evidence.
    input_records: list[dict[str, object]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        allowed = frozenset(
            {
                "record_id",
                "source_id",
                "topic",
                "direction",
                "excerpt",
                "source_sha256",
            }
        )
        if frozenset(raw_record) not in {allowed, allowed | {"locator"}}:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        source_id = raw_record.get("source_id")
        source_sha256 = raw_record.get("source_sha256")
        if not isinstance(source_id, str) or not isinstance(source_sha256, str):
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        receipt = next((item for item in receipts if item.source_id == source_id), None)
        if receipt is None or receipt.sha256 != source_sha256:
            raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
        item = {
            key: raw_record[key]
            for key in ("source_id", "topic", "direction", "excerpt")
            if key in raw_record
        }
        if "locator" in raw_record:
            item["locator"] = raw_record["locator"]
        input_records.append(item)
    records = _validated_records(profile, receipts, input_records)
    expected_record_ids = {record.record_id for record in records}
    received_record_ids = {
        raw_record.get("record_id") for raw_record in raw_records if isinstance(raw_record, dict)
    }
    if expected_record_ids != received_record_ids:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    expected_coverage = _coverage_payload(_coverage_from_records(records))
    if payload["coverage"] != expected_coverage:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return KrTradeGuidanceSnapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        source_receipts=receipts,
        records=records,
        coverage=_coverage_from_records(records),
    )


def _read_active_ready_snapshot(profile: Path) -> KrTradeGuidanceSnapshot:
    pointer_path = _active_snapshot_path(profile)
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    if not isinstance(pointer, dict) or set(pointer) != {
        "local_state_version",
        "profile",
        "snapshot_id",
        "manifest_relative_path",
    }:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    if (
        pointer["local_state_version"] != KR_TRADE_BUILD_STATE_VERSION
        or pointer["profile"] != FUNCTIONAL_UAT_KR_TRADE_PROFILE
        or not isinstance(pointer["snapshot_id"], str)
        or _SNAPSHOT_ID.fullmatch(pointer["snapshot_id"]) is None
        or pointer["manifest_relative_path"]
        != f"state/guidance-snapshots/{pointer['snapshot_id']}/manifest.json"
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    manifest_path = _snapshot_manifest_path(profile, pointer["snapshot_id"])
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    snapshot = _parse_ready_manifest(profile, manifest)
    if snapshot.snapshot_id != pointer["snapshot_id"]:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    return snapshot


def _read_registered_snapshot(profile: Path) -> KrTradeSnapshotStatus:
    path = _active_snapshot_path(profile)
    if path.is_symlink() or not path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "local_state_version",
        "profile",
        "registry_sha256",
        "as_of",
        "built_at",
        "source_receipts",
        "retrieval_ready",
    }:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    if (
        payload["local_state_version"] != KR_TRADE_LOCAL_STATE_VERSION
        or payload["profile"] != FUNCTIONAL_UAT_KR_TRADE_PROFILE
        or payload["registry_sha256"] != REGISTRY_SHA256
        or payload["retrieval_ready"] is not False
    ):
        # This revision has no multi-source ingestion projection.  A marker
        # cannot self-declare it ready merely because raw files were staged.
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        as_of = date.fromisoformat(str(payload["as_of"]))
        built_at = datetime.fromisoformat(str(payload["built_at"]))
    except (TypeError, ValueError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    if built_at.tzinfo is None or built_at.utcoffset() is None:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    receipt_count = _validated_receipts(profile, payload["source_receipts"])
    return KrTradeSnapshotStatus(
        profile=cast(Literal["functional-uat-kr-trade"], FUNCTIONAL_UAT_KR_TRADE_PROFILE),
        status="registered_not_indexed",
        registry_sha256=REGISTRY_SHA256,
        source_count=receipt_count,
        as_of=as_of,
    )


def inspect_kr_trade_snapshot(
    *,
    local_root_override: Path | None = None,
) -> KrTradeSnapshotStatus:
    """Return a safe status without starting stores or collecting material."""

    profile = local_root(local_root_override) / "profiles" / FUNCTIONAL_UAT_KR_TRADE_PROFILE
    if not profile.exists():
        return KrTradeSnapshotStatus(
            profile=cast(Literal["functional-uat-kr-trade"], FUNCTIONAL_UAT_KR_TRADE_PROFILE),
            status="source_build_required",
            registry_sha256=REGISTRY_SHA256,
            source_count=0,
            as_of=None,
        )
    if profile.is_symlink() or not profile.is_dir():
        raise FunctionalUatKrTradeError("KR_TRADE_PROFILE_UNAVAILABLE")
    path = _active_snapshot_path(profile)
    if not path.exists():
        return KrTradeSnapshotStatus(
            profile=cast(Literal["functional-uat-kr-trade"], FUNCTIONAL_UAT_KR_TRADE_PROFILE),
            status="source_build_required",
            registry_sha256=REGISTRY_SHA256,
            source_count=0,
            as_of=None,
        )
    if path.is_symlink() or not path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_SNAPSHOT_INVALID") from exc
    if (
        isinstance(marker, dict)
        and marker.get("local_state_version") == KR_TRADE_BUILD_STATE_VERSION
    ):
        snapshot = _read_active_ready_snapshot(profile)
        return KrTradeSnapshotStatus(
            profile=cast(Literal["functional-uat-kr-trade"], FUNCTIONAL_UAT_KR_TRADE_PROFILE),
            status="ready",
            registry_sha256=REGISTRY_SHA256,
            source_count=len(snapshot.source_receipts),
            as_of=snapshot.as_of,
            snapshot_id=snapshot.snapshot_id,
            record_count=len(snapshot.records),
            coverage_count=len(snapshot.coverage),
        )
    return _read_registered_snapshot(profile)


def require_kr_trade_retrieval_snapshot(
    *,
    local_root_override: Path | None = None,
) -> KrTradeGuidanceSnapshot:
    """Return a verified guidance projection or fail before serving any query."""

    status = inspect_kr_trade_snapshot(local_root_override=local_root_override)
    if status.status == "source_build_required":
        raise FunctionalUatKrTradeError("KR_TRADE_SOURCE_BUILD_REQUIRED")
    if status.status == "registered_not_indexed":
        raise FunctionalUatKrTradeError("KR_TRADE_RETRIEVAL_PROJECTION_REQUIRED")
    profile = local_root(local_root_override) / "profiles" / FUNCTIONAL_UAT_KR_TRADE_PROFILE
    return _read_active_ready_snapshot(profile)


def _input_snapshot_id(value: object, *, as_of: date, payload: object) -> str:
    if value is None:
        digest = sha256(_canonical_json_bytes(payload)).hexdigest()[:12]
        return f"krtrade-{as_of.strftime('%Y%m%d')}-{digest}"
    if not isinstance(value, str) or _SNAPSHOT_ID.fullmatch(value) is None:
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID")
    return value


def build_kr_trade_guidance_snapshot(
    *,
    staged_manifest_relative: str,
    local_root_override: Path | None = None,
) -> KrTradeSnapshotStatus:
    """Build an immutable, local-only guidance projection from staged bytes.

    This command performs no network request, starts no Docker container, and
    does not change the legacy ``functional-uat-3law`` profile.  Its input is
    a small manifest created by the operator after collecting only registry
    approved official sources.  The raw source bytes and the resulting local
    projection remain below ``_local/profiles/functional-uat-kr-trade``.
    """

    profile = ensure_functional_uat_kr_trade_profile(root=local_root_override)
    staged_path = _safe_profile_file(profile, staged_manifest_relative)
    if staged_path.is_symlink() or not staged_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_UNAVAILABLE")
    try:
        staged = json.loads(staged_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID") from exc
    expected_keys = {
        frozenset({"schema_version", "as_of", "source_receipts", "guidance_records"}),
        frozenset(
            {"schema_version", "snapshot_id", "as_of", "source_receipts", "guidance_records"}
        ),
    }
    if (
        not isinstance(staged, dict)
        or frozenset(staged) not in expected_keys
        or staged.get("schema_version") != "functional-uat-kr-trade-source-build-input/v1"
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID")
    try:
        as_of = date.fromisoformat(str(staged["as_of"]))
    except (TypeError, ValueError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID") from exc
    snapshot_id = _input_snapshot_id(staged.get("snapshot_id"), as_of=as_of, payload=staged)
    receipts = _validated_build_receipts(profile, staged["source_receipts"])
    records = _validated_records(profile, receipts, staged["guidance_records"])
    manifest = _build_manifest_payload(
        snapshot_id=snapshot_id,
        as_of=as_of,
        receipts=receipts,
        records=records,
        built_at=datetime.now(timezone.utc).isoformat(),
    )
    immutable_path = _snapshot_manifest_path(profile, snapshot_id)
    immutable_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(immutable_path, manifest)
    _replace_json(_active_snapshot_path(profile), _active_pointer_payload(snapshot_id))
    return inspect_kr_trade_snapshot(local_root_override=local_root_override)


def _guidance_source_id(snapshot_id: str, record: KrTradeGuidanceRecord) -> str:
    digest = sha256(
        "\x1f".join(
            (
                snapshot_id,
                record.record_id,
                record.source_id,
                record.topic,
                record.direction,
                record.source_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    return "guide_" + digest


def query_kr_trade_guidance_v3(
    request: GeneralGuidanceQueryRequestV3,
    *,
    local_root_override: Path | None = None,
) -> GeneralGuidanceQueryResponseV3:
    """Answer one registry-bounded guidance query from a verified local build.

    The source-build module only uses the versioned request/response models;
    it does not import or operate a Data store directly.
    """

    from src.evidence import EvidenceError

    snapshot = require_kr_trade_retrieval_snapshot(local_root_override=local_root_override)
    if request.as_of != snapshot.as_of:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="failed",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="unavailable",
            topic=None,
            errors=[
                EvidenceError(
                    code="UNSUPPORTED_GUIDANCE_AS_OF",
                    message="현재 공식 안내 스냅샷은 요청한 기준일을 지원하지 않습니다.",
                    retryable=False,
                )
            ],
        )
    entry = (
        guidance_topic_by_hint(request.topic_hint, direction=request.trade_direction)
        if request.topic_hint is not None
        else resolve_guidance_topic(request.query, direction=request.trade_direction)
    )
    if entry is None:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="no_coverage",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="no_coverage",
            topic=None,
        )
    records = tuple(
        record
        for record in snapshot.records
        if record.topic == entry.topic
        and _direction_supported(record.direction, request.trade_direction)
    )
    if not records:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="no_coverage",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="no_coverage",
            topic=entry.topic,
        )
    sources = [
        GuidanceSourceV3(
            source_id=_guidance_source_id(snapshot.snapshot_id, record),
            # The concrete guidance record has a hash-derived ID, while this
            # stable family ID is the registry entry whose URI, topic, and
            # direction were verified during the local source build.
            registry_source_id=record.source_id,
            source_kind=REGISTRY_SOURCES_BY_ID[record.source_id].source_kind,
            title=REGISTRY_SOURCES_BY_ID[record.source_id].title,
            uri=REGISTRY_SOURCES_BY_ID[record.source_id].official_uri,
            topic=entry.topic,
            direction=record.direction,
            excerpt=record.excerpt,
            version_hash=record.source_sha256,
            retrieval_methods=["registry"],
        )
        for record in sorted(records, key=lambda item: (item.source_id, item.record_id))[
            : request.top_k
        ]
    ]
    # Coverage is a statement about the built snapshot, not a claim that every
    # registry source was downloaded.  If a compatible registered source is
    # absent from this immutable build, callers receive the useful excerpts
    # together with an explicit partial-coverage classification.
    required_source_ids = active_sources_for_topic(
        entry,
        direction=request.trade_direction,
        active_source_ids=frozenset(
            source_id
            for source_id in entry.source_ids
            if REGISTRY_SOURCES_BY_ID[source_id].availability == "requires_kr_trade_snapshot"
        ),
    )
    present_source_ids = {record.source_id for record in records}
    if present_source_ids < required_source_ids:
        return GeneralGuidanceQueryResponseV3(
            request_id=request.request_id,
            status="partial",
            snapshot_id=snapshot.snapshot_id,
            as_of=request.as_of,
            coverage="partial",
            topic=entry.topic,
            results=sources,
            errors=[
                EvidenceError(
                    code="GUIDANCE_SNAPSHOT_PARTIAL_COVERAGE",
                    message="해당 주제의 등록 공식 출처 중 일부만 현재 로컬 스냅샷에 포함되어 있습니다.",
                    retryable=False,
                )
            ],
        )
    return GeneralGuidanceQueryResponseV3(
        request_id=request.request_id,
        status="covered",
        snapshot_id=snapshot.snapshot_id,
        as_of=request.as_of,
        coverage="covered",
        topic=entry.topic,
        results=sources,
    )


def register_kr_trade_source_receipts(
    *,
    staged_manifest_relative: str,
    local_root_override: Path | None = None,
) -> KrTradeSnapshotStatus:
    """Register already-staged source bytes without collecting or indexing them.

    The staged manifest is intentionally constrained to the ignored profile's
    ``data/guidance-sources`` directory.  It must contain only a date and
    receipt metadata; raw material stays in the referenced local files.  This
    legacy compatibility command deliberately remains
    ``retrieval_ready=false``.  Operators must use the v2 ``build`` input to
    create the traceable guidance projection required for serving.
    """

    profile = ensure_functional_uat_kr_trade_profile(root=local_root_override)
    manifest_path = _safe_profile_file(profile, staged_manifest_relative)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_UNAVAILABLE")
    try:
        staged = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID") from exc
    if (
        not isinstance(staged, dict)
        or set(staged)
        != {
            "schema_version",
            "as_of",
            "source_receipts",
        }
        or staged["schema_version"] != "functional-uat-kr-trade-source-input/v1"
    ):
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID")
    try:
        as_of = date.fromisoformat(str(staged["as_of"]))
    except (TypeError, ValueError) as exc:
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID") from exc
    receipts = staged["source_receipts"]
    _validated_receipts(profile, receipts)
    if not isinstance(receipts, list):  # narrowed above; keeps the marker typed safely
        raise FunctionalUatKrTradeError("KR_TRADE_STAGED_MANIFEST_INVALID")
    marker = {
        "local_state_version": KR_TRADE_LOCAL_STATE_VERSION,
        "profile": FUNCTIONAL_UAT_KR_TRADE_PROFILE,
        "registry_sha256": REGISTRY_SHA256,
        "as_of": as_of.isoformat(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_receipts": receipts,
        "retrieval_ready": False,
    }
    _atomic_json(_active_snapshot_path(profile), marker)
    return _read_registered_snapshot(profile)


__all__ = [
    "FUNCTIONAL_UAT_KR_TRADE_PROFILE",
    "FunctionalUatKrTradeError",
    "KrTradeGuidanceRecord",
    "KrTradeGuidanceSnapshot",
    "KR_TRADE_LOCAL_STATE_VERSION",
    "KR_TRADE_BUILD_STATE_VERSION",
    "KrTradeSnapshotStatus",
    "build_kr_trade_guidance_snapshot",
    "inspect_kr_trade_snapshot",
    "query_kr_trade_guidance_v3",
    "register_kr_trade_source_receipts",
    "require_kr_trade_retrieval_snapshot",
]
