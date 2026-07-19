from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from src.generation import SourceManifest
from src.generation_runtime import ActiveGenerationAttestation
from src.provider_identity import create_runtime_provider_build_identity
from src.runtime_release import (
    EvidenceRuntimeReleaseIdentity,
    GenerationReleaseMetadata,
    RuntimeReleaseError,
    RuntimeReleaseLoader,
)


class FakeActiveLoader:
    def __init__(self) -> None:
        self.epoch = 1
        self.changed_at = "2026-07-19T00:00:00.000000Z"

    def load(self) -> ActiveGenerationAttestation:
        return ActiveGenerationAttestation(
            generation_id="7" * 64,
            epoch=self.epoch,
            changed_at=self.changed_at,
            source_manifest_sha256="6" * 64,
            model_manifest_sha256="8" * 64,
            index_config_sha256="9" * 64,
            evaluation_report_sha256="b" * 64,
            seal_attestation_sha256="c" * 64,
            stores=(),
        )


class FakeMetadataLoader:
    def load(self, generation_id: str) -> GenerationReleaseMetadata:
        return GenerationReleaseMetadata(
            generation_id=generation_id,
            source_manifest_sha256="6" * 64,
            knowledge_snapshot_id="ccct-release-2026-07-10",
            supported_review_as_of=date(2026, 7, 10),
            source_manifest=cast(SourceManifest, SimpleNamespace()),
        )


def _loader(active: FakeActiveLoader | None = None) -> RuntimeReleaseLoader:
    selected = active or FakeActiveLoader()
    provider = create_runtime_provider_build_identity(
        provider_revision="1" * 40,
        schema_sha256="3" * 64,
        source_manifest_sha256="4" * 64,
    )
    return RuntimeReleaseLoader(
        provider_loader=lambda: provider,
        active_generation_loader=selected,  # type: ignore[arg-type]
        metadata_loader=FakeMetadataLoader(),
    )


def test_loader_binds_code_generation_models_evaluation_and_snapshot() -> None:
    lease = _loader().acquire()
    identity = lease.identity

    assert identity.contract == "evidence-runtime-release"
    assert identity.schema_version == "2.0"
    assert identity.retrieval_schema_version == "1.0"
    assert identity.provider_release == "evidence-api-v1.2.0"
    assert identity.knowledge_generation_id == "7" * 64
    assert identity.knowledge_generation_attestation_sha256
    assert identity.knowledge_snapshot_id == "ccct-release-2026-07-10"
    assert identity.supported_review_as_of == date(2026, 7, 10)
    assert len(identity.binding_headers()) == 8
    assert lease.generation_epoch == 1


def test_runtime_digest_excludes_only_itself() -> None:
    payload = _loader().acquire().identity.model_dump(mode="json")
    payload["evaluation_report_sha256"] = "d" * 64
    with pytest.raises(ValidationError, match="runtime_release_sha256"):
        EvidenceRuntimeReleaseIdentity.model_validate(payload)


def test_assert_unchanged_detects_a_to_b_to_a_epoch_change() -> None:
    active = FakeActiveLoader()
    loader = _loader(active)
    lease = loader.acquire()
    active.epoch = 3
    active.changed_at = "2026-07-19T00:02:00.000000Z"

    with pytest.raises(RuntimeReleaseError, match="changed during"):
        loader.assert_unchanged(lease)


def test_generation_id_is_strict_lowercase_sha256() -> None:
    payload: dict[str, Any] = _loader().acquire().identity.model_dump(mode="json")
    payload["knowledge_generation_id"] = "not-a-generation"
    with pytest.raises(ValidationError):
        EvidenceRuntimeReleaseIdentity.model_validate(payload)
