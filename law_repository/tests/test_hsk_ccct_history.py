"""Pure receipt/pointer tests for official dated HSK/CCCT archives."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from src import hsk_ccct_history as history
from src.ingest import ccct


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _item() -> dict[str, str]:
    return {
        "aplyStrtDt": "20200101",
        "bfhnAffcRtmTpcd": "2",
        "dcerCfrmLworCd": "89",
        "dcerCfrmLworNm": "수입식품안전관리 특별법",
        "hsSgn": "0303540000",
        "reqApreIttCd": "005",
        "reqApreIttNm": "식품의약품안전처",
        "reqCfrmIstmNm": "수입식품등 수입신고확인증",
    }


def _asset(path: str, raw: bytes, uri: str) -> dict[str, object]:
    return {
        "path": path,
        "official_uri": uri,
        "sha256": _sha(raw),
        "byte_size": len(raw),
        "published_at": "2026-01-01",
        "effective_at": "2026-01-01",
    }


def _write_archive(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    archive = tmp_path / "data" / "official-archives" / "hsk-ccct" / "2026-01-01"
    (archive / "hsk").mkdir(parents=True)
    (archive / "ccct").mkdir()
    levels = b"fake-levels"
    attrs = b"fake-attributes"
    (archive / "hsk" / "levels.xlsx").write_bytes(levels)
    (archive / "hsk" / "attributes.xlsx").write_bytes(attrs)
    receipt = {
        "hs": "0303540000",
        "tpcd": "2",
        "total_count": 1,
        "complete": True,
        "pages": [{"page": 1, "items": [_item()]}],
    }
    receipt_bytes = (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    (archive / "ccct" / "query-receipts.jsonl").write_bytes(receipt_bytes)
    dataset = ccct.build_dataset([{"hs": "0303540000", "tpcd": "2", "items": [_item()]}])
    manifest: dict[str, object] = {
        "schema_version": history.ARCHIVE_MANIFEST_SCHEMA,
        "archive_as_of": "2026-01-01",
        "collected_at": "2026-01-02T00:00:00+00:00",
        "hsk": {
            "level_names": _asset(
                "hsk/levels.xlsx",
                levels,
                "https://www.data.go.kr/data/15049722/fileData.do",
            ),
            "leaf_attributes": _asset(
                "hsk/attributes.xlsx",
                attrs,
                "https://www.data.go.kr/data/15130660/fileData.do",
            ),
        },
        "ccct": {
            "official_uri": "https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd",
            "query_receipts": {
                "path": "ccct/query-receipts.jsonl",
                "sha256": _sha(receipt_bytes),
                "byte_size": len(receipt_bytes),
                "published_at": "2026-01-01",
                "effective_at": "2026-01-01",
            },
            "snapshot_id": dataset.snapshot_id,
            "query_count": 1,
            "empty_query_count": 0,
            "raw_row_count": 1,
        },
    }
    (archive / history.ARCHIVE_MANIFEST_FILENAME).write_bytes(
        history.canonical_json_bytes(manifest)
    )
    return archive, manifest


def _rewrite_manifest(archive: Path, manifest: dict[str, object]) -> None:
    (archive / history.ARCHIVE_MANIFEST_FILENAME).write_bytes(
        history.canonical_json_bytes(manifest)
    )


def test_manifest_receipt_and_active_pointer_bind_exact_hashes(tmp_path: Path) -> None:
    archive, _manifest = _write_archive(tmp_path)
    loaded = history.load_archive_manifest(archive)
    pointer = {
        "schema_version": history.ACTIVE_POINTER_SCHEMA,
        "archives": [
            {
                "archive_id": loaded.archive_id,
                "archive_as_of": "2026-01-01",
                "manifest_sha256": loaded.manifest_sha256,
            }
        ],
    }
    pointer_path = (
        tmp_path / "data" / "official-archives" / "hsk-ccct" / history.ACTIVE_POINTER_FILENAME
    )
    pointer_path.write_bytes(history.canonical_json_bytes(pointer))

    catalog = history.capture_active_archive_catalog(local_root_override=tmp_path)
    assert catalog.for_date(date(2026, 1, 1)) == loaded

    # Pointer bytes alone are insufficient when an operator later changes a
    # source asset; runtime capture verifies the hash-bound local receipt too.
    (archive / "hsk" / "levels.xlsx").write_bytes(b"mutated")
    with pytest.raises(history.HskCcctArchiveError, match="ASSET_HASH_MISMATCH"):
        history.capture_active_archive_catalog(local_root_override=tmp_path)


def test_manifest_rejects_credential_bearing_official_uri(tmp_path: Path) -> None:
    archive, manifest = _write_archive(tmp_path)
    ccct_manifest = manifest["ccct"]
    assert isinstance(ccct_manifest, dict)
    ccct_manifest["official_uri"] = (
        "https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd?serviceKey=secret"
    )
    _rewrite_manifest(archive, manifest)

    with pytest.raises(history.HskCcctArchiveError, match="OFFICIAL_URI_INVALID"):
        history.load_archive_manifest(archive)


def test_manifest_rejects_malformed_port_and_pre_history_date(tmp_path: Path) -> None:
    archive, manifest = _write_archive(tmp_path)
    ccct_manifest = manifest["ccct"]
    assert isinstance(ccct_manifest, dict)
    ccct_manifest["official_uri"] = (
        "https://apis.data.go.kr:bad/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd"
    )
    _rewrite_manifest(archive, manifest)

    with pytest.raises(history.HskCcctArchiveError, match="OFFICIAL_URI_INVALID"):
        history.load_archive_manifest(archive)

    # A library caller cannot stage an unsupported pre-2026 archive simply by
    # bypassing the CLI date parser.
    archive.rename(archive.parent / "2025-12-31")
    archive = archive.parent / "2025-12-31"
    manifest["archive_as_of"] = "2025-12-31"
    ccct_manifest["official_uri"] = (
        "https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd"
    )
    _rewrite_manifest(archive, manifest)
    with pytest.raises(history.HskCcctArchiveError, match="DATE_PROVENANCE_INVALID"):
        history.load_archive_manifest(archive)


def test_ccct_receipt_requires_complete_pagination_not_just_a_row_count(tmp_path: Path) -> None:
    archive, manifest = _write_archive(tmp_path)
    bad_receipt = {
        "hs": "0303540000",
        "tpcd": "2",
        "total_count": 2,
        "complete": True,
        "pages": [{"page": 1, "items": [_item()]}],
    }
    raw = (
        json.dumps(bad_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    receipt_path = archive / "ccct" / "query-receipts.jsonl"
    receipt_path.write_bytes(raw)
    ccct_manifest = manifest["ccct"]
    assert isinstance(ccct_manifest, dict)
    asset = ccct_manifest["query_receipts"]
    assert isinstance(asset, dict)
    asset["sha256"] = _sha(raw)
    asset["byte_size"] = len(raw)
    _rewrite_manifest(archive, manifest)

    manifest_value = history.load_archive_manifest(archive)
    with pytest.raises(history.HskCcctArchiveError, match="PAGINATION_INCOMPLETE"):
        history._receipt_records(raw, manifest_value)


def test_full_load_reuses_verified_receipts_and_never_reads_current_ccct_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive, _manifest = _write_archive(tmp_path)
    from src.ingest import hsk

    node = {
        "code": "0303540000",
        "level": 10,
        "name_ko": "고등어",
        "name_en": "Mackerel",
        "is_leaf": True,
        "hsk10": "0303540000",
        "hs6": "030354",
        "heading4": "0303",
        "chapter2": "03",
        "valid_from": "2026-01-01",
        "valid_to": None,
        "quantity_unit": None,
        "weight_unit": None,
        "category_code": None,
        "category_name": None,
    }
    monkeypatch.setattr(hsk, "load_level_names", lambda _path: object())
    monkeypatch.setattr(hsk, "load_leaf_attrs", lambda _path: object())
    monkeypatch.setattr(hsk, "build_nodes", lambda _levels, _attrs: [node])

    loaded = history.load_archive(archive)
    assert loaded.manifest.archive_as_of == date(2026, 1, 1)
    assert loaded.hsk_nodes == (node,)
    assert len(loaded.ccct_dataset.queries) == 1
    assert len(loaded.ccct_dataset.source_records) == 1
