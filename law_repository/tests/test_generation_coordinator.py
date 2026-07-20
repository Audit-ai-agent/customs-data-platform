from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Sequence

import pytest

from src.generation import (
    DatasetMember,
    GenerationSpec,
    GenerationState,
    StoreAttestation,
    VerifiedEvaluationBundle,
)
from src.generation_coordinator import (
    GenerationChangedError,
    GenerationCoordinator,
    GenerationPromotionError,
)
from src.generation_repository import ActiveGenerationToken, GenerationRepositoryError

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

    def replace_dataset_membership(
        self,
        generation_id: str,
        members: Sequence[DatasetMember],
    ) -> None:
        self.repository.dataset_memberships[generation_id] = tuple(members)

    def dataset_membership(self, generation_id: str) -> tuple[DatasetMember, ...]:
        return self.repository.dataset_memberships[generation_id]

    def article_membership(self, generation_id: str) -> tuple[tuple[int, str], ...]:
        return self.repository.memberships[generation_id]

    def postgres_attestation(
        self,
        generation_id: str,
        *,
        attested_at: str,
    ) -> StoreAttestation:
        spec = self.load_spec(generation_id)
        return StoreAttestation.from_article_records(
            generation_id=generation_id,
            store_name="postgresql",
            records=self.article_membership(generation_id),
            model_manifest_sha256=spec.model_manifest_sha256,
            store_locator=f"generation/{generation_id}/postgresql",
            store_revision="postgresql-v2",
            attested_at=attested_at,
            projection={
                "articles": self.article_membership(generation_id),
                "datasets": [
                    item.as_dict() for item in self.repository.dataset_memberships[generation_id]
                ],
            },
        )

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
        expected_epoch: int,
        rollback: bool,
    ) -> None:
        if self.repository.active != expected_previous or self.repository.epoch != expected_epoch:
            raise GenerationRepositoryError("active changed")
        expected = GenerationState.RETIRED if rollback else GenerationState.SEALED
        if self.state(generation_id) is not expected:
            raise GenerationRepositoryError("wrong target state")
        if expected_previous is not None:
            self.repository.states[expected_previous] = GenerationState.RETIRED
        self.repository.states[generation_id] = GenerationState.ACTIVE
        self.repository.active = generation_id
        self.repository.epoch += 1

    def active_generation_token(self) -> ActiveGenerationToken:
        return ActiveGenerationToken(self.repository.active, self.repository.epoch)

    def abort(self, generation_id: str, *, reason: str) -> None:
        if self.state(generation_id) is not GenerationState.BUILDING or not reason:
            raise GenerationRepositoryError("cannot abort")
        self.repository.states[generation_id] = GenerationState.ABORTED

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
        self.dataset_memberships: dict[str, tuple[DatasetMember, ...]] = {}
        self.attestations: dict[str, tuple[StoreAttestation, ...]] = {}
        self.evaluations: dict[str, tuple[dict[str, Any], str, str]] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.active: str | None = None
        self.epoch = 0
        self.fail_commit_once = False

    @contextmanager
    def writer_transaction(self) -> Iterator[FakeTransaction]:
        snapshot = copy.deepcopy(
            (
                self.specs,
                self.states,
                self.memberships,
                self.dataset_memberships,
                self.attestations,
                self.evaluations,
                self.events,
                self.active,
                self.epoch,
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
                self.dataset_memberships,
                self.attestations,
                self.evaluations,
                self.events,
                self.active,
                self.epoch,
            ) = snapshot
            raise

    def active_generation_id(self) -> str | None:
        return self.active

    def active_generation_token(self) -> ActiveGenerationToken:
        return ActiveGenerationToken(self.active, self.epoch)


class FakeStore:
    def __init__(self, name: str) -> None:
        self.name = name
        self.prepared: set[str] = set()
        self.records: dict[str, tuple[tuple[int, str], ...]] = {}
        self.models: dict[str, str] = {}
        self.active: str | None = None
        self.fail_target: str | None = None

    def prepare_generation(
        self,
        generation_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        _index_config: Mapping[str, Any],
    ) -> None:
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
        _source_manifest_sha256: str,
        model_manifest_sha256: str,
        _index_config: Mapping[str, Any],
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
            projection={"records": self.records[generation_id]},
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
    evaluation_factory: Callable[[str], VerifiedEvaluationBundle],
) -> None:
    coordinator.begin_generation(spec, iter(_RECORDS), ())
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
        index_config={**spec.index_config, "dense_vector_size": 2048},
    )


def test_full_generation_lifecycle_and_explicit_rollback(
    generation_spec: GenerationSpec,
    generation_evaluation_bundle_factory: Callable[[str], VerifiedEvaluationBundle],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_bundle_factory,
    )
    coordinator.promote(generation_spec.generation_id)
    assert coordinator.verify_active_alignment() == generation_spec.generation_id

    second = _next_spec(generation_spec)
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        second,
        generation_evaluation_bundle_factory,
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
    generation_evaluation_bundle_factory: Callable[[str], VerifiedEvaluationBundle],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_bundle_factory,
    )
    coordinator.promote(generation_spec.generation_id)
    second = _next_spec(generation_spec)
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        second,
        generation_evaluation_bundle_factory,
    )
    neo4j.fail_target = second.generation_id

    with pytest.raises(GenerationPromotionError, match="failed safely"):
        coordinator.promote(second.generation_id)

    assert repository.active == generation_spec.generation_id
    assert repository.states[second.generation_id] is GenerationState.SEALED
    assert qdrant.active == neo4j.active == generation_spec.generation_id


def test_postgres_commit_failure_compensates_both_external_stores(
    generation_spec: GenerationSpec,
    generation_evaluation_bundle_factory: Callable[[str], VerifiedEvaluationBundle],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_bundle_factory,
    )
    coordinator.promote(generation_spec.generation_id)
    second = _next_spec(generation_spec)
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        second,
        generation_evaluation_bundle_factory,
    )
    repository.fail_commit_once = True

    with pytest.raises(GenerationPromotionError, match="failed safely"):
        coordinator.promote(second.generation_id)

    assert repository.active == generation_spec.generation_id
    assert repository.states[second.generation_id] is GenerationState.SEALED
    assert qdrant.active == neo4j.active == generation_spec.generation_id


def test_preflight_generation_mismatch_makes_no_changes(
    generation_spec: GenerationSpec,
    generation_evaluation_bundle_factory: Callable[[str], VerifiedEvaluationBundle],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_bundle_factory,
    )
    repository.active = "a" * 64
    repository.states[repository.active] = GenerationState.ACTIVE
    qdrant.active = None
    qdrant.prepared.add(repository.active)
    neo4j.active = repository.active

    with pytest.raises(GenerationPromotionError, match="does not match"):
        coordinator.promote(generation_spec.generation_id)

    assert repository.states[generation_spec.generation_id] is GenerationState.SEALED
    assert qdrant.active == repository.active
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


def test_guarded_read_detects_aba_epoch_even_when_generation_returns(
    generation_spec: GenerationSpec,
) -> None:
    coordinator, repository, _qdrant, _neo4j = _coordinator()
    repository.active = generation_spec.generation_id
    repository.epoch = 7
    with pytest.raises(GenerationChangedError, match="token changed"):
        with coordinator.guarded_active_generation():
            repository.epoch += 2


def test_build_resume_is_exact_and_building_generation_can_abort(
    generation_spec: GenerationSpec,
) -> None:
    coordinator, repository, _qdrant, _neo4j = _coordinator()
    coordinator.begin_generation(generation_spec, iter(_RECORDS), ())
    coordinator.begin_generation(generation_spec, iter(_RECORDS), ())
    assert coordinator.generation_status(generation_spec.generation_id) is GenerationState.BUILDING

    coordinator.abort_generation(generation_spec.generation_id, reason="operator cancelled")
    assert repository.states[generation_spec.generation_id] is GenerationState.ABORTED
    with pytest.raises(GenerationRepositoryError, match="already exists"):
        coordinator.begin_generation(generation_spec, iter(_RECORDS), ())


def test_promotion_re_attests_store_projection_after_seal(
    generation_spec: GenerationSpec,
    generation_evaluation_bundle_factory: Callable[[str], VerifiedEvaluationBundle],
) -> None:
    coordinator, repository, qdrant, neo4j = _coordinator()
    _build_and_seal(
        coordinator,
        (qdrant, neo4j),
        generation_spec,
        generation_evaluation_bundle_factory,
    )
    qdrant.records[generation_spec.generation_id] = (
        (1, "1" * 64),
        (2, "2" * 64),
        (3, "4" * 64),
    )
    with pytest.raises(GenerationPromotionError, match="failed safely"):
        coordinator.promote(generation_spec.generation_id)
    assert repository.states[generation_spec.generation_id] is GenerationState.SEALED
    assert repository.active is None
    assert qdrant.active is neo4j.active is None


def test_seal_rejects_raw_self_asserted_metrics_mapping(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    coordinator, _repository, qdrant, neo4j = _coordinator()
    coordinator.begin_generation(generation_spec, iter(_RECORDS), ())
    for store in (qdrant, neo4j):
        store.records[generation_spec.generation_id] = _RECORDS
    coordinator.validate_generation(generation_spec.generation_id, attested_at=_ATTESTED_AT)

    with pytest.raises(TypeError, match="verified evaluation bundle"):
        coordinator.seal_generation(
            generation_spec.generation_id,
            generation_evaluation_factory(generation_spec.generation_id),  # type: ignore[arg-type]
        )
