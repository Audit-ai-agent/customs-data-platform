from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from src.generation import GenerationSpec, canonical_sha256
from src.generation_stores import (
    ACTIVE_QDRANT_ALIAS,
    GenerationStoreError,
    Neo4jGenerationStore,
    QdrantGenerationStore,
    qdrant_collection_name,
)

_ATTESTED_AT = "2026-07-19T00:10:00Z"


class FakeQdrant:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.indexes: set[tuple[str, str]] = set()
        self.aliases: dict[str, str] = {}
        self.points: dict[str, list[Any]] = {}
        self.configs: dict[str, Any] = {}

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, collection_name: str, **kwargs: Any) -> None:
        self.collections.add(collection_name)
        self.points.setdefault(collection_name, [])
        self.configs[collection_name] = SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=kwargs["vectors_config"],
                    sparse_vectors=kwargs["sparse_vectors_config"],
                )
            )
        )

    def get_collection(self, collection_name: str) -> Any:
        return self.configs[collection_name]

    def create_payload_index(
        self,
        collection_name: str,
        field_name: str,
        **_kwargs: Any,
    ) -> None:
        self.indexes.add((collection_name, field_name))

    def get_aliases(self) -> Any:
        return SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=alias, collection_name=collection)
                for alias, collection in self.aliases.items()
            ]
        )

    def update_collection_aliases(self, change_aliases_operations: list[Any]) -> None:
        for operation in change_aliases_operations:
            if getattr(operation, "delete_alias", None) is not None:
                self.aliases.pop(operation.delete_alias.alias_name, None)
            if getattr(operation, "create_alias", None) is not None:
                item = operation.create_alias
                self.aliases[item.alias_name] = item.collection_name

    def scroll(self, collection_name: str, **_kwargs: Any) -> tuple[list[Any], None]:
        return self.points[collection_name], None


def test_qdrant_generation_collection_alias_and_attestation(
    generation_spec: GenerationSpec,
) -> None:
    fake = FakeQdrant()
    store = QdrantGenerationStore(
        client_factory=lambda: fake,
        store_revision="qdrant-1.15.4",
    )
    generation_id = generation_spec.generation_id
    collection = qdrant_collection_name(generation_id)

    store.prepare_generation(
        generation_id,
        generation_spec.source_manifest_sha256,
        generation_spec.model_manifest_sha256,
        generation_spec.index_config,
    )
    first_text = "first"
    second_text = "second"
    fake.points[collection] = [
        SimpleNamespace(
            id="point-1",
            vector={
                "dense": [0.0] * 1024,
                "bm25": SimpleNamespace(indices=[1], values=[1.0]),
            },
            payload={
                "generation_id": generation_id,
                "source_manifest_sha256": generation_spec.source_manifest_sha256,
                "article_pk": 1,
                "law_id": "001556",
                "law_name": "관세법",
                "hierarchy": "법률",
                "article_no": 1,
                "paragraph_no": None,
                "item_no": None,
                "enforcement_date": "2026-01-01",
                "text": first_text,
                "content_sha256": hashlib.sha256(first_text.encode()).hexdigest(),
                "model_manifest_sha256": generation_spec.model_manifest_sha256,
            },
        ),
        SimpleNamespace(
            id="point-2",
            vector={
                "dense": [0.0] * 1024,
                "bm25": SimpleNamespace(indices=[2], values=[1.0]),
            },
            payload={
                "generation_id": generation_id,
                "source_manifest_sha256": generation_spec.source_manifest_sha256,
                "article_pk": 2,
                "law_id": "001556",
                "law_name": "관세법",
                "hierarchy": "법률",
                "article_no": 2,
                "paragraph_no": None,
                "item_no": None,
                "enforcement_date": "2026-01-01",
                "text": second_text,
                "content_sha256": hashlib.sha256(second_text.encode()).hexdigest(),
                "model_manifest_sha256": generation_spec.model_manifest_sha256,
            },
        ),
    ]
    store.set_active_generation(generation_id)
    attestation = store.attest_generation(
        generation_id,
        generation_spec.source_manifest_sha256,
        generation_spec.model_manifest_sha256,
        generation_spec.index_config,
        attested_at=_ATTESTED_AT,
    )

    assert store.current_active_generation() == generation_id
    assert fake.aliases[ACTIVE_QDRANT_ALIAS] == collection
    assert attestation.article_count == 2
    assert (collection, "generation_id") in fake.indexes
    assert (collection, "model_manifest_sha256") in fake.indexes

    fake.points[collection][0].payload["text"] = "tampered"
    with pytest.raises(GenerationStoreError, match="text hash mismatch"):
        store.attest_generation(
            generation_id,
            generation_spec.source_manifest_sha256,
            generation_spec.model_manifest_sha256,
            generation_spec.index_config,
            attested_at=_ATTESTED_AT,
        )
    fake.points[collection][0].payload["text"] = first_text
    dense_config = fake.configs[collection].config.params.vectors["dense"]
    fake.configs[collection].config.params.vectors["dense"] = SimpleNamespace(
        size=999,
        distance=dense_config.distance,
    )
    with pytest.raises(GenerationStoreError, match="config does not match"):
        store.attest_generation(
            generation_id,
            generation_spec.source_manifest_sha256,
            generation_spec.model_manifest_sha256,
            generation_spec.index_config,
            attested_at=_ATTESTED_AT,
        )

    store.set_active_generation(None)
    assert store.current_active_generation() is None


def test_qdrant_attestation_rejects_cross_generation_point(
    generation_spec: GenerationSpec,
) -> None:
    fake = FakeQdrant()
    store = QdrantGenerationStore(
        client_factory=lambda: fake,
        store_revision="qdrant-1.15.4",
    )
    generation_id = generation_spec.generation_id
    collection = qdrant_collection_name(generation_id)
    store.prepare_generation(
        generation_id,
        generation_spec.source_manifest_sha256,
        generation_spec.model_manifest_sha256,
        generation_spec.index_config,
    )
    text = "cross generation"
    fake.points[collection] = [
        SimpleNamespace(
            id="point-1",
            vector={
                "dense": [0.0] * 1024,
                "bm25": SimpleNamespace(indices=[1], values=[1.0]),
            },
            payload={
                "generation_id": "f" * 64,
                "source_manifest_sha256": generation_spec.source_manifest_sha256,
                "article_pk": 1,
                "law_id": "001556",
                "law_name": "관세법",
                "hierarchy": "법률",
                "article_no": 1,
                "paragraph_no": None,
                "item_no": None,
                "enforcement_date": "2026-01-01",
                "text": text,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "model_manifest_sha256": generation_spec.model_manifest_sha256,
            },
        )
    ]
    with pytest.raises(GenerationStoreError, match="generation identity mismatch"):
        store.attest_generation(
            generation_id,
            generation_spec.source_manifest_sha256,
            generation_spec.model_manifest_sha256,
            generation_spec.index_config,
            attested_at=_ATTESTED_AT,
        )


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def single(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def consume(self) -> None:
        return None

    def __iter__(self):
        return iter(self.rows)


class FakeNeoState:
    def __init__(self) -> None:
        self.generations: dict[str, dict[str, str]] = {}
        self.articles: dict[str, dict[int, dict[str, Any]]] = {}
        self.hsk: dict[str, dict[str, dict[str, Any]]] = {}
        self.relations: dict[str, list[dict[str, Any]]] = {}
        self.active: str | None = None
        self.closed = 0


class FakeNeoSession:
    def __init__(self, state: FakeNeoState) -> None:
        self.state = state

    def __enter__(self) -> FakeNeoSession:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def run(self, query: str, **params: Any) -> FakeResult:
        if "MERGE (g:KnowledgeGeneration" in query:
            generation_id = params["generation_id"]
            existing = self.state.generations.setdefault(
                generation_id,
                {
                    "source_manifest_sha256": params["source_manifest_sha256"],
                    "model_manifest_sha256": params["model_manifest_sha256"],
                    "index_config_sha256": params["index_config_sha256"],
                },
            )
            self.state.articles.setdefault(generation_id, {})
            self.state.hsk.setdefault(generation_id, {})
            self.state.relations.setdefault(generation_id, [])
            return FakeResult([dict(existing)])
        if "MERGE (h:GenerationHSK" in query:
            generation_id = params["generation_id"]
            if generation_id not in self.state.generations:
                return FakeResult([])
            for row in params["rows"]:
                self.state.hsk[generation_id][row["code"]] = {
                    **dict(row),
                    "snapshot_id": params["snapshot_id"],
                    "source_manifest_sha256": params["source_manifest_sha256"],
                    "model_manifest_sha256": params["model_manifest_sha256"],
                    "content_sha256": row["payload_sha256"],
                    "relation_snapshot_id": params["snapshot_id"],
                }
            return FakeResult([{"hsk_count": len(params["rows"])}])
        if "MERGE (a:GenerationArticle" in query:
            generation_id = params["generation_id"]
            if generation_id not in self.state.generations:
                return FakeResult([])
            self.state.articles[generation_id][params["article_pk"]] = {
                "article_pk": params["article_pk"],
                "law_id": params["law_id"],
                "law_name": None,
                "hierarchy": None,
                "article_no": None,
                "title": None,
                "content_sha256": params["content_sha256"],
                "source_manifest_sha256": params["source_manifest_sha256"],
                "model_manifest_sha256": params["model_manifest_sha256"],
            }
            return FakeResult([{"article_pk": params["article_pk"]}])
        if "RETURN g.generation_id AS generation_id" in query and "MERGE (p" in query:
            generation_id = params["generation_id"]
            if generation_id not in self.state.generations:
                return FakeResult([])
            self.state.active = generation_id
            return FakeResult([{"generation_id": generation_id}])
        if "MATCH (:KnowledgeGenerationPointer" in query:
            return (
                FakeResult([{"generation_id": self.state.active}])
                if self.state.active is not None
                else FakeResult([])
            )
        if "DELETE r" in query and "KnowledgeGenerationPointer" in query:
            self.state.active = None
            return FakeResult([])
        if "RETURN a.article_pk AS article_pk" in query:
            generation_id = params["generation_id"]
            rows = [
                dict(row) for _, row in sorted(self.state.articles.get(generation_id, {}).items())
            ]
            return FakeResult(rows)
        if "RETURN source.article_pk AS source_article_pk" in query:
            return FakeResult(list(self.state.relations.get(params["generation_id"], [])))
        if "RETURN h.code AS code" in query:
            rows = [
                dict(row)
                for _, row in sorted(self.state.hsk.get(params["generation_id"], {}).items())
            ]
            return FakeResult(rows)
        raise AssertionError(f"unexpected Cypher: {query}")


class FakeNeoDriver:
    def __init__(self, state: FakeNeoState) -> None:
        self.state = state

    def session(self, **_kwargs: Any) -> FakeNeoSession:
        return FakeNeoSession(self.state)

    def close(self) -> None:
        self.state.closed += 1


def test_neo4j_generation_projection_pointer_and_attestation(
    generation_spec: GenerationSpec,
) -> None:
    state = FakeNeoState()
    store = Neo4jGenerationStore(
        driver_factory=lambda: FakeNeoDriver(state),
        store_revision="neo4j-5",
    )
    generation_id = generation_spec.generation_id
    store.prepare_generation(
        generation_id,
        generation_spec.source_manifest_sha256,
        generation_spec.model_manifest_sha256,
        generation_spec.index_config,
    )
    for article_pk, content_hash in ((1, "1" * 64), (2, "2" * 64)):
        store.upsert_article_projection(
            generation_id=generation_id,
            article_pk=article_pk,
            law_id="001556",
            content_sha256=content_hash,
            source_manifest_sha256=generation_spec.source_manifest_sha256,
            model_manifest_sha256=generation_spec.model_manifest_sha256,
        )
    hsk_rows = []
    for index in range(11_327):
        payload = {
            "code": f"{index:010d}",
            "hs6": f"{index:010d}"[:6],
            "heading4": f"{index:010d}"[:4],
            "chapter2": f"{index:010d}"[:2],
            "name_ko": f"품목 {index}",
            "name_en": f"item {index}",
            "valid_from": "2026-01-01",
            "valid_to": None,
            "quantity_unit": None,
            "weight_unit": "KG",
            "category_code": None,
            "category_name": None,
        }
        hsk_rows.append({**payload, "payload_sha256": canonical_sha256(payload)})
    store.upsert_hsk_projections(
        generation_id,
        "hsk-2026-01-01",
        generation_spec.source_manifest_sha256,
        generation_spec.model_manifest_sha256,
        hsk_rows,
    )
    store.set_active_generation(generation_id)
    attestation = store.attest_generation(
        generation_id,
        generation_spec.source_manifest_sha256,
        generation_spec.model_manifest_sha256,
        generation_spec.index_config,
        attested_at=_ATTESTED_AT,
    )

    assert store.current_active_generation() == generation_id
    assert attestation.article_count == 2
    assert state.closed >= 5

    state.relations[generation_id] = [
        {
            "source_article_pk": 1,
            "target_article_pk": 2,
            "relation_type": "CITES",
            "basis": "",
        }
    ]
    with pytest.raises(GenerationStoreError, match="relation is invalid"):
        store.attest_generation(
            generation_id,
            generation_spec.source_manifest_sha256,
            generation_spec.model_manifest_sha256,
            generation_spec.index_config,
            attested_at=_ATTESTED_AT,
        )

    store.set_active_generation(None)
    assert store.current_active_generation() is None
