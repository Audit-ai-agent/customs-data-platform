from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.provider_identity import (
    HEADER_PROVIDER_BUILD_SHA256,
    HEADER_PROVIDER_RELEASE,
    HEADER_PROVIDER_REVISION,
    HEADER_SCHEMA_SHA256,
    ProviderIdentityError,
    binding_header_mismatches,
    create_provider_build_identity,
    load_provider_build_identity,
)
from tools.provider_build import create_provider_build_document

REVISION = "1" * 40


def _write_bundle(root: Path):
    schema_path = root / "contracts/evidence/v1/rag_retrieval_v1.schema.json"
    source_path = root / "law_repository/src/sample.py"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_bytes(b'{"title":"frozen retrieval schema"}\n')
    source_path.write_bytes(b"VALUE = 1\n")

    entries = []
    for relative_path in (
        "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        "law_repository/src/sample.py",
    ):
        payload = (root / relative_path).read_bytes()
        entries.append({
            "path": relative_path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        })
    manifest = {
        "schema_version": "1.0",
        "source_repository": "customs-data-platform",
        "source_commit": REVISION,
        "files": entries,
    }
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode()
    (root / "source-manifest.json").write_bytes(manifest_raw)
    identity = create_provider_build_identity(
        provider_revision=REVISION,
        schema_sha256=hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        source_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
    )
    (root / "provider-build.json").write_text(
        json.dumps(identity.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return identity


def test_fail_closed_loader_accepts_one_exact_attested_bundle(tmp_path: Path) -> None:
    expected = _write_bundle(tmp_path)
    assert load_provider_build_identity(bundle_root=tmp_path) == expected


@pytest.mark.parametrize("target", ["schema", "source", "manifest"])
def test_fail_closed_loader_rejects_every_attested_byte_mismatch(
    tmp_path: Path,
    target: str,
) -> None:
    _write_bundle(tmp_path)
    paths = {
        "schema": tmp_path / "contracts/evidence/v1/rag_retrieval_v1.schema.json",
        "source": tmp_path / "law_repository/src/sample.py",
        "manifest": tmp_path / "source-manifest.json",
    }
    with paths[target].open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ProviderIdentityError):
        load_provider_build_identity(bundle_root=tmp_path)


def test_fail_closed_loader_rejects_duplicate_or_extra_identity_fields(
    tmp_path: Path,
) -> None:
    identity = _write_bundle(tmp_path)
    serialized = json.dumps(identity.model_dump(), ensure_ascii=False)
    (tmp_path / "provider-build.json").write_text(
        '{"contract":"evidence-retrieval",' + serialized[1:],
        encoding="utf-8",
    )
    with pytest.raises(ProviderIdentityError, match="duplicate JSON key"):
        load_provider_build_identity(bundle_root=tmp_path)

    _write_bundle(tmp_path)
    payload = identity.model_dump()
    payload["fixture"] = True
    (tmp_path / "provider-build.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProviderIdentityError, match="fields are not exact"):
        load_provider_build_identity(bundle_root=tmp_path)


def test_build_generator_and_runtime_use_the_same_exact_hash_input(tmp_path: Path) -> None:
    expected = _write_bundle(tmp_path)
    generated = create_provider_build_document(
        provider_revision=REVISION,
        retrieval_schema=(
            tmp_path / "contracts/evidence/v1/rag_retrieval_v1.schema.json"
        ).read_bytes(),
        source_manifest=(tmp_path / "source-manifest.json").read_bytes(),
    )
    assert generated == expected.model_dump()
    assert set(generated) == {
        "contract",
        "schema_version",
        "provider_release",
        "provider_revision",
        "schema_sha256",
        "source_manifest_sha256",
        "provider_build_sha256",
    }


def test_exact_binding_headers_are_complete_and_mismatch_is_named(tmp_path: Path) -> None:
    identity = _write_bundle(tmp_path)
    expected = identity.binding_headers()
    assert set(expected) == {
        HEADER_SCHEMA_SHA256,
        HEADER_PROVIDER_RELEASE,
        HEADER_PROVIDER_REVISION,
        HEADER_PROVIDER_BUILD_SHA256,
    }
    assert binding_header_mismatches(expected, identity) == ()
    lowered = {key.lower(): value for key, value in expected.items()}
    assert binding_header_mismatches(lowered, identity) == ()
    lowered[HEADER_PROVIDER_REVISION.lower()] = "2" * 40
    assert binding_header_mismatches(lowered, identity) == (
        HEADER_PROVIDER_REVISION,
    )
