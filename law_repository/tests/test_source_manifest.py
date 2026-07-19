from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from src.ingest.laws import TARGET_LAWS
from src.source_manifest import (
    LawSourceManifest,
    SourceManifestError,
    canonical_json_bytes,
    load_verified_source_manifest,
    target_law_manifest_sha256,
)


def _manifest(tmp_path):
    laws = []
    payload = {"법령": {"기본정보": {"법령ID": "fixture"}}}
    data = canonical_json_bytes(payload)
    digest = hashlib.sha256(data).hexdigest()
    for index, target in enumerate(TARGET_LAWS, 1):
        current = f"law_current_{target['law_id']}.json"
        effective = f"law_effective_{target['law_id']}_{index}.json"
        (tmp_path / current).write_bytes(data)
        (tmp_path / effective).write_bytes(data)
        laws.append(
            {
                "law_id": target["law_id"],
                "law_name": target["name"],
                "current_mst": str(index),
                "effective_mst": str(index),
                "effective_date": "2026-07-19",
                "current_file": current,
                "current_sha256": digest,
                "effective_file": effective,
                "effective_sha256": digest,
                "current_source_uri": (
                    f"https://www.law.go.kr/DRF/lawService.do?target=law&ID={target['law_id']}"
                ),
                "effective_source_uri": (
                    f"https://www.law.go.kr/DRF/lawService.do?target=law&MST={index}"
                ),
                "article_count": 1,
                "article_content_set_sha256": "a" * 64,
            }
        )
    return {
        "schema_version": "1.0",
        "source": "Korean Ministry of Government Legislation LAW OPEN DATA",
        "collected_at": datetime(2026, 7, 19, tzinfo=timezone.utc).isoformat(),
        "collection_as_of": "2026-07-19",
        "target_law_manifest_sha256": target_law_manifest_sha256(),
        "collector_revision": "1" * 40,
        "law_count": 58,
        "laws": laws,
    }


def test_complete_manifest_and_every_source_asset_are_hash_verified(tmp_path) -> None:
    payload = _manifest(tmp_path)
    manifest_bytes = canonical_json_bytes(payload)
    (tmp_path / "source-manifest.json").write_bytes(manifest_bytes)

    manifest, digest = load_verified_source_manifest(tmp_path)

    assert isinstance(manifest, LawSourceManifest)
    assert len(manifest.laws) == 58
    assert digest == hashlib.sha256(manifest_bytes).hexdigest()


def test_missing_target_law_cannot_be_promoted_as_complete(tmp_path) -> None:
    payload = _manifest(tmp_path)
    payload["laws"].pop()

    with pytest.raises(ValueError):
        LawSourceManifest.model_validate(payload)


def test_asset_tamper_is_detected(tmp_path) -> None:
    payload = _manifest(tmp_path)
    (tmp_path / "source-manifest.json").write_bytes(canonical_json_bytes(payload))
    (tmp_path / payload["laws"][0]["effective_file"]).write_text(
        '{"tampered":true}',
        encoding="utf-8",
    )

    with pytest.raises(SourceManifestError, match="SOURCE_ASSET_HASH_MISMATCH"):
        load_verified_source_manifest(tmp_path)


def test_manifest_rejects_credential_bearing_source_uri(tmp_path) -> None:
    payload = _manifest(tmp_path)
    payload["laws"][0]["current_source_uri"] += "&OC=secret"

    with pytest.raises(ValueError):
        LawSourceManifest.model_validate(payload)
