"""PostgreSQL authority for immutable knowledge generations.

The active pointer in this repository is the final authority.  External store
aliases are switched first and the PostgreSQL pointer is committed last by the
coordinator, so a failed promotion never exposes an uncommitted generation to
readers that resolve through PostgreSQL.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence

from psycopg.types.json import Jsonb

from src.db.postgres import connect
from src.generation import (
    DatasetMember,
    GenerationSpec,
    GenerationState,
    GenerationValidationError,
    SourceManifest,
    StoreAttestation,
    canonical_sha256,
    validate_dataset_members,
)

# Stable application-level lock key.  This lock is transaction-scoped so it is
# released by PostgreSQL even if the writer process crashes.
GENERATION_WRITER_LOCK_KEY = 5426369279540913742


class GenerationBusyError(RuntimeError):
    """Another generation writer currently owns the PostgreSQL lock."""


class GenerationRepositoryError(RuntimeError):
    """Persisted generation state violates the coordinator contract."""


@dataclass(frozen=True, slots=True)
class ActiveGenerationToken:
    generation_id: str | None
    epoch: int


class GenerationTransaction(Protocol):
    def create(self, spec: GenerationSpec) -> None: ...

    def state(self, generation_id: str) -> GenerationState: ...

    def active_generation_id(self) -> str | None: ...

    def active_generation_token(self) -> ActiveGenerationToken: ...

    def load_spec(self, generation_id: str) -> GenerationSpec: ...

    def replace_article_membership(
        self,
        generation_id: str,
        records: Iterable[tuple[int, str]],
    ) -> None: ...

    def article_membership(self, generation_id: str) -> tuple[tuple[int, str], ...]: ...

    def replace_dataset_membership(
        self,
        generation_id: str,
        members: Sequence[DatasetMember],
    ) -> None: ...

    def dataset_membership(self, generation_id: str) -> tuple[DatasetMember, ...]: ...

    def postgres_attestation(
        self,
        generation_id: str,
        *,
        attested_at: str,
    ) -> StoreAttestation: ...

    def validate(
        self,
        generation_id: str,
        attestations: Sequence[StoreAttestation],
    ) -> None: ...

    def stored_attestations(self, generation_id: str) -> tuple[StoreAttestation, ...]: ...

    def seal(
        self,
        generation_id: str,
        *,
        evaluation_report: Mapping[str, Any],
        evaluation_report_sha256: str,
        seal_attestation_sha256: str,
    ) -> None: ...

    def activate(
        self,
        generation_id: str,
        *,
        expected_previous: str | None,
        expected_epoch: int,
        rollback: bool,
    ) -> None: ...

    def record_reconciled(
        self,
        generation_id: str,
        payload: Mapping[str, Any],
    ) -> None: ...

    def abort(self, generation_id: str, *, reason: str) -> None: ...


class GenerationRepository(Protocol):
    def writer_transaction(self) -> AbstractContextManager[GenerationTransaction]: ...

    def active_generation_id(self) -> str | None: ...

    def active_generation_token(self) -> ActiveGenerationToken: ...


class _PostgresGenerationTransaction:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def _fetch_state(self, generation_id: str) -> GenerationState:
        with self._connection.cursor() as cur:
            cur.execute(
                "SELECT state FROM knowledge_generations WHERE generation_id=%s",
                (generation_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise GenerationRepositoryError(f"unknown generation: {generation_id}")
        try:
            return GenerationState(row[0])
        except ValueError as exc:
            raise GenerationRepositoryError("persisted generation state is invalid") from exc

    def state(self, generation_id: str) -> GenerationState:
        return self._fetch_state(generation_id)

    def create(self, spec: GenerationSpec) -> None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO knowledge_generations (
                  generation_id,
                  state,
                  source_manifest,
                  source_manifest_sha256,
                  model_manifest,
                  model_manifest_sha256,
                  index_config,
                  index_config_sha256
                ) VALUES (%s, 'building', %s, %s, %s, %s, %s, %s)
                """,
                (
                    spec.generation_id,
                    Jsonb(spec.source_manifest.as_dict()),
                    spec.source_manifest_sha256,
                    Jsonb(spec.model_manifest),
                    spec.model_manifest_sha256,
                    Jsonb(spec.index_config),
                    spec.index_config_sha256,
                ),
            )
            cur.executemany(
                """
                INSERT INTO knowledge_generation_laws (
                  generation_id,
                  law_id,
                  law_name,
                  mst,
                  official_uri,
                  content_sha256,
                  current_article_count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        spec.generation_id,
                        item.law_id,
                        item.law_name,
                        item.mst,
                        item.official_uri,
                        item.content_sha256,
                        item.current_article_count,
                    )
                    for item in spec.source_manifest.laws
                ],
            )
            cur.executemany(
                """
                INSERT INTO knowledge_generation_datasets (
                  generation_id,
                  dataset_id,
                  snapshot_id,
                  source_definition,
                  source_definition_sha256
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (
                        spec.generation_id,
                        item.dataset_id,
                        item.snapshot_id,
                        Jsonb(item.as_dict()),
                        canonical_sha256(item.as_dict()),
                    )
                    for item in spec.source_manifest.datasets
                ],
            )
            self._insert_event(
                cur,
                spec.generation_id,
                "created",
                None,
                GenerationState.BUILDING,
                {},
            )

    def load_spec(self, generation_id: str) -> GenerationSpec:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT source_manifest, source_manifest_sha256,
                       model_manifest, model_manifest_sha256,
                       index_config, index_config_sha256
                FROM knowledge_generations
                WHERE generation_id=%s
                """,
                (generation_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise GenerationRepositoryError(f"unknown generation: {generation_id}")
        source, source_hash, models, model_hash, config, config_hash = row
        spec = GenerationSpec(
            source_manifest=SourceManifest.from_mapping(source),
            model_manifest=models,
            index_config=config,
        )
        if (
            spec.generation_id != generation_id
            or spec.source_manifest_sha256 != source_hash
            or spec.model_manifest_sha256 != model_hash
            or spec.index_config_sha256 != config_hash
        ):
            raise GenerationRepositoryError("persisted generation identity hash mismatch")
        return spec

    def replace_article_membership(
        self,
        generation_id: str,
        records: Iterable[tuple[int, str]],
    ) -> None:
        if self._fetch_state(generation_id) is not GenerationState.BUILDING:
            raise GenerationRepositoryError("article membership can only change while building")
        normalized: list[tuple[str, int, str]] = []
        seen: set[int] = set()
        model_manifest_sha256 = self.load_spec(generation_id).model_manifest_sha256
        for article_pk, content_sha256 in records:
            if isinstance(article_pk, bool) or not isinstance(article_pk, int) or article_pk <= 0:
                raise GenerationValidationError("article_pk must be a positive integer")
            if article_pk in seen:
                raise GenerationValidationError("article membership contains duplicate IDs")
            seen.add(article_pk)
            # StoreAttestation performs the canonical SHA validation without
            # requiring this repository to expose a second implementation.
            StoreAttestation.from_article_records(
                generation_id=generation_id,
                store_name="postgresql",
                records=[(article_pk, content_sha256)],
                model_manifest_sha256=model_manifest_sha256,
                store_locator=f"generation/{generation_id}/postgresql",
                store_revision="knowledge-generation-schema-v1",
                attested_at="1970-01-01T00:00:00Z",
            )
            normalized.append((generation_id, article_pk, content_sha256))
        if not normalized:
            raise GenerationValidationError("article membership cannot be empty")
        with self._connection.cursor() as cur:
            cur.execute(
                "DELETE FROM knowledge_generation_articles WHERE generation_id=%s",
                (generation_id,),
            )
            cur.executemany(
                """
                INSERT INTO knowledge_generation_articles (
                  generation_id,
                  article_pk,
                  law_id,
                  article_no,
                  paragraph_no,
                  item_no,
                  title,
                  version_mst,
                  valid_from,
                  content,
                  content_sha256
                )
                SELECT %s, a.id, a.law_id, a.article_no, a.paragraph_no,
                       a.item_no, a.title, a.version_mst, a.valid_from,
                       a.content, a.content_hash
                FROM law_articles a
                WHERE a.id=%s
                  AND a.content_hash=%s
                  AND a.is_current
                  AND NOT EXISTS (
                    SELECT 1 FROM law_articles child
                    WHERE child.parent_article_pk=a.id
                  )
                """,
                normalized,
            )
            cur.execute(
                """
                SELECT count(*)
                FROM knowledge_generation_articles
                WHERE generation_id=%s
                """,
                (generation_id,),
            )
            row = cur.fetchone()
            if row is None or int(row[0]) != len(normalized):
                raise GenerationRepositoryError(
                    "article membership does not match the current PostgreSQL ledger"
                )

    def article_membership(self, generation_id: str) -> tuple[tuple[int, str], ...]:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_pk, content_sha256
                FROM knowledge_generation_articles
                WHERE generation_id=%s
                ORDER BY article_pk
                """,
                (generation_id,),
            )
            rows = cur.fetchall()
        return tuple((int(row[0]), str(row[1])) for row in rows)

    def replace_dataset_membership(
        self,
        generation_id: str,
        members: Sequence[DatasetMember],
    ) -> None:
        if self._fetch_state(generation_id) is not GenerationState.BUILDING:
            raise GenerationRepositoryError("dataset membership can only change while building")
        spec = self.load_spec(generation_id)
        normalized = validate_dataset_members(spec.source_manifest, members)
        ccct_snapshot = next(
            item.snapshot_id for item in spec.source_manifest.datasets if item.dataset_id == "ccct"
        )
        expected_ccct = self._ccct_ledger_members(ccct_snapshot)
        actual_ccct = tuple(item for item in normalized if item.dataset_id == "ccct")
        if actual_ccct != expected_ccct:
            raise GenerationRepositoryError(
                "CCCT generation membership does not exactly match PostgreSQL snapshot"
            )
        with self._connection.cursor() as cur:
            cur.execute(
                "DELETE FROM knowledge_generation_dataset_members WHERE generation_id=%s",
                (generation_id,),
            )
            cur.executemany(
                """
                INSERT INTO knowledge_generation_dataset_members (
                  generation_id,
                  dataset_id,
                  snapshot_id,
                  member_kind,
                  member_key,
                  payload_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        generation_id,
                        item.dataset_id,
                        item.snapshot_id,
                        item.member_kind,
                        item.member_key,
                        item.payload_sha256,
                    )
                    for item in normalized
                ],
            )

    def _ccct_ledger_members(self, snapshot_id: str) -> tuple[DatasetMember, ...]:
        members: list[DatasetMember] = []

        def add(kind: str, key: str, payload: Mapping[str, Any]) -> None:
            members.append(
                DatasetMember(
                    dataset_id="ccct",
                    snapshot_id=snapshot_id,
                    member_kind=kind,
                    member_key=key,
                    payload_sha256=canonical_sha256(payload),
                )
            )

        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT hsk10::text, trade_type, source_uri, item_count,
                       query_hash, raw_response
                FROM ccct_queries
                WHERE snapshot_id=%s
                ORDER BY hsk10, trade_type
                """,
                (snapshot_id,),
            )
            for row in cur.fetchall():
                payload = {
                    "hsk10": str(row[0]),
                    "trade_type": str(row[1]),
                    "source_uri": str(row[2]),
                    "item_count": int(row[3]),
                    "query_hash": str(row[4]),
                    "raw_response": row[5],
                }
                add("ccct_query", f"{row[0]}:{row[1]}", payload)
            cur.execute(
                """
                SELECT requirement_id, hsk10::text, trade_type, law_code,
                       law_name, agency_code, agency_name, document_code,
                       document_name, valid_from::text, valid_to::text,
                       rtm_tpcd, requirement_version_hash
                FROM ccct_requirements
                WHERE snapshot_id=%s
                ORDER BY requirement_id
                """,
                (snapshot_id,),
            )
            for row in cur.fetchall():
                payload = {
                    "requirement_id": str(row[0]),
                    "hsk10": str(row[1]),
                    "trade_type": str(row[2]),
                    "law_code": str(row[3]),
                    "law_name": str(row[4]),
                    "agency_code": str(row[5]),
                    "agency_name": str(row[6]),
                    "document_code": str(row[7]),
                    "document_name": str(row[8]),
                    "valid_from": str(row[9]) if row[9] is not None else None,
                    "valid_to": str(row[10]) if row[10] is not None else None,
                    "rtm_tpcd": str(row[11]),
                    "requirement_version_hash": str(row[12]),
                }
                add("ccct_requirement", str(row[0]), payload)
            cur.execute(
                """
                SELECT source_record_id, requirement_id, hsk10::text,
                       trade_type, source_item_index, source_uri,
                       raw_record_hash, raw_record
                FROM ccct_source_records
                WHERE snapshot_id=%s
                ORDER BY source_record_id
                """,
                (snapshot_id,),
            )
            for row in cur.fetchall():
                payload = {
                    "source_record_id": str(row[0]),
                    "requirement_id": str(row[1]),
                    "hsk10": str(row[2]),
                    "trade_type": str(row[3]),
                    "source_item_index": int(row[4]),
                    "source_uri": str(row[5]),
                    "raw_record_hash": str(row[6]),
                    "raw_record": row[7],
                }
                add("ccct_source_record", str(row[0]), payload)
            cur.execute(
                """
                SELECT DISTINCT d.document_code, d.display_name, d.normalized_name
                FROM ccct_requirement_documents d
                JOIN ccct_requirements r USING (document_code)
                WHERE r.snapshot_id=%s
                ORDER BY d.document_code
                """,
                (snapshot_id,),
            )
            for row in cur.fetchall():
                payload = {
                    "document_code": str(row[0]),
                    "display_name": str(row[1]),
                    "normalized_name": str(row[2]),
                }
                add("ccct_document", str(row[0]), payload)
        return tuple(
            sorted(
                members,
                key=lambda item: (
                    item.dataset_id,
                    item.member_kind,
                    item.member_key,
                ),
            )
        )

    def dataset_membership(self, generation_id: str) -> tuple[DatasetMember, ...]:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT dataset_id, snapshot_id, member_kind, member_key, payload_sha256
                FROM knowledge_generation_dataset_members
                WHERE generation_id=%s
                ORDER BY dataset_id, member_kind, member_key
                """,
                (generation_id,),
            )
            rows = cur.fetchall()
        return tuple(
            DatasetMember(
                dataset_id=str(row[0]),
                snapshot_id=str(row[1]),
                member_kind=str(row[2]),
                member_key=str(row[3]),
                payload_sha256=str(row[4]),
            )
            for row in rows
        )

    def postgres_attestation(
        self,
        generation_id: str,
        *,
        attested_at: str,
    ) -> StoreAttestation:
        spec = self.load_spec(generation_id)
        records = self.article_membership(generation_id)
        members = validate_dataset_members(
            spec.source_manifest,
            self.dataset_membership(generation_id),
        )
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT article_pk, law_id, article_no, paragraph_no, item_no,
                       title, version_mst, valid_from::text, content,
                       content_sha256
                FROM knowledge_generation_articles
                WHERE generation_id=%s
                ORDER BY article_pk
                """,
                (generation_id,),
            )
            article_rows = [
                {
                    "article_pk": int(row[0]),
                    "law_id": str(row[1]),
                    "article_no": int(row[2]),
                    "paragraph_no": int(row[3]) if row[3] is not None else None,
                    "item_no": int(row[4]) if row[4] is not None else None,
                    "title": str(row[5]) if row[5] is not None else None,
                    "version_mst": str(row[6]),
                    "valid_from": str(row[7]),
                    "content": str(row[8]),
                    "content_sha256": str(row[9]),
                }
                for row in cur.fetchall()
            ]
        return StoreAttestation.from_article_records(
            generation_id=generation_id,
            store_name="postgresql",
            records=records,
            model_manifest_sha256=spec.model_manifest_sha256,
            store_locator=f"generation/{generation_id}/postgresql",
            store_revision="knowledge-generation-schema-v2",
            attested_at=attested_at,
            projection={
                "schema_version": "2.0",
                "articles": article_rows,
                "datasets": [item.as_dict() for item in members],
                "index_config": spec.index_config,
            },
        )

    def validate(
        self,
        generation_id: str,
        attestations: Sequence[StoreAttestation],
    ) -> None:
        if self._fetch_state(generation_id) is not GenerationState.BUILDING:
            raise GenerationRepositoryError("only a building generation can be validated")
        if not self.article_membership(generation_id):
            raise GenerationRepositoryError("PostgreSQL article membership is empty")
        spec = self.load_spec(generation_id)
        validate_dataset_members(
            spec.source_manifest,
            self.dataset_membership(generation_id),
        )
        with self._connection.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO knowledge_generation_store_attestations (
                  generation_id,
                  store_name,
                  article_count,
                  article_id_set_sha256,
                  article_content_set_sha256,
                  projection_sha256,
                  model_manifest_sha256,
                  store_locator,
                  store_revision,
                  attestation,
                  attestation_sha256,
                  attested_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (generation_id, store_name) DO UPDATE SET
                  article_count=EXCLUDED.article_count,
                  article_id_set_sha256=EXCLUDED.article_id_set_sha256,
                  article_content_set_sha256=EXCLUDED.article_content_set_sha256,
                  projection_sha256=EXCLUDED.projection_sha256,
                  model_manifest_sha256=EXCLUDED.model_manifest_sha256,
                  store_locator=EXCLUDED.store_locator,
                  store_revision=EXCLUDED.store_revision,
                  attestation=EXCLUDED.attestation,
                  attestation_sha256=EXCLUDED.attestation_sha256,
                  attested_at=EXCLUDED.attested_at
                """,
                [
                    (
                        generation_id,
                        item.store_name,
                        item.article_count,
                        item.article_id_set_sha256,
                        item.article_content_set_sha256,
                        item.projection_sha256,
                        item.model_manifest_sha256,
                        item.store_locator,
                        item.store_revision,
                        Jsonb(item.as_dict()),
                        item.sha256,
                        item.attested_at,
                    )
                    for item in attestations
                ],
            )
            cur.execute(
                """
                UPDATE knowledge_generations
                SET state='validated', validated_at=now()
                WHERE generation_id=%s AND state='building'
                """,
                (generation_id,),
            )
            if cur.rowcount != 1:
                raise GenerationRepositoryError("generation validation transition failed")
            self._insert_event(
                cur,
                generation_id,
                "validated",
                GenerationState.BUILDING,
                GenerationState.VALIDATED,
                {},
            )

    def stored_attestations(self, generation_id: str) -> tuple[StoreAttestation, ...]:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT attestation, attestation_sha256
                FROM knowledge_generation_store_attestations
                WHERE generation_id=%s
                ORDER BY store_name
                """,
                (generation_id,),
            )
            rows = cur.fetchall()
        result: list[StoreAttestation] = []
        for payload, stored_sha256 in rows:
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
                raise GenerationRepositoryError("stored attestation is invalid") from exc
            if item.sha256 != stored_sha256:
                raise GenerationRepositoryError("stored attestation hash mismatch")
            result.append(item)
        return tuple(result)

    def seal(
        self,
        generation_id: str,
        *,
        evaluation_report: Mapping[str, Any],
        evaluation_report_sha256: str,
        seal_attestation_sha256: str,
    ) -> None:
        if self._fetch_state(generation_id) is not GenerationState.VALIDATED:
            raise GenerationRepositoryError("only a validated generation can be sealed")
        with self._connection.cursor() as cur:
            cur.execute(
                """
                UPDATE knowledge_generations
                SET state='sealed',
                    evaluation_report=%s,
                    evaluation_report_sha256=%s,
                    human_reviewed_gold=true,
                    seal_attestation_sha256=%s,
                    sealed_at=now()
                WHERE generation_id=%s AND state='validated'
                """,
                (
                    Jsonb(evaluation_report),
                    evaluation_report_sha256,
                    seal_attestation_sha256,
                    generation_id,
                ),
            )
            if cur.rowcount != 1:
                raise GenerationRepositoryError("generation seal transition failed")
            self._insert_event(
                cur,
                generation_id,
                "sealed",
                GenerationState.VALIDATED,
                GenerationState.SEALED,
                {
                    "evaluation_report_sha256": evaluation_report_sha256,
                    "seal_attestation_sha256": seal_attestation_sha256,
                },
            )

    def active_generation_id(self) -> str | None:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT p.generation_id
                FROM knowledge_generation_active_pointer p
                JOIN knowledge_generations g ON g.generation_id=p.generation_id
                WHERE p.singleton AND g.state='active'
                """
            )
            row = cur.fetchone()
        return str(row[0]) if row else None

    def active_generation_token(self) -> ActiveGenerationToken:
        with self._connection.cursor() as cur:
            cur.execute(
                """
                SELECT p.generation_id, p.epoch
                FROM knowledge_generation_active_pointer p
                JOIN knowledge_generations g ON g.generation_id=p.generation_id
                WHERE p.singleton AND g.state='active'
                """
            )
            row = cur.fetchone()
        if row is None:
            return ActiveGenerationToken(None, 0)
        epoch = int(row[1])
        if epoch <= 0:
            raise GenerationRepositoryError("active generation epoch is invalid")
        return ActiveGenerationToken(str(row[0]), epoch)

    def activate(
        self,
        generation_id: str,
        *,
        expected_previous: str | None,
        expected_epoch: int,
        rollback: bool,
    ) -> None:
        token = self.active_generation_token()
        current = token.generation_id
        if current != expected_previous or token.epoch != expected_epoch:
            raise GenerationRepositoryError("active generation changed during promotion")
        target_state = self._fetch_state(generation_id)
        expected_state = GenerationState.RETIRED if rollback else GenerationState.SEALED
        if target_state is not expected_state:
            raise GenerationRepositoryError(
                f"promotion requires {expected_state.value}, got {target_state.value}"
            )
        with self._connection.cursor() as cur:
            if current is not None:
                cur.execute(
                    """
                    UPDATE knowledge_generations
                    SET state='retired', retired_at=now()
                    WHERE generation_id=%s AND state='active'
                    """,
                    (current,),
                )
                if cur.rowcount != 1:
                    raise GenerationRepositoryError("previous active retirement failed")
                self._insert_event(
                    cur,
                    current,
                    "retired",
                    GenerationState.ACTIVE,
                    GenerationState.RETIRED,
                    {"replacement_generation_id": generation_id},
                )
            cur.execute(
                """
                UPDATE knowledge_generations
                SET state='active',
                    previous_generation_id=%s,
                    activated_at=now(),
                    retired_at=NULL
                WHERE generation_id=%s AND state=%s
                """,
                (current, generation_id, expected_state.value),
            )
            if cur.rowcount != 1:
                raise GenerationRepositoryError("target generation activation failed")
            cur.execute(
                """
                INSERT INTO knowledge_generation_active_pointer (
                  singleton, generation_id, previous_generation_id, epoch, changed_at
                ) VALUES (true, %s, %s, 1, now())
                ON CONFLICT (singleton) DO UPDATE SET
                  generation_id=EXCLUDED.generation_id,
                  previous_generation_id=EXCLUDED.previous_generation_id,
                  epoch=knowledge_generation_active_pointer.epoch + 1,
                  changed_at=now()
                """,
                (generation_id, current),
            )
            self._insert_event(
                cur,
                generation_id,
                "rolled_back" if rollback else "promoted",
                expected_state,
                GenerationState.ACTIVE,
                {"previous_generation_id": current},
                active_generation_id=generation_id,
            )

    def record_reconciled(
        self,
        generation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        with self._connection.cursor() as cur:
            state = self._fetch_state(generation_id)
            self._insert_event(
                cur,
                generation_id,
                "reconciled",
                state,
                state,
                payload,
                active_generation_id=self.active_generation_id(),
            )

    def abort(self, generation_id: str, *, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise GenerationRepositoryError("abort reason is invalid")
        if self._fetch_state(generation_id) is not GenerationState.BUILDING:
            raise GenerationRepositoryError("only a building generation can be aborted")
        with self._connection.cursor() as cur:
            cur.execute(
                """
                UPDATE knowledge_generations
                SET state='aborted', aborted_at=now()
                WHERE generation_id=%s AND state='building'
                """,
                (generation_id,),
            )
            if cur.rowcount != 1:
                raise GenerationRepositoryError("generation abort transition failed")
            self._insert_event(
                cur,
                generation_id,
                "aborted",
                GenerationState.BUILDING,
                GenerationState.ABORTED,
                {"reason": reason.strip()},
            )

    @staticmethod
    def _insert_event(
        cur: Any,
        generation_id: str,
        event_type: str,
        from_state: GenerationState | None,
        to_state: GenerationState,
        payload: Mapping[str, Any],
        *,
        active_generation_id: str | None = None,
    ) -> None:
        cur.execute(
            """
            INSERT INTO knowledge_generation_events (
              generation_id,
              event_type,
              from_state,
              to_state,
              active_generation_id,
              event_payload
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                generation_id,
                event_type,
                from_state.value if from_state else None,
                to_state.value,
                active_generation_id,
                Jsonb(payload),
            ),
        )


class PostgresGenerationRepository:
    """Live-callable repository with a fail-fast transaction writer lock."""

    def __init__(
        self,
        connection_factory: Callable[[], AbstractContextManager[Any]] = connect,
    ) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def writer_transaction(self) -> Iterator[GenerationTransaction]:
        with self._connection_factory() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_xact_lock(%s)",
                    (GENERATION_WRITER_LOCK_KEY,),
                )
                row = cur.fetchone()
            if row is None or row[0] is not True:
                raise GenerationBusyError("another knowledge-generation writer is active")
            yield _PostgresGenerationTransaction(connection)

    def active_generation_id(self) -> str | None:
        with self._connection_factory() as connection:
            return _PostgresGenerationTransaction(connection).active_generation_id()

    def active_generation_token(self) -> ActiveGenerationToken:
        with self._connection_factory() as connection:
            return _PostgresGenerationTransaction(connection).active_generation_token()


__all__ = [
    "GENERATION_WRITER_LOCK_KEY",
    "ActiveGenerationToken",
    "GenerationBusyError",
    "GenerationRepository",
    "GenerationRepositoryError",
    "GenerationTransaction",
    "PostgresGenerationRepository",
]
