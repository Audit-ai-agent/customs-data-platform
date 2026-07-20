"""Fail-closed orchestration for generation validation, sealing, and promotion."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Sequence

from src.generation import (
    DatasetMember,
    GenerationSpec,
    GenerationState,
    StoreAttestation,
    VerifiedEvaluationBundle,
    build_seal_attestation,
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
        dataset_membership: Sequence[DatasetMember],
    ) -> str:
        """Create or idempotently resume one exact building generation."""

        with self._repository.writer_transaction() as tx:
            try:
                state = tx.state(spec.generation_id)
            except GenerationRepositoryError as exc:
                if "unknown generation" not in str(exc):
                    raise
                tx.create(spec)
            else:
                if state is not GenerationState.BUILDING:
                    raise GenerationRepositoryError(
                        f"generation already exists in {state.value} state"
                    )
                if tx.load_spec(spec.generation_id).as_dict() != spec.as_dict():
                    raise GenerationRepositoryError(
                        "building generation identity does not match resume spec"
                    )
            tx.replace_article_membership(spec.generation_id, article_membership)
            tx.replace_dataset_membership(spec.generation_id, dataset_membership)
        # External creation is intentionally after the PostgreSQL building
        # record commits.  A failure leaves a non-readable building generation
        # that can be retried; it never changes the active pointer.
        for store in self._stores:
            store.prepare_generation(
                spec.generation_id,
                spec.source_manifest_sha256,
                spec.model_manifest_sha256,
                spec.index_config,
            )
        return spec.generation_id

    def generation_status(self, generation_id: str) -> GenerationState:
        with self._repository.writer_transaction() as tx:
            return tx.state(generation_id)

    def abort_generation(self, generation_id: str, *, reason: str) -> None:
        with self._repository.writer_transaction() as tx:
            tx.abort(generation_id, reason=reason)

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
            postgres = tx.postgres_attestation(
                generation_id,
                attested_at=attested_at,
            )
            external = [
                store.attest_generation(
                    generation_id,
                    spec.source_manifest_sha256,
                    spec.model_manifest_sha256,
                    spec.index_config,
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
        evaluation_bundle: VerifiedEvaluationBundle,
    ) -> str:
        """Seal only matching stores and human-reviewed Production RAG Gold."""

        if not isinstance(evaluation_bundle, VerifiedEvaluationBundle):
            raise TypeError("seal_generation requires a verified evaluation bundle")
        report = evaluation_bundle.report
        report_sha256 = evaluation_bundle.report_sha256
        if report.get("generation_id") != generation_id:
            raise GenerationRepositoryError("evaluation bundle generation does not match target")
        with self._repository.writer_transaction() as tx:
            if tx.state(generation_id) is not GenerationState.VALIDATED:
                raise GenerationRepositoryError("generation is not validated")
            spec = tx.load_spec(generation_id)
            spec.source_manifest.require_release_eligible_sources(
                as_of=str(report["generated_at"])[:10],
            )
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
        try:
            with self._repository.writer_transaction() as tx:
                target_state = tx.state(generation_id)
                required = GenerationState.RETIRED if rollback else GenerationState.SEALED
                if target_state is not required:
                    raise GenerationRepositoryError(
                        f"target must be {required.value}, got {target_state.value}"
                    )
                token = tx.active_generation_token()
                previous = token.generation_id
                if previous == generation_id:
                    raise GenerationRepositoryError("target generation is already active")
                spec = tx.load_spec(generation_id)
                sealed_attestations = validate_store_attestations(
                    generation_id,
                    spec.model_manifest_sha256,
                    tx.stored_attestations(generation_id),
                )
                attested_at = (
                    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                )
                live_attestations = validate_store_attestations(
                    generation_id,
                    spec.model_manifest_sha256,
                    (
                        tx.postgres_attestation(
                            generation_id,
                            attested_at=attested_at,
                        ),
                        *(
                            store.attest_generation(
                                generation_id,
                                spec.source_manifest_sha256,
                                spec.model_manifest_sha256,
                                spec.index_config,
                                attested_at=attested_at,
                            )
                            for store in self._stores
                        ),
                    ),
                )
                sealed_projection = {
                    item.store_name: item.sealed_projection_sha256 for item in sealed_attestations
                }
                live_projection = {
                    item.store_name: item.sealed_projection_sha256 for item in live_attestations
                }
                if live_projection != sealed_projection:
                    raise GenerationPromotionError("store projection changed after generation seal")
                for store in self._stores:
                    observed = store.current_active_generation()
                    if observed != previous:
                        raise GenerationPromotionError(
                            f"{store.name} active generation does not match PostgreSQL"
                        )
                for store in self._stores:
                    store.set_active_generation(generation_id)
                # PostgreSQL is committed last.  Evidence readers that resolve
                # from its O(1) pointer cannot observe the new generation
                # before both external stores have switched successfully.
                tx.activate(
                    generation_id,
                    expected_previous=previous,
                    expected_epoch=token.epoch,
                    rollback=rollback,
                )
        except Exception as exc:
            try:
                self._converge_external_to_postgres()
            except Exception as compensation_exc:
                raise GenerationCompensationError(
                    "promotion failed and external stores could not converge to PostgreSQL"
                ) from compensation_exc
            if isinstance(exc, (GenerationRepositoryError, GenerationPromotionError)):
                raise
            raise GenerationPromotionError("generation promotion failed safely") from exc

    def _converge_external_to_postgres(self) -> str | None:
        with self._repository.writer_transaction() as tx:
            token = tx.active_generation_token()
            for store in self._stores:
                if store.current_active_generation() != token.generation_id:
                    store.set_active_generation(token.generation_id)
            if tx.active_generation_token() != token:
                raise GenerationChangedError("PostgreSQL active token changed during convergence")
            return token.generation_id

    def reconcile_external_to_postgres(self) -> str | None:
        """Repair aliases after a process crash, using PostgreSQL as authority."""

        try:
            with self._repository.writer_transaction() as tx:
                token = tx.active_generation_token()
                authoritative = token.generation_id
                previous_by_store = {
                    store.name: store.current_active_generation() for store in self._stores
                }
                changed: list[ExternalGenerationStore] = []
                for store in self._stores:
                    if previous_by_store[store.name] != authoritative:
                        store.set_active_generation(authoritative)
                        changed.append(store)
                if tx.active_generation_token() != token:
                    raise GenerationChangedError(
                        "PostgreSQL active token changed during reconciliation"
                    )
                if authoritative is not None and changed:
                    tx.record_reconciled(
                        authoritative,
                        {
                            "epoch": token.epoch,
                            "stores": [store.name for store in changed],
                            "previous": previous_by_store,
                        },
                    )
        except (GenerationChangedError, GenerationRepositoryError):
            raise
        except Exception as exc:
            raise GenerationPromotionError("external reconciliation failed safely") from exc
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

        before = self._repository.active_generation_token()
        if before.generation_id is None:
            raise GenerationRepositoryError("no active knowledge generation")
        yield before.generation_id
        after = self._repository.active_generation_token()
        if after != before:
            raise GenerationChangedError("active generation token changed during read")


__all__ = [
    "GenerationChangedError",
    "GenerationCompensationError",
    "GenerationCoordinator",
    "GenerationPromotionError",
]
