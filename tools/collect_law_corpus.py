"""Collect an immutable 58-law corpus from the approved official API.

The command deliberately requires an explicit bulk-collection acknowledgement.
It never prints the LAW_API_OC credential and writes only credential-free source
URIs into the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
sys.path.insert(0, str(LAW_ROOT))

from src import lawgo  # noqa: E402
from src.ingest.laws import TARGET_LAWS, build_meta, parse_articles  # noqa: E402
from src.source_manifest import (  # noqa: E402
    LawSourceAsset,
    LawSourceManifest,
    article_content_set_sha256,
    canonical_json_bytes,
    target_law_manifest_sha256,
    validate_collector_revision,
)

OFFICIAL_CURRENT_URI = "https://www.law.go.kr/DRF/lawService.do?target=law&ID={law_id}"
OFFICIAL_MST_URI = "https://www.law.go.kr/DRF/lawService.do?target=law&MST={mst}"


class CollectionError(RuntimeError):
    pass


class _RateLimiter:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            remaining = self.seconds - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


def _official_get(
    limiter: _RateLimiter,
    endpoint: str,
    **params: Any,
) -> dict[str, Any]:
    limiter.wait()
    return lawgo.get(endpoint, **params)


def _law_search_record(payload: dict[str, Any], law_id: str) -> dict[str, Any]:
    rows = lawgo.as_list(payload.get("LawSearch", {}).get("law"))
    for row in rows:
        if lawgo.squash(row.get("법령ID")) == law_id:
            return row
    raise CollectionError("OFFICIAL_LAW_SEARCH_RECORD_MISSING")


def _effective_candidates(payload: dict[str, Any], law_id: str, as_of: date) -> list[str]:
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


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    data = canonical_json_bytes(payload)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _collect_one(
    target: dict[str, str],
    *,
    output: Path,
    as_of: date,
    api_get: Callable[..., dict[str, Any]],
) -> LawSourceAsset:
    law_id = target["law_id"]
    law_name = target["name"]
    current_body = api_get("lawService.do", target="law", ID=law_id)
    search = api_get("lawSearch.do", target="law", query=law_name, display=100)
    current_mst = lawgo.squash(_law_search_record(search, law_id).get("법령일련번호"))
    if not current_mst:
        raise CollectionError("OFFICIAL_CURRENT_MST_MISSING")
    current_meta = build_meta(current_body, current_mst)
    if current_meta["law_id"] != law_id:
        raise CollectionError("OFFICIAL_CURRENT_LAW_ID_MISMATCH")

    effective_body = current_body
    effective_mst = current_mst
    if current_meta["enforcement_date"] > as_of.strftime("%Y%m%d"):
        effective_search = api_get(
            "lawSearch.do",
            target="eflaw",
            query=law_name,
            display=100,
        )
        selected: tuple[str, dict[str, Any], dict[str, Any]] | None = None
        for candidate_mst in _effective_candidates(effective_search, law_id, as_of)[:10]:
            candidate_body = api_get("lawService.do", target="law", MST=candidate_mst)
            candidate_meta = build_meta(candidate_body, candidate_mst)
            if candidate_meta["law_id"] == law_id and candidate_meta[
                "enforcement_date"
            ] <= as_of.strftime("%Y%m%d"):
                selected = (candidate_mst, candidate_body, candidate_meta)
                break
        if selected is None:
            raise CollectionError("OFFICIAL_EFFECTIVE_VERSION_MISSING")
        effective_mst, effective_body, current_effective_meta = selected
    else:
        current_effective_meta = current_meta

    rows = parse_articles(effective_body, current_effective_meta)
    if not rows:
        raise CollectionError("OFFICIAL_EFFECTIVE_ARTICLES_MISSING")
    article_count = sum(1 + len(row.get("children", [])) for row in rows)
    current_file = f"law_current_{law_id}.json"
    effective_file = f"law_effective_{law_id}_{effective_mst}.json"
    current_sha = _write_json(output / current_file, current_body)
    effective_sha = (
        current_sha
        if effective_body is current_body and effective_file == current_file
        else _write_json(output / effective_file, effective_body)
    )
    return LawSourceAsset(
        law_id=law_id,
        law_name=law_name,
        current_mst=current_mst,
        effective_mst=effective_mst,
        effective_date=as_of.isoformat(),
        current_file=current_file,
        current_sha256=current_sha,
        effective_file=effective_file,
        effective_sha256=effective_sha,
        current_source_uri=OFFICIAL_CURRENT_URI.format(law_id=law_id),
        effective_source_uri=OFFICIAL_MST_URI.format(mst=effective_mst),
        article_count=article_count,
        article_content_set_sha256=article_content_set_sha256(rows),
    )


def _git_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return validate_collector_revision(completed.stdout.strip())


def collect(
    *,
    output: Path,
    as_of: date,
    rate_limit_seconds: float,
    collector_revision: str,
) -> tuple[Path, str]:
    if output.exists():
        raise CollectionError("OUTPUT_ALREADY_EXISTS")
    output_parent = output.parent.resolve(strict=True)
    staging = output_parent / f".{output.name}.staging-{os.getpid()}"
    if staging.exists():
        raise CollectionError("STAGING_ALREADY_EXISTS")
    staging.mkdir()
    limiter = _RateLimiter(rate_limit_seconds)
    try:
        laws = [
            _collect_one(
                target,
                output=staging,
                as_of=as_of,
                api_get=lambda endpoint, **params: _official_get(
                    limiter,
                    endpoint,
                    **params,
                ),
            )
            for target in TARGET_LAWS
        ]
        manifest = LawSourceManifest(
            schema_version="1.0",
            source="Korean Ministry of Government Legislation LAW OPEN DATA",
            collected_at=datetime.now(timezone.utc),
            collection_as_of=as_of.isoformat(),
            target_law_manifest_sha256=target_law_manifest_sha256(),
            collector_revision=collector_revision,
            law_count=58,
            laws=laws,
        )
        manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
        (staging / "source-manifest.json").write_bytes(manifest_bytes)
        staging.rename(output)
        return output / "source-manifest.json", hashlib.sha256(manifest_bytes).hexdigest()
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--rate-limit-seconds", type=float, default=2.0)
    parser.add_argument(
        "--confirm-approved-bulk-collection",
        action="store_true",
        help="confirm that the operator has an approved OC and authorizes this bounded 58-law run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_approved_bulk_collection:
        print(
            "BLOCKED: explicit approved bulk-collection confirmation is required", file=sys.stderr
        )
        return 2
    if args.rate_limit_seconds < 1.0 or args.rate_limit_seconds > 30.0:
        print("BLOCKED: rate limit must be between 1 and 30 seconds", file=sys.stderr)
        return 2
    try:
        manifest_path, manifest_sha = collect(
            output=args.output.resolve(),
            as_of=args.as_of,
            rate_limit_seconds=args.rate_limit_seconds,
            collector_revision=_git_revision(),
        )
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "PASS",
                "law_count": 58,
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
