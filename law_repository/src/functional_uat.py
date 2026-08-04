"""Bounded, local-only builder for the real three-law functional UAT profile.

This module proves that real local stores, pinned models, and the Evidence HTTP
boundary work together without turning a feature check into a release pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.embed.model_identity import DENSE_MODEL, RERANK_MODEL
from src.evidence import DatabaseEvidenceBackend
from src.functional_uat_spec import (
    FUNCTIONAL_UAT_AS_OF,
    FUNCTIONAL_UAT_LAW_IDS,
    FUNCTIONAL_UAT_LAWS,
)
from src.local_profiles import (
    FUNCTIONAL_UAT_PROFILE,
    LocalProfileError,
    artifact_root,
    ensure_functional_uat_profile,
    functional_uat_law_collection_environment,
    apply_functional_uat_runtime_environment,
    shared_model_cache_directory,
    validate_functional_uat_law_api_credential,
)

FUNCTIONAL_UAT_MANIFEST_VERSION = "functional-uat/v1"
FUNCTIONAL_UAT_IMAGE_REFERENCES = {
    "postgres": "pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b",
    "qdrant": "qdrant/qdrant@sha256:6ac4807063bbecddca0250bfbcff52acf18c22263b904d12919349e6d0a408f1",
    "neo4j": "neo4j@sha256:362542416de6c09a971484d1893878016cc3b5cdec166e54b1c824a220ecd6b9",
}
_OFFICIAL_CURRENT_URI = "https://www.law.go.kr/DRF/lawService.do?target=law&ID={law_id}"
_OFFICIAL_MST_URI = "https://www.law.go.kr/DRF/lawService.do?target=law&MST={mst}"


class FunctionalUatError(RuntimeError):
    """A functional UAT prerequisite or state check failed without a secret."""


class FunctionalUatEvidenceBackend(DatabaseEvidenceBackend):
    """Local-store backend whose review date is the validated UAT manifest date.

    A normal Data-store snapshot reports its build date.  Functional UAT
    instead has one explicitly fixed legal review date; the serving launcher
    constructs this backend only after validating that manifest and its local
    source/model/store state.
    """

    def __init__(self, *, review_as_of: date) -> None:
        if review_as_of != FUNCTIONAL_UAT_AS_OF:
            raise FunctionalUatError("FUNCTIONAL_UAT_AS_OF_MISMATCH")
        super().__init__()
        self._review_as_of = review_as_of

    def snapshot_effective_date(self, _snapshot_id: str) -> date:
        return self._review_as_of


LawApiGet = Callable[..., dict[str, Any]]


def _official_law_api_get(*args: object, **kwargs: object) -> dict[str, Any]:
    """Load the optional Law API client only for an explicit UAT collection."""

    from src import lawgo

    return lawgo.get(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class FunctionalLawSource:
    law_id: str
    law_name: str
    current_mst: str
    effective_mst: str
    current_file: str
    current_sha256: str
    effective_file: str
    effective_sha256: str
    article_count: int

    def public_manifest(self) -> dict[str, object]:
        return {
            "law_id": self.law_id,
            "law_name": self.law_name,
            "current_mst": self.current_mst,
            "effective_mst": self.effective_mst,
            "current_sha256": self.current_sha256,
            "effective_sha256": self.effective_sha256,
            "current_source_uri": _OFFICIAL_CURRENT_URI.format(law_id=self.law_id),
            "effective_source_uri": _OFFICIAL_MST_URI.format(mst=self.effective_mst),
            "article_count": self.article_count,
        }


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise FunctionalUatError("local UAT path is unsafe")
    absolute.mkdir(parents=True, exist_ok=True)
    if not absolute.is_dir():
        raise FunctionalUatError("local UAT path is unsafe")
    return absolute


def _atomic_bytes(path: Path, data: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise FunctionalUatError("functional UAT output already exists")
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise FunctionalUatError("functional UAT output could not be written") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _write_json(path: Path, value: object) -> str:
    payload = canonical_json_bytes(value)
    _atomic_bytes(path, payload)
    return sha256_bytes(payload)


def _law_search_record(payload: Mapping[str, Any], law_id: str) -> Mapping[str, Any]:
    from src import lawgo

    for row in lawgo.as_list(payload.get("LawSearch", {}).get("law")):
        if lawgo.squash(row.get("법령ID")) == law_id:
            return row
    raise FunctionalUatError("OFFICIAL_LAW_SEARCH_RECORD_MISSING")


def _effective_candidates(
    payload: Mapping[str, Any],
    law_id: str,
    as_of: date,
) -> list[str]:
    from src import lawgo

    candidates: list[tuple[str, str]] = []
    for row in lawgo.as_list(payload.get("LawSearch", {}).get("law")):
        if lawgo.squash(row.get("법령ID")) != law_id:
            continue
        effective = "".join(
            character for character in lawgo.squash(row.get("시행일자")) if character.isdigit()
        )
        mst = lawgo.squash(row.get("법령일련번호"))
        if len(effective) == 8 and mst and effective <= as_of.strftime("%Y%m%d"):
            candidates.append((effective, mst))
    candidates.sort(reverse=True)
    return list(dict.fromkeys(mst for _effective, mst in candidates))


def _collect_one(
    *,
    law_id: str,
    law_name: str,
    as_of: date,
    output: Path,
    api_get: LawApiGet,
) -> FunctionalLawSource:
    from src import lawgo
    from src.ingest.laws import build_meta, parse_articles

    current_body = api_get("lawService.do", target="law", ID=law_id)
    search = api_get("lawSearch.do", target="law", query=law_name, display=100)
    current_mst = lawgo.squash(_law_search_record(search, law_id).get("법령일련번호"))
    if not current_mst:
        raise FunctionalUatError("OFFICIAL_CURRENT_MST_MISSING")
    current_meta = build_meta(current_body, current_mst)
    if current_meta.get("law_id") != law_id:
        raise FunctionalUatError("OFFICIAL_CURRENT_LAW_ID_MISMATCH")

    effective_body = current_body
    effective_mst = current_mst
    effective_meta = current_meta
    if current_meta["enforcement_date"] > as_of.strftime("%Y%m%d"):
        effective_search = api_get("lawSearch.do", target="eflaw", query=law_name, display=100)
        selected: tuple[str, dict[str, Any], dict[str, Any]] | None = None
        for candidate_mst in _effective_candidates(effective_search, law_id, as_of)[:10]:
            candidate_body = api_get("lawService.do", target="law", MST=candidate_mst)
            candidate_meta = build_meta(candidate_body, candidate_mst)
            if candidate_meta.get("law_id") == law_id and candidate_meta[
                "enforcement_date"
            ] <= as_of.strftime("%Y%m%d"):
                selected = (candidate_mst, candidate_body, candidate_meta)
                break
        if selected is None:
            raise FunctionalUatError("OFFICIAL_EFFECTIVE_VERSION_MISSING")
        effective_mst, effective_body, effective_meta = selected

    rows = parse_articles(effective_body, effective_meta)
    if not rows:
        raise FunctionalUatError("OFFICIAL_EFFECTIVE_ARTICLES_MISSING")
    current_file = f"law_current_{law_id}.json"
    effective_file = f"law_effective_{law_id}_{effective_mst}.json"
    current_sha = _write_json(output / current_file, current_body)
    effective_sha = _write_json(output / effective_file, effective_body)
    return FunctionalLawSource(
        law_id=law_id,
        law_name=law_name,
        current_mst=current_mst,
        effective_mst=effective_mst,
        current_file=current_file,
        current_sha256=current_sha,
        effective_file=effective_file,
        effective_sha256=effective_sha,
        article_count=sum(1 + len(row.get("children", [])) for row in rows),
    )


def collect_three_law_sources(
    *,
    profile_directory: Path,
    as_of: date,
    api_get: LawApiGet | None = None,
) -> tuple[Path, tuple[FunctionalLawSource, ...], str]:
    """Collect only the three approved law IDs into ignored local state."""

    if as_of != FUNCTIONAL_UAT_AS_OF:
        raise FunctionalUatError("FUNCTIONAL_UAT_AS_OF_MISMATCH")
    resolved_api_get = api_get or _official_law_api_get
    destination_parent = _safe_directory(profile_directory / "data" / "laws")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    destination = destination_parent / run_id
    if destination.exists():  # practically impossible, but preserves no-overwrite semantics
        raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_OUTPUT_EXISTS")
    destination.mkdir()
    try:
        sources = tuple(
            _collect_one(
                law_id=law_id,
                law_name=law_name,
                as_of=as_of,
                output=destination,
                api_get=resolved_api_get,
            )
            for law_id, law_name in FUNCTIONAL_UAT_LAWS
        )
        collected_at = datetime.now(timezone.utc).isoformat()
        source_manifest = {
            "schema_version": FUNCTIONAL_UAT_MANIFEST_VERSION,
            "profile": FUNCTIONAL_UAT_PROFILE,
            "as_of": as_of.isoformat(),
            "collected_at": collected_at,
            "law_ids": [item.law_id for item in sources],
            "sources": [item.public_manifest() for item in sources],
        }
        _write_json(destination / "source-manifest.json", source_manifest)
        return destination, sources, collected_at
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _assert_empty_profile_stores() -> None:
    """Refuse to mix a new bounded build with untracked existing state."""

    from src.db.neo4j import driver as neo4j_driver
    from src.db.postgres import connect
    from src.db.qdrant import COLLECTION, client as qdrant_client

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM laws), "
                "(SELECT count(*) FROM law_articles), "
                "(SELECT count(*) FROM knowledge_snapshots)"
            )
            postgres_counts = tuple(int(value) for value in cur.fetchone())
        qdrant = qdrant_client()
        qdrant_count = (
            int(qdrant.count(COLLECTION, exact=True).count)
            if qdrant.collection_exists(COLLECTION)
            else 0
        )
        driver = neo4j_driver()
        try:
            with driver.session() as session:
                neo4j_count = int(
                    session.run("MATCH (n) RETURN count(n) AS count").single()["count"]
                )
        finally:
            driver.close()
    except Exception as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_STORES_UNAVAILABLE") from exc
    if any(postgres_counts) or qdrant_count or neo4j_count:
        raise FunctionalUatError("FUNCTIONAL_UAT_PROFILE_DIRTY")


def _load_sources_into_stores(sources: tuple[FunctionalLawSource, ...], source_root: Path) -> None:
    from src.db.postgres import connect
    from src.ingest.laws import build_meta, index_qdrant, load_law, parse_articles
    from src.ingest import graph

    for source in sources:
        path = source_root / source.effective_file
        if not path.is_file() or path.is_symlink():
            raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_MISSING")
        raw = path.read_bytes()
        if sha256_bytes(raw) != source.effective_sha256:
            raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_HASH_MISMATCH")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_INVALID") from exc
        meta = build_meta(body, source.effective_mst)
        if meta.get("law_id") != source.law_id:
            raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_ID_MISMATCH")
        rows = parse_articles(body, meta)
        if sum(1 + len(row.get("children", [])) for row in rows) != source.article_count:
            raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_ARTICLE_MISMATCH")
        with connect() as conn, conn.cursor() as cur:
            load_law(cur, meta, rows)
    # HSK and CCCT are tracked source inputs; their normal loaders create the
    # same local projections without requiring an additional external key.
    from src.ingest import ccct, hsk

    hsk.main()
    ccct.load()
    index_qdrant()
    graph.build()


def _store_counts() -> dict[str, int]:
    from src.db.neo4j import driver as neo4j_driver
    from src.db.postgres import connect
    from src.db.qdrant import COLLECTION, client as qdrant_client

    law_ids = [law_id for law_id, _name in FUNCTIONAL_UAT_LAWS]
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM law_articles a
                WHERE a.is_current
                  AND a.law_id = ANY(%s)
                  AND NOT EXISTS (
                    SELECT 1 FROM law_articles child WHERE child.parent_article_pk=a.id
                  )
                """,
                (law_ids,),
            )
            postgres_count = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(DISTINCT law_id) FROM laws WHERE law_id = ANY(%s)", (law_ids,)
            )
            law_count = int(cur.fetchone()[0])
        qdrant_count = int(qdrant_client().count(COLLECTION, exact=True).count)
        driver = neo4j_driver()
        try:
            with driver.session() as session:
                neo4j_count = int(
                    session.run(
                        "MATCH (a:Article) WHERE a.law_id IN $law_ids RETURN count(a) AS count",
                        law_ids=law_ids,
                    ).single()["count"]
                )
        finally:
            driver.close()
    except Exception as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_STORE_COUNT_UNAVAILABLE") from exc
    if (
        law_count != len(law_ids)
        or not postgres_count
        or postgres_count != qdrant_count
        or postgres_count != neo4j_count
    ):
        raise FunctionalUatError("FUNCTIONAL_UAT_STORE_COUNT_MISMATCH")
    return {
        "law_count": law_count,
        "postgresql": postgres_count,
        "qdrant": qdrant_count,
        "neo4j": neo4j_count,
    }


def prefetch_models(
    *,
    profile_directory: Path,
    cache_directory: Path | None = None,
    downloader: Callable[..., str] | None = None,
) -> dict[str, dict[str, str]]:
    """Download immutable model revisions into the ignored UAT cache once."""

    cache = cache_directory or shared_model_cache_directory(root=profile_directory.parents[1])
    cache = _safe_directory(cache)
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    if downloader is None:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise FunctionalUatError("FUNCTIONAL_UAT_MODEL_DOWNLOADER_UNAVAILABLE") from exc
        downloader = snapshot_download
    inventory = {"dense": DENSE_MODEL, "reranker": RERANK_MODEL}
    try:
        for identity in inventory.values():
            downloader(
                repo_id=identity.model_id,
                revision=identity.revision,
                cache_dir=str(cache),
                local_files_only=False,
            )
    except Exception as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_MODEL_PREFETCH_FAILED") from exc
    finally:
        os.environ["HF_HUB_OFFLINE"] = "1"
    return {name: asdict(identity) for name, identity in inventory.items()}


def _verify_cached_models(
    profile_directory: Path,
    *,
    cache_directory: Path | None = None,
) -> dict[str, dict[str, str]]:
    cache = cache_directory or shared_model_cache_directory(root=profile_directory.parents[1])
    cache = _safe_directory(cache)
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_MODEL_DOWNLOADER_UNAVAILABLE") from exc
    inventory = {"dense": DENSE_MODEL, "reranker": RERANK_MODEL}
    try:
        for identity in inventory.values():
            snapshot_download(
                repo_id=identity.model_id,
                revision=identity.revision,
                cache_dir=str(cache),
                local_files_only=True,
            )
    except Exception as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_MODEL_CACHE_MISSING") from exc
    return {name: asdict(identity) for name, identity in inventory.items()}


def docker_image_digests() -> dict[str, str]:
    """Read compose image digests without displaying image metadata or secrets."""

    observed: dict[str, str] = {}
    for service, reference in FUNCTIONAL_UAT_IMAGE_REFERENCES.items():
        completed = subprocess.run(
            ["docker", "image", "inspect", reference, "--format", "{{json .RepoDigests}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if completed.returncode != 0:
            raise FunctionalUatError("FUNCTIONAL_UAT_IMAGE_UNAVAILABLE")
        try:
            digests = json.loads(completed.stdout.strip())
        except json.JSONDecodeError as exc:
            raise FunctionalUatError("FUNCTIONAL_UAT_IMAGE_DIGEST_UNAVAILABLE") from exc
        expected_digest = reference.split("@", 1)[1]
        if not isinstance(digests, list) or not any(
            str(item).endswith(expected_digest) for item in digests
        ):
            raise FunctionalUatError("FUNCTIONAL_UAT_IMAGE_DIGEST_MISMATCH")
        observed[service] = expected_digest
    return observed


def _new_artifact_manifest_path(*, root: Path | None = None) -> Path:
    target_root = _safe_directory(artifact_root(root) / "functional-uat")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    destination = target_root / run_id
    destination.mkdir()
    return destination / "manifest.json"


def _write_active_manifest(profile_directory: Path, manifest: Mapping[str, object]) -> Path:
    state = _safe_directory(profile_directory / "state")
    output = state / "active-manifest.json"
    if output.exists() or output.is_symlink():
        raise FunctionalUatError("FUNCTIONAL_UAT_ACTIVE_MANIFEST_EXISTS")
    _atomic_bytes(output, canonical_json_bytes(manifest))
    return output


def discard_active_functional_uat_manifest(
    *,
    local_root_override: Path | None = None,
) -> bool:
    """Remove only the active build marker after an explicit profile reset.

    The caller is responsible for stopping and removing the matching
    profile-scoped Docker volumes first.  Raw official sources and UAT
    artifacts are deliberately preserved: they can help explain a previous
    local run and are never touched by a store reset.
    """

    try:
        profile = ensure_functional_uat_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_PROFILE_UNAVAILABLE") from exc
    state = profile / "state"
    if state.is_symlink():
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_PATH_UNSAFE")
    if not state.exists():
        return False
    if not state.is_dir():
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_PATH_UNSAFE")
    manifest = state / "active-manifest.json"
    if not manifest.exists() and not manifest.is_symlink():
        return False
    if manifest.is_symlink() or not manifest.is_file():
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_PATH_UNSAFE")
    try:
        manifest.unlink()
    except OSError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_RESET_FAILED") from exc
    return True


def build_functional_uat(
    *,
    as_of: date,
    local_root_override: Path | None = None,
    artifact_root_override: Path | None = None,
    api_get: LawApiGet | None = None,
) -> Path:
    """Build the complete real-data UAT state once from clean profile stores."""

    if as_of != FUNCTIONAL_UAT_AS_OF:
        raise FunctionalUatError("FUNCTIONAL_UAT_AS_OF_MISMATCH")
    try:
        profile = ensure_functional_uat_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_PROFILE_UNAVAILABLE") from exc
    try:
        validate_functional_uat_law_api_credential(root=local_root_override)
    except LocalProfileError as exc:
        raise FunctionalUatError("LAW_API_CREDENTIAL_UNAVAILABLE") from exc
    try:
        apply_functional_uat_runtime_environment(root=local_root_override)
    except LocalProfileError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_PROFILE_UNAVAILABLE") from exc

    # Migrations are safe/idempotent; check content after tables exist and
    # before a single source row, HSK node, or vector is written.
    try:
        from scripts.migrate import main as migrate_main

        migrate_main()
    except Exception as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_MIGRATION_FAILED") from exc
    _assert_empty_profile_stores()
    models = prefetch_models(
        profile_directory=profile,
        cache_directory=shared_model_cache_directory(root=local_root_override),
    )
    try:
        # Collection is the sole point at which this process may hold the
        # personal Law API credential.  The context clears both os.environ
        # and the shared Settings object before the local stores are loaded.
        with functional_uat_law_collection_environment(root=local_root_override):
            source_root, sources, source_collected_at = collect_three_law_sources(
                profile_directory=profile,
                as_of=as_of,
                api_get=api_get,
            )
    except LocalProfileError as exc:
        raise FunctionalUatError("LAW_API_CREDENTIAL_UNAVAILABLE") from exc
    _load_sources_into_stores(sources, source_root)
    counts = _store_counts()
    images = docker_image_digests()
    source_hashes = {source.law_id: source.effective_sha256 for source in sources}
    manifest: dict[str, object] = {
        "schema_version": FUNCTIONAL_UAT_MANIFEST_VERSION,
        "profile": FUNCTIONAL_UAT_PROFILE,
        "as_of": as_of.isoformat(),
        "law_ids": [law_id for law_id, _name in FUNCTIONAL_UAT_LAWS],
        "source_collected_at": source_collected_at,
        "source_hashes": source_hashes,
        "source_assets": [
            {
                **source.public_manifest(),
                "current_file": str((source_root / source.current_file).relative_to(profile)),
                "effective_file": str((source_root / source.effective_file).relative_to(profile)),
            }
            for source in sources
        ],
        "model_ids/revisions": models,
        "store_counts": counts,
        "image_digests": images,
        "data_build_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    artifact = _new_artifact_manifest_path(root=artifact_root_override)
    _atomic_bytes(artifact, canonical_json_bytes(manifest))
    _write_active_manifest(profile, manifest)
    return artifact


def _load_active_manifest(profile_directory: Path) -> tuple[dict[str, object], str]:
    path = profile_directory / "state" / "active-manifest.json"
    if not path.is_file() or path.is_symlink():
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_UNAVAILABLE")
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID") from exc
    if not isinstance(manifest, dict):
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    required = {
        "schema_version",
        "profile",
        "as_of",
        "law_ids",
        "source_collected_at",
        "source_hashes",
        "source_assets",
        "model_ids/revisions",
        "store_counts",
        "image_digests",
        "data_build_timestamp",
    }
    if set(manifest) != required:
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    if (
        manifest["schema_version"] != FUNCTIONAL_UAT_MANIFEST_VERSION
        or manifest["profile"] != FUNCTIONAL_UAT_PROFILE
        or manifest["as_of"] != FUNCTIONAL_UAT_AS_OF.isoformat()
        or manifest["law_ids"] != [law_id for law_id, _name in FUNCTIONAL_UAT_LAWS]
    ):
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_MISMATCH")
    for key in ("source_collected_at", "data_build_timestamp"):
        value = manifest[key]
        if not isinstance(value, str):
            raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
        try:
            parsed_timestamp = datetime.fromisoformat(value)
        except ValueError as exc:
            raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID") from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    return manifest, sha256_bytes(raw)


def _profile_local_file(profile: Path, relative: str) -> Path:
    """Resolve a manifest file without following an untrusted local symlink."""

    value = Path(relative)
    if (
        not relative
        or value.is_absolute()
        or value.drive
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_PATH_INVALID")
    candidate = profile.joinpath(*value.parts)
    current = profile
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_PATH_INVALID")
    try:
        candidate.resolve(strict=False).relative_to(profile.resolve())
    except ValueError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_PATH_INVALID") from exc
    return candidate


def _verify_source_file(*, profile: Path, relative: str, expected_sha256: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    candidate = _profile_local_file(profile, relative)
    if not candidate.is_file() or sha256_bytes(candidate.read_bytes()) != expected_sha256:
        raise FunctionalUatError("FUNCTIONAL_UAT_SOURCE_HASH_MISMATCH")


def validate_functional_uat_state(
    *,
    local_root_override: Path | None = None,
) -> tuple[dict[str, object], str]:
    """Fail closed before serving if local sources/models/images/stores drift."""

    try:
        profile = ensure_functional_uat_profile(root=local_root_override)
        apply_functional_uat_runtime_environment(root=local_root_override)
    except LocalProfileError as exc:
        raise FunctionalUatError("FUNCTIONAL_UAT_PROFILE_UNAVAILABLE") from exc
    manifest, digest = _load_active_manifest(profile)
    source_assets = manifest["source_assets"]
    source_hashes = manifest["source_hashes"]
    if not isinstance(source_assets, list) or not isinstance(source_hashes, dict):
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    expected_law_ids = [law_id for law_id, _name in FUNCTIONAL_UAT_LAWS]
    if (
        len(source_assets) != len(expected_law_ids)
        or set(source_hashes) != set(expected_law_ids)
        or any(
            not isinstance(law_id, str)
            or not isinstance(source_hashes[law_id], str)
            or re.fullmatch(r"[0-9a-f]{64}", source_hashes[law_id]) is None
            for law_id in source_hashes
        )
    ):
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    seen_law_ids: set[str] = set()
    for asset in source_assets:
        if not isinstance(asset, dict):
            raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
        law_id = asset.get("law_id")
        if not isinstance(law_id, str) or law_id not in expected_law_ids or law_id in seen_law_ids:
            raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
        seen_law_ids.add(law_id)
        for kind in ("current", "effective"):
            relative = asset.get(f"{kind}_file")
            expected = asset.get(f"{kind}_sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
            if kind == "effective" and source_hashes[law_id] != expected:
                raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
            _verify_source_file(profile=profile, relative=relative, expected_sha256=expected)
    if seen_law_ids != set(expected_law_ids):
        raise FunctionalUatError("FUNCTIONAL_UAT_MANIFEST_INVALID")
    observed_models = _verify_cached_models(
        profile,
        cache_directory=shared_model_cache_directory(root=local_root_override),
    )
    if manifest["model_ids/revisions"] != observed_models:
        raise FunctionalUatError("FUNCTIONAL_UAT_MODEL_CACHE_MISMATCH")
    if manifest["image_digests"] != docker_image_digests():
        raise FunctionalUatError("FUNCTIONAL_UAT_IMAGE_DIGEST_MISMATCH")
    if manifest["store_counts"] != _store_counts():
        raise FunctionalUatError("FUNCTIONAL_UAT_STORE_COUNT_MISMATCH")
    return manifest, digest


__all__ = [
    "FUNCTIONAL_UAT_AS_OF",
    "FUNCTIONAL_UAT_LAW_IDS",
    "FUNCTIONAL_UAT_LAWS",
    "FUNCTIONAL_UAT_IMAGE_REFERENCES",
    "FUNCTIONAL_UAT_MANIFEST_VERSION",
    "FunctionalLawSource",
    "FunctionalUatEvidenceBackend",
    "FunctionalUatError",
    "build_functional_uat",
    "canonical_json_bytes",
    "collect_three_law_sources",
    "discard_active_functional_uat_manifest",
    "docker_image_digests",
    "prefetch_models",
    "sha256_bytes",
    "validate_functional_uat_state",
]
