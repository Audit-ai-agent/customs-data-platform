from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from src.generation import (
    GenerationSpec,
    GenerationValidationError,
    SourceManifest,
    canonical_sha256,
    release_model_manifest,
)
from src.generation_builder import (
    GenerationArticle,
    ProductionGenerationBuilder,
    build_verified_baseline_manifest,
    dataset_membership_from_tracked_sources,
    dataset_release_subjects,
    hsk_projection_rows_from_tracked_sources,
    law_sources_from_articles,
    verify_dataset_release_approval,
    verify_official_law_corpus,
)
from src.generation_builder import _official_source_root_sha256
from src.ingest.laws import TARGET_LAWS, build_meta, parse_articles
from src.source_manifest import (
    article_content_set_sha256,
    canonical_json_bytes,
    load_verified_source_manifest,
    target_law_manifest_sha256,
)


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


def _official_source_fixture(
    root: Path,
) -> tuple[tuple[GenerationArticle, ...], Path]:
    law_assets: list[dict[str, Any]] = []
    articles: list[GenerationArticle] = []
    for index, target in enumerate(
        sorted(TARGET_LAWS, key=lambda item: item["law_id"]),
        start=1,
    ):
        payload = {
            "법령": {
                "기본정보": {
                    "법령ID": target["law_id"],
                    "법령명_한글": target["name"],
                    "시행일자": "20260719",
                    "공포일자": "20260701",
                    "제개정구분": {"content": "일부개정"},
                    "소관부처": {"content": "테스트 부처"},
                },
                "조문": {
                    "조문단위": {
                        "조문여부": "조문",
                        "조문번호": "1",
                        "조문가지번호": "0",
                        "조문제목": "목적",
                        "조문내용": f"제1조({target['name']} 목적) 테스트 조문",
                        "조문시행일자": "20260719",
                    }
                },
            }
        }
        raw = canonical_json_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        current_name = f"law_current_{target['law_id']}.json"
        effective_name = f"law_effective_{target['law_id']}_{index}.json"
        (root / current_name).write_bytes(raw)
        (root / effective_name).write_bytes(raw)
        meta = build_meta(payload, str(index))
        parsed = parse_articles(payload, meta)
        leaf = parsed[0]
        text = str(leaf["content"])
        articles.append(
            GenerationArticle(
                article_pk=index,
                law_id=target["law_id"],
                law_name=target["name"],
                hierarchy="법률",
                article_no=int(leaf["article_no"]),
                paragraph_no=None,
                item_no=None,
                title="목적",
                enforcement_date="2026-07-19",
                text=text,
                content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                version_mst=str(index),
            )
        )
        law_assets.append(
            {
                "law_id": target["law_id"],
                "law_name": target["name"],
                "current_mst": str(index),
                "effective_mst": str(index),
                "effective_date": "2026-07-19",
                "current_file": current_name,
                "current_sha256": digest,
                "effective_file": effective_name,
                "effective_sha256": digest,
                "current_source_uri": (
                    f"https://www.law.go.kr/DRF/lawService.do?target=law&ID={target['law_id']}"
                ),
                "effective_source_uri": (
                    f"https://www.law.go.kr/DRF/lawService.do?target=law&MST={index}"
                ),
                "article_count": 1,
                "article_content_set_sha256": article_content_set_sha256(parsed),
            }
        )
    source = {
        "schema_version": "1.0",
        "source": "Korean Ministry of Government Legislation LAW OPEN DATA",
        "collected_at": datetime(2026, 7, 19, tzinfo=timezone.utc).isoformat(),
        "collection_as_of": "2026-07-19",
        "target_law_manifest_sha256": target_law_manifest_sha256(),
        "collector_revision": "1" * 40,
        "law_count": 58,
        "laws": law_assets,
    }
    (root / "source-manifest.json").write_bytes(canonical_json_bytes(source))
    manifest, manifest_sha = load_verified_source_manifest(root)
    approval = {
        "schema_version": "1.0",
        "purpose": "production-knowledge-release",
        "official_manifest_sha256": manifest_sha,
        "source_root_sha256": _official_source_root_sha256(manifest, manifest_sha),
        "target_law_manifest_sha256": manifest.target_law_manifest_sha256,
        "approved_by": "reviewer-001",
        "approved_at": "2026-07-19T01:00:00Z",
        "valid_through": "2026-12-31",
    }
    approval_path = root / "law-approval.json"
    approval_path.write_bytes(canonical_json_bytes(approval))
    return tuple(articles), approval_path


def _custom_spec(
    articles: tuple[GenerationArticle, ...],
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> GenerationSpec:
    manifest = generation_manifest_factory()
    manifest["laws"] = law_sources_from_articles(articles)
    return GenerationSpec(
        source_manifest=SourceManifest.from_mapping(manifest),
        model_manifest=release_model_manifest(),
        index_config={
            "schema_version": "1.0",
            "dense_vector_name": "dense",
            "dense_vector_size": 1024,
            "distance": "cosine",
            "sparse_vector_name": "bm25",
            "sparse_modifier": "idf",
            "qdrant_collection_mode": "generation-isolated",
            "neo4j_projection_mode": "generation-tagged",
        },
    )


class FakeArticleSource:
    def __init__(self, articles: tuple[GenerationArticle, ...]) -> None:
        self.articles = articles

    def load(self) -> tuple[GenerationArticle, ...]:
        return self.articles


class FakeCoordinator:
    def __init__(self) -> None:
        self.begun: tuple[str, tuple[tuple[int, str], ...]] | None = None

    def begin_generation(self, spec, membership, dataset_membership):
        self.begun = (spec.generation_id, tuple(membership))
        return spec.generation_id


class FakeQdrantBuilderStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.fail = fail
        self.active_mutations = 0

    def upsert_article_projections(self, _generation, _source, _model, rows):
        if self.fail:
            raise RuntimeError("injected isolated Qdrant build failure")
        self.batches.append(list(rows))


class FakeNeo4jBuilderStore:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.relation_batches: list[list[dict[str, Any]]] = []
        self.active_mutations = 0

    def upsert_article_projections(self, _generation, _source, _model, rows):
        self.batches.append(list(rows))

    def upsert_relations(self, _generation, rows):
        self.relation_batches.append(list(rows))

    def upsert_hsk_projections(
        self,
        _generation,
        _snapshot,
        _source,
        _model,
        rows,
    ):
        self.relation_batches.append(list(rows))


def test_builder_populates_only_isolated_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    articles = _articles()
    spec = _custom_spec(articles, generation_manifest_factory)
    monkeypatch.setattr(
        "src.generation_builder.verify_official_law_corpus",
        lambda *_args, **_kwargs: spec.source_manifest.law_corpus.as_dict(),
    )
    monkeypatch.setattr(
        "src.generation_builder.dataset_membership_from_tracked_sources",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "src.generation_builder.verify_dataset_release_approval",
        lambda *_args, **_kwargs: "a" * 64,
    )
    monkeypatch.setattr(
        "src.generation_builder.hsk_projection_rows_from_tracked_sources",
        lambda *_args, **_kwargs: (
            {
                "code": "0101211000",
                "payload_sha256": "1" * 64,
            },
        ),
    )
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

    generation_id = builder.build(
        spec,
        source_root=Path(__file__).resolve().parents[1] / "data",
        official_law_source_root=tmp_path,
        approval_artifact=tmp_path / "approval.json",
        dataset_approval_artifact=tmp_path / "dataset-approval.json",
        batch_size=10,
    )

    assert generation_id == spec.generation_id
    assert coordinator.begun is not None
    assert len(coordinator.begun[1]) == 58
    assert sum(len(batch) for batch in qdrant.batches) == 58
    assert sum(len(batch) for batch in neo4j.batches) == 58
    assert qdrant.active_mutations == neo4j.active_mutations == 0


def test_build_failure_leaves_activation_outside_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    articles = _articles()
    spec = _custom_spec(articles, generation_manifest_factory)
    monkeypatch.setattr(
        "src.generation_builder.verify_official_law_corpus",
        lambda *_args, **_kwargs: spec.source_manifest.law_corpus.as_dict(),
    )
    monkeypatch.setattr(
        "src.generation_builder.dataset_membership_from_tracked_sources",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "src.generation_builder.verify_dataset_release_approval",
        lambda *_args, **_kwargs: "a" * 64,
    )
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
        builder.build(
            spec,
            source_root=Path(__file__).resolve().parents[1] / "data",
            official_law_source_root=tmp_path,
            approval_artifact=tmp_path / "approval.json",
            dataset_approval_artifact=tmp_path / "dataset-approval.json",
        )

    assert coordinator.begun is not None
    assert qdrant.active_mutations == neo4j.active_mutations == 0


def test_tracked_ccct_and_hsk_files_recompute_to_pinned_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    freshness = {
        "status": "bounded_snapshot",
        "observed_at": "2026-07-06T00:00:00Z",
        "valid_through": "2026-07-06",
        "limitation": "The tracked source does not prove changes after 2026-07-06.",
        "approval_artifact_sha256": "a" * 64,
        "release_eligible": False,
    }
    data_directory = Path(__file__).resolve().parents[1] / "data"
    binding = SourceManifest.from_mapping(
        generation_manifest_factory(release_eligible=False)
    ).law_corpus.as_dict()
    monkeypatch.setattr(
        "src.generation_builder.verify_official_law_corpus",
        lambda *_args, **_kwargs: binding,
    )
    monkeypatch.setattr(
        "src.generation_builder.verify_dataset_release_approval",
        lambda *_args, **_kwargs: "a" * 64,
    )

    manifest = build_verified_baseline_manifest(
        _articles(),
        data_directory=data_directory,
        official_law_source_root=tmp_path,
        approval_artifact=tmp_path / "approval.json",
        dataset_approval_artifact=tmp_path / "dataset-approval.json",
        generated_at="2026-07-19T00:00:00Z",
        freshness=copy.deepcopy(freshness),
    )

    assert len(manifest.laws) == 58
    assert {item.dataset_id for item in manifest.datasets} == {"ccct", "hsk"}
    assert all(not item.freshness.release_eligible for item in manifest.datasets)


def test_official_source_root_and_approval_bind_exact_postgres_projection(
    tmp_path: Path,
) -> None:
    articles, approval = _official_source_fixture(tmp_path)

    binding = verify_official_law_corpus(
        articles,
        source_root=tmp_path,
        approval_artifact=approval,
    )

    assert binding["official_manifest_sha256"]
    assert binding["source_root_sha256"]
    tampered = list(articles)
    first = tampered[0]
    text = first.text + " tampered"
    tampered[0] = GenerationArticle(
        article_pk=first.article_pk,
        law_id=first.law_id,
        law_name=first.law_name,
        hierarchy=first.hierarchy,
        article_no=first.article_no,
        paragraph_no=first.paragraph_no,
        item_no=first.item_no,
        title=first.title,
        enforcement_date=first.enforcement_date,
        text=text,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        version_mst=first.version_mst,
    )
    with pytest.raises(GenerationValidationError, match="do not exactly match official"):
        verify_official_law_corpus(
            tuple(tampered),
            source_root=tmp_path,
            approval_artifact=approval,
        )


def test_tracked_sources_produce_exact_relational_and_hsk_membership(
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    data_directory = Path(__file__).resolve().parents[1] / "data"
    manifest = SourceManifest.from_mapping(generation_manifest_factory())

    members = dataset_membership_from_tracked_sources(manifest, data_directory)
    hsk_rows = hsk_projection_rows_from_tracked_sources(data_directory)
    counts: dict[str, int] = {}
    for item in members:
        counts[item.member_kind] = counts.get(item.member_kind, 0) + 1

    assert counts == {
        "ccct_document": 63,
        "ccct_query": 22_654,
        "ccct_requirement": 14_726,
        "ccct_source_record": 14_726,
        "hsk_leaf": 11_327,
    }
    assert len(hsk_rows) == 11_327
    assert all(
        row["payload_sha256"]
        == canonical_sha256({key: value for key, value in row.items() if key != "payload_sha256"})
        for row in hsk_rows
    )


def test_dataset_release_approval_bytes_are_required(
    tmp_path: Path,
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    draft = SourceManifest.from_mapping(generation_manifest_factory())
    approval = {
        "schema_version": "1.0",
        "purpose": "production-dataset-release",
        "datasets": dataset_release_subjects(draft),
        "approved_by": "reviewer-001",
        "approved_at": "2026-07-19T01:00:00Z",
        "valid_through": "2026-12-31",
    }
    raw = canonical_json_bytes(approval)
    payload = generation_manifest_factory()
    for dataset in payload["datasets"]:
        dataset["freshness"]["approval_artifact_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest = SourceManifest.from_mapping(payload)
    path = tmp_path / "dataset-approval.json"
    path.write_bytes(raw)

    assert verify_dataset_release_approval(manifest, path) == hashlib.sha256(raw).hexdigest()

    path.write_bytes(raw + b" ")
    with pytest.raises(
        GenerationValidationError,
        match="freshness does not match approval",
    ):
        verify_dataset_release_approval(manifest, path)
