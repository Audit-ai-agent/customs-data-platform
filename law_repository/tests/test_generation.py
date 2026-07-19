from __future__ import annotations

import copy
from typing import Any, Callable

import pytest

from src.generation import (
    GenerationSpec,
    GenerationValidationError,
    SourceManifest,
    StoreAttestation,
    build_seal_attestation,
    canonical_sha256,
    release_model_manifest,
    validate_rag_evaluation_report,
    validate_store_attestations,
)

_ATTESTED_AT = "2026-07-19T00:10:00Z"
_RECORDS = ((1, "1" * 64), (2, "2" * 64))


def _attestations(spec: GenerationSpec) -> tuple[StoreAttestation, ...]:
    return tuple(
        StoreAttestation.from_article_records(
            generation_id=spec.generation_id,
            store_name=store,
            records=_RECORDS,
            model_manifest_sha256=spec.model_manifest_sha256,
            store_locator=f"generation/{spec.generation_id}/{store}",
            store_revision=f"{store}-v1",
            attested_at=_ATTESTED_AT,
        )
        for store in ("postgresql", "qdrant", "neo4j")
    )


def test_source_manifest_is_canonical_and_contains_58_targets(
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    original = generation_manifest_factory()
    reordered = copy.deepcopy(original)
    reordered["laws"][0] = dict(reversed(list(reordered["laws"][0].items())))

    first = SourceManifest.from_mapping(original)
    second = SourceManifest.from_mapping(reordered)

    assert len(first.laws) == 58
    assert {item.dataset_id for item in first.datasets} == {"ccct", "hsk"}
    assert first.sha256 == second.sha256


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["laws"].pop(), "all 58 target laws"),
        (
            lambda value: value["laws"][0].__setitem__("law_name", "잘못된 법령"),
            "approved target",
        ),
        (
            lambda value: value["laws"][0].__setitem__("official_uri", "https://example.com/law"),
            "official HTTPS",
        ),
        (
            lambda value: value["laws"][0].__setitem__(
                "official_uri", "https://www.law.go.kr/법령/x?OC=secret"
            ),
            "official HTTPS",
        ),
    ],
)
def test_source_manifest_rejects_incomplete_or_unofficial_laws(
    generation_manifest_factory: Callable[..., dict[str, Any]],
    mutation: Callable[[dict[str, Any]], Any],
    message: str,
) -> None:
    value = generation_manifest_factory()
    mutation(value)
    with pytest.raises(GenerationValidationError, match=message):
        SourceManifest.from_mapping(value)


def test_ccct_baseline_binds_tracked_source_and_derived_counts(
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    source_tamper = generation_manifest_factory()
    source_tamper["datasets"][0]["source_files"][0]["sha256"] = "0" * 64
    with pytest.raises(GenerationValidationError, match="CCCT baseline source"):
        SourceManifest.from_mapping(source_tamper)

    count_tamper = generation_manifest_factory()
    count_tamper["datasets"][0]["metrics"]["query_count"] -= 1
    with pytest.raises(GenerationValidationError, match="CCCT baseline derived counts"):
        SourceManifest.from_mapping(count_tamper)


def test_hsk_baseline_binds_both_source_files_and_parser_counts(
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    source_tamper = generation_manifest_factory()
    source_tamper["datasets"][1]["source_files"][0]["byte_size"] -= 1
    with pytest.raises(GenerationValidationError, match="HSK baseline source"):
        SourceManifest.from_mapping(source_tamper)

    count_tamper = generation_manifest_factory()
    count_tamper["datasets"][1]["metrics"]["missing_leaf_attribute_count"] = 1
    with pytest.raises(GenerationValidationError, match="HSK baseline parser counts"):
        SourceManifest.from_mapping(count_tamper)


def test_source_freshness_limitation_is_hashed_and_blocks_seal(
    generation_manifest_factory: Callable[..., dict[str, Any]],
) -> None:
    value = generation_manifest_factory(release_eligible=False)
    value["datasets"][0]["freshness"] = {
        "status": "bounded_snapshot",
        "observed_at": "2026-07-06T00:00:00Z",
        "valid_through": "2026-07-06",
        "limitation": "No source changes after the tracked snapshot are represented.",
        "release_eligible": False,
    }
    manifest = SourceManifest.from_mapping(value)
    with pytest.raises(GenerationValidationError, match="not release-eligible"):
        manifest.require_release_eligible_sources()


def test_generation_identity_changes_for_every_release_input(
    generation_spec: GenerationSpec,
) -> None:
    changed_config = GenerationSpec(
        source_manifest=generation_spec.source_manifest,
        model_manifest=generation_spec.model_manifest,
        index_config={**generation_spec.index_config, "dense_vector_size": 2048},
    )
    changed_models = copy.deepcopy(release_model_manifest())
    changed_models["models"]["dense"]["revision"] = "0" * 40
    changed_model_spec = GenerationSpec(
        source_manifest=generation_spec.source_manifest,
        model_manifest=changed_models,
        index_config=generation_spec.index_config,
    )

    assert changed_config.generation_id != generation_spec.generation_id
    assert changed_model_spec.generation_id != generation_spec.generation_id


def test_model_alias_is_not_an_immutable_generation_identity(
    generation_spec: GenerationSpec,
) -> None:
    models = copy.deepcopy(release_model_manifest())
    models["models"]["dense"]["revision"] = "main"
    with pytest.raises(GenerationValidationError, match="full commit"):
        GenerationSpec(
            source_manifest=generation_spec.source_manifest,
            model_manifest=models,
            index_config=generation_spec.index_config,
        )


def test_store_attestations_require_identical_sets_and_model(
    generation_spec: GenerationSpec,
) -> None:
    attestations = _attestations(generation_spec)
    validated = validate_store_attestations(
        generation_spec.generation_id,
        generation_spec.model_manifest_sha256,
        attestations,
    )
    assert [item.store_name for item in validated] == ["neo4j", "postgresql", "qdrant"]

    mismatch = StoreAttestation.from_article_records(
        generation_id=generation_spec.generation_id,
        store_name="qdrant",
        records=((1, "1" * 64),),
        model_manifest_sha256=generation_spec.model_manifest_sha256,
        store_locator="generation/qdrant",
        store_revision="qdrant-v1",
        attested_at=_ATTESTED_AT,
    )
    with pytest.raises(GenerationValidationError, match="do not match"):
        validate_store_attestations(
            generation_spec.generation_id,
            generation_spec.model_manifest_sha256,
            (attestations[0], mismatch, attestations[2]),
        )


def test_store_attestation_rejects_duplicate_or_missing_article_ids(
    generation_spec: GenerationSpec,
) -> None:
    kwargs = {
        "generation_id": generation_spec.generation_id,
        "store_name": "postgresql",
        "model_manifest_sha256": generation_spec.model_manifest_sha256,
        "store_locator": "generation/postgresql",
        "store_revision": "pg-v1",
        "attested_at": _ATTESTED_AT,
    }
    with pytest.raises(GenerationValidationError, match="unique"):
        StoreAttestation.from_article_records(
            records=((1, "1" * 64), (1, "1" * 64)),
            **kwargs,
        )
    with pytest.raises(GenerationValidationError, match="positive or non-empty"):
        StoreAttestation.from_article_records(
            records=((None, "1" * 64),),  # type: ignore[arg-type]
            **kwargs,
        )


def test_rag_release_report_and_seal_hash_are_canonical(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
) -> None:
    report = generation_evaluation_factory(generation_spec.generation_id)
    validated, report_hash = validate_rag_evaluation_report(
        report,
        generation_id=generation_spec.generation_id,
    )
    payload, seal_hash = build_seal_attestation(
        generation_spec,
        _attestations(generation_spec),
        report_hash,
    )

    assert report_hash == canonical_sha256(validated)
    assert payload["generation_id"] == generation_spec.generation_id
    assert seal_hash == canonical_sha256(payload)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("gold", "human_reviewed"), False, "human-reviewed"),
        (("gold", "law_coverage_count"), 57, "all target laws"),
        (("hybrid", "ndcg_at_10"), 0.79, "at least 0.8"),
        (("structured", "required_evidence_recall"), 0.99, "must equal 1.0"),
        (("mock_count",), 1, "mock retrieval"),
        (("unexpected_skips",), 1, "cannot contain skips"),
    ],
)
def test_rag_release_report_fails_closed(
    generation_spec: GenerationSpec,
    generation_evaluation_factory: Callable[[str], dict[str, Any]],
    path: tuple[str, ...],
    value: Any,
    message: str,
) -> None:
    report = generation_evaluation_factory(generation_spec.generation_id)
    target: dict[str, Any] = report
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    with pytest.raises(GenerationValidationError, match=message):
        validate_rag_evaluation_report(
            report,
            generation_id=generation_spec.generation_id,
        )
