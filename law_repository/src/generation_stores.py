"""Generation-isolated Qdrant and Neo4j adapters.

Legacy ``customs_law_current`` and legacy ``Article`` nodes are intentionally
untouched.  New generations use dedicated Qdrant collections and
``GenerationArticle`` Neo4j projections until an explicit promotion changes
their active aliases/pointers.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from qdrant_client import models

from src.db.neo4j import driver as neo4j_driver
from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR, client as qdrant_client
from src.generation import GenerationValidationError, StoreAttestation

ACTIVE_QDRANT_ALIAS = "customs_law_active"
QDRANT_GENERATION_PREFIX = "customs_law_gen_"
_GENERATION_ID = re.compile(r"^[0-9a-f]{64}$")
_QDRANT_PAGE_SIZE = 256
_QDRANT_MAX_ARTICLES = 2_000_000
_POINT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "customs-data-platform/knowledge-generation/article",
)


class GenerationStoreError(RuntimeError):
    """An external generation store failed a safety invariant."""


class ExternalGenerationStore(Protocol):
    name: str

    def prepare_generation(self, generation_id: str, model_manifest_sha256: str) -> None: ...

    def current_active_generation(self) -> str | None: ...

    def set_active_generation(self, generation_id: str | None) -> None: ...

    def attest_generation(
        self,
        generation_id: str,
        model_manifest_sha256: str,
        *,
        attested_at: str,
    ) -> StoreAttestation: ...


def _validate_generation_id(generation_id: str) -> str:
    if not isinstance(generation_id, str) or _GENERATION_ID.fullmatch(generation_id) is None:
        raise GenerationValidationError("generation_id must be a lowercase SHA-256")
    return generation_id


def qdrant_collection_name(generation_id: str) -> str:
    return f"{QDRANT_GENERATION_PREFIX}{_validate_generation_id(generation_id)}"


class QdrantGenerationStore:
    """Live Qdrant adapter using one immutable collection per generation."""

    name = "qdrant"

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = qdrant_client,
        store_revision: str,
        active_alias: str = ACTIVE_QDRANT_ALIAS,
    ) -> None:
        if not store_revision.strip():
            raise ValueError("store_revision is required")
        self._client_factory = client_factory
        self._store_revision = store_revision
        self._active_alias = active_alias

    def prepare_generation(self, generation_id: str, model_manifest_sha256: str) -> None:
        collection = qdrant_collection_name(generation_id)
        if not _GENERATION_ID.fullmatch(model_manifest_sha256):
            raise GenerationValidationError("model_manifest_sha256 must be a lowercase SHA-256")
        qc = self._client_factory()
        if not qc.collection_exists(collection):
            qc.create_collection(
                collection_name=collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=1024,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    BM25_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
                },
            )
        indexes = {
            "generation_id": models.PayloadSchemaType.KEYWORD,
            "article_pk": models.PayloadSchemaType.INTEGER,
            "law_id": models.PayloadSchemaType.KEYWORD,
            "content_sha256": models.PayloadSchemaType.KEYWORD,
            "model_manifest_sha256": models.PayloadSchemaType.KEYWORD,
        }
        for field, schema in indexes.items():
            qc.create_payload_index(
                collection_name=collection,
                field_name=field,
                field_schema=schema,
            )
        # The hash is validated again for every point during attestation.

    def _active_collection(self, qc: Any) -> str | None:
        response = qc.get_aliases()
        aliases = getattr(response, "aliases", response)
        matches = [
            getattr(item, "collection_name", None)
            for item in aliases
            if getattr(item, "alias_name", None) == self._active_alias
        ]
        if len(matches) > 1:
            raise GenerationStoreError("Qdrant active alias is ambiguous")
        return matches[0] if matches else None

    def current_active_generation(self) -> str | None:
        collection = self._active_collection(self._client_factory())
        if collection is None:
            return None
        if not collection.startswith(QDRANT_GENERATION_PREFIX):
            raise GenerationStoreError("Qdrant active alias targets a legacy collection")
        generation_id = collection[len(QDRANT_GENERATION_PREFIX) :]
        return _validate_generation_id(generation_id)

    def set_active_generation(self, generation_id: str | None) -> None:
        qc = self._client_factory()
        current = self._active_collection(qc)
        target = qdrant_collection_name(generation_id) if generation_id is not None else None
        if target == current:
            return
        if target is not None and not qc.collection_exists(target):
            raise GenerationStoreError("target Qdrant generation collection does not exist")
        operations: list[Any] = []
        if current is not None:
            operations.append(
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=self._active_alias)
                )
            )
        if target is not None:
            operations.append(
                models.CreateAliasOperation(
                    create_alias=models.CreateAlias(
                        collection_name=target,
                        alias_name=self._active_alias,
                    )
                )
            )
        if operations:
            qc.update_collection_aliases(change_aliases_operations=operations)

    def upsert_article_projections(
        self,
        generation_id: str,
        model_manifest_sha256: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write a bounded batch to an isolated generation collection."""

        collection = qdrant_collection_name(generation_id)
        if not rows:
            return
        points: list[models.PointStruct] = []
        for row in rows:
            required = {
                "article_pk",
                "law_id",
                "law_name",
                "hierarchy",
                "article_no",
                "paragraph_no",
                "item_no",
                "enforcement_date",
                "text",
                "content_sha256",
                "dense_vector",
                "sparse_indices",
                "sparse_values",
            }
            if set(row) != required:
                raise GenerationValidationError("Qdrant projection fields do not match schema")
            article_pk = row["article_pk"]
            if isinstance(article_pk, bool) or not isinstance(article_pk, int) or article_pk <= 0:
                raise GenerationValidationError("article_pk must be a positive integer")
            content_sha256 = row["content_sha256"]
            if (
                not isinstance(content_sha256, str)
                or _GENERATION_ID.fullmatch(content_sha256) is None
            ):
                raise GenerationValidationError("content_sha256 must be a lowercase SHA-256")
            point_id = str(uuid.uuid5(_POINT_NAMESPACE, f"{generation_id}:{article_pk}"))
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector={
                        DENSE_VECTOR: row["dense_vector"],
                        BM25_VECTOR: models.SparseVector(
                            indices=row["sparse_indices"],
                            values=row["sparse_values"],
                        ),
                    },
                    payload={
                        "generation_id": generation_id,
                        "model_manifest_sha256": model_manifest_sha256,
                        "article_pk": article_pk,
                        "law_id": row["law_id"],
                        "law_name": row["law_name"],
                        "hierarchy": row["hierarchy"],
                        "article_no": row["article_no"],
                        "paragraph_no": row["paragraph_no"],
                        "item_no": row["item_no"],
                        "enforcement_date": row["enforcement_date"],
                        "text": row["text"],
                        "content_sha256": content_sha256,
                    },
                )
            )
        qc = self._client_factory()
        if not qc.collection_exists(collection):
            raise GenerationStoreError("Qdrant generation was not prepared")
        qc.upsert(collection_name=collection, points=points, wait=True)

    def attest_generation(
        self,
        generation_id: str,
        model_manifest_sha256: str,
        *,
        attested_at: str,
    ) -> StoreAttestation:
        collection = qdrant_collection_name(generation_id)
        qc = self._client_factory()
        if not qc.collection_exists(collection):
            raise GenerationStoreError("Qdrant generation collection is missing")
        records: list[tuple[int | str, str]] = []
        offset: Any = None
        seen_offsets: set[str] = set()
        while True:
            points, next_offset = qc.scroll(
                collection_name=collection,
                limit=_QDRANT_PAGE_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = getattr(point, "payload", None)
                if not isinstance(payload, dict):
                    raise GenerationStoreError("Qdrant generation point payload is missing")
                if payload.get("generation_id") != generation_id:
                    raise GenerationStoreError("Qdrant point generation identity mismatch")
                if payload.get("model_manifest_sha256") != model_manifest_sha256:
                    raise GenerationStoreError("Qdrant point model identity mismatch")
                records.append((payload.get("article_pk"), payload.get("content_sha256")))
                if len(records) > _QDRANT_MAX_ARTICLES:
                    raise GenerationStoreError("Qdrant generation exceeds attestation limit")
            if next_offset is None:
                break
            marker = str(next_offset)
            if marker in seen_offsets:
                raise GenerationStoreError("Qdrant generation pagination loop")
            seen_offsets.add(marker)
            offset = next_offset
        return StoreAttestation.from_article_records(
            generation_id=generation_id,
            store_name=self.name,
            records=records,
            model_manifest_sha256=model_manifest_sha256,
            store_locator=collection,
            store_revision=self._store_revision,
            attested_at=attested_at,
        )


class Neo4jGenerationStore:
    """Live Neo4j adapter with generation-tagged article projections."""

    name = "neo4j"

    def __init__(
        self,
        *,
        driver_factory: Callable[[], Any] = neo4j_driver,
        store_revision: str,
        database: str | None = None,
    ) -> None:
        if not store_revision.strip():
            raise ValueError("store_revision is required")
        self._driver_factory = driver_factory
        self._store_revision = store_revision
        self._database = database

    @contextmanager
    def _driver(self) -> Iterator[Any]:
        drv = self._driver_factory()
        try:
            yield drv
        finally:
            drv.close()

    def _session(self, driver: Any) -> Any:
        return driver.session(database=self._database) if self._database else driver.session()

    def prepare_generation(self, generation_id: str, model_manifest_sha256: str) -> None:
        _validate_generation_id(generation_id)
        _validate_generation_id(model_manifest_sha256)
        with self._driver() as drv, self._session(drv) as session:
            row = session.run(
                """
                MERGE (g:KnowledgeGeneration {generation_id: $generation_id})
                ON CREATE SET
                  g.model_manifest_sha256=$model_manifest_sha256,
                  g.created_at=datetime(),
                  g.state='building'
                RETURN g.model_manifest_sha256 AS model_manifest_sha256
                """,
                generation_id=generation_id,
                model_manifest_sha256=model_manifest_sha256,
            ).single()
        if row is None or row["model_manifest_sha256"] != model_manifest_sha256:
            raise GenerationStoreError("Neo4j generation model identity mismatch")

    def upsert_article_projection(
        self,
        *,
        generation_id: str,
        article_pk: int,
        law_id: str,
        content_sha256: str,
        model_manifest_sha256: str,
    ) -> None:
        _validate_generation_id(generation_id)
        _validate_generation_id(content_sha256)
        _validate_generation_id(model_manifest_sha256)
        if isinstance(article_pk, bool) or not isinstance(article_pk, int) or article_pk <= 0:
            raise GenerationValidationError("article_pk must be a positive integer")
        with self._driver() as drv, self._session(drv) as session:
            result = session.run(
                """
                MATCH (g:KnowledgeGeneration {generation_id: $generation_id})
                MERGE (a:GenerationArticle {
                  generation_id: $generation_id,
                  article_pk: $article_pk
                })
                SET a.law_id=$law_id,
                    a.content_sha256=$content_sha256,
                    a.model_manifest_sha256=$model_manifest_sha256
                MERGE (g)-[:CONTAINS]->(a)
                RETURN a.article_pk AS article_pk
                """,
                generation_id=generation_id,
                article_pk=article_pk,
                law_id=law_id,
                content_sha256=content_sha256,
                model_manifest_sha256=model_manifest_sha256,
            ).single()
        if result is None:
            raise GenerationStoreError("Neo4j generation was not prepared")

    def upsert_article_projections(
        self,
        generation_id: str,
        model_manifest_sha256: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write generation-tagged nodes without touching legacy Article nodes."""

        _validate_generation_id(generation_id)
        _validate_generation_id(model_manifest_sha256)
        if not rows:
            return
        normalized: list[dict[str, Any]] = []
        for row in rows:
            required = {
                "article_pk",
                "law_id",
                "law_name",
                "hierarchy",
                "article_no",
                "title",
                "content_sha256",
            }
            if set(row) != required:
                raise GenerationValidationError("Neo4j projection fields do not match schema")
            normalized.append(dict(row))
        with self._driver() as drv, self._session(drv) as session:
            result = session.run(
                """
                MATCH (g:KnowledgeGeneration {generation_id: $generation_id})
                WITH g
                UNWIND $rows AS row
                MERGE (a:GenerationArticle {
                  generation_id: $generation_id,
                  article_pk: row.article_pk
                })
                SET a.law_id=row.law_id,
                    a.law_name=row.law_name,
                    a.hierarchy=row.hierarchy,
                    a.article_no=row.article_no,
                    a.title=row.title,
                    a.content_sha256=row.content_sha256,
                    a.model_manifest_sha256=$model_manifest_sha256
                MERGE (g)-[:CONTAINS]->(a)
                RETURN count(a) AS article_count
                """,
                generation_id=generation_id,
                model_manifest_sha256=model_manifest_sha256,
                rows=normalized,
            ).single()
        if result is None or int(result["article_count"]) != len(normalized):
            raise GenerationStoreError("Neo4j generation batch projection failed")

    def upsert_relation(
        self,
        *,
        generation_id: str,
        source_article_pk: int,
        target_article_pk: int,
        relation_type: str,
    ) -> None:
        _validate_generation_id(generation_id)
        if not relation_type.strip() or len(relation_type) > 100:
            raise GenerationValidationError("relation_type is invalid")
        with self._driver() as drv, self._session(drv) as session:
            result = session.run(
                """
                MATCH (source:GenerationArticle {
                  generation_id: $generation_id,
                  article_pk: $source_article_pk
                })
                MATCH (target:GenerationArticle {
                  generation_id: $generation_id,
                  article_pk: $target_article_pk
                })
                MERGE (source)-[r:GENERATION_RELATION {
                  generation_id: $generation_id,
                  relation_type: $relation_type
                }]->(target)
                RETURN count(r) AS relation_count
                """,
                generation_id=generation_id,
                source_article_pk=source_article_pk,
                target_article_pk=target_article_pk,
                relation_type=relation_type,
            ).single()
        if result is None or int(result["relation_count"]) != 1:
            raise GenerationStoreError("Neo4j relation endpoints are missing")

    def upsert_relations(
        self,
        generation_id: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        _validate_generation_id(generation_id)
        if not rows:
            return
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if set(row) != {
                "source_article_pk",
                "target_article_pk",
                "relation_type",
                "basis",
            }:
                raise GenerationValidationError("Neo4j relation fields do not match schema")
            relation_type = row["relation_type"]
            if not isinstance(relation_type, str) or relation_type not in {
                "CITES",
                "DELEGATES",
                "REFERS_TO",
            }:
                raise GenerationValidationError("relation_type is unsupported")
            normalized.append(dict(row))
        with self._driver() as drv, self._session(drv) as session:
            result = session.run(
                """
                UNWIND $rows AS row
                MATCH (source:GenerationArticle {
                  generation_id: $generation_id,
                  article_pk: row.source_article_pk
                })
                MATCH (target:GenerationArticle {
                  generation_id: $generation_id,
                  article_pk: row.target_article_pk
                })
                MERGE (source)-[r:GENERATION_RELATION {
                  generation_id: $generation_id,
                  relation_type: row.relation_type
                }]->(target)
                SET r.basis=row.basis
                RETURN count(r) AS relation_count
                """,
                generation_id=generation_id,
                rows=normalized,
            ).single()
        if result is None or int(result["relation_count"]) != len(normalized):
            raise GenerationStoreError("Neo4j generation relation projection failed")

    def current_active_generation(self) -> str | None:
        with self._driver() as drv, self._session(drv) as session:
            rows = list(
                session.run(
                    """
                    MATCH (:KnowledgeGenerationPointer {name: 'active'})
                          -[:POINTS_TO]->(g:KnowledgeGeneration)
                    RETURN g.generation_id AS generation_id
                    """
                )
            )
        if len(rows) > 1:
            raise GenerationStoreError("Neo4j active generation pointer is ambiguous")
        return _validate_generation_id(rows[0]["generation_id"]) if rows else None

    def set_active_generation(self, generation_id: str | None) -> None:
        if generation_id is not None:
            _validate_generation_id(generation_id)
        with self._driver() as drv, self._session(drv) as session:
            if generation_id is None:
                session.run(
                    """
                    MATCH (p:KnowledgeGenerationPointer {name: 'active'})
                          -[r:POINTS_TO]->()
                    DELETE r
                    """
                ).consume()
                return
            row = session.run(
                """
                MATCH (g:KnowledgeGeneration {generation_id: $generation_id})
                MERGE (p:KnowledgeGenerationPointer {name: 'active'})
                WITH p, g
                OPTIONAL MATCH (p)-[old:POINTS_TO]->()
                DELETE old
                MERGE (p)-[:POINTS_TO]->(g)
                SET p.epoch=coalesce(p.epoch, 0) + 1,
                    p.changed_at=datetime()
                RETURN g.generation_id AS generation_id
                """,
                generation_id=generation_id,
            ).single()
        if row is None or row["generation_id"] != generation_id:
            raise GenerationStoreError("target Neo4j generation does not exist")

    def attest_generation(
        self,
        generation_id: str,
        model_manifest_sha256: str,
        *,
        attested_at: str,
    ) -> StoreAttestation:
        _validate_generation_id(generation_id)
        with self._driver() as drv, self._session(drv) as session:
            rows = list(
                session.run(
                    """
                    MATCH (:KnowledgeGeneration {generation_id: $generation_id})
                          -[:CONTAINS]->(a:GenerationArticle {
                            generation_id: $generation_id
                          })
                    RETURN a.article_pk AS article_pk,
                           a.content_sha256 AS content_sha256,
                           a.model_manifest_sha256 AS model_manifest_sha256
                    ORDER BY a.article_pk
                    """,
                    generation_id=generation_id,
                )
            )
        records: list[tuple[int | str, str]] = []
        for row in rows:
            if row["model_manifest_sha256"] != model_manifest_sha256:
                raise GenerationStoreError("Neo4j article model identity mismatch")
            records.append((row["article_pk"], row["content_sha256"]))
        return StoreAttestation.from_article_records(
            generation_id=generation_id,
            store_name=self.name,
            records=records,
            model_manifest_sha256=model_manifest_sha256,
            store_locator=f"generation/{generation_id}/neo4j",
            store_revision=self._store_revision,
            attested_at=attested_at,
        )


__all__ = [
    "ACTIVE_QDRANT_ALIAS",
    "ExternalGenerationStore",
    "GenerationStoreError",
    "Neo4jGenerationStore",
    "QDRANT_GENERATION_PREFIX",
    "QdrantGenerationStore",
    "qdrant_collection_name",
]
