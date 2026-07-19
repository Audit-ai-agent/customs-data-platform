"""Generation-isolated Qdrant and Neo4j adapters.

Legacy ``customs_law_current`` and legacy ``Article`` nodes are intentionally
untouched.  New generations use dedicated Qdrant collections and
``GenerationArticle`` Neo4j projections until an explicit promotion changes
their active aliases/pointers.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from qdrant_client import models

from src.db.neo4j import driver as neo4j_driver
from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR, client as qdrant_client
from src.generation import (
    GenerationValidationError,
    StoreAttestation,
    canonical_sha256,
    validate_index_config,
)

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

    def prepare_generation(
        self,
        generation_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        index_config: Mapping[str, Any],
    ) -> None: ...

    def current_active_generation(self) -> str | None: ...

    def set_active_generation(self, generation_id: str | None) -> None: ...

    def attest_generation(
        self,
        generation_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        index_config: Mapping[str, Any],
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

    def prepare_generation(
        self,
        generation_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        index_config: Mapping[str, Any],
    ) -> None:
        collection = qdrant_collection_name(generation_id)
        config = validate_index_config(index_config)
        _validate_generation_id(source_manifest_sha256)
        if not _GENERATION_ID.fullmatch(model_manifest_sha256):
            raise GenerationValidationError("model_manifest_sha256 must be a lowercase SHA-256")
        qc = self._client_factory()
        if not qc.collection_exists(collection):
            qc.create_collection(
                collection_name=collection,
                vectors_config={
                    DENSE_VECTOR: models.VectorParams(
                        size=config["dense_vector_size"],
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
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write a bounded batch to an isolated generation collection."""

        collection = qdrant_collection_name(generation_id)
        _validate_generation_id(source_manifest_sha256)
        _validate_generation_id(model_manifest_sha256)
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
                        "source_manifest_sha256": source_manifest_sha256,
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
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        index_config: Mapping[str, Any],
        *,
        attested_at: str,
    ) -> StoreAttestation:
        collection = qdrant_collection_name(generation_id)
        qc = self._client_factory()
        if not qc.collection_exists(collection):
            raise GenerationStoreError("Qdrant generation collection is missing")
        config = validate_index_config(index_config)
        observed_config = self._collection_projection(qc, collection)
        expected_config = {
            "dense_vector_name": config["dense_vector_name"],
            "dense_vector_size": config["dense_vector_size"],
            "distance": config["distance"],
            "sparse_vector_name": config["sparse_vector_name"],
            "sparse_modifier": config["sparse_modifier"],
        }
        if observed_config != expected_config:
            raise GenerationStoreError("Qdrant collection config does not match generation")
        records: list[tuple[int | str, str]] = []
        projections: list[dict[str, Any]] = []
        offset: Any = None
        seen_offsets: set[str] = set()
        while True:
            points, next_offset = qc.scroll(
                collection_name=collection,
                limit=_QDRANT_PAGE_SIZE,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for point in points:
                payload = getattr(point, "payload", None)
                if not isinstance(payload, dict):
                    raise GenerationStoreError("Qdrant generation point payload is missing")
                if payload.get("generation_id") != generation_id:
                    raise GenerationStoreError("Qdrant point generation identity mismatch")
                if payload.get("model_manifest_sha256") != model_manifest_sha256:
                    raise GenerationStoreError("Qdrant point model identity mismatch")
                if payload.get("source_manifest_sha256") != source_manifest_sha256:
                    raise GenerationStoreError("Qdrant point source identity mismatch")
                required_payload = {
                    "generation_id",
                    "source_manifest_sha256",
                    "model_manifest_sha256",
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
                }
                if set(payload) != required_payload:
                    raise GenerationStoreError("Qdrant point payload fields do not match schema")
                text = payload.get("text")
                content_sha256 = payload.get("content_sha256")
                if (
                    not isinstance(text, str)
                    or not text
                    or hashlib.sha256(text.encode("utf-8")).hexdigest() != content_sha256
                ):
                    raise GenerationStoreError("Qdrant payload text hash mismatch")
                vectors = getattr(point, "vector", None)
                dense, sparse_indices, sparse_values = self._vectors(vectors, config)
                article_pk = payload.get("article_pk")
                records.append((article_pk, content_sha256))
                projections.append(
                    {
                        "point_id": str(getattr(point, "id", "")),
                        "payload": payload,
                        "dense_vector_sha256": canonical_sha256(dense),
                        "sparse_indices": sparse_indices,
                        "sparse_values_sha256": canonical_sha256(sparse_values),
                    }
                )
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
            projection={
                "schema_version": "2.0",
                "collection_config": observed_config,
                "points": sorted(
                    projections,
                    key=lambda item: (
                        item["payload"]["article_pk"],
                        item["point_id"],
                    ),
                ),
            },
        )

    @staticmethod
    def _collection_projection(qc: Any, collection: str) -> dict[str, Any]:
        try:
            info = qc.get_collection(collection)
            params = info.config.params
            vectors = params.vectors
            sparse_vectors = params.sparse_vectors
            dense = (
                vectors[DENSE_VECTOR]
                if isinstance(vectors, Mapping)
                else getattr(vectors, DENSE_VECTOR)
            )
            sparse = (
                sparse_vectors[BM25_VECTOR]
                if isinstance(sparse_vectors, Mapping)
                else getattr(sparse_vectors, BM25_VECTOR)
            )
            distance = getattr(dense, "distance")
            modifier = getattr(sparse, "modifier")
            return {
                "dense_vector_name": DENSE_VECTOR,
                "dense_vector_size": int(getattr(dense, "size")),
                "distance": str(getattr(distance, "value", distance)).lower(),
                "sparse_vector_name": BM25_VECTOR,
                "sparse_modifier": str(getattr(modifier, "value", modifier)).lower(),
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            raise GenerationStoreError("Qdrant collection config is unavailable") from None

    @staticmethod
    def _vectors(
        value: Any,
        config: Mapping[str, Any],
    ) -> tuple[list[float], list[int], list[float]]:
        if not isinstance(value, Mapping):
            raise GenerationStoreError("Qdrant point vectors are missing")
        dense_raw = value.get(config["dense_vector_name"])
        sparse_raw = value.get(config["sparse_vector_name"])
        if not isinstance(dense_raw, (list, tuple)):
            raise GenerationStoreError("Qdrant dense vector is missing")
        dense = [float(item) for item in dense_raw]
        if len(dense) != config["dense_vector_size"] or any(
            not math.isfinite(item) for item in dense
        ):
            raise GenerationStoreError("Qdrant dense vector does not match config")
        indices_raw = (
            sparse_raw.get("indices")
            if isinstance(sparse_raw, Mapping)
            else getattr(sparse_raw, "indices", None)
        )
        values_raw = (
            sparse_raw.get("values")
            if isinstance(sparse_raw, Mapping)
            else getattr(sparse_raw, "values", None)
        )
        if not isinstance(indices_raw, (list, tuple)) or not isinstance(values_raw, (list, tuple)):
            raise GenerationStoreError("Qdrant sparse vector is missing")
        indices = [int(item) for item in indices_raw]
        values = [float(item) for item in values_raw]
        if (
            not indices
            or len(indices) != len(values)
            or indices != sorted(indices)
            or len(indices) != len(set(indices))
            or any(item < 0 for item in indices)
            or any(not math.isfinite(item) for item in values)
        ):
            raise GenerationStoreError("Qdrant sparse vector is invalid")
        return dense, indices, values


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

    def prepare_generation(
        self,
        generation_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        index_config: Mapping[str, Any],
    ) -> None:
        _validate_generation_id(generation_id)
        _validate_generation_id(source_manifest_sha256)
        _validate_generation_id(model_manifest_sha256)
        config = validate_index_config(index_config)
        index_config_sha256 = canonical_sha256(config)
        with self._driver() as drv, self._session(drv) as session:
            row = session.run(
                """
                MERGE (g:KnowledgeGeneration {generation_id: $generation_id})
                ON CREATE SET
                  g.source_manifest_sha256=$source_manifest_sha256,
                  g.model_manifest_sha256=$model_manifest_sha256,
                  g.index_config_sha256=$index_config_sha256,
                  g.created_at=datetime(),
                  g.state='building'
                RETURN g.source_manifest_sha256 AS source_manifest_sha256,
                       g.model_manifest_sha256 AS model_manifest_sha256,
                       g.index_config_sha256 AS index_config_sha256
                """,
                generation_id=generation_id,
                source_manifest_sha256=source_manifest_sha256,
                model_manifest_sha256=model_manifest_sha256,
                index_config_sha256=index_config_sha256,
            ).single()
        if (
            row is None
            or row["source_manifest_sha256"] != source_manifest_sha256
            or row["model_manifest_sha256"] != model_manifest_sha256
            or row["index_config_sha256"] != index_config_sha256
        ):
            raise GenerationStoreError("Neo4j generation model identity mismatch")

    def upsert_article_projection(
        self,
        *,
        generation_id: str,
        article_pk: int,
        law_id: str,
        content_sha256: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
    ) -> None:
        _validate_generation_id(generation_id)
        _validate_generation_id(content_sha256)
        _validate_generation_id(source_manifest_sha256)
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
                    a.source_manifest_sha256=$source_manifest_sha256,
                    a.model_manifest_sha256=$model_manifest_sha256
                MERGE (g)-[:CONTAINS]->(a)
                RETURN a.article_pk AS article_pk
                """,
                generation_id=generation_id,
                article_pk=article_pk,
                law_id=law_id,
                content_sha256=content_sha256,
                source_manifest_sha256=source_manifest_sha256,
                model_manifest_sha256=model_manifest_sha256,
            ).single()
        if result is None:
            raise GenerationStoreError("Neo4j generation was not prepared")

    def upsert_article_projections(
        self,
        generation_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Write generation-tagged nodes without touching legacy Article nodes."""

        _validate_generation_id(generation_id)
        _validate_generation_id(source_manifest_sha256)
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
                    a.source_manifest_sha256=$source_manifest_sha256,
                    a.model_manifest_sha256=$model_manifest_sha256
                MERGE (g)-[:CONTAINS]->(a)
                RETURN count(a) AS article_count
                """,
                generation_id=generation_id,
                source_manifest_sha256=source_manifest_sha256,
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

    def upsert_hsk_projections(
        self,
        generation_id: str,
        snapshot_id: str,
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        """Copy the exact immutable HSK leaf release into generation nodes."""

        _validate_generation_id(generation_id)
        _validate_generation_id(source_manifest_sha256)
        _validate_generation_id(model_manifest_sha256)
        if not snapshot_id.strip() or not rows:
            raise GenerationValidationError("HSK generation projection is empty")
        normalized: list[dict[str, Any]] = []
        for row in rows:
            required = {
                "code",
                "hs6",
                "heading4",
                "chapter2",
                "name_ko",
                "name_en",
                "valid_from",
                "valid_to",
                "quantity_unit",
                "weight_unit",
                "category_code",
                "category_name",
                "payload_sha256",
            }
            if set(row) != required:
                raise GenerationValidationError("GenerationHSK fields do not match schema")
            hsk10 = row["code"]
            if not isinstance(hsk10, str) or re.fullmatch(r"[0-9]{10}", hsk10) is None:
                raise GenerationValidationError("GenerationHSK hsk10 is invalid")
            if (
                canonical_sha256({k: row[k] for k in row if k != "payload_sha256"})
                != row["payload_sha256"]
            ):
                raise GenerationValidationError("GenerationHSK payload hash mismatch")
            normalized.append(dict(row))
        if len({row["code"] for row in normalized}) != len(normalized):
            raise GenerationValidationError("GenerationHSK contains duplicate leaves")
        with self._driver() as drv, self._session(drv) as session:
            result = session.run(
                """
                MATCH (g:KnowledgeGeneration {generation_id: $generation_id})
                WITH g
                UNWIND $rows AS row
                MERGE (h:GenerationHSK {
                  generation_id: $generation_id,
                  snapshot_id: $snapshot_id,
                  code: row.code
                })
                SET h.source_manifest_sha256=$source_manifest_sha256,
                    h.model_manifest_sha256=$model_manifest_sha256,
                    h.content_sha256=row.payload_sha256,
                    h.hs6=row.hs6,
                    h.heading4=row.heading4,
                    h.chapter2=row.chapter2,
                    h.name_ko=row.name_ko,
                    h.name_en=row.name_en,
                    h.valid_from=row.valid_from,
                    h.valid_to=row.valid_to,
                    h.quantity_unit=row.quantity_unit,
                    h.weight_unit=row.weight_unit,
                    h.category_code=row.category_code,
                    h.category_name=row.category_name,
                    h.payload_sha256=row.payload_sha256
                MERGE (g)-[:CONTAINS_HSK {
                  generation_id: $generation_id,
                  snapshot_id: $snapshot_id
                }]->(h)
                RETURN count(h) AS hsk_count
                """,
                generation_id=generation_id,
                snapshot_id=snapshot_id,
                source_manifest_sha256=source_manifest_sha256,
                model_manifest_sha256=model_manifest_sha256,
                rows=normalized,
            ).single()
        if result is None or int(result["hsk_count"]) != len(normalized):
            raise GenerationStoreError("Neo4j HSK generation projection failed")

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
        source_manifest_sha256: str,
        model_manifest_sha256: str,
        index_config: Mapping[str, Any],
        *,
        attested_at: str,
    ) -> StoreAttestation:
        _validate_generation_id(generation_id)
        config = validate_index_config(index_config)
        with self._driver() as drv, self._session(drv) as session:
            rows = list(
                session.run(
                    """
                    MATCH (:KnowledgeGeneration {generation_id: $generation_id})
                          -[:CONTAINS]->(a:GenerationArticle {
                            generation_id: $generation_id
                          })
                    RETURN a.article_pk AS article_pk,
                           a.law_id AS law_id,
                           a.law_name AS law_name,
                           a.hierarchy AS hierarchy,
                           a.article_no AS article_no,
                           a.title AS title,
                           a.content_sha256 AS content_sha256,
                           a.source_manifest_sha256 AS source_manifest_sha256,
                           a.model_manifest_sha256 AS model_manifest_sha256
                    ORDER BY a.article_pk
                    """,
                    generation_id=generation_id,
                )
            )
            relation_rows = list(
                session.run(
                    """
                    MATCH (source:GenerationArticle {
                      generation_id: $generation_id
                    })-[r:GENERATION_RELATION {
                      generation_id: $generation_id
                    }]->(target:GenerationArticle {
                      generation_id: $generation_id
                    })
                    RETURN source.article_pk AS source_article_pk,
                           target.article_pk AS target_article_pk,
                           r.relation_type AS relation_type,
                           r.basis AS basis
                    ORDER BY source.article_pk, target.article_pk,
                             r.relation_type, r.basis
                    """,
                    generation_id=generation_id,
                )
            )
            hsk_rows = list(
                session.run(
                    """
                    MATCH (:KnowledgeGeneration {generation_id: $generation_id})
                          -[rel:CONTAINS_HSK {
                            generation_id: $generation_id
                          }]->(h:GenerationHSK {
                            generation_id: $generation_id
                          })
                    RETURN h.code AS code,
                           h.snapshot_id AS snapshot_id,
                           h.source_manifest_sha256 AS source_manifest_sha256,
                           h.model_manifest_sha256 AS model_manifest_sha256,
                           h.content_sha256 AS content_sha256,
                           h.hs6 AS hs6,
                           h.heading4 AS heading4,
                           h.chapter2 AS chapter2,
                           h.name_ko AS name_ko,
                           h.name_en AS name_en,
                           h.valid_from AS valid_from,
                           h.valid_to AS valid_to,
                           h.quantity_unit AS quantity_unit,
                           h.weight_unit AS weight_unit,
                           h.category_code AS category_code,
                           h.category_name AS category_name,
                           h.payload_sha256 AS payload_sha256,
                           rel.snapshot_id AS relation_snapshot_id
                    ORDER BY h.code
                    """,
                    generation_id=generation_id,
                )
            )
        records: list[tuple[int | str, str]] = []
        node_projection: list[dict[str, Any]] = []
        for row in rows:
            if row["model_manifest_sha256"] != model_manifest_sha256:
                raise GenerationStoreError("Neo4j article model identity mismatch")
            if row["source_manifest_sha256"] != source_manifest_sha256:
                raise GenerationStoreError("Neo4j article source identity mismatch")
            records.append((row["article_pk"], row["content_sha256"]))
            node_projection.append(dict(row))
        relation_projection: list[dict[str, Any]] = []
        article_ids = {int(row["article_pk"]) for row in rows}
        for row in relation_rows:
            normalized = dict(row)
            if (
                normalized["source_article_pk"] not in article_ids
                or normalized["target_article_pk"] not in article_ids
                or normalized["relation_type"] not in {"CITES", "DELEGATES", "REFERS_TO"}
                or not isinstance(normalized["basis"], str)
                or not normalized["basis"].strip()
            ):
                raise GenerationStoreError("Neo4j generation relation is invalid")
            relation_projection.append(normalized)
        hsk_projection: list[dict[str, Any]] = []
        hsk_keys: set[str] = set()
        for row in hsk_rows:
            normalized = dict(row)
            if normalized["snapshot_id"] != normalized["relation_snapshot_id"]:
                raise GenerationStoreError("Neo4j HSK snapshot relation mismatch")
            payload = {
                key: normalized[key]
                for key in (
                    "code",
                    "hs6",
                    "heading4",
                    "chapter2",
                    "name_ko",
                    "name_en",
                    "valid_from",
                    "valid_to",
                    "quantity_unit",
                    "weight_unit",
                    "category_code",
                    "category_name",
                )
            }
            if (
                canonical_sha256(payload) != normalized["payload_sha256"]
                or normalized["content_sha256"] != normalized["payload_sha256"]
                or normalized["model_manifest_sha256"] != model_manifest_sha256
                or normalized["source_manifest_sha256"] != source_manifest_sha256
            ):
                raise GenerationStoreError("Neo4j HSK payload hash mismatch")
            if normalized["code"] in hsk_keys:
                raise GenerationStoreError("Neo4j HSK projection contains duplicates")
            hsk_keys.add(normalized["code"])
            hsk_projection.append(normalized)
        if len(hsk_projection) != 11_327:
            raise GenerationStoreError("Neo4j HSK projection is not the exact baseline")
        return StoreAttestation.from_article_records(
            generation_id=generation_id,
            store_name=self.name,
            records=records,
            model_manifest_sha256=model_manifest_sha256,
            store_locator=f"generation/{generation_id}/neo4j",
            store_revision=self._store_revision,
            attested_at=attested_at,
            projection={
                "schema_version": "2.0",
                "index_config": config,
                "articles": node_projection,
                "relations": relation_projection,
                "hsk": hsk_projection,
            },
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
