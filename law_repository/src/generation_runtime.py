"""O(1) active-generation attestation loader for Evidence runtime wiring."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.db.postgres import connect
from src.deadline import RequestDeadline
from src.generation import (
    GenerationSpec,
    GenerationValidationError,
    SourceManifest,
    StoreAttestation,
    build_seal_attestation,
    canonical_sha256,
    validate_rag_evaluation_report,
    validate_store_attestations,
)
from src.generation_stores import ExternalGenerationStore


def _deadline_connection(deadline: RequestDeadline | None = None):
    return connect(deadline=deadline)


class ActiveGenerationError(RuntimeError):
    """The active pointer or its sealed evidence is absent or inconsistent."""


@dataclass(frozen=True, slots=True)
class ActiveGenerationAttestation:
    generation_id: str
    epoch: int
    changed_at: str
    source_manifest_sha256: str
    model_manifest_sha256: str
    index_config_sha256: str
    evaluation_report_sha256: str
    seal_attestation_sha256: str
    stores: tuple[StoreAttestation, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "generation_id": self.generation_id,
            "epoch": self.epoch,
            "changed_at": self.changed_at,
            "source_manifest_sha256": self.source_manifest_sha256,
            "model_manifest_sha256": self.model_manifest_sha256,
            "index_config_sha256": self.index_config_sha256,
            "evaluation_report_sha256": self.evaluation_report_sha256,
            "seal_attestation_sha256": self.seal_attestation_sha256,
            "stores": [item.as_dict() for item in self.stores],
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.as_dict())


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ActiveGenerationError("active pointer timestamp is not timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _store_attestation(payload: Mapping[str, Any], stored_sha256: str) -> StoreAttestation:
    try:
        item = StoreAttestation(
            generation_id=payload["generation_id"],
            store_name=payload["store_name"],
            article_count=payload["article_count"],
            article_id_set_sha256=payload["article_id_set_sha256"],
            article_content_set_sha256=payload["article_content_set_sha256"],
            projection_sha256=payload["projection_sha256"],
            model_manifest_sha256=payload["model_manifest_sha256"],
            store_locator=payload["store_locator"],
            store_revision=payload["store_revision"],
            attested_at=payload["attested_at"],
        )
    except (KeyError, TypeError, GenerationValidationError) as exc:
        raise ActiveGenerationError("stored generation attestation is invalid") from exc
    if item.sha256 != stored_sha256:
        raise ActiveGenerationError("stored generation attestation hash mismatch")
    return item


class ActiveGenerationLoader:
    """Load a sealed pointer without scanning laws, vectors, or graph nodes."""

    def __init__(
        self,
        *,
        connection_factory: Callable[[RequestDeadline | None], Any] | None = None,
        external_stores: Sequence[ExternalGenerationStore] = (),
    ) -> None:
        names = [store.name for store in external_stores]
        if len(names) != len(set(names)) or any(name not in {"qdrant", "neo4j"} for name in names):
            raise ValueError("external generation store names are invalid")
        self._connection_factory = connection_factory or _deadline_connection
        self._external_stores = tuple(external_stores)

    def load(
        self,
        deadline: RequestDeadline | None = None,
    ) -> ActiveGenerationAttestation:
        connection_context = (
            self._connection_factory(deadline)
            if deadline is not None
            else self._connection_factory()  # type: ignore[call-arg]
        )
        with connection_context as connection:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.generation_id, p.epoch, p.changed_at,
                           g.state,
                           g.source_manifest, g.source_manifest_sha256,
                           g.model_manifest, g.model_manifest_sha256,
                           g.index_config, g.index_config_sha256,
                           g.evaluation_report, g.evaluation_report_sha256,
                           g.human_reviewed_gold, g.seal_attestation_sha256
                    FROM knowledge_generation_active_pointer p
                    JOIN knowledge_generations g
                      ON g.generation_id=p.generation_id
                    WHERE p.singleton
                    """
                )
                row = cur.fetchone()
            if row is None:
                raise ActiveGenerationError("no active knowledge generation")
            (
                generation_id,
                epoch,
                changed_at,
                state,
                source_manifest,
                source_manifest_sha256,
                model_manifest,
                model_manifest_sha256,
                index_config,
                index_config_sha256,
                evaluation_report,
                evaluation_report_sha256,
                human_reviewed_gold,
                seal_attestation_sha256,
            ) = row
            if state != "active" or human_reviewed_gold is not True:
                raise ActiveGenerationError("active generation is not sealed and reviewed")
            try:
                spec = GenerationSpec(
                    source_manifest=SourceManifest.from_mapping(source_manifest),
                    model_manifest=model_manifest,
                    index_config=index_config,
                )
                spec.source_manifest.require_release_eligible_sources()
            except GenerationValidationError as exc:
                raise ActiveGenerationError("active generation identity is invalid") from exc
            if (
                spec.generation_id != generation_id
                or spec.source_manifest_sha256 != source_manifest_sha256
                or spec.model_manifest_sha256 != model_manifest_sha256
                or spec.index_config_sha256 != index_config_sha256
            ):
                raise ActiveGenerationError("active generation identity hash mismatch")
            try:
                report, computed_report_sha256 = validate_rag_evaluation_report(
                    evaluation_report,
                    generation_id=generation_id,
                )
            except GenerationValidationError as exc:
                raise ActiveGenerationError("active generation evaluation is invalid") from exc
            if report != evaluation_report or computed_report_sha256 != evaluation_report_sha256:
                raise ActiveGenerationError("active generation evaluation hash mismatch")
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT attestation, attestation_sha256
                    FROM knowledge_generation_store_attestations
                    WHERE generation_id=%s
                    ORDER BY store_name
                    """,
                    (generation_id,),
                )
                attestation_rows = cur.fetchall()
            attestations = tuple(
                _store_attestation(payload, stored_hash)
                for payload, stored_hash in attestation_rows
            )
            try:
                ordered = validate_store_attestations(
                    generation_id,
                    model_manifest_sha256,
                    attestations,
                )
                _, computed_seal_sha256 = build_seal_attestation(
                    spec,
                    ordered,
                    evaluation_report_sha256,
                )
            except GenerationValidationError as exc:
                raise ActiveGenerationError("active store attestations are invalid") from exc
            if computed_seal_sha256 != seal_attestation_sha256:
                raise ActiveGenerationError("active generation seal hash mismatch")
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT generation_id, epoch
                    FROM knowledge_generation_active_pointer
                    WHERE singleton
                    """
                )
                final_pointer = cur.fetchone()
            if final_pointer != (generation_id, epoch):
                raise ActiveGenerationError("active generation changed during attestation load")

        for store in self._external_stores:
            current = (
                store.current_active_generation(deadline)
                if deadline is not None
                else store.current_active_generation()
            )
            if current != generation_id:
                raise ActiveGenerationError(
                    f"{store.name} active pointer does not match PostgreSQL"
                )
        return ActiveGenerationAttestation(
            generation_id=generation_id,
            epoch=int(epoch),
            changed_at=_utc_timestamp(changed_at),
            source_manifest_sha256=source_manifest_sha256,
            model_manifest_sha256=model_manifest_sha256,
            index_config_sha256=index_config_sha256,
            evaluation_report_sha256=evaluation_report_sha256,
            seal_attestation_sha256=seal_attestation_sha256,
            stores=ordered,
        )


__all__ = [
    "ActiveGenerationAttestation",
    "ActiveGenerationError",
    "ActiveGenerationLoader",
]
