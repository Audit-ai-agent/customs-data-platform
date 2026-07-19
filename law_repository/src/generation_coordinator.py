"""Fail-closed orchestration for generation validation, sealing, and promotion."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping, Sequence

from src.generation import (
    GenerationSpec,
    GenerationState,
    StoreAttestation,
    build_seal_attestation,
    validate_rag_evaluation_report,
    validate_store_attestations,
)
from src.generation_repository import (
    GenerationRepository,
    GenerationRepositoryError,
)
from src.generation_stores import ExternalGenerationStore


class GenerationPromotionError(RuntimeError):
    """Promotion failed before PostgreSQL committed the new active pointer."""


class GenerationCompensationError(GenerationPromotionError):
    """Promotion failed and an external store could not be restored."""


class GenerationChangedError(RuntimeError):
    """The active generation changed during a guarded read."""


class GenerationCoordinator:
    """Coordinates PostgreSQL authority with Qdrant and Neo4j projections."""

    def __init__(
        self,
        repository: GenerationRepository,
        external_stores: Sequence[ExternalGenerationStore],
    ) -> None:
        by_name = {store.name: store for store in external_stores}
        if len(by_name) != len(external_stores) or set(by_name) != {"qdrant", "neo4j"}:
            raise ValueError("exactly one qdrant and one neo4j store are required")
        self._repository = repository
        self._stores = tuple(by_name[name] for name in ("qdrant", "neo4j"))

    def begin_generation(
        self,
        spec: GenerationSpec,
        article_membership: Iterable[tuple[int, str]],
    ) -> str:
        """Create immutable PostgreSQL identity and isolated external targets."""

        with self._repository.writer_transaction() as tx:
            tx.create(spec)
            tx.replace_article_membership(spec.generation_id, article_membership)
        # External creation is intentionally after the PostgreSQL building
        # record commits.  A failure leaves a non-readable building generation
        # that can be retried; it never changes the active pointer.
        for store in self._stores:
            store.prepare_generation(spec.generation_id, spec.model_manifest_sha256)
        return spec.generation_id

    def validate_generation(
        self,
        generation_id: str,
        *,
        attested_at: str,
    ) -> tuple[StoreAttestation, ...]:
        """Attest identical article sets in all three stores."""

        with self._repository.writer_transaction() as tx:
            if tx.state(generation_id) is not GenerationState.BUILDING:
                raise GenerationRepositoryError("generation is not building")
            spec = tx.load_spec(generation_id)
            postgres = StoreAttestation.from_article_records(
                generation_id=generation_id,
                store_name="postgresql",
                records=tx.article_membership(generation_id),
                model_manifest_sha256=spec.model_manifest_sha256,
                store_locator=f"generation/{generation_id}/postgresql",
                store_revision="knowledge-generation-schema-v1",
                attested_at=attested_at,
            )
            external = [
                store.attest_generation(
                    generation_id,
                    spec.model_manifest_sha256,
                    attested_at=attested_at,
                )
                for store in self._stores
            ]
            attestations = validate_store_attestations(
                generation_id,
                spec.model_manifest_sha256,
                (postgres, *external),
            )
            tx.validate(generation_id, attestations)
        return attestations

    def seal_generation(
        self,
        generation_id: str,
        evaluation_report: Mapping[str, Any],
    ) -> str:
        """Seal only matching stores and human-reviewed Production RAG Gold."""

        report, report_sha256 = validate_rag_evaluation_report(
            evaluation_report,
            generation_id=generation_id,
        )
        with self._repository.writer_transaction() as tx:
            if tx.state(generation_id) is not GenerationState.VALIDATED:
                raise GenerationRepositoryError("generation is not validated")
            spec = tx.load_spec(generation_id)
            spec.source_manifest.require_release_eligible_sources()
            attestations = validate_store_attestations(
                generation_id,
                spec.model_manifest_sha256,
                tx.stored_attestations(generation_id),
            )
            _, seal_sha256 = build_seal_attestation(
                spec,
                attestations,
                report_sha256,
            )
            tx.seal(
                generation_id,
                evaluation_report=report,
                evaluation_report_sha256=report_sha256,
                seal_attestation_sha256=seal_sha256,
            )
        return seal_sha256

    def promote(self, generation_id: str) -> None:
        self._switch_active(generation_id, rollback=False)

    def rollback(self, generation_id: str) -> None:
        self._switch_active(generation_id, rollback=True)

    def _switch_active(self, generation_id: str, *, rollback: bool) -> None:
        previous: str | None = None
        switched: list[ExternalGenerationStore] = []
        try:
            with self._repository.writer_transaction() as tx:
                target_state = tx.state(generation_id)
                required = GenerationState.RETIRED if rollback else GenerationState.SEALED
                if target_state is not required:
                    raise GenerationRepositoryError(
                        f"target must be {required.value}, got {target_state.value}"
                    )
                previous = tx.active_generation_id()
                if previous == generation_id:
                    raise GenerationRepositoryError("target generation is already active")
                for store in self._stores:
                    observed = store.current_active_generation()
                    if observed != previous:
                        raise GenerationPromotionError(
                            f"{store.name} active generation does not match PostgreSQL"
                        )
                for store in self._stores:
                    store.set_active_generation(generation_id)
                    switched.append(store)
                # PostgreSQL is committed last.  Evidence readers that resolve
                # from its O(1) pointer cannot observe the new generation
                # before both external stores have switched successfully.
                tx.activate(
                    generation_id,
                    expected_previous=previous,
                    rollback=rollback,
                )
        except Exception as exc:
            compensation_errors: list[str] = []
            for store in reversed(switched):
                try:
                    store.set_active_generation(previous)
                except Exception as compensation_exc:  # pragma: no cover - rare dual failure
                    compensation_errors.append(f"{store.name}:{type(compensation_exc).__name__}")
            if compensation_errors:
                raise GenerationCompensationError(
                    "promotion failed and compensation was incomplete: "
                    + ",".join(compensation_errors)
                ) from exc
            if isinstance(exc, (GenerationRepositoryError, GenerationPromotionError)):
                raise
            raise GenerationPromotionError("generation promotion failed safely") from exc

    def reconcile_external_to_postgres(self) -> str | None:
        """Repair aliases after a process crash, using PostgreSQL as authority."""

        authoritative = self._repository.active_generation_id()
        previous_by_store = {
            store.name: store.current_active_generation() for store in self._stores
        }
        changed: list[ExternalGenerationStore] = []
        try:
            for store in self._stores:
                if previous_by_store[store.name] != authoritative:
                    store.set_active_generation(authoritative)
                    changed.append(store)
        except Exception as exc:
            compensation_errors: list[str] = []
            for store in reversed(changed):
                try:
                    store.set_active_generation(previous_by_store[store.name])
                except Exception as compensation_exc:  # pragma: no cover
                    compensation_errors.append(f"{store.name}:{type(compensation_exc).__name__}")
            if compensation_errors:
                raise GenerationCompensationError(
                    "reconciliation compensation was incomplete: " + ",".join(compensation_errors)
                ) from exc
            raise GenerationPromotionError("external reconciliation failed safely") from exc
        if authoritative is not None and changed:
            with self._repository.writer_transaction() as tx:
                if tx.active_generation_id() != authoritative:
                    raise GenerationChangedError(
                        "PostgreSQL active generation changed during reconciliation"
                    )
                tx.record_reconciled(
                    authoritative,
                    {
                        "stores": [store.name for store in changed],
                        "previous": previous_by_store,
                    },
                )
        return authoritative

    def verify_active_alignment(self) -> str | None:
        active = self._repository.active_generation_id()
        for store in self._stores:
            if store.current_active_generation() != active:
                raise GenerationPromotionError(f"{store.name} active generation is not aligned")
        return active

    @contextmanager
    def guarded_active_generation(self) -> Iterator[str]:
        """Detect a promotion crossing a multi-store read operation."""

        before = self._repository.active_generation_id()
        if before is None:
            raise GenerationRepositoryError("no active knowledge generation")
        yield before
        after = self._repository.active_generation_id()
        if after != before:
            raise GenerationChangedError("active generation changed during read")


__all__ = [
    "GenerationChangedError",
    "GenerationCompensationError",
    "GenerationCoordinator",
    "GenerationPromotionError",
]
