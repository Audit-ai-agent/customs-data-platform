from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from src.generation import (
    GenerationSpec,
    StoreAttestation,
    build_seal_attestation,
    validate_rag_evaluation_report,
)
from src.generation_runtime import ActiveGenerationError, ActiveGenerationLoader

_ATTESTED_AT = "2026-07-19T00:10:00Z"
_RECORDS = ((1, "1" * 64), (2, "2" * 64))


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, query: str, _params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.split())
        if "FROM knowledge_generation_active_pointer p" in normalized:
            self.rows = [self.connection.active_row]
        elif "FROM knowledge_generation_store_attestations" in normalized:
            self.rows = list(self.connection.attestation_rows)
        elif "SELECT generation_id, epoch" in normalized:
            self.rows = [self.connection.final_pointer]
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class FakeConnection:
    def __init__(
        self,
        active_row: tuple[Any, ...],
        attestation_rows: list[tuple[Any, ...]],
        final_pointer: tuple[str, int],
    ) -> None:
        self.active_row = active_row
        self.attestation_rows = attestation_rows
        self.final_pointer = final_pointer

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


class FakeExternalStore:
    def __init__(self, name: str, active: str | None) -> None:
        self.name = name
        self.active = active

    def current_active_generation(self) -> str | None:
        return self.active


def _loader_fixture(
    spec: GenerationSpec,
    evaluation: dict[str, Any],
    *,
    seal_override: str | None = None,
    final_pointer: tuple[str, int] | None = None,
    external_override: str | None = None,
) -> ActiveGenerationLoader:
    report, report_hash = validate_rag_evaluation_report(
        evaluation,
        generation_id=spec.generation_id,
    )
    attestations = tuple(
        StoreAttestation.from_article_records(
            generation_id=spec.generation_id,
            store_name=name,
            records=_RECORDS,
            model_manifest_sha256=spec.model_manifest_sha256,
            store_locator=f"generation/{spec.generation_id}/{name}",
            store_revision=f"{name}-v1",
            attested_at=_ATTESTED_AT,
        )
        for name in ("postgresql", "qdrant", "neo4j")
    )
    _, seal_hash = build_seal_attestation(spec, attestations, report_hash)
    active_row = (
        spec.generation_id,
        7,
        datetime(2026, 7, 19, tzinfo=timezone.utc),
        "active",
        spec.source_manifest.as_dict(),
        spec.source_manifest_sha256,
        spec.model_manifest,
        spec.model_manifest_sha256,
        spec.index_config,
        spec.index_config_sha256,
        report,
        report_hash,
        True,
        seal_override or seal_hash,
    )
    connection = FakeConnection(
        active_row,
        [(item.as_dict(), item.sha256) for item in attestations],
        final_pointer or (spec.generation_id, 7),
    )
    external_active = external_override or spec.generation_id
    return ActiveGenerationLoader(
        connection_factory=lambda: connection,
        external_stores=(
            FakeExternalStore("qdrant", external_active),  # type: ignore[arg-type]
            FakeExternalStore("neo4j", spec.generation_id),  # type: ignore[arg-type]
        ),
    )


def test_runtime_loader_returns_sealed_o1_attestation(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    loader = _loader_fixture(
        generation_spec,
        generation_evaluation_factory(generation_spec.generation_id),
    )

    attestation = loader.load()

    assert attestation.generation_id == generation_spec.generation_id
    assert attestation.epoch == 7
    assert len(attestation.stores) == 3
    assert len(attestation.sha256) == 64


def test_runtime_loader_rejects_seal_tamper(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    loader = _loader_fixture(
        generation_spec,
        generation_evaluation_factory(generation_spec.generation_id),
        seal_override="0" * 64,
    )
    with pytest.raises(ActiveGenerationError, match="seal hash mismatch"):
        loader.load()


def test_runtime_loader_rejects_pointer_change_during_load(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    loader = _loader_fixture(
        generation_spec,
        generation_evaluation_factory(generation_spec.generation_id),
        final_pointer=("f" * 64, 8),
    )
    with pytest.raises(ActiveGenerationError, match="changed during"):
        loader.load()


def test_runtime_loader_rejects_external_alias_mismatch(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    loader = _loader_fixture(
        generation_spec,
        generation_evaluation_factory(generation_spec.generation_id),
        external_override="f" * 64,
    )
    with pytest.raises(ActiveGenerationError, match="qdrant active pointer"):
        loader.load()
