"""Canonical provenance contract for a complete official 58-law source corpus."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.ingest.laws import TARGET_LAWS, build_meta, parse_articles

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
LAW_ID = Annotated[str, Field(pattern=r"^[0-9]{6}$")]
MST = Annotated[str, Field(pattern=r"^[0-9]+$", min_length=1, max_length=32)]
MAX_SOURCE_ASSET_BYTES = 32 * 1024 * 1024


class SourceManifestError(RuntimeError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LawSourceAsset(_StrictModel):
    law_id: LAW_ID
    law_name: str = Field(min_length=1, max_length=300)
    current_mst: MST
    effective_mst: MST
    effective_date: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    current_file: str = Field(min_length=1, max_length=200)
    current_sha256: SHA256
    effective_file: str = Field(min_length=1, max_length=200)
    effective_sha256: SHA256
    current_source_uri: str = Field(min_length=1, max_length=500)
    effective_source_uri: str = Field(min_length=1, max_length=500)
    article_count: int = Field(gt=0)
    article_content_set_sha256: SHA256

    @model_validator(mode="after")
    def _safe_relative_assets_and_official_sources(self) -> "LawSourceAsset":
        for filename in (self.current_file, self.effective_file):
            path = PurePosixPath(filename)
            if (
                path.is_absolute()
                or len(path.parts) != 1
                or path.name != filename
                or filename in {".", ".."}
                or "\\" in filename
                or "\x00" in filename
            ):
                raise ValueError("source asset path must be a plain relative filename")
        expected_sources = (
            (
                self.current_source_uri,
                {"target": ["law"], "ID": [self.law_id]},
            ),
            (
                self.effective_source_uri,
                {"target": ["law"], "MST": [self.effective_mst]},
            ),
        )
        for uri, expected_query in expected_sources:
            parsed = urlsplit(uri)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "www.law.go.kr"
                or parsed.port not in {None, 443}
                or parsed.path != "/DRF/lawService.do"
                or parsed.fragment
                or "OC=" in parsed.query.upper()
                or parse_qs(
                    parsed.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                != expected_query
            ):
                raise ValueError("source URI must identify the official credential-free endpoint")
        if self.current_file != f"law_current_{self.law_id}.json":
            raise ValueError("current source filename does not match the law identity")
        if self.effective_file != f"law_effective_{self.law_id}_{self.effective_mst}.json":
            raise ValueError("effective source filename does not match the law identity")
        return self


class LawSourceManifest(_StrictModel):
    schema_version: Literal["1.0"]
    source: Literal["Korean Ministry of Government Legislation LAW OPEN DATA"]
    collected_at: datetime
    collection_as_of: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    target_law_manifest_sha256: SHA256
    collector_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    law_count: Literal[58]
    laws: list[LawSourceAsset] = Field(min_length=58, max_length=58)

    @model_validator(mode="after")
    def _exact_target_set(self) -> "LawSourceManifest":
        expected = {(item["law_id"], item["name"]) for item in TARGET_LAWS}
        actual = {(item.law_id, item.law_name) for item in self.laws}
        if actual != expected or len(actual) != len(self.laws):
            raise ValueError("manifest must contain the exact unique 58-law target set")
        if self.target_law_manifest_sha256 != target_law_manifest_sha256():
            raise ValueError("target-law manifest hash does not match this release")
        if self.collected_at.tzinfo is None:
            raise ValueError("collected_at must be timezone-aware")
        return self


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def target_law_manifest_sha256() -> str:
    normalized = [
        {"law_id": item["law_id"], "law_name": item["name"]}
        for item in sorted(TARGET_LAWS, key=lambda value: value["law_id"])
    ]
    return hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()


def hash_file(path: Path, *, max_bytes: int = MAX_SOURCE_ASSET_BYTES) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        raise SourceManifestError("SOURCE_ASSET_UNAVAILABLE") from None
    if size <= 0 or size > max_bytes:
        raise SourceManifestError("SOURCE_ASSET_SIZE_INVALID")
    digest = hashlib.sha256()
    observed = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                observed += len(chunk)
                if observed > max_bytes:
                    raise SourceManifestError("SOURCE_ASSET_SIZE_INVALID")
                digest.update(chunk)
    except SourceManifestError:
        raise
    except OSError:
        raise SourceManifestError("SOURCE_ASSET_UNAVAILABLE") from None
    if observed != size:
        raise SourceManifestError("SOURCE_ASSET_CHANGED_DURING_READ")
    return digest.hexdigest()


def article_content_set_sha256(rows: list[dict[str, Any]]) -> str:
    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "article_no": row["article_no"],
                "paragraph_no": row["paragraph_no"],
                "content_sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
            }
        )
        for child in row.get("children", []):
            entries.append(
                {
                    "article_no": child["article_no"],
                    "paragraph_no": child["paragraph_no"],
                    "content_sha256": hashlib.sha256(child["content"].encode("utf-8")).hexdigest(),
                }
            )
    entries.sort(
        key=lambda item: (
            item["article_no"],
            item["paragraph_no"] if item["paragraph_no"] is not None else -1,
            item["content_sha256"],
        )
    )
    return hashlib.sha256(canonical_json_bytes(entries)).hexdigest()


def load_verified_source_manifest(
    root: Path,
    manifest_name: str = "source-manifest.json",
) -> tuple[LawSourceManifest, str]:
    root = root.resolve(strict=True)
    manifest_path = (root / manifest_name).resolve(strict=True)
    if manifest_path.parent != root or manifest_path.is_symlink():
        raise SourceManifestError("SOURCE_MANIFEST_PATH_INVALID")
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes)
        manifest = LawSourceManifest.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise SourceManifestError("SOURCE_MANIFEST_INVALID") from None

    for asset in manifest.laws:
        decoded_documents: dict[str, dict[str, Any]] = {}
        for filename, expected_hash in (
            (asset.current_file, asset.current_sha256),
            (asset.effective_file, asset.effective_sha256),
        ):
            path = (root / filename).resolve(strict=True)
            if path.parent != root or path.is_symlink():
                raise SourceManifestError("SOURCE_ASSET_PATH_INVALID")
            if hash_file(path) != expected_hash:
                raise SourceManifestError("SOURCE_ASSET_HASH_MISMATCH")
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise SourceManifestError("SOURCE_ASSET_JSON_INVALID") from None
            if not isinstance(document, dict):
                raise SourceManifestError("SOURCE_ASSET_JSON_INVALID")
            decoded_documents[filename] = document

        try:
            current_meta = build_meta(
                decoded_documents[asset.current_file],
                asset.current_mst,
            )
            effective_meta = build_meta(
                decoded_documents[asset.effective_file],
                asset.effective_mst,
            )
            effective_rows = parse_articles(
                decoded_documents[asset.effective_file],
                effective_meta,
            )
            expected_effective_date = datetime.strptime(
                asset.effective_date,
                "%Y-%m-%d",
            ).date()
            observed_effective_date = datetime.strptime(
                effective_meta["enforcement_date"],
                "%Y%m%d",
            ).date()
            observed_article_count = sum(
                1 + len(row.get("children", [])) for row in effective_rows
            )
        except (KeyError, TypeError, ValueError):
            raise SourceManifestError("SOURCE_ASSET_SEMANTIC_MISMATCH") from None

        if (
            current_meta["law_id"] != asset.law_id
            or current_meta["law_name"] != asset.law_name
            or effective_meta["law_id"] != asset.law_id
            or effective_meta["law_name"] != asset.law_name
            or observed_effective_date > expected_effective_date
            or observed_article_count != asset.article_count
            or article_content_set_sha256(effective_rows)
            != asset.article_content_set_sha256
        ):
            raise SourceManifestError("SOURCE_ASSET_SEMANTIC_MISMATCH")

    return manifest, hashlib.sha256(manifest_bytes).hexdigest()


def validate_collector_revision(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise SourceManifestError("COLLECTOR_REVISION_INVALID")
    return value


__all__ = [
    "LawSourceAsset",
    "LawSourceManifest",
    "SourceManifestError",
    "article_content_set_sha256",
    "canonical_json_bytes",
    "hash_file",
    "load_verified_source_manifest",
    "target_law_manifest_sha256",
    "validate_collector_revision",
]
