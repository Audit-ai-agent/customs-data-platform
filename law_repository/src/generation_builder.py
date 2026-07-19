"""Build isolated knowledge-generation projections from verified sources."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote

from src.db.postgres import connect
from src.generation import (
    CCCT_BASELINE_METRICS,
    CCCT_BASELINE_SNAPSHOT_ID,
    CCCT_BASELINE_SOURCE,
    GenerationSpec,
    GenerationValidationError,
    HSK_BASELINE_METRICS,
    HSK_BASELINE_SNAPSHOT_ID,
    HSK_BASELINE_SOURCES,
    SourceManifest,
    canonical_sha256,
)
from src.generation_coordinator import GenerationCoordinator
from src.generation_stores import Neo4jGenerationStore, QdrantGenerationStore
from src.ingest.graph import HIER_RANK, LawRegistry, extract_refs


@dataclass(frozen=True, slots=True)
class GenerationArticle:
    article_pk: int
    law_id: str
    law_name: str
    hierarchy: str
    article_no: int
    paragraph_no: int | None
    item_no: int | None
    title: str | None
    enforcement_date: str
    text: str
    content_sha256: str
    version_mst: str

    def __post_init__(self) -> None:
        if isinstance(self.article_pk, bool) or self.article_pk <= 0:
            raise GenerationValidationError("article_pk must be positive")
        actual = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if actual != self.content_sha256:
            raise GenerationValidationError(
                f"PostgreSQL article content hash mismatch: {self.article_pk}"
            )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "law_id": self.law_id,
            "article_no": self.article_no,
            "paragraph_no": self.paragraph_no,
            "item_no": self.item_no,
            "version_mst": self.version_mst,
            "content_sha256": self.content_sha256,
        }


class PostgresGenerationArticleSource:
    """Read current indexable articles without mutating the legacy ledger."""

    def __init__(self, connection_factory: Callable[[], Any] = connect) -> None:
        self._connection_factory = connection_factory

    def load(self) -> tuple[GenerationArticle, ...]:
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.law_id, l.law_name, l.hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.title,
                       a.valid_from, a.content, a.content_hash, a.version_mst
                FROM law_articles a
                JOIN laws l USING (law_id)
                WHERE a.is_current
                  AND NOT EXISTS (
                    SELECT 1 FROM law_articles child
                    WHERE child.parent_article_pk=a.id
                  )
                ORDER BY a.id
                """
            )
            rows = cur.fetchall()
        articles = tuple(
            GenerationArticle(
                article_pk=int(row[0]),
                law_id=str(row[1]),
                law_name=str(row[2]),
                hierarchy=str(row[3]),
                article_no=int(row[4]),
                paragraph_no=int(row[5]) if row[5] is not None else None,
                item_no=int(row[6]) if row[6] is not None else None,
                title=str(row[7]) if row[7] is not None else None,
                enforcement_date=row[8].isoformat(),
                text=str(row[9]),
                content_sha256=str(row[10]),
                version_mst=str(row[11]),
            )
            for row in rows
        )
        if not articles:
            raise GenerationValidationError("PostgreSQL has no current indexable articles")
        return articles


def law_content_sha256(articles: Iterable[GenerationArticle]) -> str:
    rows = sorted(
        (item.identity_dict() for item in articles),
        key=lambda item: (
            item["law_id"],
            item["article_no"],
            item["paragraph_no"] if item["paragraph_no"] is not None else -1,
            item["item_no"] if item["item_no"] is not None else -1,
            item["content_sha256"],
        ),
    )
    if not rows:
        raise GenerationValidationError("law content hash cannot be computed from no articles")
    return canonical_sha256(rows)


def verify_articles_against_manifest(
    articles: Sequence[GenerationArticle],
    manifest: SourceManifest,
) -> None:
    by_law: dict[str, list[GenerationArticle]] = {}
    for article in articles:
        by_law.setdefault(article.law_id, []).append(article)
    if set(by_law) != {item.law_id for item in manifest.laws}:
        raise GenerationValidationError("PostgreSQL law set does not match source manifest")
    for source in manifest.laws:
        law_articles = by_law[source.law_id]
        if any(item.law_name != source.law_name for item in law_articles):
            raise GenerationValidationError(f"law name mismatch: {source.law_id}")
        if {item.version_mst for item in law_articles} != {source.mst}:
            raise GenerationValidationError(f"law MST mismatch: {source.law_id}")
        if len(law_articles) != source.current_article_count:
            raise GenerationValidationError(f"law article count mismatch: {source.law_id}")
        if law_content_sha256(law_articles) != source.content_sha256:
            raise GenerationValidationError(f"law content hash mismatch: {source.law_id}")


def verify_manifest_source_files(manifest: SourceManifest, source_root: Path) -> None:
    resolved_root = source_root.resolve(strict=True)
    for dataset in manifest.datasets:
        for source in dataset.source_files:
            path = (resolved_root / source.name).resolve(strict=True)
            if path.parent != resolved_root:
                raise GenerationValidationError("source file escapes the approved source root")
            stat = path.stat()
            if stat.st_size != source.byte_size:
                raise GenerationValidationError(f"source file size mismatch: {source.name}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != source.sha256:
                raise GenerationValidationError(f"source file hash mismatch: {source.name}")


def law_sources_from_articles(articles: Sequence[GenerationArticle]) -> list[dict[str, Any]]:
    by_law: dict[str, list[GenerationArticle]] = {}
    for article in articles:
        by_law.setdefault(article.law_id, []).append(article)
    result: list[dict[str, Any]] = []
    for law_id, rows in sorted(by_law.items()):
        names = {item.law_name for item in rows}
        msts = {item.version_mst for item in rows}
        if len(names) != 1 or len(msts) != 1:
            raise GenerationValidationError(f"law identity is ambiguous: {law_id}")
        law_name = next(iter(names))
        result.append(
            {
                "law_id": law_id,
                "law_name": law_name,
                "mst": next(iter(msts)),
                "official_uri": f"https://www.law.go.kr/법령/{quote(law_name, safe='')}",
                "content_sha256": law_content_sha256(rows),
                "current_article_count": len(rows),
            }
        )
    return result


def build_verified_baseline_manifest(
    articles: Sequence[GenerationArticle],
    *,
    data_directory: Path,
    generated_at: str,
    freshness: Mapping[str, Any],
) -> SourceManifest:
    """Recompute the tracked CCCT/HSK baseline before emitting a manifest."""

    from src.ingest.ccct import build_dataset
    from src.ingest.hsk import build_nodes, load_leaf_attrs, load_level_names

    ccct_path = data_directory / CCCT_BASELINE_SOURCE["name"]
    hsk_sources = {item["name"]: data_directory / item["name"] for item in HSK_BASELINE_SOURCES}
    ccct = build_dataset()
    ccct_metrics = {
        "query_count": len(ccct.queries),
        "unique_hsk_count": len({item["hsk10"] for item in ccct.queries}),
        "trade_variant_count": len({item["trade_type"] for item in ccct.queries}),
        "source_requirement_count": len(ccct.requirements),
        "document_count": len(ccct.documents),
    }
    if ccct.snapshot_id != CCCT_BASELINE_SNAPSHOT_ID or ccct_metrics != CCCT_BASELINE_METRICS:
        raise GenerationValidationError("recomputed CCCT baseline does not match attestation")

    level_file = hsk_sources["관세청_HS부호 단위별 품목명_20260101.xlsx"]
    leaf_file = hsk_sources["관세청_HS부호_20260101.xlsx"]
    levels = load_level_names(level_file)
    leaf_attrs = load_leaf_attrs(leaf_file)
    nodes = build_nodes(levels, leaf_attrs)
    leaf_nodes = [item for item in nodes if item["is_leaf"]]
    hsk_metrics = {
        "hierarchy_node_count": len(nodes),
        "unique_hierarchy_node_count": len({item["code"] for item in nodes}),
        "leaf_node_count": len(leaf_nodes),
        "leaf_attribute_count": len(leaf_attrs),
        "missing_leaf_attribute_count": sum(1 for item in leaf_nodes if "valid_from" not in item),
    }
    if hsk_metrics != HSK_BASELINE_METRICS:
        raise GenerationValidationError("recomputed HSK baseline does not match attestation")

    def source_file(path: Path, revision: str) -> dict[str, Any]:
        return {
            "name": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byte_size": path.stat().st_size,
            "source_revision": revision,
        }

    manifest = SourceManifest.from_mapping(
        {
            "schema_version": "1.0",
            "generated_at": generated_at,
            "laws": law_sources_from_articles(articles),
            "datasets": [
                {
                    "dataset_id": "ccct",
                    "snapshot_id": ccct.snapshot_id,
                    "official_uri": "https://www.customs.go.kr/service/ccct",
                    "source_files": [source_file(ccct_path, "git:4ef50b1")],
                    "metrics": ccct_metrics,
                    "freshness": dict(freshness),
                },
                {
                    "dataset_id": "hsk",
                    "snapshot_id": HSK_BASELINE_SNAPSHOT_ID,
                    "official_uri": "https://www.customs.go.kr/service/hsk",
                    "source_files": sorted(
                        (
                            source_file(level_file, "source-date:2026-01-01"),
                            source_file(leaf_file, "source-date:2026-01-01"),
                        ),
                        key=lambda item: item["name"],
                    ),
                    "metrics": hsk_metrics,
                    "freshness": dict(freshness),
                },
            ],
        }
    )
    verify_manifest_source_files(manifest, data_directory)
    return manifest


class ProductionGenerationBuilder:
    """Populate isolated Qdrant/Neo4j targets; never changes active pointers."""

    def __init__(
        self,
        *,
        coordinator: GenerationCoordinator,
        article_source: PostgresGenerationArticleSource,
        qdrant: QdrantGenerationStore,
        neo4j: Neo4jGenerationStore,
        dense_encoder: Callable[[list[str]], Any] | None = None,
        sparse_encoder: Callable[[str], tuple[list[int], list[float]]] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._article_source = article_source
        self._qdrant = qdrant
        self._neo4j = neo4j
        self._dense_encoder = dense_encoder or self._default_dense_encoder
        self._sparse_encoder = sparse_encoder or self._default_sparse_encoder

    @staticmethod
    def _default_dense_encoder(texts: list[str]) -> Any:
        from src.embed.dense import encode

        return encode(texts)

    @staticmethod
    def _default_sparse_encoder(text: str) -> tuple[list[int], list[float]]:
        from src.embed.bm25 import encode_doc

        return encode_doc(text)

    def build(
        self,
        spec: GenerationSpec,
        *,
        source_root: Path,
        batch_size: int = 64,
    ) -> str:
        if isinstance(batch_size, bool) or not 1 <= batch_size <= 512:
            raise ValueError("batch_size must be from 1 to 512")
        verify_manifest_source_files(spec.source_manifest, source_root)
        articles = self._article_source.load()
        verify_articles_against_manifest(articles, spec.source_manifest)
        generation_id = self._coordinator.begin_generation(
            spec,
            ((item.article_pk, item.content_sha256) for item in articles),
        )
        for offset in range(0, len(articles), batch_size):
            batch = articles[offset : offset + batch_size]
            texts = [item.text for item in batch]
            dense_vectors = self._dense_encoder(texts)
            qdrant_rows: list[dict[str, Any]] = []
            neo4j_rows: list[dict[str, Any]] = []
            for item, dense_vector in zip(batch, dense_vectors, strict=True):
                sparse_indices, sparse_values = self._sparse_encoder(item.text)
                qdrant_rows.append(
                    {
                        "article_pk": item.article_pk,
                        "law_id": item.law_id,
                        "law_name": item.law_name,
                        "hierarchy": item.hierarchy,
                        "article_no": item.article_no,
                        "paragraph_no": item.paragraph_no,
                        "item_no": item.item_no,
                        "enforcement_date": item.enforcement_date,
                        "text": item.text,
                        "content_sha256": item.content_sha256,
                        "dense_vector": (
                            dense_vector.tolist()
                            if hasattr(dense_vector, "tolist")
                            else dense_vector
                        ),
                        "sparse_indices": sparse_indices,
                        "sparse_values": sparse_values,
                    }
                )
                neo4j_rows.append(
                    {
                        "article_pk": item.article_pk,
                        "law_id": item.law_id,
                        "law_name": item.law_name,
                        "hierarchy": item.hierarchy,
                        "article_no": item.article_no,
                        "title": item.title,
                        "content_sha256": item.content_sha256,
                    }
                )
            self._qdrant.upsert_article_projections(
                generation_id,
                spec.model_manifest_sha256,
                qdrant_rows,
            )
            self._neo4j.upsert_article_projections(
                generation_id,
                spec.model_manifest_sha256,
                neo4j_rows,
            )
        relations = self._build_relations(articles)
        for offset in range(0, len(relations), 500):
            self._neo4j.upsert_relations(
                generation_id,
                relations[offset : offset + 500],
            )
        return generation_id

    @staticmethod
    def _build_relations(
        articles: Sequence[GenerationArticle],
    ) -> list[dict[str, Any]]:
        laws: dict[str, tuple[str, str]] = {}
        by_key: dict[tuple[str, int], int] = {}
        for item in articles:
            laws[item.law_id] = (item.law_name, item.hierarchy)
            by_key.setdefault((item.law_id, item.article_no), item.article_pk)
        registry = LawRegistry(laws)
        relations: dict[tuple[int, int, str], dict[str, Any]] = {}
        for item in articles:
            references = extract_refs(
                item.text,
                item.law_id,
                item.article_no,
                registry,
            )
            for (target_law, target_article), basis in references.items():
                target_pk = by_key.get((target_law, target_article))
                if target_pk is None:
                    continue
                source_pk = item.article_pk
                if target_law == item.law_id:
                    relation_type = "CITES"
                elif registry.base_of[target_law] == registry.base_of[item.law_id]:
                    relation_type = "DELEGATES"
                    if HIER_RANK[item.hierarchy] > HIER_RANK[registry.hier_of[target_law]]:
                        source_pk, target_pk = target_pk, source_pk
                else:
                    relation_type = "REFERS_TO"
                key = (source_pk, target_pk, relation_type)
                relations.setdefault(
                    key,
                    {
                        "source_article_pk": source_pk,
                        "target_article_pk": target_pk,
                        "relation_type": relation_type,
                        "basis": basis,
                    },
                )
        return [relations[key] for key in sorted(relations)]


__all__ = [
    "GenerationArticle",
    "PostgresGenerationArticleSource",
    "ProductionGenerationBuilder",
    "build_verified_baseline_manifest",
    "law_content_sha256",
    "law_sources_from_articles",
    "verify_articles_against_manifest",
    "verify_manifest_source_files",
]
