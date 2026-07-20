"""Operator CLI for isolated knowledge-generation release operations.

Examples:
  python -m src.generation_cli manifest --output source.json ...
  python -m src.generation_cli build --source-manifest source.json ...
  python -m src.generation_cli validate <generation-id> ...
  python -m src.generation_cli seal <generation-id> --report rag-report.json ...
  python -m src.generation_cli promote <generation-id> \
    --confirm-generation-id <generation-id> ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from src.generation import (
    GenerationSpec,
    GenerationValidationError,
    SourceManifest,
    canonical_json_bytes,
    load_verified_evaluation_bundle,
    release_model_manifest,
)
from src.generation_builder import (
    PostgresGenerationArticleSource,
    ProductionGenerationBuilder,
    build_verified_baseline_manifest,
)
from src.generation_coordinator import GenerationCoordinator
from src.generation_repository import PostgresGenerationRepository
from src.generation_runtime import ActiveGenerationLoader
from src.generation_stores import Neo4jGenerationStore, QdrantGenerationStore

_MAX_JSON_BYTES = 16 * 1024 * 1024


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _load_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("JSON input exceeds 16 MiB")
    value = json.loads(
        raw.decode("utf-8", errors="strict"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any], *, overwrite: bool) -> None:
    target = path.resolve()
    if target.exists() and not overwrite:
        raise ValueError("output exists; pass --overwrite to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(canonical_json_bytes(value) + b"\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _stores(args: argparse.Namespace) -> tuple[QdrantGenerationStore, Neo4jGenerationStore]:
    return (
        QdrantGenerationStore(store_revision=args.qdrant_revision),
        Neo4jGenerationStore(store_revision=args.neo4j_revision),
    )


def _coordinator(
    args: argparse.Namespace,
) -> tuple[GenerationCoordinator, QdrantGenerationStore, Neo4jGenerationStore]:
    qdrant, neo4j = _stores(args)
    return (
        GenerationCoordinator(
            PostgresGenerationRepository(),
            (qdrant, neo4j),
        ),
        qdrant,
        neo4j,
    )


def _spec(args: argparse.Namespace) -> GenerationSpec:
    manifest = SourceManifest.from_mapping(_load_json_object(Path(args.source_manifest)))
    return GenerationSpec(
        source_manifest=manifest,
        model_manifest=release_model_manifest(),
        index_config=_load_json_object(Path(args.index_config)),
    )


def _cmd_manifest(args: argparse.Namespace) -> int:
    articles = PostgresGenerationArticleSource().load()
    dataset_approval = Path(args.dataset_approval_artifact).read_bytes()
    freshness = {
        "status": args.freshness_status,
        "observed_at": args.observed_at,
        "valid_through": args.valid_through,
        "limitation": args.limitation,
        "approval_artifact_sha256": hashlib.sha256(dataset_approval).hexdigest(),
        "release_eligible": args.valid_through >= args.generated_at[:10],
    }
    manifest = build_verified_baseline_manifest(
        articles,
        data_directory=Path(args.data_directory),
        official_law_source_root=Path(args.official_law_source_root),
        approval_artifact=Path(args.approval_artifact),
        dataset_approval_artifact=Path(args.dataset_approval_artifact),
        generated_at=args.generated_at,
        freshness=freshness,
    )
    _write_json_atomic(
        Path(args.output),
        manifest.as_dict(),
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            {
                "status": "manifest_verified",
                "source_manifest_sha256": manifest.sha256,
                "target_law_count": len(manifest.laws),
                "release_eligible": all(
                    item.freshness.release_eligible for item in manifest.datasets
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    spec = _spec(args)
    coordinator, qdrant, neo4j = _coordinator(args)
    builder = ProductionGenerationBuilder(
        coordinator=coordinator,
        article_source=PostgresGenerationArticleSource(),
        qdrant=qdrant,
        neo4j=neo4j,
    )
    generation_id = builder.build(
        spec,
        source_root=Path(args.source_root),
        official_law_source_root=Path(args.official_law_source_root),
        approval_artifact=Path(args.approval_artifact),
        dataset_approval_artifact=Path(args.dataset_approval_artifact),
        batch_size=args.batch_size,
    )
    print(
        json.dumps(
            {
                "status": "building",
                "generation_id": generation_id,
                "active_pointer_changed": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    coordinator, _qdrant, _neo4j = _coordinator(args)
    attestations = coordinator.validate_generation(
        args.generation_id,
        attested_at=args.attested_at,
    )
    print(
        json.dumps(
            {
                "status": "validated",
                "generation_id": args.generation_id,
                "store_attestation_sha256": {item.store_name: item.sha256 for item in attestations},
            },
            sort_keys=True,
        )
    )
    return 0


def _cmd_seal(args: argparse.Namespace) -> int:
    coordinator, _qdrant, _neo4j = _coordinator(args)
    seal_sha256 = coordinator.seal_generation(
        args.generation_id,
        load_verified_evaluation_bundle(
            Path(args.bundle),
            generation_id=args.generation_id,
        ),
    )
    print(
        json.dumps(
            {
                "status": "sealed",
                "generation_id": args.generation_id,
                "seal_attestation_sha256": seal_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


def _confirmed_generation(args: argparse.Namespace) -> str:
    if args.confirm_generation_id != args.generation_id:
        raise ValueError("confirmation generation ID does not match target")
    return args.generation_id


def _cmd_promote(args: argparse.Namespace) -> int:
    generation_id = _confirmed_generation(args)
    coordinator, _qdrant, _neo4j = _coordinator(args)
    coordinator.promote(generation_id)
    print(
        json.dumps(
            {"status": "active", "generation_id": generation_id},
            sort_keys=True,
        )
    )
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    generation_id = _confirmed_generation(args)
    coordinator, _qdrant, _neo4j = _coordinator(args)
    coordinator.rollback(generation_id)
    print(
        json.dumps(
            {"status": "active_after_rollback", "generation_id": generation_id},
            sort_keys=True,
        )
    )
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    if args.confirm != "POSTGRESQL_IS_AUTHORITY":
        raise ValueError("reconciliation confirmation is required")
    coordinator, _qdrant, _neo4j = _coordinator(args)
    generation_id = coordinator.reconcile_external_to_postgres()
    print(
        json.dumps(
            {"status": "reconciled", "generation_id": generation_id},
            sort_keys=True,
        )
    )
    return 0


def _cmd_active(args: argparse.Namespace) -> int:
    qdrant, neo4j = _stores(args)
    attestation = ActiveGenerationLoader(
        external_stores=(qdrant, neo4j),
    ).load()
    payload = attestation.as_dict()
    payload["attestation_sha256"] = attestation.sha256
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _add_store_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qdrant-revision", required=True)
    parser.add_argument("--neo4j-revision", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest", help="Build verified 58-law/CCCT/HSK manifest")
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--data-directory", required=True)
    manifest.add_argument("--official-law-source-root", required=True)
    manifest.add_argument("--approval-artifact", required=True)
    manifest.add_argument("--dataset-approval-artifact", required=True)
    manifest.add_argument("--generated-at", required=True)
    manifest.add_argument(
        "--freshness-status",
        required=True,
        choices=("verified_current", "bounded_snapshot"),
    )
    manifest.add_argument("--observed-at", required=True)
    manifest.add_argument("--valid-through", required=True)
    manifest.add_argument("--limitation")
    manifest.add_argument("--overwrite", action="store_true")
    manifest.set_defaults(handler=_cmd_manifest)

    build = subparsers.add_parser("build", help="Build isolated generation projections")
    build.add_argument("--source-manifest", required=True)
    build.add_argument("--index-config", required=True)
    build.add_argument("--source-root", required=True)
    build.add_argument("--official-law-source-root", required=True)
    build.add_argument("--approval-artifact", required=True)
    build.add_argument("--dataset-approval-artifact", required=True)
    build.add_argument("--batch-size", type=int, default=64)
    _add_store_options(build)
    build.set_defaults(handler=_cmd_build)

    validate = subparsers.add_parser("validate", help="Cross-store attest a built generation")
    validate.add_argument("generation_id")
    validate.add_argument("--attested-at", required=True)
    _add_store_options(validate)
    validate.set_defaults(handler=_cmd_validate)

    seal = subparsers.add_parser("seal", help="Seal validated generation with RAG evidence")
    seal.add_argument("generation_id")
    seal.add_argument("--bundle", required=True)
    _add_store_options(seal)
    seal.set_defaults(handler=_cmd_seal)

    for name, handler in (("promote", _cmd_promote), ("rollback", _cmd_rollback)):
        command = subparsers.add_parser(name)
        command.add_argument("generation_id")
        command.add_argument("--confirm-generation-id", required=True)
        _add_store_options(command)
        command.set_defaults(handler=handler)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--confirm", required=True)
    _add_store_options(reconcile)
    reconcile.set_defaults(handler=_cmd_reconcile)

    active = subparsers.add_parser("active", help="Print runtime active attestation")
    _add_store_options(active)
    active.set_defaults(handler=_cmd_active)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        # Operator logs receive a stable error class only.  Connection strings,
        # source records, and provider responses must not cross this boundary.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
