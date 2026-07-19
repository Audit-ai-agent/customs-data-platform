from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

import pytest

from src.generation import (
    GenerationSpec,
    GenerationState,
    StoreAttestation,
)
from src.generation_coordinator import (
    GenerationChangedError,
    GenerationCoordinator,
    GenerationPromotionError,
)
from src.generation_repository import GenerationRepositoryError

_RECORDS = ((1, "1" * 64), (2, "2" * 64), (3, "3" * 64))
_ATTESTED_AT = "2026-07-19T00:10:00Z"


class FakeTransaction:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository

    def create(self, spec: GenerationSpec) -> None:
        if spec.generation_id in self.repository.specs:
            raise GenerationRepositoryError("duplicate generation")
        self.repository.specs[spec.generation_id] = spec
        self.repository.states[spec.generation_id] = GenerationState.BUILDING

    def state(self, generation_id: str) -> GenerationState:
        try:
            return self.repository.states[generation_id]
        except KeyError as exc:
            raise GenerationRepositoryError("unknown generation") from exc

    def active_generation_id(self) -> str | None:
        return self.repository.active

    def load_spec(self, generation_id: str) -> GenerationSpec:
        return self.repository.specs[generation_id]

    def replace_article_membership(
        self,
        generation_id: str,
        records: Iterator[tuple[int, str]],
    ) -> None:
        self.repository.memberships[generation_id] = tuple(records)

    def article_membership(self, generation_id: str) -> tuple[tuple[int, str], ...]:
        return self.repository.memberships[generation_id]

    def validate(
        self,
        generation_id: str,
        attestations: Sequence[StoreAttestation],
    ) -> None:
        if self.state(generation_id) is not GenerationState.BUILDING:
            raise GenerationRepositoryError("not building")
        self.repository.attestations[generation_id] = tuple(attestations)
        self.repository.states[generation_id] = GenerationState.VALIDATED

    def stored_attestations(self, generation_id: str) -> tuple[StoreAttestation, ...]:
        return self.repository.attestations[generation_id]

    def seal(
        self,
        generation_id: str,
        *,
        evaluation_report: Mapping[str, Any],
        evaluation_report_sha256: str,
        seal_attestation_sha256: str,
    ) -> None:
        if self.state(generation_id) is not GenerationState.VALIDATED:
            raise GenerationRepositoryError("not validated")
        self.repository.evaluations[generation_id] = (
            dict(evaluation_report),
            evaluation_report_sha256,
            seal_attestation_sha256,
        )
        self.repository.states[generation_id] = GenerationState.SEALED

    def activate(
        self,
        generation_id: str,
        *,
        expected_previous: str | None,
        rollback: bool,
    ) -> None:
        if self.repository.active != expected_previous:
            raise GenerationRepositoryError("active changed")
        expected = GenerationState.RETIRED if rollback else GenerationState.SEALED
        if self.state(generation_id) is not expected:
            raise GenerationRepositoryError("wrong target state")
        if expected_previous is not None:
            self.repository.states[expected_previous] = GenerationState.RETIRED
        self.repository.states[generation_id] = GenerationState.ACTIVE
        self.repository.active = generation_id

    def record_reconciled(
        self,
        generation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.repository.events.append((generation_id, dict(payload)))


class FakeRepository:
    def __init__(self) -> None:
        self.specs: dict[str, GenerationSpec] = {}
        self.states: dict[str, GenerationState] = {}
        self.memberships: dict[str, tuple[tuple[int, str], ...]] = {}
        self.attestations: dict[str, tuple[StoreAttestation, ...]] = {}
        self.evaluations: dict[str, tuple[dict[str, Any], str, str]] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.active: str | None = None
        self.fail_commit_once = False

    @contextmanager
    def writer_transaction(self) -> Iterator[FakeTransaction]:
        snapshot = copy.deepcopy(
            (
                self.specs,
                self.states,
                self.memberships,
                self.attestations,
                self.evaluations,
                self.events,
                self.active,
            )
        )
        try:
            yield FakeTransaction(self)
            if self.fail_commit_once:
                self.fail_commit_once = False
                raise RuntimeError("injected PostgreSQL commit failure")
        except Exception:
            (
                self.specs,
                self.states,
                self.memberships,
                self.attestations,
                self.evaluations,
                self.events,
                self.active,
            ) = snapshot
            raise

    def active_generation_id(self) -> str | None:
        return self.active


class FakeStore:
    def __init__(self, name: str) -> None:
        self.name = name
        self.prepared: set[str] = set()
        self.records: dict[str, tuple[tuple[int, str], ...]] = {}
        self.models: dict[str, str] = {}
        self.active: str | None = None
        self.fail_target: str | None = None

    def prepare_generation(self, generation_id: str, model_manifest_sha256: str) -> None:
        self.prepared.add(generation_id)
        self.models[generation_id] = model_manifest_sha256

    def current_active_generation(self) -> str | None:
        return self.active

    def set_active_generation(self, generation_id: str | None) -> None:
        if generation_id == self.fail_target:
            raise RuntimeError(f"injected {self.name} switch failure")
        if generation_id is not None and generation_id not in self.prepared:
            raise RuntimeError("generation was not prepared")
        self.active = generation_id

    def attest_generation(
        self,
        generation_id: str,
        model_manifest_sha256: str,
        *,
        attested_at: str,
    ) -> StoreAttestation:
        return StoreAttestation.from_article_records(
            generation_id=generation_id,
            store_name=self.name,
            records=self.records[generation_id],
            model_manifest_sha256=model_manifest_sha256,
            store_locator=f"generation/{generation_id}/{self.name}",
            store_revision=f"{self.name}-v1",
            attested_at=attested_at,
        )


def _coordinator() -> tuple[GenerationCoordinator, FakeRepository, FakeStore, FakeStore]:
    repository = FakeRepository()
    qdrant = FakeStore("qdrant")
    neo4j = FakeStore("neo4j")
    return (
        GenerationCoordinator(repository, (qdrant, neo4j)),
        repository,
        qdrant,
        neo4j,
    )


def _build_and_seal(
    coordinator: GenerationCoordinator,
    stores: tuple[FakeStore, FakeStore],
    spec: GenerationSpec,
    evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    coordinator.begin_generation(spec, iter(_RECORDS))
    for store in stores:
        store.records[spec.generation_id] = _RECORDS
    coordinator.validate_generation(spec.generation_id, attested_at=_ATTESTED_AT)
    coordinator.seal_generation(
        spec.generation_id,
        evaluation_factory(spec.generation_id),
    )


def _next_spec(spec: GenerationSpec) -> GenerationSpec:
    return GenerationSpec(
        source_manifest=spec.source_manifest,
        model_manifest=spec.model_manifest,
        index_config={**spec.index_config, "generation_sequence": 2},
    )


def test_full_generation_lifecycle_and_explicit_rollback(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_factory,
    )
    coordinator.promote(generation_spec.generation_id)
    assert coordinator.verify_active_alignment() == generation_spec.generation_id

    second = _next_spec(generation_spec)
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        second,
        generation_evaluation_factory,
    )
    coordinator.promote(second.generation_id)
    assert repository.states[generation_spec.generation_id] is GenerationState.RETIRED
    assert repository.active == second.generation_id

    coordinator.rollback(generation_spec.generation_id)
    assert repository.active == generation_spec.generation_id
    assert repository.states[second.generation_id] is GenerationState.RETIRED
    assert qdrant.active == neo4j.active == generation_spec.generation_id


def test_external_switch_failure_preserves_previous_active_generation(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_factory,
    )
    coordinator.promote(generation_spec.generation_id)
    second = _next_spec(generation_spec)
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        second,
        generation_evaluation_factory,
    )
    neo4j.fail_target = second.generation_id

    with pytest.raises(GenerationPromotionError, match="failed safely"):
        coordinator.promote(second.generation_id)

    assert repository.active == generation_spec.generation_id
    assert repository.states[second.generation_id] is GenerationState.SEALED
    assert qdrant.active == neo4j.active == generation_spec.generation_id


def test_postgres_commit_failure_compensates_both_external_stores(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_factory,
    )
    coordinator.promote(generation_spec.generation_id)
    second = _next_spec(generation_spec)
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        second,
        generation_evaluation_factory,
    )
    repository.fail_commit_once = True

    with pytest.raises(GenerationPromotionError, match="failed safely"):
        coordinator.promote(second.generation_id)

    assert repository.active == generation_spec.generation_id
    assert repository.states[second.generation_id] is GenerationState.SEALED
    assert qdrant.active == neo4j.active == generation_spec.generation_id


def test_preflight_generation_mismatch_makes_no_changes(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_factory,
    )
    repository.active = "a" * 64
    repository.states[repository.active] = GenerationState.ACTIVE
    qdrant.active = None
    neo4j.active = repository.active

    with pytest.raises(GenerationPromotionError, match="does not match"):
        coordinator.promote(generation_spec.generation_id)

    assert repository.states[generation_spec.generation_id] is GenerationState.SEALED
    assert qdrant.active is None
    assert neo4j.active == repository.active


def test_reconcile_uses_postgres_as_authority_and_records_event(
    generation_spec: GenerationSpec,
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    repository.active = generation_spec.generation_id
    repository.specs[generation_spec.generation_id] = generation_spec
    repository.states[generation_spec.generation_id] = GenerationState.ACTIVE
    for store in (qdrant, neo4j):
        store.prepared.add(generation_spec.generation_id)
        store.active = None

    assert coordinator.reconcile_external_to_postgres() == generation_spec.generation_id
    assert qdrant.active == neo4j.active == generation_spec.generation_id
    assert repository.events[-1][0] == generation_spec.generation_id


def test_guarded_read_detects_generation_change(generation_spec: GenerationSpec) -> None:
    coordinator, repository, _qdrant, _neo4j = _coordinator()
    repository.active = generation_spec.generation_id
    with pytest.raises(GenerationChangedError, match="changed during read"):
        with coordinator.guarded_active_generation() as generation_id:
            assert generation_id == generation_spec.generation_id
            repository.active = "f" * 64
