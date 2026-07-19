from __future__ import annotations

import hashlib

import pytest

from tools.provider_build import (
    ProviderBuildError,
    create_runtime_provider_build_document,
    create_runtime_provider_lock_document,
)


def _inputs():
    retrieval = b'{"title":"retrieval-v1"}\n'
    readiness = b'{"title":"readiness-v2"}\n'
    manifest = b'{"source_commit":"fixture"}\n'
    build = create_runtime_provider_build_document(
        provider_revision="1" * 40,
        retrieval_schema=retrieval,
        source_manifest=manifest,
    )
    return build, retrieval, readiness, manifest


def test_provider_lock_has_one_exact_canonical_field_set() -> None:
    build, retrieval, readiness, manifest = _inputs()

    lock = create_runtime_provider_lock_document(
        runtime_provider_build=build,
        retrieval_schema=retrieval,
        readiness_schema=readiness,
        source_manifest=manifest,
    )

    assert lock == {
        "contract": "evidence-runtime-release-provider-lock",
        "schema_version": "1.0",
        "provider_release": "evidence-api-v1.2.0",
        "provider_revision": "1" * 40,
        "provider_build_sha256": build["provider_build_sha256"],
        "retrieval_schema_sha256": hashlib.sha256(retrieval).hexdigest(),
        "readiness_schema_sha256": hashlib.sha256(readiness).hexdigest(),
        "provider_source_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }


@pytest.mark.parametrize(
    "mutation",
    ["extra_field", "provider_build", "retrieval", "manifest"],
)
def test_provider_lock_rejects_noncanonical_or_cross_context_input(
    mutation: str,
) -> None:
    build, retrieval, readiness, manifest = _inputs()
    if mutation == "extra_field":
        build["unexpected"] = "not-canonical"
    elif mutation == "provider_build":
        build["provider_build_sha256"] = "0" * 64
    elif mutation == "retrieval":
        retrieval += b"tampered"
    else:
        manifest += b"tampered"

    with pytest.raises(ProviderBuildError):
        create_runtime_provider_lock_document(
            runtime_provider_build=build,
            retrieval_schema=retrieval,
            readiness_schema=readiness,
            source_manifest=manifest,
        )
