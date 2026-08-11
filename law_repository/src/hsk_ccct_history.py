"""Verified local HSK/CCCT archive history.

The checked-in HSK and CCCT inputs remain the fixed 2026-08-05 functional
UAT contract snapshot.  This module is intentionally separate: an operator
may import a dated *official* archive only after all local receipt bytes pass
validation.  It has no network client, no credential handling, and never
uses the old current CCCT ledger as historical source material.

Raw archive material lives only below::

    _local/data/official-archives/hsk-ccct/YYYY-MM-DD/

Each directory contains the canonical ``archive.manifest.json`` and the
hash-bound HSK XLSX / CCCT query-receipt files named by that manifest.  An
import persists normalized historical rows in PostgreSQL without replacing
the current Neo4j projection.  A separately explicit active-pointer is what
allows a running law-history service to use an imported date.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, cast
from urllib.parse import parse_qsl, urlsplit

from src import repository

ARCHIVE_MANIFEST_SCHEMA = "hsk-ccct-official-archive/v1"
ACTIVE_POINTER_SCHEMA = "hsk-ccct-active-archives/v1"
ARCHIVE_MANIFEST_FILENAME = "archive.manifest.json"
ACTIVE_POINTER_FILENAME = "active-archives.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HSK_HOSTS = frozenset({"data.go.kr", "www.data.go.kr"})
_CCCT_HOSTS = frozenset({"apis.data.go.kr"})
_CCCT_PATH_PREFIX = "/1220000/retrieveCcctLworCd/"
_SECRET_QUERY_PART = re.compile(r"(?:secret|token|credential|apikey|api_key|servicekey)", re.I)


class HskCcctArchiveError(RuntimeError):
    """A historical HSK/CCCT archive is absent, malformed, or unverified."""


def canonical_json_bytes(value: object) -> bytes:
    """Canonical local receipt bytes; never include a platform-specific path."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class ArchiveAsset:
    relative_path: str
    official_uri: str
    sha256: str
    byte_size: int
    published_at: date
    effective_at: date


@dataclass(frozen=True, slots=True)
class CcctArchiveReceipt:
    official_uri: str
    query_receipts: ArchiveAsset
    snapshot_id: str
    query_count: int
    empty_query_count: int
    raw_row_count: int


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    archive_id: str
    archive_as_of: date
    collected_at: datetime
    manifest_sha256: str
    directory: Path
    level_names: ArchiveAsset
    leaf_attributes: ArchiveAsset
    ccct: CcctArchiveReceipt

    @property
    def hsk_snapshot_id(self) -> str:
        return f"hsk_{self.manifest_sha256}"


@dataclass(frozen=True, slots=True)
class LoadedArchive:
    manifest: ArchiveManifest
    hsk_nodes: tuple[dict[str, object], ...]
    hsk_projection_sha256: str
    ccct_dataset: Any


@dataclass(frozen=True, slots=True)
class ActiveArchiveCatalog:
    """One immutable snapshot of the local archive activation pointer."""

    pointer_sha256: str
    archives: tuple[ArchiveManifest, ...]

    def for_date(self, review_as_of: date) -> ArchiveManifest | None:
        for archive in self.archives:
            if archive.archive_as_of == review_as_of:
                return archive
        return None


@dataclass(frozen=True, slots=True)
class ImportedArchive:
    archive_id: str
    archive_as_of: date
    hsk_snapshot_id: str
    ccct_snapshot_id: str


def _local_root(override: Path | None = None) -> Path:
    from src.local_profiles import local_root

    return local_root(override)


def archive_root(*, local_root_override: Path | None = None) -> Path:
    """Return the designated raw-archive location without creating it."""

    return _local_root(local_root_override) / "data" / "official-archives" / "hsk-ccct"


def archive_directory(archive_as_of: date, *, local_root_override: Path | None = None) -> Path:
    return archive_root(local_root_override=local_root_override) / archive_as_of.isoformat()


def _assert_safe_directory(path: Path, *, require_exists: bool) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_PATH_UNSAFE")
    if require_exists and (not absolute.exists() or not absolute.is_dir() or absolute.is_symlink()):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_DIRECTORY_MISSING")
    return absolute


def _safe_asset_path(directory: Path, relative: str) -> Path:
    """Resolve one manifest-relative file without accepting Windows escapes."""

    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_PATH_INVALID")
    parsed = PurePosixPath(relative)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or re.match(r"^[A-Za-z]:", relative) is not None
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_PATH_INVALID")
    root = _assert_safe_directory(directory, require_exists=True)
    candidate = root.joinpath(*parsed.parts)
    current = root
    for part in parsed.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_PATH_INVALID")
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except ValueError as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_PATH_INVALID") from exc
    return candidate


def _parse_date(value: object, *, code: str) -> date:
    if not isinstance(value, str):
        raise HskCcctArchiveError(code)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HskCcctArchiveError(code) from exc


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    return parsed


def _official_uri(value: object, *, kind: str) -> str:
    if not isinstance(value, str) or not value:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    # ``SplitResult.port`` itself raises for strings such as ``:bad``.  A
    # malformed operator receipt is still a normal validation failure, never
    # an uncaught URL-parser exception that could escape a CLI/API boundary.
    try:
        port = parsed.port
    except ValueError as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID") from exc
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID")
    query_names = {name.lower() for name, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(_SECRET_QUERY_PART.search(name) for name in query_names):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID")
    if kind == "hsk":
        if host not in _HSK_HOSTS or not parsed.path.startswith("/data/"):
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID")
    elif kind == "ccct":
        if host not in _CCCT_HOSTS or not parsed.path.startswith(_CCCT_PATH_PREFIX) or parsed.query:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID")
    else:  # pragma: no cover - internal misuse guard
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_OFFICIAL_URI_INVALID")
    return value


def _asset(value: object, *, kind: str) -> ArchiveAsset:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "official_uri",
        "sha256",
        "byte_size",
        "published_at",
        "effective_at",
    }:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    relative_path = value["path"]
    digest = value["sha256"]
    byte_size = value["byte_size"]
    if (
        not isinstance(relative_path, str)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or type(byte_size) is not int
        or byte_size < 1
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    return ArchiveAsset(
        relative_path=relative_path,
        official_uri=_official_uri(value["official_uri"], kind=kind),
        sha256=digest,
        byte_size=byte_size,
        published_at=_parse_date(value["published_at"], code="HSK_CCCT_ARCHIVE_MANIFEST_INVALID"),
        effective_at=_parse_date(value["effective_at"], code="HSK_CCCT_ARCHIVE_MANIFEST_INVALID"),
    )


def _local_receipt_asset(value: object) -> ArchiveAsset:
    """Validate a hash-bound local receipt file without inventing an URI."""

    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "sha256",
        "byte_size",
        "published_at",
        "effective_at",
    }:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    relative_path = value["path"]
    digest = value["sha256"]
    byte_size = value["byte_size"]
    if (
        not isinstance(relative_path, str)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or type(byte_size) is not int
        or byte_size < 1
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    return ArchiveAsset(
        relative_path=relative_path,
        official_uri="",
        sha256=digest,
        byte_size=byte_size,
        published_at=_parse_date(value["published_at"], code="HSK_CCCT_ARCHIVE_MANIFEST_INVALID"),
        effective_at=_parse_date(value["effective_at"], code="HSK_CCCT_ARCHIVE_MANIFEST_INVALID"),
    )


def _read_asset(directory: Path, asset: ArchiveAsset) -> bytes:
    path = _safe_asset_path(directory, asset.relative_path)
    if not path.is_file() or path.is_symlink():
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_MISSING")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_MISSING") from exc
    if len(raw) != asset.byte_size or sha256_bytes(raw) != asset.sha256:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_ASSET_HASH_MISMATCH")
    return raw


def _read_manifest_bytes(directory: Path) -> tuple[dict[str, object], str]:
    path = _safe_asset_path(directory, ARCHIVE_MANIFEST_FILENAME)
    if not path.is_file() or path.is_symlink():
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_MISSING")
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    return cast(dict[str, object], value), sha256_bytes(raw)


def _archive_id(archive_as_of: date, manifest_sha256: str) -> str:
    return f"hsk_ccct_{sha256_bytes(canonical_json_bytes({'as_of': archive_as_of.isoformat(), 'manifest': manifest_sha256}))}"


def load_archive_manifest(
    directory: Path,
    *,
    verify_assets: bool = True,
) -> ArchiveManifest:
    """Validate a date directory and its safe, official-source manifest."""

    root = _assert_safe_directory(directory, require_exists=True)
    value, manifest_sha256 = _read_manifest_bytes(root)
    if set(value) != {"schema_version", "archive_as_of", "collected_at", "hsk", "ccct"}:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    if value["schema_version"] != ARCHIVE_MANIFEST_SCHEMA:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    archive_as_of = _parse_date(value["archive_as_of"], code="HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    if archive_as_of < date(2026, 1, 1):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_DATE_PROVENANCE_INVALID")
    if root.name != archive_as_of.isoformat():
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_DIRECTORY_DATE_MISMATCH")
    hsk = value["hsk"]
    if not isinstance(hsk, Mapping) or set(hsk) != {"level_names", "leaf_attributes"}:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    level_names = _asset(hsk["level_names"], kind="hsk")
    leaf_attributes = _asset(hsk["leaf_attributes"], kind="hsk")
    if level_names.relative_path == leaf_attributes.relative_path:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    ccct = value["ccct"]
    if not isinstance(ccct, Mapping) or set(ccct) != {
        "official_uri",
        "query_receipts",
        "snapshot_id",
        "query_count",
        "empty_query_count",
        "raw_row_count",
    }:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    snapshot_id = ccct["snapshot_id"]
    counts = (ccct["query_count"], ccct["empty_query_count"], ccct["raw_row_count"])
    if (
        not isinstance(snapshot_id, str)
        or re.fullmatch(r"ccct_[0-9a-f]{64}", snapshot_id) is None
        or any(type(count) is not int or count < 0 for count in counts)
        or cast(int, ccct["empty_query_count"]) > cast(int, ccct["query_count"])
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    receipt_asset = _local_receipt_asset(ccct["query_receipts"])
    # A receipt is a local file, not a data.go.kr file. Bind its byte receipt
    # to the separately verified CCCT official source URI.
    receipt_asset = ArchiveAsset(
        relative_path=receipt_asset.relative_path,
        official_uri=_official_uri(ccct["official_uri"], kind="ccct"),
        sha256=receipt_asset.sha256,
        byte_size=receipt_asset.byte_size,
        published_at=receipt_asset.published_at,
        effective_at=receipt_asset.effective_at,
    )
    if receipt_asset.relative_path in {level_names.relative_path, leaf_attributes.relative_path}:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_MANIFEST_INVALID")
    manifest = ArchiveManifest(
        archive_id=_archive_id(archive_as_of, manifest_sha256),
        archive_as_of=archive_as_of,
        collected_at=_parse_datetime(value["collected_at"]),
        manifest_sha256=manifest_sha256,
        directory=root,
        level_names=level_names,
        leaf_attributes=leaf_attributes,
        ccct=CcctArchiveReceipt(
            official_uri=receipt_asset.official_uri,
            query_receipts=receipt_asset,
            snapshot_id=snapshot_id,
            query_count=cast(int, ccct["query_count"]),
            empty_query_count=cast(int, ccct["empty_query_count"]),
            raw_row_count=cast(int, ccct["raw_row_count"]),
        ),
    )
    if (
        manifest.level_names.published_at > archive_as_of
        or manifest.leaf_attributes.published_at > archive_as_of
        or manifest.ccct.query_receipts.published_at > archive_as_of
        or manifest.level_names.effective_at > archive_as_of
        or manifest.leaf_attributes.effective_at > archive_as_of
        or manifest.ccct.query_receipts.effective_at > archive_as_of
        or manifest.collected_at.date() < archive_as_of
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_DATE_PROVENANCE_INVALID")
    if verify_assets:
        _read_asset(root, level_names)
        _read_asset(root, leaf_attributes)
        _read_asset(root, receipt_asset)
    return manifest


def _receipt_records(raw: bytes, manifest: ArchiveManifest) -> list[dict[str, object]]:
    """Validate all query/page receipts before flattening them for CCCT ingest."""

    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID") from exc
    if not lines:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
    records: list[dict[str, object]] = []
    keys: set[tuple[str, str]] = set()
    empty_count = 0
    raw_row_count = 0
    for line in lines:
        if not line:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID") from exc
        if not isinstance(receipt, dict) or set(receipt) != {
            "hs",
            "tpcd",
            "total_count",
            "complete",
            "pages",
        }:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
        hsk10 = receipt["hs"]
        tpcd = receipt["tpcd"]
        total_count = receipt["total_count"]
        pages = receipt["pages"]
        if (
            not isinstance(hsk10, str)
            or re.fullmatch(r"[0-9]{10}", hsk10) is None
            or tpcd not in {"1", "2"}
            or type(total_count) is not int
            or total_count < 0
            or receipt["complete"] is not True
            or not isinstance(pages, list)
            or not pages
        ):
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
        key = (hsk10, cast(str, tpcd))
        if key in keys:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
        keys.add(key)
        flattened: list[dict[str, object]] = []
        for number, page in enumerate(pages, start=1):
            if not isinstance(page, dict) or set(page) != {"page", "items"}:
                raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
            if page["page"] != number or not isinstance(page["items"], list):
                raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
            for item in cast(list[object], page["items"]):
                if not isinstance(item, dict):
                    raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_RECEIPT_INVALID")
                flattened.append(cast(dict[str, object], item))
        if len(flattened) != total_count:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_PAGINATION_INCOMPLETE")
        if total_count == 0:
            empty_count += 1
        raw_row_count += len(flattened)
        records.append({"hs": hsk10, "tpcd": tpcd, "items": flattened})
    if (
        len(records) != manifest.ccct.query_count
        or empty_count != manifest.ccct.empty_query_count
        or raw_row_count != manifest.ccct.raw_row_count
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_COVERAGE_MISMATCH")
    return records


def load_archive(directory: Path) -> LoadedArchive:
    """Fully validate raw HSK and CCCT archive material for a single import."""

    manifest = load_archive_manifest(directory, verify_assets=True)
    try:
        from src.ingest import ccct, hsk

        levels_path = _safe_asset_path(manifest.directory, manifest.level_names.relative_path)
        attrs_path = _safe_asset_path(manifest.directory, manifest.leaf_attributes.relative_path)
        nodes = tuple(
            dict(row)
            for row in hsk.build_nodes(
                hsk.load_level_names(levels_path), hsk.load_leaf_attrs(attrs_path)
            )
        )
        if not nodes or not any(node.get("is_leaf") is True for node in nodes):
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_HSK_SEMANTICS_INVALID")
        receipts = _read_asset(manifest.directory, manifest.ccct.query_receipts)
        dataset = ccct.build_dataset(_receipt_records(receipts, manifest))
    except HskCcctArchiveError:
        raise
    except Exception as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_SEMANTICS_INVALID") from exc
    if (
        dataset.snapshot_id != manifest.ccct.snapshot_id
        or len(dataset.queries) != manifest.ccct.query_count
        or len(dataset.source_records) != manifest.ccct.raw_row_count
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_CCCT_SEMANTICS_MISMATCH")
    projection = tuple(sorted(nodes, key=lambda row: str(row["code"])))
    return LoadedArchive(
        manifest=manifest,
        hsk_nodes=nodes,
        hsk_projection_sha256=sha256_bytes(canonical_json_bytes(projection)),
        ccct_dataset=dataset,
    )


def _node_rows(loaded: LoadedArchive) -> Iterable[dict[str, object]]:
    for node in loaded.hsk_nodes:
        row = dict(node)
        row["version_hash"] = sha256_bytes(canonical_json_bytes(node))
        yield row


def _assert_designated_archive_directory(
    manifest: ArchiveManifest, *, local_root_override: Path | None
) -> None:
    expected = _assert_safe_directory(
        archive_directory(manifest.archive_as_of, local_root_override=local_root_override),
        require_exists=True,
    )
    if manifest.directory != expected:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_DIRECTORY_INVALID")


def import_archive(
    directory: Path,
    *,
    activate: bool = False,
    local_root_override: Path | None = None,
) -> ImportedArchive:
    """Persist a fully verified archive without replacing current graph state."""

    loaded = load_archive(directory)
    _assert_designated_archive_directory(loaded.manifest, local_root_override=local_root_override)
    from psycopg.types.json import Jsonb

    from src.db.postgres import connect
    from src.ingest import ccct

    def batches(rows: Iterable[dict[str, object]], size: int = 500):
        batch: list[dict[str, object]] = []
        for row in rows:
            batch.append(row)
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch

    manifest = loaded.manifest
    try:
        with connect() as conn, conn.cursor() as cur:
            # One transaction means a crash cannot expose an archive binding
            # whose CCCT snapshot or HSK node rows were only partly inserted.
            ccct.persist_postgres(loaded.ccct_dataset, make_current=False, cursor=cur)
            cur.execute(
                """
                INSERT INTO hsk_ccct_archives (
                  archive_id, archive_as_of, manifest_sha256, collected_at,
                  hsk_level_names_uri, hsk_level_names_sha256, hsk_level_names_byte_size,
                  hsk_level_names_published_at, hsk_level_names_effective_at,
                  hsk_leaf_attributes_uri, hsk_leaf_attributes_sha256, hsk_leaf_attributes_byte_size,
                  hsk_leaf_attributes_published_at, hsk_leaf_attributes_effective_at,
                  ccct_official_uri, ccct_receipts_sha256, ccct_receipts_byte_size,
                  ccct_receipts_published_at, ccct_receipts_effective_at
                ) VALUES (
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s
                )
                ON CONFLICT (archive_id) DO NOTHING
                """,
                (
                    manifest.archive_id,
                    manifest.archive_as_of,
                    manifest.manifest_sha256,
                    manifest.collected_at,
                    manifest.level_names.official_uri,
                    manifest.level_names.sha256,
                    manifest.level_names.byte_size,
                    manifest.level_names.published_at,
                    manifest.level_names.effective_at,
                    manifest.leaf_attributes.official_uri,
                    manifest.leaf_attributes.sha256,
                    manifest.leaf_attributes.byte_size,
                    manifest.leaf_attributes.published_at,
                    manifest.leaf_attributes.effective_at,
                    manifest.ccct.official_uri,
                    manifest.ccct.query_receipts.sha256,
                    manifest.ccct.query_receipts.byte_size,
                    manifest.ccct.query_receipts.published_at,
                    manifest.ccct.query_receipts.effective_at,
                ),
            )
            cur.execute(
                """
                INSERT INTO hsk_snapshots (
                  hsk_snapshot_id, archive_id, snapshot_as_of, projection_sha256,
                  node_count, leaf_count
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (hsk_snapshot_id) DO NOTHING
                """,
                (
                    manifest.hsk_snapshot_id,
                    manifest.archive_id,
                    manifest.archive_as_of,
                    loaded.hsk_projection_sha256,
                    len(loaded.hsk_nodes),
                    sum(node.get("is_leaf") is True for node in loaded.hsk_nodes),
                ),
            )
            for batch in batches(_node_rows(loaded)):
                cur.execute(
                    """
                    INSERT INTO hsk_node_versions (
                      hsk_snapshot_id, code, level, name_ko, name_en, is_leaf,
                      hsk10, hs6, heading4, chapter2, valid_from, valid_to,
                      quantity_unit, weight_unit, category_code, category_name, version_hash
                    )
                    SELECT %s, x.code, x.level, x.name_ko, x.name_en, x.is_leaf,
                           x.hsk10, x.hs6, x.heading4, x.chapter2, x.valid_from,
                           x.valid_to, x.quantity_unit, x.weight_unit, x.category_code,
                           x.category_name, x.version_hash
                    FROM jsonb_to_recordset(%s) AS x(
                      code text, level integer, name_ko text, name_en text, is_leaf boolean,
                      hsk10 text, hs6 text, heading4 text, chapter2 text,
                      valid_from date, valid_to date, quantity_unit text, weight_unit text,
                      category_code text, category_name text, version_hash text
                    )
                    ON CONFLICT (hsk_snapshot_id, code) DO NOTHING
                    """,
                    (manifest.hsk_snapshot_id, Jsonb(batch)),
                )
            cur.execute(
                """
                INSERT INTO hsk_ccct_archive_ccct_bindings (
                  archive_id, ccct_snapshot_id, query_count, raw_row_count, empty_query_count
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (archive_id) DO NOTHING
                """,
                (
                    manifest.archive_id,
                    manifest.ccct.snapshot_id,
                    manifest.ccct.query_count,
                    manifest.ccct.raw_row_count,
                    manifest.ccct.empty_query_count,
                ),
            )
    except HskCcctArchiveError:
        raise
    except Exception as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_IMPORT_FAILED") from exc
    _assert_archive_persisted(loaded)
    imported = ImportedArchive(
        archive_id=manifest.archive_id,
        archive_as_of=manifest.archive_as_of,
        hsk_snapshot_id=manifest.hsk_snapshot_id,
        ccct_snapshot_id=manifest.ccct.snapshot_id,
    )
    if activate:
        activate_archive(directory, local_root_override=local_root_override)
    return imported


def _pointer_path(*, local_root_override: Path | None = None) -> Path:
    return archive_root(local_root_override=local_root_override) / ACTIVE_POINTER_FILENAME


def _empty_pointer() -> dict[str, object]:
    return {"schema_version": ACTIVE_POINTER_SCHEMA, "archives": []}


def _read_pointer(*, local_root_override: Path | None = None) -> tuple[dict[str, object], str]:
    path = _pointer_path(local_root_override=local_root_override)
    root = archive_root(local_root_override=local_root_override)
    if root.exists() or root.is_symlink():
        _assert_safe_directory(root, require_exists=True)
    if not path.exists() and not path.is_symlink():
        payload = _empty_pointer()
        return payload, sha256_bytes(canonical_json_bytes(payload))
    if path.is_symlink() or not path.is_file():
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID") from exc
    if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
    return cast(dict[str, object], payload), sha256_bytes(raw)


def capture_active_archive_catalog(
    *, local_root_override: Path | None = None, verify_assets: bool = True
) -> ActiveArchiveCatalog:
    """Read the active pointer and bind every entry to its immutable receipt."""

    payload, pointer_sha256 = _read_pointer(local_root_override=local_root_override)
    if (
        set(payload) != {"schema_version", "archives"}
        or payload["schema_version"] != ACTIVE_POINTER_SCHEMA
    ):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
    entries = payload["archives"]
    if not isinstance(entries, list):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
    archives: list[ArchiveManifest] = []
    dates: set[date] = set()
    ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "archive_id",
            "archive_as_of",
            "manifest_sha256",
        }:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
        archive_as_of = _parse_date(entry["archive_as_of"], code="HSK_CCCT_ARCHIVE_POINTER_INVALID")
        archive_id = entry["archive_id"]
        digest = entry["manifest_sha256"]
        if (
            not isinstance(archive_id, str)
            or re.fullmatch(r"hsk_ccct_[0-9a-f]{64}", archive_id) is None
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or archive_as_of in dates
            or archive_id in ids
        ):
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
        manifest = load_archive_manifest(
            archive_directory(archive_as_of, local_root_override=local_root_override),
            verify_assets=verify_assets,
        )
        if manifest.archive_id != archive_id or manifest.manifest_sha256 != digest:
            raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
        archives.append(manifest)
        dates.add(archive_as_of)
        ids.add(archive_id)
    if archives != sorted(archives, key=lambda archive: archive.archive_as_of):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
    return ActiveArchiveCatalog(pointer_sha256=pointer_sha256, archives=tuple(archives))


def _atomic_write_pointer(path: Path, payload: Mapping[str, object]) -> None:
    root = _assert_safe_directory(path.parent, require_exists=False)
    root.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_INVALID")
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=root
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_POINTER_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _assert_archive_persisted(loaded: LoadedArchive) -> None:
    from src.db.postgres import connect

    manifest = loaded.manifest
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  a.archive_as_of, a.manifest_sha256, a.collected_at,
                  a.hsk_level_names_uri, a.hsk_level_names_sha256,
                  a.hsk_level_names_byte_size, a.hsk_level_names_published_at,
                  a.hsk_level_names_effective_at,
                  a.hsk_leaf_attributes_uri, a.hsk_leaf_attributes_sha256,
                  a.hsk_leaf_attributes_byte_size, a.hsk_leaf_attributes_published_at,
                  a.hsk_leaf_attributes_effective_at,
                  a.ccct_official_uri, a.ccct_receipts_sha256,
                  a.ccct_receipts_byte_size, a.ccct_receipts_published_at,
                  a.ccct_receipts_effective_at,
                  h.hsk_snapshot_id, h.snapshot_as_of, h.projection_sha256,
                  h.node_count, h.leaf_count,
                  b.ccct_snapshot_id, b.query_count, b.raw_row_count, b.empty_query_count,
                  s.source_type, s.content_hash, s.query_count, s.raw_row_count
                FROM hsk_ccct_archives a
                JOIN hsk_snapshots h ON h.archive_id=a.archive_id
                JOIN hsk_ccct_archive_ccct_bindings b ON b.archive_id=a.archive_id
                JOIN knowledge_snapshots s ON s.snapshot_id=b.ccct_snapshot_id
                WHERE a.archive_id=%s
                """,
                (manifest.archive_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_STORE_UNAVAILABLE") from exc
    expected = (
        manifest.archive_as_of,
        manifest.manifest_sha256,
        manifest.collected_at,
        manifest.level_names.official_uri,
        manifest.level_names.sha256,
        manifest.level_names.byte_size,
        manifest.level_names.published_at,
        manifest.level_names.effective_at,
        manifest.leaf_attributes.official_uri,
        manifest.leaf_attributes.sha256,
        manifest.leaf_attributes.byte_size,
        manifest.leaf_attributes.published_at,
        manifest.leaf_attributes.effective_at,
        manifest.ccct.official_uri,
        manifest.ccct.query_receipts.sha256,
        manifest.ccct.query_receipts.byte_size,
        manifest.ccct.query_receipts.published_at,
        manifest.ccct.query_receipts.effective_at,
        manifest.hsk_snapshot_id,
        manifest.archive_as_of,
        loaded.hsk_projection_sha256,
        len(loaded.hsk_nodes),
        sum(node.get("is_leaf") is True for node in loaded.hsk_nodes),
        manifest.ccct.snapshot_id,
        manifest.ccct.query_count,
        manifest.ccct.raw_row_count,
        manifest.ccct.empty_query_count,
        "ccct",
        loaded.ccct_dataset.content_hash,
        manifest.ccct.query_count,
        manifest.ccct.raw_row_count,
    )
    if row != expected:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_STORE_MISMATCH")


def activate_archive(
    directory: Path, *, local_root_override: Path | None = None
) -> ActiveArchiveCatalog:
    """Explicitly add one imported archive to the service-visible pointer."""

    loaded = load_archive(directory)
    manifest = loaded.manifest
    _assert_designated_archive_directory(manifest, local_root_override=local_root_override)
    _assert_archive_persisted(loaded)
    active = capture_active_archive_catalog(
        local_root_override=local_root_override,
        verify_assets=True,
    )
    entries: list[dict[str, object]] = [
        {
            "archive_id": archive.archive_id,
            "archive_as_of": archive.archive_as_of.isoformat(),
            "manifest_sha256": archive.manifest_sha256,
        }
        for archive in active.archives
    ]
    replacement: dict[str, object] = {
        "archive_id": manifest.archive_id,
        "archive_as_of": manifest.archive_as_of.isoformat(),
        "manifest_sha256": manifest.manifest_sha256,
    }
    for entry in entries:
        if entry.get("archive_as_of") == replacement["archive_as_of"]:
            if entry != replacement:
                # A different receipt for the same date needs an explicit
                # governance migration; silently replacing served history is
                # never acceptable.
                raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_DATE_ALREADY_ACTIVE")
            return capture_active_archive_catalog(local_root_override=local_root_override)
    entries.append(replacement)
    entries.sort(key=lambda row: str(row["archive_as_of"]))
    _atomic_write_pointer(
        _pointer_path(local_root_override=local_root_override),
        {"schema_version": ACTIVE_POINTER_SCHEMA, "archives": entries},
    )
    return capture_active_archive_catalog(local_root_override=local_root_override)


def resolve_hsk_snapshot(hsk_snapshot_id: str, code: str | None) -> repository.HskResolution:
    """Resolve HS6/HSK10 strictly against the selected PostgreSQL snapshot."""

    try:
        normalized = repository.normalize_hsk(code)
    except ValueError:
        return repository.HskResolution("invalid", None, None)
    if normalized is None:
        return repository.HskResolution("missing", None, None)
    from src.db.postgres import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            if len(normalized) == 10:
                cur.execute(
                    """
                    SELECT code FROM hsk_node_versions
                    WHERE hsk_snapshot_id=%s AND code=%s AND is_leaf
                    """,
                    (hsk_snapshot_id, normalized),
                )
            else:
                cur.execute(
                    """
                    SELECT code FROM hsk_node_versions
                    WHERE hsk_snapshot_id=%s AND is_leaf AND code LIKE %s
                    ORDER BY code
                    """,
                    (hsk_snapshot_id, f"{normalized}%"),
                )
            leaves = [cast(str, row[0]) for row in cur.fetchall()]
    except Exception as exc:
        raise HskCcctArchiveError("HSK_CCCT_ARCHIVE_HSK_LOOKUP_UNAVAILABLE") from exc
    return repository.resolve_hsk_candidates(normalized, leaves)


__all__ = [
    "ACTIVE_POINTER_FILENAME",
    "ACTIVE_POINTER_SCHEMA",
    "ARCHIVE_MANIFEST_FILENAME",
    "ARCHIVE_MANIFEST_SCHEMA",
    "ActiveArchiveCatalog",
    "ArchiveManifest",
    "HskCcctArchiveError",
    "ImportedArchive",
    "activate_archive",
    "archive_directory",
    "archive_root",
    "capture_active_archive_catalog",
    "canonical_json_bytes",
    "import_archive",
    "load_archive",
    "load_archive_manifest",
    "resolve_hsk_snapshot",
    "sha256_bytes",
]
