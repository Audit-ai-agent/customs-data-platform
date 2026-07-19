from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Callable

import pytest

from src.generation import (
    GenerationSpec,
    SourceManifest,
    release_model_manifest,
)
from src.generation_builder import (
    GenerationArticle,
    ProductionGenerationBuilder,
    build_verified_baseline_manifest,
    law_sources_from_articles,
)
from src.ingest.laws import TARGET_LAWS


def _articles() -> tuple[GenerationArticle, ...]:
    rows: list[GenerationArticle] = []
    for article_pk, item in enumerate(
        sorted(TARGET_LAWS, key=lambda candidate: candidate["law_id"]),
        start=1,
    ):
        text = f"[{item['name']} 제1조]\n이 법령은 합성 generation builder 검증용이다."
        rows.append(
            GenerationArticle(
                article_pk=article_pk,
                law_id=item["law_id"],
                law_name=item["name"],
                hierarchy=(
                    "시행규칙"
                    if item["name"].endswith("시행규칙")
                    else "시행령"
                    if item["name"].endswith("시행령")
                    else "법률"
                ),
                article_no=1,
                paragraph_no=None,
                item_no=None,
                title="목적",
                enforcement_date="2026-01-01",
                text=text,
                content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                version_mst=f"{article_pk:06d}",
            )
        )
    return tuple(rows)


def _custom_spec(
    articles: tuple[GenerationArticle, ...],
    tmp_path: Path,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> GenerationSpec:
    manifest = generation_manifest_factory()
    manifest["laws"] = law_sources_from_articles(articles)
    for dataset in manifest["datasets"]:
        name = f"{dataset['dataset_id']}.source"
        content = f"{dataset['dataset_id']}-content".encode()
        (tmp_path / name).write_bytes(content)
        dataset["snapshot_id"] = f"{dataset['dataset_id']}-future-release"
        dataset["source_files"] = [
            {
                "name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
                "source_revision": "release-1",
            }
        ]
        dataset["metrics"] = {"record_count": 1}
    return GenerationSpec(
        source_manifest=SourceManifest.from_mapping(manifest),
        model_manifest=release_model_manifest(),
        index_config={"schema_version": "1.0", "test": "isolated-build"},
    )


class FakeArticleSource:
    def __init__(self, articles: tuple[GenerationArticle, ...]) -> None:
        self.articles = articles

    def load(self) -> tuple[GenerationArticle, ...]:
        return self.articles


class FakeCoordinator:
    def __init__(self) -> None:
        self.begun: tuple[str, tuple[tuple[int, str], ...]] | None = None

    def begin_generation(self, spec, membership):
        self.begun = (spec.generation_id, tuple(membership))
        return spec.generation_id


class FakeQdrantBuilderStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.fail = fail
        self.active_mutations = 0

    def upsert_article_projections(self, _generation, _model, rows):
        if self.fail:
            raise RuntimeError("injected isolated Qdrant build failure")
        self.batches.append(list(rows))


class FakeNeo4jBuilderStore:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.relation_batches: list[list[dict[str, Any]]] = []
        self.active_mutations = 0

    def upsert_article_projections(self, _generation, _model, rows):
        self.batches.append(list(rows))

    def upsert_relations(self, _generation, rows):
        self.relation_batches.append(list(rows))


def test_builder_populates_only_isolated_targets(
    tmp_path: Path,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    articles = _articles()
    spec = _custom_spec(articles, tmp_path, generation_manifest_factory)
    coordinator = FakeCoordinator()
    qdrant = FakeQdrantBuilderStore()
    neo4j = FakeNeo4jBuilderStore()
    builder = ProductionGenerationBuilder(
        coordinator=coordinator,  # type: ignore[arg-type]
        article_source=FakeArticleSource(articles),  # type: ignore[arg-type]
        qdrant=qdrant,  # type: ignore[arg-type]
        neo4j=neo4j,  # type: ignore[arg-type]
        dense_encoder=lambda texts: [[0.1, 0.2] for _ in texts],
        sparse_encoder=lambda _text: ([1], [1.0]),
    )

    generation_id = builder.build(spec, source_root=tmp_path, batch_size=10)

    assert generation_id == spec.generation_id
    assert coordinator.begun is not None
    assert len(coordinator.begun[1]) == 58
    assert sum(len(batch) for batch in qdrant.batches) == 58
    assert sum(len(batch) for batch in neo4j.batches) == 58
    assert qdrant.active_mutations == neo4j.active_mutations == 0


def test_build_failure_leaves_activation_outside_builder(
    tmp_path: Path,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    articles = _articles()
    spec = _custom_spec(articles, tmp_path, generation_manifest_factory)
    coordinator = FakeCoordinator()
    qdrant = FakeQdrantBuilderStore(fail=True)
    neo4j = FakeNeo4jBuilderStore()
    builder = ProductionGenerationBuilder(
        coordinator=coordinator,  # type: ignore[arg-type]
        article_source=FakeArticleSource(articles),  # type: ignore[arg-type]
        qdrant=qdrant,  # type: ignore[arg-type]
        neo4j=neo4j,  # type: ignore[arg-type]
        dense_encoder=lambda texts: [[0.1, 0.2] for _ in texts],
        sparse_encoder=lambda _text: ([1], [1.0]),
    )

    with pytest.raises(RuntimeError, match="isolated Qdrant"):
        builder.build(spec, source_root=tmp_path)

    assert coordinator.begun is not None
    assert qdrant.active_mutations == neo4j.active_mutations == 0


def test_tracked_ccct_and_hsk_files_recompute_to_pinned_baseline() -> None:
    freshness = {
        "status": "bounded_snapshot",
        "observed_at": "2026-07-06T00:00:00Z",
        "valid_through": "2026-07-06",
        "limitation": "The tracked source does not prove changes after 2026-07-06.",
        "release_eligible": False,
    }
    data_directory = Path(__file__).resolve().parents[1] / "data"

    manifest = build_verified_baseline_manifest(
        _articles(),
        data_directory=data_directory,
        generated_at="2026-07-19T00:00:00Z",
        freshness=copy.deepcopy(freshness),
    )

    assert len(manifest.laws) == 58
    assert {item.dataset_id for item in manifest.datasets} == {"ccct", "hsk"}
    assert all(not item.freshness.release_eligible for item in manifest.datasets)
